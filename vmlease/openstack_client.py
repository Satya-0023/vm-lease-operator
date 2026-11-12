"""A small OpenStack REST client: Keystone v3 password auth plus the Nova, Glance and Neutron
calls this operator needs.

Written by hand on purpose (instead of openstacksdk) so every HTTP call is visible:

1. POST {auth_url}/auth/tokens with username/password scoped to a project.
   Keystone returns the token in the X-Subject-Token header and a *service catalog* in the body.
2. The catalog says where each service lives (compute -> Nova URL, image -> Glance URL, ...).
3. Every later call sends the token as X-Auth-Token. If the token is close to expiry, or a call
   returns 401, we log in again once and retry.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from . import metrics

REFRESH_MARGIN = timedelta(seconds=60)


class OpenStackError(Exception):
    def __init__(self, status, message):
        super().__init__(f"OpenStack API error {status}: {message}")
        self.status = status
        self.message = message


def _error_message(response):
    """Nova wraps errors as {"itemNotFound": {"message": ...}}; Glance/Neutron use other shapes."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or response.reason
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, dict) and "message" in value:
                return value["message"]
        if "NeutronError" in body:
            return body["NeutronError"].get("message", "")
    return str(body)[:200]


class OpenStackClient:
    def __init__(self, auth_url, username, password, project_name,
                 user_domain="Default", project_domain="Default", region=None, timeout=10):
        self.auth_url = auth_url.rstrip("/")
        self.username = username
        self.password = password
        self.project_name = project_name
        self.user_domain = user_domain
        self.project_domain = project_domain
        self.region = region
        self.timeout = timeout
        self.session = requests.Session()
        self._token = None
        self._token_expires = None
        self._endpoints = {}

    @classmethod
    def from_env(cls):
        """Read the same OS_* variables the `openstack` CLI reads from an openrc file."""
        return cls(
            auth_url=os.environ["OS_AUTH_URL"],
            username=os.environ["OS_USERNAME"],
            password=os.environ["OS_PASSWORD"],
            project_name=os.environ["OS_PROJECT_NAME"],
            user_domain=os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
            project_domain=os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default"),
            region=os.environ.get("OS_REGION_NAME") or None,
        )

    # ---------------- auth ----------------
    def authenticate(self):
        body = {"auth": {
            "identity": {"methods": ["password"], "password": {"user": {
                "name": self.username, "domain": {"name": self.user_domain}, "password": self.password}}},
            "scope": {"project": {"name": self.project_name, "domain": {"name": self.project_domain}}},
        }}
        response = self.session.post(f"{self.auth_url}/auth/tokens", json=body, timeout=self.timeout)
        if response.status_code != 201:
            raise OpenStackError(response.status_code, f"login failed: {_error_message(response)}")
        token = response.json()["token"]
        self._token = response.headers["X-Subject-Token"]
        self._token_expires = datetime.fromisoformat(token["expires_at"].replace("Z", "+00:00"))
        self._endpoints = {}
        for service in token.get("catalog", []):
            for endpoint in service["endpoints"]:
                if endpoint["interface"] == "public" and self.region in (None, endpoint.get("region")):
                    self._endpoints[service["type"]] = endpoint["url"].rstrip("/")

    def _token_valid(self):
        return self._token and datetime.now(timezone.utc) < self._token_expires - REFRESH_MARGIN

    def _request(self, method, service, path, **kwargs):
        if not self._token_valid():
            self.authenticate()
        response = self._send(method, service, path, **kwargs)
        if response.status_code == 401:
            self.authenticate()  # token revoked or expired early: log in once more and retry once
            response = self._send(method, service, path, **kwargs)
        return response

    def _send(self, method, service, path, **kwargs):
        if service not in self._endpoints:
            raise OpenStackError(0, f"service '{service}' is not in the Keystone catalog")
        started = time.perf_counter()
        response = self.session.request(method, self._endpoints[service] + path,
                                        headers={"X-Auth-Token": self._token}, timeout=self.timeout, **kwargs)
        metrics.OPENSTACK_LATENCY.labels(service, method).observe(time.perf_counter() - started)
        return response

    def _call(self, method, service, path, ok=(200,), **kwargs):
        response = self._request(method, service, path, **kwargs)
        if response.status_code not in ok:
            raise OpenStackError(response.status_code, _error_message(response))
        return response

    # ---------------- lookups: name or ID -> ID ----------------
    def resolve_flavor(self, name_or_id):
        flavors = self._call("GET", "compute", "/flavors").json()["flavors"]
        for flavor in flavors:
            if name_or_id in (flavor["id"], flavor["name"]):
                return flavor["id"]
        raise OpenStackError(404, f"flavor '{name_or_id}' not found")

    def resolve_image(self, name_or_id):
        images = self._call("GET", "image", "/v2/images", params={"name": name_or_id}).json()["images"]
        if len(images) > 1:
            raise OpenStackError(409, f"image name '{name_or_id}' is ambiguous; use the image ID")
        if images:
            return images[0]["id"]
        response = self._request("GET", "image", f"/v2/images/{name_or_id}")
        if response.status_code == 200:
            return response.json()["id"]
        raise OpenStackError(404, f"image '{name_or_id}' not found")

    def resolve_network(self, name_or_id):
        for key in ("name", "id"):
            nets = self._call("GET", "network", "/v2.0/networks", params={key: name_or_id}).json()["networks"]
            if len(nets) > 1:
                raise OpenStackError(409, f"network name '{name_or_id}' is ambiguous; use the network ID")
            if nets:
                return nets[0]["id"]
        raise OpenStackError(404, f"network '{name_or_id}' not found")

    # ---------------- servers (Nova) ----------------
    def create_server(self, name, flavor_id, image_id, network_id=None, metadata=None):
        server = {"name": name, "flavorRef": flavor_id, "imageRef": image_id, "metadata": metadata or {}}
        if network_id:
            server["networks"] = [{"uuid": network_id}]
        response = self._call("POST", "compute", "/servers", ok=(202,), json={"server": server})
        return response.json()["server"]["id"]

    def get_server(self, server_id):
        """Return the server dict, or None if Nova says it does not exist."""
        response = self._request("GET", "compute", f"/servers/{server_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise OpenStackError(response.status_code, _error_message(response))
        return response.json()["server"]

    def find_servers(self, name, metadata_key, metadata_value):
        """Servers with this exact name AND this metadata tag. Used to adopt a VM we created
        but never managed to record (for example the operator crashed right after the POST)."""
        pattern = "^" + name.replace(".", r"\.") + "$"  # Nova's name filter is a regex
        servers = self._call("GET", "compute", "/servers/detail", params={"name": pattern}).json()["servers"]
        return [s for s in servers if s.get("metadata", {}).get(metadata_key) == metadata_value]

    def delete_server(self, server_id):
        """Return True if deleted, False if it was already gone. Both are fine for us."""
        response = self._call("DELETE", "compute", f"/servers/{server_id}", ok=(204, 404))
        return response.status_code == 204

    def stop_server(self, server_id):
        self._call("POST", "compute", f"/servers/{server_id}/action", ok=(202,), json={"os-stop": None})
