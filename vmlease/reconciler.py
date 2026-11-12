"""The decision logic: given what the user asked for (spec), what we last recorded (status) and
what OpenStack says right now, decide what to do and what the new status is.

It knows nothing about Kubernetes or kopf. main.py feeds it plain dicts and applies the result,
which is why the tests can drive it directly against fakestack.

The loop is *level-triggered*: it does not care which event woke it up. Every run looks at the
whole current state and moves it one step toward the desired state, so a missed event or an
operator restart is harmless; the next run fixes things.
"""
from dataclasses import dataclass
from datetime import datetime

from . import lease
from .openstack_client import OpenStackError

NOVA_PHASES = {"BUILD": "Building", "ACTIVE": "Active", "SHUTOFF": "Stopped", "ERROR": "Error"}
UID_KEY = "vmlease-uid"  # Nova metadata tag that ties a server to exactly one Kubernetes object
# 400 bad request, 403 forbidden / over quota, 404 unknown flavor/image/network, 409 ambiguous name
USER_FIXABLE = {400, 403, 404, 409}


@dataclass
class VMRef:
    namespace: str
    name: str
    uid: str
    created_at: datetime

    @property
    def server_name(self):
        return f"{self.namespace}-{self.name}"


@dataclass
class Event:
    type: str      # "Normal" or "Warning", as in `kubectl describe`
    reason: str
    message: str


def reconcile(vm, spec, status, cloud, now):
    """Return (new_status, events)."""
    status = dict(status or {})
    events = []

    # 1. Work out the lease.
    try:
        expiry = lease.expires_at(vm.created_at, spec["ttl"])
    except ValueError as err:
        status.update(phase="Error", message=str(err))
        return status, [Event("Warning", "InvalidSpec", str(err))]
    status["expiresAt"] = lease.to_iso(expiry)

    if status.get("phase") == "Expired":
        return status, events  # terminal: an expired lease is never revived

    # 2. Lease over? Reclaim the VM and stop here.
    if lease.is_expired(expiry, now):
        if spec.get("onExpiry", "Delete") == "Stop":
            server = cloud.get_server(status["serverId"]) if status.get("serverId") else None
            if server and server["status"] == "ACTIVE":
                cloud.stop_server(server["id"])
            outcome = "stopped"
        else:
            delete_owned_servers(vm, status, cloud)
            status["ip"] = None
            outcome = "deleted"
        status.update(phase="Expired", message=f"lease ended at {status['expiresAt']}; server {outcome}")
        return status, [Event("Normal", "LeaseExpired", status["message"])]

    # 3. Observe the real server, creating it if it is missing.
    server_id = status.get("serverId")
    server = cloud.get_server(server_id) if server_id else None
    if server is None:
        if server_id:
            events.append(Event("Warning", "ServerMissing",
                                f"server {server_id} was deleted outside the operator; recreating"))
        adopted = cloud.find_servers(vm.server_name, UID_KEY, vm.uid)
        if adopted:
            server = adopted[0]
            events.append(Event("Normal", "Adopted", f"found existing server {server['id']} tagged with our UID"))
        else:
            try:
                server = {"id": _create(vm, spec, cloud), "status": "BUILD"}
            except OpenStackError as err:
                if err.status not in USER_FIXABLE:
                    raise  # cloud or auth trouble (5xx, 401, timeout): caller keeps old state, retries
                # The user's request can't be met (unknown flavor, quota...). Show it in the phase so
                # `kubectl get` makes it obvious. No serverId yet, so the next run simply tries again.
                status.update(phase="Error", message=err.message)
                return status, events + [Event("Warning", "ProvisionFailed", err.message)]
            events.append(Event("Normal", "Created", f"created server {server['id']} ({vm.server_name})"))

    # 4. Report what OpenStack says.
    old_phase = status.get("phase")
    phase = NOVA_PHASES.get(server["status"], "Unknown")
    status.update(serverId=server["id"], phase=phase, ip=_first_ip(server),
                  message=server.get("fault", {}).get("message", "") if phase == "Error" else "")
    if phase != old_phase and phase == "Active":
        events.append(Event("Normal", "Ready", f"server {server['id']} is ACTIVE at {status['ip']}"))
    if phase != old_phase and phase == "Error":
        events.append(Event("Warning", "ServerError", status["message"] or "Nova reports ERROR"))
    return status, events


def delete_owned_servers(vm, status, cloud):
    """Delete the recorded server plus any stray server tagged with this object's UID."""
    ids = [status["serverId"]] if status.get("serverId") else []
    ids += [s["id"] for s in cloud.find_servers(vm.server_name, UID_KEY, vm.uid) if s["id"] not in ids]
    for server_id in ids:
        cloud.delete_server(server_id)
    return ids


def _create(vm, spec, cloud):
    flavor_id = cloud.resolve_flavor(spec["flavor"])
    image_id = cloud.resolve_image(spec["image"])
    network_id = cloud.resolve_network(spec["network"]) if spec.get("network") else None
    metadata = {UID_KEY: vm.uid, "vmlease-namespace": vm.namespace, "vmlease-name": vm.name}
    return cloud.create_server(vm.server_name, flavor_id, image_id, network_id, metadata)


def _first_ip(server):
    for addresses in (server.get("addresses") or {}).values():
        for address in addresses:
            return address["addr"]
    return None
