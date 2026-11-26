"""Feed/manual present-value writes and engine start/stop/restart."""
from __future__ import annotations

import inspect

import pytest

from bacpack.web.api.writes import write_feed, write_manual
from tests.web._helpers import _create_device, _create_object, _create_schedule_object


# --- Engine + feed ---------------------------------------------------------


@pytest.mark.parametrize("endpoint", [write_feed, write_manual])
def test_queued_write_endpoints_run_in_the_thread_pool(endpoint):
    # FastAPI runs a plain def endpoint in its thread pool; an async one runs
    # its commit on the event loop, which waits there for a held write lock.
    assert not inspect.iscoroutinefunction(endpoint)


def test_engine_requires_bind_address(web_client):
    _create_device(web_client)
    r = web_client.post("/api/engine/start", json={"device_id": 599001})
    assert r.status_code == 400


def test_engine_start_stop_and_feed_write(web_client):
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})

    r = web_client.post("/api/engine/start", json={"device_id": 599001})
    assert r.status_code == 200
    status = r.json()
    entry = next(row for row in status if row["device_id"] == 599001)
    assert entry["process_status"] == "running"

    r = web_client.post("/api/feed/599001/analog-value/1", json={"value": 42.0})
    assert r.status_code == 200
    assert r.json() == {"queued": True, "value": 42.0}

    r = web_client.post("/api/manual/599001/analog-value/1", json={"value": 7.0})
    assert r.status_code == 200
    assert r.json() == {"queued": True, "value": 7.0}

    # relinquish (value: null) the manual override over the real HTTP API --
    # the "clear override" path -- falls back to the still-active feed value
    r = web_client.post("/api/manual/599001/analog-value/1", json={"value": None})
    assert r.status_code == 200
    assert r.json() == {"queued": True, "value": None}

    r = web_client.post("/api/engine/stop", json={"device_id": 599001})
    assert r.status_code == 200
    status = r.json()
    entry = next(row for row in status if row["device_id"] == 599001)
    assert entry["process_status"] == "stopped"


def test_feed_write_to_unknown_object_404(web_client):
    _create_device(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    r = web_client.post("/api/feed/599001/analog-value/99", json={"value": 1.0})
    assert r.status_code == 404


# --- Bad-type feed/manual writes -------------------------------------------
# A value that doesn't cast to the object's presentValue type used to raise
# straight out of the route uncaught -- the feed route had no try/except at
# all, and the manual route only caught TypeError, missing the ValueError a
# non-numeric string raises from the same cast.


def test_feed_write_rejects_wrong_type_value(web_client):
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    # _validate_queued_value restores a synchronous guard against a plainly-
    # wrong-typed value at queue time.
    r = web_client.post("/api/feed/599001/analog-value/1", json={"value": {"nested": "dict"}})
    assert r.status_code == 400


def test_feed_write_rejects_non_numeric_string_for_analog(web_client):
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    r = web_client.post("/api/feed/599001/analog-value/1", json={"value": "not-a-number"})
    assert r.status_code == 400
    assert "not a valid number" in r.json()["detail"]


def test_manual_write_rejects_non_numeric_string_for_analog(web_client):
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    r = web_client.post("/api/manual/599001/analog-value/1", json={"value": "not-a-number"})
    assert r.status_code == 400
    assert "not a valid number" in r.json()["detail"]


def test_feed_write_while_not_live_409(web_client):
    _create_device(web_client)
    _create_object(web_client)
    r = web_client.post("/api/feed/599001/analog-value/1", json={"value": 1.0})
    assert r.status_code == 409


def test_feed_write_applied_end_to_end_through_pending_write_poller(
    web_client, tmp_path, monkeypatch
):
    """True end-to-end proof for the write chain: API write -> pending-write
    slot -> PendingWritePoller (inside the device's real owning "child" --
    web_client's FakeDeviceProcess runs the real _serve_device, including a
    real PendingWritePoller, as a background asyncio task, see
    tests/fakes.py) -> the live present-value actually changes.

    The test doesn't read the value back through the API, because checking the
    pending slot clearing alone is NOT sufficient proof:
    PendingWritePoller._clear runs unconditionally, even when applying the
    write raised or the live object couldn't be found (see pending_writes.py's
    apply_due). The real signal used instead: the object is created trended in
    "cov" mode, whose hook fires synchronously off the live object's own
    presentValue changing (see TrendSampler._register_cov_hook) -- a
    TrendSample with this write's exact value can only exist if the write
    genuinely reached the live object, independent of whatever the pending slot
    says.
    """
    import time

    from bacpack import device_process
    from bacpack.db import make_engine, make_session_factory
    from bacpack.models import BacnetObject, TrendSample

    # The device's DeviceWatch applies queued writes every
    # STOP_CHECK_TICK_SECONDS; shorten it so this test needn't wait
    # the full second.
    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.05)

    _create_device(web_client)
    _create_object(web_client, trended=True, trend_mode="cov")
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    r = web_client.post("/api/feed/599001/analog-value/1", json={"value": 42.0})
    assert r.status_code == 200

    # A second, independent connection to web_client's own DB file (see
    # conftest.py's web_client fixture -- tmp_path is the same directory
    # instance across both fixtures within this one test).
    engine = make_engine(tmp_path / "web-test.db")
    session_factory = make_session_factory(engine)

    deadline = time.monotonic() + 2.0
    applied = False
    while time.monotonic() < deadline:
        with session_factory() as session:
            obj = (
                session.query(BacnetObject)
                .filter_by(device_id=599001, object_type="analog-value", instance_number=1)
                .first()
            )
            if (
                session.query(TrendSample)
                .filter_by(object_id=obj.id, value=42.0)
                .first()
                is not None
            ):
                applied = True
                break
        time.sleep(0.05)

    assert applied, (
        "no TrendSample recording the fed value 42.0 ever appeared -- the write "
        "was queued but never actually reached the live object"
    )


# --- Device clock --------------------------------------------------------

import datetime as dt


def _status_row(client, device_id=599001):
    return next(r for r in client.get("/api/engine/status").json() if r["device_id"] == device_id)


def test_engine_status_carries_the_device_clock(web_client):
    _create_device(web_client)
    row = _status_row(web_client)
    assert row["clock_offset_seconds"] == 0.0
    assert row["clock_change_pending"] is False
    assert dt.datetime.fromisoformat(row["device_time"]).tzinfo is not None


def test_engine_status_carries_the_backup_failure_timeout(web_client):
    # Written over BACnet and saved by the device; the API reads it.
    _create_device(web_client)
    assert _status_row(web_client)["backup_failure_timeout"] == 60


def test_set_clock_on_a_stopped_device_writes_the_saved_clock(web_client):
    _create_device(web_client)

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": 3600})

    assert r.status_code == 200, r.text
    assert r.json()["clock_offset_seconds"] == 3600.0
    assert r.json()["clock_change_pending"] is False
    assert _status_row(web_client)["clock_offset_seconds"] == 3600.0


def test_set_clock_by_device_time(web_client):
    _create_device(web_client)

    r = web_client.put("/api/devices/599001/clock", json={"device_time": "2030-06-15T08:30:00"})

    assert r.status_code == 200, r.text
    assert r.json()["device_time"].startswith("2030-06-15T08:30:0")


def test_set_clock_on_a_running_device_is_handed_off_as_pending(web_client):
    _create_device(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    assert web_client.post("/api/engine/start", json={"device_id": 599001}).status_code == 200

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": -120})

    assert r.status_code == 200, r.text
    assert r.json()["clock_change_pending"] is True
    assert r.json()["clock_offset_seconds"] == 0.0  # the child hasn't applied it yet
    assert _status_row(web_client)["clock_change_pending"] is True


def _set_process_status(client, status, device_id=599001):
    from bacpack.models import Device

    session_factory = client.app.state.session_factory
    with session_factory() as session:
        session.get(Device, device_id).runtime.process_status = status
        session.commit()


def test_set_clock_on_a_crashed_device_writes_the_saved_clock(web_client):
    """A crashed device has no live child either -- same direct-write path a
    stopped device gets (writes.py's _NO_LIVE_PROCESS)."""
    _create_device(web_client)
    _set_process_status(web_client, "crashed")

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": 900})

    assert r.status_code == 200, r.text
    assert r.json()["clock_offset_seconds"] == 900.0
    assert r.json()["clock_change_pending"] is False
    assert _status_row(web_client)["clock_offset_seconds"] == 900.0


def test_set_clock_on_a_stopping_device_is_handed_off_as_pending(web_client):
    """'stopping' isn't in _NO_LIVE_PROCESS -- the child is still shutting
    down, so this must hand off the same way a running device does. (See
    EngineManager._mark_stopped for what happens if it never gets applied.)
    """
    _create_device(web_client)
    _set_process_status(web_client, "stopping")

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": -300})

    assert r.status_code == 200, r.text
    assert r.json()["clock_change_pending"] is True
    assert r.json()["clock_offset_seconds"] == 0.0
    assert _status_row(web_client)["clock_change_pending"] is True


def test_set_clock_reset_is_offset_zero(web_client):
    _create_device(web_client)
    web_client.put("/api/devices/599001/clock", json={"offset_seconds": 3600})

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": 0})

    assert r.json()["clock_offset_seconds"] == 0.0


def test_set_clock_needs_exactly_one_field(web_client):
    _create_device(web_client)
    assert web_client.put("/api/devices/599001/clock", json={}).status_code == 422
    both = {"offset_seconds": 0, "device_time": "2030-01-01T00:00:00"}
    assert web_client.put("/api/devices/599001/clock", json=both).status_code == 422


def test_set_clock_rejects_a_device_time_with_a_utc_offset(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001/clock", json={"device_time": "2030-01-01T00:00:00+02:00"})
    assert r.status_code == 422


def test_set_clock_rejects_unrepresentable_times(web_client):
    _create_device(web_client)
    for body in (
        {"device_time": "1800-01-01T00:00:00"},
        {"device_time": "2200-01-01T00:00:00"},
        {"offset_seconds": 1e12},
        {"offset_seconds": -1e12},
    ):
        assert web_client.put("/api/devices/599001/clock", json=body).status_code == 422, body


def test_set_clock_unknown_device_404(web_client):
    r = web_client.put("/api/devices/424242/clock", json={"offset_seconds": 0})
    assert r.status_code == 404


def test_manual_and_feed_writes_queue_in_separate_slots(web_client):
    """Manual (priority 8) and feed (priority 10) writes used to
    share one pending slot, so the second within a poller tick replaced the
    first and it was never applied."""
    from bacpack.models import BacnetObject

    _create_device(web_client)
    obj = _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    assert web_client.post("/api/manual/599001/analog-value/1", json={"value": 7.0}).status_code == 200
    assert web_client.post("/api/feed/599001/analog-value/1", json={"value": 42.0}).status_code == 200

    with web_client.app.state.session_factory() as session:
        row = session.get(BacnetObject, obj["id"])
        assert (row.runtime.pending_manual_queued, row.runtime.pending_manual_value) == (True, 7.0)
        assert (row.runtime.pending_feed_queued, row.runtime.pending_feed_value) == (True, 42.0)


def test_feed_and_manual_urls_take_the_object_type_in_any_case(web_client):
    # Every other fixed-choice input is case-insensitive;
    # the object type in these two URLs was still matched exactly.
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    assert web_client.post("/api/engine/start", json={"device_id": 599001}).status_code == 200
    for path in ("feed", "manual"):
        for spelling in ("Analog-Value", "ANALOG_VALUE", "analogValue"):
            r = web_client.post(f"/api/{path}/599001/{spelling}/1", json={"value": 3.0})
            assert r.status_code == 200, (path, spelling, r.text)
    # The type check still runs on the canonical type.
    r = web_client.post("/api/feed/599001/Analog-Value/1", json={"value": "not-a-number"})
    assert r.status_code == 400
    web_client.post("/api/engine/stop", json={"device_id": 599001})


# --- Non-finite numbers -----------------------------------------------------
# A NaN feed value never cleared its pending write (NaN != NaN) and was
# re-applied every second.


@pytest.mark.parametrize("kind", ["feed", "manual"])
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", '"nan"', '"1e999"'])
def test_non_finite_write_value_is_refused(web_client, kind, token):
    _create_device(web_client)
    _create_object(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    assert web_client.post("/api/engine/start", json={"device_id": 599001}).status_code == 200
    r = web_client.post(
        f"/api/{kind}/599001/analog-value/1",
        content='{"value": %s}' % token,
        headers={"content-type": "application/json"},
    )
    assert r.status_code in (400, 422), r.text


# --- Restart all running devices --------------------------------------------


def _device_on(client, device_id, bind_address):
    r = client.post(
        "/api/devices", json={"id": device_id, "name": f"D{device_id}", "bind_address": bind_address}
    )
    assert r.status_code == 201, r.text


def _status_of(client, device_id):
    return next(r for r in client.get("/api/engine/status").json() if r["device_id"] == device_id)


def test_restart_all_restarts_only_the_running_devices(web_client):
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    _device_on(web_client, 3, "127.0.0.1/8:47903")  # never started
    for device_id in (1, 2):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    processes = web_client.app.state.engine.processes
    before = {device_id: processes[device_id] for device_id in (1, 2)}

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    assert r.json() == {"restarted": [1, 2], "failed": []}
    for device_id in (1, 2):
        assert processes[device_id] is not before[device_id]  # a fresh child
        assert _status_of(web_client, device_id)["process_status"] == "running"
    assert 3 not in processes
    assert _status_of(web_client, 3)["process_status"] == "stopped"


def test_restart_all_leaves_a_crashed_device_alone(web_client, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "PORT_FREE_WAIT_SECONDS", 0.2)
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "192.0.2.1/24")  # TEST-NET: never on this machine
    assert web_client.post("/api/engine/start", json={"device_id": 1}).status_code == 200
    assert web_client.post("/api/engine/start", json={"device_id": 2}).status_code == 400
    assert _status_of(web_client, 2)["process_status"] == "crashed"

    r = web_client.post("/api/engine/restart-all")

    assert r.json() == {"restarted": [1], "failed": []}
    assert _status_of(web_client, 2)["process_status"] == "crashed"


def test_restart_all_reports_a_failure_and_keeps_going(web_client):
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    for device_id in (1, 2):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    # Both devices now share one address: the lower id gets it, device 2's
    # start is refused, and device 3 is still restarted after that.
    _device_on(web_client, 3, "127.0.0.1/8:47903")
    assert web_client.post("/api/engine/start", json={"device_id": 3}).status_code == 200
    assert web_client.put("/api/devices/2", json={"bind_address": "127.0.0.1/8:47901"}).status_code == 200
    before = web_client.app.state.engine.processes[3]

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restarted"] == [1, 3]
    assert [f["device_id"] for f in body["failed"]] == [2]
    assert "already in use by currently-running device 1" in body["failed"][0]["error"]
    assert web_client.app.state.engine.processes[3] is not before
    assert _status_of(web_client, 2)["process_status"] != "running"
    from bacpack.models import Device

    with web_client.app.state.session_factory() as session:
        resume = {d.id: d.runtime.resume_on_start for d in session.query(Device).filter(Device.id.in_([1, 2, 3]))}
    assert resume == {1: False, 2: False, 3: False}  # a refused start isn't brought back at next startup


def test_restart_all_starts_devices_whose_new_ports_were_each_others_old_ones(web_client):
    # Each new port is held by another device's old process until that one
    # stops, so a device-by-device restart fails all but the last.
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    _device_on(web_client, 3, "127.0.0.1/8:47903")
    for device_id in (1, 2, 3):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    for device_id, port in ((1, 47902), (2, 47903), (3, 47901)):
        r = web_client.put(f"/api/devices/{device_id}", json={"bind_address": f"127.0.0.1/8:{port}"})
        assert r.status_code == 200, r.text

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    assert r.json() == {"restarted": [1, 2, 3], "failed": []}
    for device_id in (1, 2, 3):
        assert _status_of(web_client, device_id)["process_status"] == "running"


def _resume_flags(client, device_ids):
    from bacpack.models import Device

    with client.app.state.session_factory() as session:
        rows = session.query(Device).filter(Device.id.in_(device_ids))
        return {d.id: d.runtime.resume_on_start for d in rows}


def test_restart_all_skips_a_device_whose_stop_fails_and_restarts_the_rest(web_client, monkeypatch):
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    for device_id in (1, 2):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    engine = web_client.app.state.engine
    real_stop = engine._stop_locked

    async def stop(device_id, **kwargs):
        if device_id == 1:
            raise RuntimeError("database is locked")
        await real_stop(device_id, **kwargs)

    monkeypatch.setattr(engine, "_stop_locked", stop)

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restarted"] == [2]
    assert [f["device_id"] for f in body["failed"]] == [1]
    assert "database is locked" in body["failed"][0]["error"]
    assert _resume_flags(web_client, [1, 2]) == {1: False, 2: False}


def test_restart_all_keeps_going_after_an_unexpected_start_error(web_client, monkeypatch):
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    for device_id in (1, 2):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    engine = web_client.app.state.engine
    real_start = engine._start_locked

    async def start(device_id):
        if device_id == 1:
            raise OSError("the worker executable is missing")
        await real_start(device_id)

    monkeypatch.setattr(engine, "_start_locked", start)

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restarted"] == [2]
    assert [f["device_id"] for f in body["failed"]] == [1]
    assert "the worker executable is missing" in body["failed"][0]["error"]
    assert _resume_flags(web_client, [1, 2]) == {1: False, 2: False}


def test_restart_all_skips_a_device_that_cannot_be_judged_and_restarts_the_rest(web_client, monkeypatch):
    _device_on(web_client, 1, "127.0.0.1/8:47901")
    _device_on(web_client, 2, "127.0.0.1/8:47902")
    for device_id in (1, 2):
        assert web_client.post("/api/engine/start", json={"device_id": device_id}).status_code == 200
    engine = web_client.app.state.engine
    real_reconcile = engine._reconcile_untracked
    before = engine.processes[1]

    async def reconcile(device_id, **kwargs):
        if device_id == 1:
            raise PermissionError("access denied")
        await real_reconcile(device_id, **kwargs)

    monkeypatch.setattr(engine, "_reconcile_untracked", reconcile)

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restarted"] == [2]
    assert [f["device_id"] for f in body["failed"]] == [1]
    assert engine.processes[1] is before  # left running as it was


def test_restart_all_judges_a_running_row_no_process_backs_before_restarting(web_client):
    # A "running" row this server doesn't track, whose process is gone: it
    # is marked crashed like the health check would, not started again.
    from bacpack.models import Device

    _device_on(web_client, 1, "127.0.0.1/8:47901")
    with web_client.app.state.session_factory() as session:
        device = session.get(Device, 1)
        device.runtime.process_status = "running"
        device.runtime.process_pid = None
        session.commit()

    r = web_client.post("/api/engine/restart-all")

    assert r.status_code == 200, r.text
    assert r.json() == {"restarted": [], "failed": []}
    assert _status_of(web_client, 1)["process_status"] == "crashed"
    assert 1 not in web_client.app.state.engine.processes


def test_restart_all_with_nothing_running(web_client):
    _device_on(web_client, 1, "127.0.0.1/8:47901")

    r = web_client.post("/api/engine/restart-all")

    assert r.json() == {"restarted": [], "failed": []}
    assert _status_of(web_client, 1)["process_status"] == "stopped"


# --- Where feed and manual writes apply -------------------------------------


def _start(web_client):
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    r = web_client.post("/api/engine/start", json={"device_id": 599001})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_feed_and_manual_refuse_a_schedule(web_client, route):
    # Queued with a 200, then overwrote the schedule's value or failed.
    _create_device(web_client)
    assert _create_schedule_object(web_client).status_code == 201
    _start(web_client)

    r = web_client.post(f"/api/{route}/599001/schedule/1", json={"value": 5.0})

    assert r.status_code == 400
    assert "schedule" in r.json()["detail"]


def test_manual_refuses_an_input(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="binary-input", name="BI-1")
    _start(web_client)

    r = web_client.post("/api/manual/599001/binary-input/1", json={"value": "active"})

    assert r.status_code == 400
    assert "out_of_service" in r.json()["detail"]
    # The feed still takes it: an input's feed is how a source drives it.
    assert web_client.post("/api/feed/599001/binary-input/1", json={"value": "active"}).status_code == 200


@pytest.mark.parametrize(
    "value, queued",
    [("active", "active"), ("Active", "active"), ("INACTIVE", "inactive"), (1, 1), (0, 0), (1.0, 1),
     (True, True), (False, False), (None, None)],
)
def test_binary_feed_values(web_client, value, queued):
    _create_device(web_client)
    _create_object(web_client, object_type="binary-value", name="BV-1")
    _start(web_client)

    r = web_client.post("/api/feed/599001/binary-value/1", json={"value": value})

    assert r.status_code == 200, r.text
    # True == 1 == 1.0 in Python: the type is part of what a client script reads.
    echoed = r.json()["value"]
    assert (type(echoed), echoed) == (type(queued), queued)


@pytest.mark.parametrize("value", ["abc", 2, 0.5, "1", [1]])
def test_binary_feed_refuses_other_values(web_client, value):
    _create_device(web_client)
    _create_object(web_client, object_type="binary-output", name="BO-1")
    _start(web_client)

    r = web_client.post("/api/feed/599001/binary-output/1", json={"value": value})

    assert r.status_code == 400


@pytest.mark.parametrize("value, status", [(1, 200), (3, 200), (2.0, 200), (0, 400), (4, 400), (1.5, 400), (True, 400), ("2", 400)])
def test_multi_state_feed_values(web_client, value, status):
    _create_device(web_client)
    _create_object(
        web_client, object_type="multi-state-value", name="MSV-1", state_text=["Off", "Low", "High"]
    )
    _start(web_client)

    r = web_client.post("/api/feed/599001/multi-state-value/1", json={"value": value})

    assert r.status_code == status, r.text


@pytest.mark.parametrize("kind", ["feed", "manual"])
@pytest.mark.parametrize("object_type", ["analog-value", "multi-state-value"])
def test_an_integer_too_large_for_a_float_is_a_400(web_client, kind, object_type):
    # float() raises OverflowError, not ValueError, for such an integer.
    _create_device(web_client)
    _create_object(web_client, object_type=object_type, name="V-1", state_text=["Off", "On"] if "multi" in object_type else None)
    _start(web_client)

    r = web_client.post(
        f"/api/{kind}/599001/{object_type}/1",
        content='{"value": 1%s}' % ("0" * 400),
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 400, r.text


def test_set_clock_on_a_starting_device_is_handed_off_as_pending(web_client):
    # While starting, the row still says "stopped" (the pre-spawn reset), but
    # the child has already read the saved clock: a direct write would never
    # reach it.
    from tests.fakes import FakeProcess

    _create_device(web_client)
    process = FakeProcess("db", 599001)
    process.start()
    web_client.app.state.engine.processes[599001] = process

    r = web_client.put("/api/devices/599001/clock", json={"offset_seconds": 600})

    assert r.status_code == 200, r.text
    assert r.json()["clock_change_pending"] is True
    assert r.json()["clock_offset_seconds"] == 0.0


def test_engine_start_and_stop_say_why_while_an_orphaned_child_is_awaited(web_client):
    import time

    _create_device(web_client)
    engine = web_client.app.state.engine
    with web_client.app.state.session_factory() as session:
        from bacpack.models import Device

        session.get(Device, 599001).runtime.process_status = "starting"
        session.commit()
    engine._unjudged.add(599001)
    engine._grace_until = time.monotonic() + 60

    for path in ("/api/engine/start", "/api/engine/stop", "/api/engine/restart"):
        r = web_client.post(path, json={"device_id": 599001})
        assert r.status_code == 400, path
        assert "may still be starting after a server restart" in r.json()["detail"]
