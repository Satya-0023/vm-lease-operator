"""Lease arithmetic. Pure functions, no I/O, so they are trivial to unit test."""
import re
from datetime import datetime, timedelta, timezone

_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text):
    """'2h30m' -> timedelta(hours=2, minutes=30). Units: d, h, m, s, in that order."""
    match = _DURATION.match(text or "")
    if not text or not match:
        raise ValueError(f"invalid duration '{text}', expected something like 30m, 2h or 1d12h")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    duration = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if duration <= timedelta(0):
        raise ValueError("duration must be greater than zero")
    return duration


def expires_at(created_at, ttl):
    """The lease runs from when the Kubernetes object was created. Raising spec.ttl extends it."""
    return created_at + parse_duration(ttl)


def is_expired(expiry, now):
    return now >= expiry


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
