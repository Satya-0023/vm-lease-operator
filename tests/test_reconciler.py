from datetime import datetime, timedelta, timezone

import pytest
import requests

from vmlease.openstack_client import OpenStackError
from vmlease.reconciler import UID_KEY, VMRef, reconcile

CREATED = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
SPEC = {"flavor": "m1.small", "image": "cirros", "network": "private", "ttl": "1h"}
VM = VMRef(namespace="dev", name="web", uid="uid-123", created_at=CREATED)


def run(cloud, status=None, spec=SPEC, minutes=1):
    return reconcile(VM, spec, status, cloud, CREATED + timedelta(minutes=minutes))


def reasons(events):
    return [e.reason for e in events]


def test_creates_server_then_reports_active(cloud):
    status, events = run(cloud)
    assert reasons(events) == ["Created"]
    assert status["phase"] == "Building"  # Nova answers 202 and boots the VM in the background
    status, events = run(cloud, status, minutes=2)
    assert reasons(events) == ["Ready"]
    assert status["phase"] == "Active"
    assert status["ip"].startswith("10.0.0.")
    assert status["expiresAt"] == "2026-11-12T11:00:00Z"
    server = cloud.get_server(status["serverId"])
    assert server["name"] == "dev-web"
    assert server["metadata"][UID_KEY] == "uid-123"


def test_steady_state_is_quiet(cloud):
    status, _ = run(cloud)
    status, _ = run(cloud, status, minutes=2)
    again, events = run(cloud, status, minutes=3)
    assert events == []
    assert again == status  # nothing changed, so main.py patches nothing


def test_recreates_server_deleted_out_of_band(cloud, fakestack):
    status, _ = run(cloud)
    requests.delete(f"{fakestack}/_fake/servers/{status['serverId']}")
    healed, events = run(cloud, status, minutes=2)
    assert reasons(events) == ["ServerMissing", "Created"]
    assert healed["serverId"] != status["serverId"]
    assert healed["phase"] == "Building"


def test_adopts_server_when_status_was_lost(cloud):
    # Simulates: server created, then the operator crashed before it could write status.
    status, _ = run(cloud)
    adopted, events = run(cloud, status=None, minutes=2)
    assert reasons(events) == ["Adopted", "Ready"]
    assert adopted["serverId"] == status["serverId"]
    assert len(cloud.find_servers("dev-web", UID_KEY, "uid-123")) == 1  # no duplicate VM


def test_lease_expiry_deletes_server(cloud):
    status, _ = run(cloud)
    expired, events = run(cloud, status, minutes=60)
    assert reasons(events) == ["LeaseExpired"]
    assert expired["phase"] == "Expired"
    assert cloud.get_server(status["serverId"]) is None
    # Expired is terminal: later runs do nothing and never recreate the VM.
    later, events = run(cloud, expired, minutes=90)
    assert events == [] and later["phase"] == "Expired"
    assert cloud.find_servers("dev-web", UID_KEY, "uid-123") == []


def test_lease_expiry_can_stop_instead(cloud):
    spec = {**SPEC, "onExpiry": "Stop"}
    status, _ = run(cloud, spec=spec)
    status, _ = run(cloud, status, spec=spec, minutes=2)
    expired, _ = run(cloud, status, spec=spec, minutes=61)
    assert expired["phase"] == "Expired"
    assert cloud.get_server(status["serverId"])["status"] == "SHUTOFF"


def test_raising_ttl_extends_lease(cloud):
    status, _ = run(cloud)
    status, _ = run(cloud, status, minutes=2)
    extended, events = run(cloud, status, spec={**SPEC, "ttl": "3h"}, minutes=61)
    assert events == []
    assert extended["phase"] == "Active"
    assert extended["expiresAt"] == "2026-11-12T13:00:00Z"


def test_unknown_flavor_is_reported(cloud):
    status, events = run(cloud, spec={**SPEC, "flavor": "m1.huge"})
    assert status["phase"] == "Error"
    assert "m1.huge" in status["message"]
    assert reasons(events) == ["ProvisionFailed"]


def test_invalid_ttl(cloud):
    status, events = run(cloud, spec={**SPEC, "ttl": "forever"})
    assert status["phase"] == "Error"
    assert reasons(events) == ["InvalidSpec"]


def test_nova_error_is_surfaced(cloud, fakestack):
    status, _ = run(cloud)
    requests.post(f"{fakestack}/_fake/servers/{status['serverId']}/error")
    broken, events = run(cloud, status, minutes=2)
    assert broken["phase"] == "Error"
    assert broken["message"] == "No valid host was found."
    assert reasons(events) == ["ServerError"]


@pytest.mark.parametrize("code", [401, 500, 503])
def test_cloud_or_auth_trouble_raises_for_retry(cloud, monkeypatch, code):
    # Not the user's fault, so don't flip the phase to Error; main.py keeps the old state.
    def outage(*_args, **_kwargs):
        raise OpenStackError(code, "cloud trouble")
    monkeypatch.setattr(cloud, "create_server", outage)
    with pytest.raises(OpenStackError):
        run(cloud)
