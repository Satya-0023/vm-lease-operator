"""fakestack: a tiny in-memory OpenStack cloud for demos and tests.

It implements a small but real subset of four OpenStack REST APIs, using the same URLs,
headers and JSON shapes as a DevStack install:

  Keystone (identity)  POST /identity/v3/auth/tokens
  Nova     (compute)   /compute/v2.1/flavors, /servers, /servers/detail, /servers/<id>/action
  Glance   (image)     /image/v2/images
  Neutron  (network)   /networking/v2.0/networks

Because the operator's client only speaks those public APIs, the same client code works
against a real cloud. Everything lives in memory and is lost on restart.

/_fake/* endpoints are NOT OpenStack. They let a demo or test play "another admin" who deletes
a VM behind the operator's back, breaks one, or revokes all tokens.
"""
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

USERS = {"demo": "secret"}
PROJECT = {"id": "p-demo", "name": "demo", "domain": {"id": "default", "name": "Default"}}
FLAVORS = [
    {"id": "1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1},
    {"id": "2", "name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20},
    {"id": "3", "name": "m1.medium", "vcpus": 2, "ram": 4096, "disk": 40},
]
IMAGES = [
    {"id": "9b1c0b6e-cirros", "name": "cirros", "status": "active"},
    {"id": "4f2d7a10-ubuntu", "name": "ubuntu-24.04", "status": "active"},
]
NETWORKS = [{"id": "6e3a1f52-private", "name": "private", "status": "ACTIVE"}]


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(code, kind, message):
    # Nova wraps errors as {"itemNotFound": {"message": ..., "code": 404}} and so on.
    return jsonify({kind: {"message": message, "code": code}}), code


def create_app(build_seconds=None, token_ttl=None):
    app = Flask(__name__)
    build_seconds = float(os.environ.get("FAKESTACK_BUILD_SECONDS", 3) if build_seconds is None else build_seconds)
    token_ttl = int(os.environ.get("FAKESTACK_TOKEN_TTL", 3600) if token_ttl is None else token_ttl)

    lock = threading.Lock()
    tokens = {}   # token string -> expiry datetime
    servers = {}  # server id -> dict
    next_ip = [10]

    def public_url():
        return os.environ.get("FAKESTACK_PUBLIC_URL", request.host_url.rstrip("/"))

    def authorized():
        token = request.headers.get("X-Auth-Token")
        expires = tokens.get(token)
        return expires is not None and expires > _now()

    def refresh_status(server):
        # A real VM takes a while to boot; we fake that with a timer checked on every read.
        if server["status"] == "BUILD" and time.time() - server["_created_epoch"] >= build_seconds:
            server["status"] = "ACTIVE"
            server["addresses"] = {"private": [{"addr": f"10.0.0.{next_ip[0]}", "version": 4}]}
            next_ip[0] += 1
            server["updated"] = _iso(_now())

    def view(server):
        refresh_status(server)
        return {k: v for k, v in server.items() if not k.startswith("_")}

    @app.before_request
    def require_token():
        path = request.path
        if path.startswith("/identity/") or path.startswith("/_fake/") or path == "/healthz":
            return None
        if not authorized():
            return _error(401, "unauthorized", "The request you have made requires authentication.")
        return None

    # ---------------- Keystone ----------------
    @app.post("/identity/v3/auth/tokens")
    def issue_token():
        body = request.get_json(silent=True) or {}
        try:
            user = body["auth"]["identity"]["password"]["user"]
            name, password = user["name"], user["password"]
            project = body["auth"]["scope"]["project"]["name"]
        except (KeyError, TypeError):
            return _error(400, "error", "Expected password auth scoped to a project.")
        if USERS.get(name) != password or project != PROJECT["name"]:
            return _error(401, "error", "The request you have made requires authentication.")

        token = uuid.uuid4().hex
        expires = _now() + timedelta(seconds=token_ttl)
        with lock:
            tokens[token] = expires
        base = public_url()
        catalog = [
            {"type": "compute", "name": "nova",
             "endpoints": [{"interface": "public", "region": "RegionOne", "url": f"{base}/compute/v2.1"}]},
            {"type": "image", "name": "glance",
             "endpoints": [{"interface": "public", "region": "RegionOne", "url": f"{base}/image"}]},
            {"type": "network", "name": "neutron",
             "endpoints": [{"interface": "public", "region": "RegionOne", "url": f"{base}/networking"}]},
        ]
        resp = jsonify({"token": {"expires_at": _iso(expires), "project": PROJECT,
                                  "user": {"name": name}, "catalog": catalog}})
        resp.status_code = 201
        resp.headers["X-Subject-Token"] = token
        return resp

    # ---------------- Nova ----------------
    @app.get("/compute/v2.1/flavors")
    def list_flavors():
        return jsonify({"flavors": [{"id": f["id"], "name": f["name"]} for f in FLAVORS]})

    @app.post("/compute/v2.1/servers")
    def create_server():
        spec = (request.get_json(silent=True) or {}).get("server") or {}
        if not spec.get("name"):
            return _error(400, "badRequest", "Server name is required.")
        if spec.get("flavorRef") not in {f["id"] for f in FLAVORS}:
            return _error(400, "badRequest", f"Flavor {spec.get('flavorRef')} could not be found.")
        if spec.get("imageRef") not in {i["id"] for i in IMAGES}:
            return _error(400, "badRequest", f"Image {spec.get('imageRef')} could not be found.")
        for net in spec.get("networks") or []:
            if net.get("uuid") not in {n["id"] for n in NETWORKS}:
                return _error(400, "badRequest", f"Network {net.get('uuid')} could not be found.")

        server_id = str(uuid.uuid4())
        now = _now()
        with lock:
            servers[server_id] = {
                "id": server_id,
                "name": spec["name"],
                "status": "BUILD",
                "flavor": {"id": spec["flavorRef"]},
                "image": {"id": spec["imageRef"]},
                "metadata": dict(spec.get("metadata") or {}),
                "addresses": {},
                "created": _iso(now),
                "updated": _iso(now),
                "tenant_id": PROJECT["id"],
                "_created_epoch": time.time(),
            }
        return jsonify({"server": {"id": server_id, "links": []}}), 202

    @app.get("/compute/v2.1/servers/detail")
    def list_servers():
        pattern = request.args.get("name")  # Nova treats this filter as a regular expression
        with lock:
            found = [view(s) for s in servers.values() if not pattern or re.search(pattern, s["name"])]
        return jsonify({"servers": found})

    @app.get("/compute/v2.1/servers/<server_id>")
    def get_server(server_id):
        with lock:
            server = servers.get(server_id)
            if server is None:
                return _error(404, "itemNotFound", f"Instance {server_id} could not be found.")
            return jsonify({"server": view(server)})

    @app.delete("/compute/v2.1/servers/<server_id>")
    def delete_server(server_id):
        with lock:
            if servers.pop(server_id, None) is None:
                return _error(404, "itemNotFound", f"Instance {server_id} could not be found.")
        return "", 204

    @app.post("/compute/v2.1/servers/<server_id>/action")
    def server_action(server_id):
        action = request.get_json(silent=True) or {}
        with lock:
            server = servers.get(server_id)
            if server is None:
                return _error(404, "itemNotFound", f"Instance {server_id} could not be found.")
            refresh_status(server)
            if "os-stop" in action:
                if server["status"] != "ACTIVE":
                    return _error(409, "conflictingRequest",
                                  f"Cannot 'stop' instance {server_id} while it is in vm_state {server['status'].lower()}")
                server["status"] = "SHUTOFF"
                server["updated"] = _iso(_now())
                return "", 202
        return _error(400, "badRequest", "Unsupported action.")

    # ---------------- Glance ----------------
    @app.get("/image/v2/images")
    def list_images():
        name = request.args.get("name")
        return jsonify({"images": [i for i in IMAGES if not name or i["name"] == name]})

    @app.get("/image/v2/images/<image_id>")
    def get_image(image_id):
        for image in IMAGES:
            if image["id"] == image_id:
                return jsonify(image)
        return _error(404, "itemNotFound", f"No image found with ID {image_id}")

    # ---------------- Neutron ----------------
    @app.get("/networking/v2.0/networks")
    def list_networks():
        name, net_id = request.args.get("name"), request.args.get("id")
        found = [n for n in NETWORKS if (not name or n["name"] == name) and (not net_id or n["id"] == net_id)]
        return jsonify({"networks": found})

    # ---------------- Test / demo helpers (not OpenStack) ----------------
    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/_fake/servers")
    def fake_list():
        with lock:
            return jsonify([view(s) for s in servers.values()])

    @app.delete("/_fake/servers/<server_id>")
    def fake_delete(server_id):
        """Someone deleted the VM directly in Horizon/CLI, behind the operator's back."""
        with lock:
            gone = servers.pop(server_id, None)
        return ("", 204) if gone else ("", 404)

    @app.post("/_fake/servers/<server_id>/error")
    def fake_break(server_id):
        """The hypervisor failed the VM."""
        with lock:
            server = servers.get(server_id)
            if server is None:
                return "", 404
            server["status"] = "ERROR"
            server["fault"] = {"code": 500, "message": "No valid host was found."}
        return "", 204

    @app.delete("/_fake/tokens")
    def fake_revoke_tokens():
        """Revoke every token, like a Keystone restart with fernet key rotation."""
        with lock:
            tokens.clear()
        return "", 204

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
