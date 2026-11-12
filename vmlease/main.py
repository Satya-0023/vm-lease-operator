"""Kubernetes side of the operator, built on kopf.

Only two handlers:
  * a timer that runs reconcile() for every OpenStackVM every RECONCILE_INTERVAL seconds
    (kopf starts it as soon as an object appears, so creation is picked up right away);
  * a delete handler. Because it exists, kopf puts a *finalizer* on each object, so
    `kubectl delete` waits until we have deleted the VM in OpenStack. No orphaned VMs.

Run locally:   kopf run --standalone --all-namespaces -m vmlease.main
"""
import logging
import os
from datetime import datetime, timezone

import kopf
from prometheus_client import start_http_server

from . import metrics
from .openstack_client import OpenStackClient
from .reconciler import VMRef, delete_owned_servers, reconcile

GROUP, VERSION, PLURAL = "vmlease.io", "v1alpha1", "openstackvms"
INTERVAL = float(os.environ.get("RECONCILE_INTERVAL", "10"))

cloud = None


@kopf.on.startup()
def startup(settings, logger, **_):
    global cloud
    cloud = OpenStackClient.from_env()
    cloud.authenticate()  # fail fast on bad credentials instead of on the first VM
    settings.persistence.finalizer = "vmlease.io/cleanup"
    settings.posting.level = logging.WARNING  # only our own events, not kopf's chatter
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    logger.info("connected to OpenStack at %s", cloud.auth_url)


def _vm_ref(meta):
    created = datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))
    return VMRef(namespace=meta["namespace"], name=meta["name"], uid=meta["uid"], created_at=created)


@kopf.timer(GROUP, VERSION, PLURAL, interval=INTERVAL)
def reconcile_vm(body, meta, spec, status, patch, logger, **_):
    current = {k: v for k, v in (status or {}).items() if not k.startswith("kopf")}
    try:
        new_status, events = reconcile(_vm_ref(meta), spec, current, cloud, datetime.now(timezone.utc))
    except Exception as err:  # cloud unreachable, 5xx...: record it, keep the old state, retry next tick
        metrics.RECONCILES.labels("error").inc()
        logger.warning("reconcile failed, will retry in %ss: %s", INTERVAL, err)
        if current.get("message") != f"retrying: {err}":
            patch.status["message"] = f"retrying: {err}"
        return
    metrics.RECONCILES.labels("ok").inc()
    for event in events:
        kopf.event(body, type=event.type, reason=event.reason, message=event.message)
        metrics.EVENTS.labels(event.reason).inc()
        logger.info("%s: %s", event.reason, event.message)
    for key, value in new_status.items():
        if current.get(key) != value:  # patch only what changed, so idle objects cost no API writes
            patch.status[key] = value
    metrics.record_phase(meta["uid"], new_status.get("phase"))


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete_vm(meta, status, logger, **_):
    deleted = delete_owned_servers(_vm_ref(meta), status or {}, cloud)
    metrics.record_phase(meta["uid"], None)
    logger.info("deleted servers %s; releasing finalizer", deleted or "(none)")
