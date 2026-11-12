import threading

import pytest
from werkzeug.serving import make_server

from fakestack.app import create_app
from vmlease.openstack_client import OpenStackClient


@pytest.fixture
def fakestack():
    """A real HTTP fakestack on a random local port, fresh state per test. VMs boot instantly."""
    server = make_server("127.0.0.1", 0, create_app(build_seconds=0), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def cloud(fakestack):
    return OpenStackClient(f"{fakestack}/identity/v3", "demo", "secret", "demo")
