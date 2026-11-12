import pytest
import requests

from vmlease.openstack_client import OpenStackClient, OpenStackError


def test_login_reads_service_catalog(cloud, fakestack):
    cloud.authenticate()
    assert cloud._endpoints == {
        "compute": f"{fakestack}/compute/v2.1",
        "image": f"{fakestack}/image",
        "network": f"{fakestack}/networking",
    }


def test_bad_password_is_rejected(fakestack):
    client = OpenStackClient(f"{fakestack}/identity/v3", "demo", "wrong", "demo")
    with pytest.raises(OpenStackError) as err:
        client.authenticate()
    assert err.value.status == 401


def test_resolve_names_to_ids(cloud):
    assert cloud.resolve_flavor("m1.small") == "2"
    assert cloud.resolve_flavor("2") == "2"
    assert cloud.resolve_image("cirros") == "9b1c0b6e-cirros"
    assert cloud.resolve_image("9b1c0b6e-cirros") == "9b1c0b6e-cirros"
    assert cloud.resolve_network("private") == "6e3a1f52-private"
    with pytest.raises(OpenStackError) as err:
        cloud.resolve_flavor("m1.huge")
    assert err.value.status == 404


def test_server_lifecycle(cloud):
    server_id = cloud.create_server("demo-vm", "1", "9b1c0b6e-cirros", "6e3a1f52-private", {"k": "v"})
    server = cloud.get_server(server_id)
    assert server["status"] == "ACTIVE"
    assert server["metadata"] == {"k": "v"}
    assert [s["id"] for s in cloud.find_servers("demo-vm", "k", "v")] == [server_id]
    assert cloud.find_servers("demo-vm", "k", "other") == []
    cloud.stop_server(server_id)
    assert cloud.get_server(server_id)["status"] == "SHUTOFF"
    assert cloud.delete_server(server_id) is True
    assert cloud.delete_server(server_id) is False  # already gone is not an error
    assert cloud.get_server(server_id) is None


def test_find_servers_matches_exact_name_only(cloud):
    cloud.create_server("team.vm", "1", "9b1c0b6e-cirros", metadata={"k": "v"})
    cloud.create_server("teamXvm", "1", "9b1c0b6e-cirros", metadata={"k": "v"})
    cloud.create_server("team.vm-2", "1", "9b1c0b6e-cirros", metadata={"k": "v"})
    assert [s["name"] for s in cloud.find_servers("team.vm", "k", "v")] == ["team.vm"]


def test_relogin_after_token_revoked(cloud, fakestack):
    cloud.authenticate()
    old_token = cloud._token
    requests.delete(f"{fakestack}/_fake/tokens")  # every token is now invalid
    assert cloud.resolve_flavor("m1.tiny") == "1"  # 401, re-login, retry: the caller never notices
    assert cloud._token != old_token
