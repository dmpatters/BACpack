"""GET/PUT /api/settings/network: network role/BBMD validation, trend retention and soft limit."""
from __future__ import annotations

import pytest

from tests.web._helpers import _create_device, _create_object


# --- Settings ---------------------------------------------------------


def test_network_settings_roundtrip(web_client):
    r = web_client.put(
        "/api/settings/network",
        json={"bind_address": "192.168.1.50/24"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bind_address"] == "192.168.1.50/24"

    r = web_client.get("/api/settings/network")
    assert r.json()["bind_address"] == "192.168.1.50/24"


# --- Network settings validation ------------------------------------------
# bind_address/foreign_bbmd/bbmd_peers used to save with a 200 no matter
# what -- see validate_network_settings in bacnet.py. These specifically
# exercise it through the real update_network_settings *route* (a sync
# FastAPI endpoint, dispatched via TestClient/AnyIO to a worker thread with
# no event loop of its own) rather than calling the validator directly --
# that thread-dispatch context is exactly what an earlier version of this
# fix got wrong (it called build_network_port_object, which needs a running
# event loop and doesn't have one there, turning *every* save into a false-
# positive 400 -- caught by re-running the full suite before shipping, not
# by a targeted test, which is why these exist now).


def test_update_network_settings_rejects_bad_bind_address(web_client):
    r = web_client.put("/api/settings/network", json={"bind_address": "192.168.1.999/24"})
    assert r.status_code == 400
    assert "bind_address" in r.json()["detail"]

    # Confirm it didn't half-apply.
    assert web_client.get("/api/settings/network").json()["bind_address"] is None


def test_network_settings_exposes_trend_retention_and_soft_limit(web_client):
    r = web_client.get("/api/settings/network")
    body = r.json()
    # 14 is the log retention default: the wrong setting would pass an "or".
    assert body["trend_retention_days"] == 90
    assert body["trend_soft_limit_mb"] is None

    r = web_client.put("/api/settings/network", json={"trend_retention_days": 30, "trend_soft_limit_mb": 5})
    body = r.json()
    assert body["trend_retention_days"] == 30
    assert body["trend_soft_limit_mb"] == 5


def test_soft_limit_out_of_bounds_is_rejected(web_client):
    r = web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 2000})
    assert r.status_code == 422


def test_lowering_trend_soft_limit_immediately_prunes_to_fit(web_client, monkeypatch):
    from bacpack import trending

    # BYTES_PER_TREND_SAMPLE is the real empirical estimate (105 bytes), far
    # too small relative to trend_soft_limit_mb's whole-megabyte granularity
    # to exercise pruning with only a handful of samples -- even the API's
    # smallest active setting, 1 MB, holds ~9986 real-sized samples. Patched
    # to a much larger per-sample size here (same pattern as
    # STOP_CHECK_TICK_SECONDS elsewhere: a plain module global, read fresh on
    # every call) purely so this test's small sample count can cross a
    # whole-MB boundary.
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 300_000)

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        trending.record_sample(session_factory, obj["id"], v)

    # 3 samples' worth of MB, rounded up -- enough room for exactly 3.
    limit_mb = max(1, (3 * trending.BYTES_PER_TREND_SAMPLE) // (1024 * 1024) + 1)
    r = web_client.put("/api/settings/network", json={"trend_soft_limit_mb": limit_mb})
    assert r.status_code == 200
    assert web_client.app.state.trend_pruner.wait_idle(5)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend")
    assert len(r.json()) <= 3


@pytest.mark.usefixtures("quiet_retention")
def test_saving_an_unchanged_soft_limit_kicks_nothing(web_client, monkeypatch):
    from bacpack.trending import TrendPruner

    kicks = []
    real_kick = TrendPruner.kick
    monkeypatch.setattr(TrendPruner, "kick", lambda self: (kicks.append(1), real_kick(self)))

    assert web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 5}).status_code == 200
    assert web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 5}).status_code == 200
    assert web_client.put("/api/settings/network", json={"trend_retention_days": 7}).status_code == 200
    assert len(kicks) == 1


# --- Explicit nulls and ranges ----------------------------------------------


def test_network_settings_no_longer_has_auth_fields(web_client):
    body = web_client.get("/api/settings/network").json()
    assert not [key for key in body if key.startswith("auth_")]
    for field, value in (("auth_enabled", True), ("auth_username", "admin"), ("auth_password", "pw")):
        r = web_client.put("/api/settings/network", json={field: value})
        assert r.status_code == 422, (field, r.text)


def test_the_network_role_moved_to_each_device(web_client):
    assert "network_role" not in web_client.get("/api/settings/network").json()
    for field, value in (
        ("network_role", "bbmd"), ("foreign_bbmd", "10.0.0.5"), ("foreign_ttl", 90), ("bbmd_peers", ["10.0.0.9"]),
    ):
        r = web_client.put("/api/settings/network", json={field: value})
        assert r.status_code == 422, (field, r.text)
        assert "per device" in r.text


def test_trend_retention_days_is_bounded(web_client):
    # 2^64 500ed, and a huge value overflowed the retention cutoff.
    assert web_client.put("/api/settings/network", json={"trend_retention_days": 36500}).status_code == 200
    assert web_client.put("/api/settings/network", json={"trend_retention_days": 36501}).status_code == 422
