from datetime import datetime, timedelta, timezone

import pytest

from vmlease import lease


@pytest.mark.parametrize("text, expected", [
    ("30s", timedelta(seconds=30)),
    ("90m", timedelta(minutes=90)),
    ("2h30m", timedelta(hours=2, minutes=30)),
    ("1d12h", timedelta(days=1, hours=12)),
])
def test_parse_duration(text, expected):
    assert lease.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "10", "1h1d", "0s", "-5m", None])
def test_parse_duration_rejects_bad_input(text):
    with pytest.raises(ValueError):
        lease.parse_duration(text)


def test_expiry_boundary():
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = lease.expires_at(created, "1h")
    assert not lease.is_expired(expiry, created + timedelta(minutes=59, seconds=59))
    assert lease.is_expired(expiry, created + timedelta(hours=1))


def test_iso_round_trip():
    dt = datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    assert lease.to_iso(dt) == "2026-11-12T10:30:00Z"
    assert lease.from_iso("2026-11-12T10:30:00Z") == dt
