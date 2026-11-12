"""Prometheus metrics, served on :9100/metrics by the operator."""
from prometheus_client import Counter, Gauge, Histogram

RECONCILES = Counter("vmlease_reconcile_total", "Reconcile runs", ["result"])
EVENTS = Counter("vmlease_events_total", "Lifecycle events emitted, by reason", ["reason"])
VMS = Gauge("vmlease_vms", "OpenStackVM objects currently known, by phase", ["phase"])
OPENSTACK_LATENCY = Histogram("vmlease_openstack_request_seconds",
                              "Latency of OpenStack API calls", ["service", "method"])

_phase_by_uid = {}


def record_phase(uid, phase):
    """Remember each object's phase and refresh the per-phase gauge. phase=None forgets it."""
    if phase is None:
        _phase_by_uid.pop(uid, None)
    else:
        _phase_by_uid[uid] = phase
    for known in ("Pending", "Building", "Active", "Stopped", "Error", "Expired", "Unknown"):
        VMS.labels(known).set(sum(1 for p in _phase_by_uid.values() if p == known))
