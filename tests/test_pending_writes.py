"""PendingWritePoller -- applies the cross-process write queue (there is no
direct cross-process IPC for manual/feed writes). Tested against a real
in-process Application (no multiprocessing needed -- this class only ever
runs inside a single device's own child process, same testing shape as
ConnectorPoller/TrendSampler).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time

import pytest
from bacpypes3.primitivedata import ObjectIdentifier

from bacpack import pending_writes
from bacpack.object_builder import build_application
from bacpack.bacnet import write_manual_value
from bacpack.models import BacnetObject, Device
from bacpack.pending_writes import PendingWritePoller


def _build(session_factory, device_id=1):
    with session_factory() as session:
        device = session.get(Device, device_id)
        objects = list(device.objects)
    app = build_application(device, objects)
    return app


@pytest.mark.asyncio
async def test_applies_a_pending_feed_write_and_clears_it(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        session.commit()
        object_id = obj.id

    app = _build(session_factory)
    poller = PendingWritePoller(session_factory, app, 1)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.queue_feed_write(42.0)
        session.commit()

    await poller.apply_due()

    live = app.get_object_id(ObjectIdentifier(("analog-value", 1)))
    assert await live.read_property("presentValue") == 42.0

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_queued is False
        assert obj.runtime.pending_feed_value is None


@pytest.mark.asyncio
async def test_applies_a_pending_manual_relinquish(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        session.commit()
        object_id = obj.id

    app = _build(session_factory)
    live = app.get_object_id(ObjectIdentifier(("analog-value", 1)))
    await write_manual_value(live, 5.0)

    poller = PendingWritePoller(session_factory, app, 1)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.queue_manual_write(None)
        session.commit()

    await poller.apply_due()

    assert await live.read_property("presentValue") == 0.0  # class default, relinquished


@pytest.mark.asyncio
async def test_a_bad_pending_write_does_not_wedge_the_next_tick(session_factory):
    """Regression-shaped test, matching this project's established pattern
    (see ConnectorPoller/TrendSampler): one object's queued write failing to
    apply must not stop every other object's pending write, or permanently
    stop the poller.
    """
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        bad = BacnetObject(device=device, object_type="analog-input", instance_number=1, name="AI-1")
        good = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        session.add_all([bad, good])
        session.commit()
        bad_id, good_id = bad.id, good.id

    app = _build(session_factory)
    poller = PendingWritePoller(session_factory, app, 1)
    with session_factory() as session:
        bad_row = session.get(BacnetObject, bad_id)
        # analog-input has no priority array -- write_feed_value handles
        # this fine (see bacnet.py), but a bogus manual (priority 8) write to
        # a non-commandable object raises TypeError inside
        # write_manual_value, exercising the poller's own try/except.
        bad_row.queue_manual_write(1.0)
        good_row = session.get(BacnetObject, good_id)
        good_row.queue_feed_write(7.0)
        session.commit()

    await poller.apply_due()

    live_good = app.get_object_id(ObjectIdentifier(("analog-value", 2)))
    assert await live_good.read_property("presentValue") == 7.0


# --- Time sync -----------------------------------------------------------

from bacpack import device_clock
from bacpack.models import TrendSample


@pytest.mark.asyncio
async def test_applies_a_pending_clock_offset_and_clears_it(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(
                device=device, object_type="analog-value", instance_number=1, name="AV-1", trended=True
            )
        )
        session.commit()

    app = _build(session_factory)
    poller = PendingWritePoller(session_factory, app, 1)
    with session_factory() as session:
        session.get(Device, 1).runtime.pending_clock_offset = 7200.0
        session.commit()

    await poller.apply_due()

    assert device_clock.offset() == 7200.0
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.pending_clock_offset is None
        assert device.clock_offset_seconds == 7200.0
        assert session.query(TrendSample).filter_by(kind="time-change").count() == 1


@pytest.mark.asyncio
async def test_a_newer_pending_clock_offset_set_mid_apply_is_not_cleared(session_factory, monkeypatch):
    import bacpack.pending_writes as pw

    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    real_apply = pw.apply_clock_offset

    async def _apply_then_race(*args, **kwargs):
        result = await real_apply(*args, **kwargs)
        with session_factory() as session:  # the UI sets a newer value right now
            session.get(Device, 1).runtime.pending_clock_offset = 42.0
            session.commit()
        return result

    monkeypatch.setattr(pw, "apply_clock_offset", _apply_then_race)
    with session_factory() as session:
        session.get(Device, 1).runtime.pending_clock_offset = 10.0
        session.commit()

    app = _build(session_factory)
    await PendingWritePoller(session_factory, app, 1).apply_due()

    with session_factory() as session:
        assert session.get(Device, 1).runtime.pending_clock_offset == 42.0  # survives for the next tick


# --- Separate manual/feed slots --------------------------------------------


def _make_av(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        session.commit()
        return obj.id


@pytest.mark.asyncio
async def test_manual_and_feed_writes_queued_together_are_both_applied(session_factory):
    object_id = _make_av(session_factory)
    app = _build(session_factory)
    live = app.get_object_id(ObjectIdentifier(("analog-value", 1)))
    poller = PendingWritePoller(session_factory, app, 1)

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.queue_manual_write(5.0)
        obj.queue_feed_write(7.0)
        session.commit()
    await poller.apply_due()
    assert await live.read_property("presentValue") == 5.0  # priority 8 wins

    # Relinquishing the manual override falls back to the feed value, which
    # was only there if the feed write was applied too.
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_manual_write(None)
        session.commit()
    await poller.apply_due()
    assert await live.read_property("presentValue") == 7.0

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert not obj.runtime.pending_manual_queued and not obj.runtime.pending_feed_queued


@pytest.mark.asyncio
async def test_a_newer_write_queued_mid_apply_is_not_cleared(session_factory, monkeypatch):
    import bacpack.pending_writes as pw

    object_id = _make_av(session_factory)
    real_write = pw.write_feed_value

    async def _write_then_race(live_obj, value):
        await real_write(live_obj, value)
        with session_factory() as session:  # a connector queues a newer value right now
            session.get(BacnetObject, object_id).queue_feed_write(99.0)
            session.commit()

    monkeypatch.setattr(pw, "write_feed_value", _write_then_race)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_feed_write(1.0)
        session.commit()

    app = _build(session_factory)
    await PendingWritePoller(session_factory, app, 1).apply_due()

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert (obj.runtime.pending_feed_queued, obj.runtime.pending_feed_value) == (True, 99.0)


# --- An object the running device doesn't have -----------------------------
# One added (or renumbered) since the device started isn't live until a
# restart; its queued write used to be cleared without a word.


@pytest.mark.asyncio
async def test_a_write_to_an_object_added_since_start_is_logged_once(session_factory, caplog):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
    app = _build(session_factory)

    with session_factory() as session:
        obj = BacnetObject(device_id=1, object_type="analog-value", instance_number=9, name="AV-9")
        session.add(obj)
        session.commit()
        object_id = obj.id

    poller = PendingWritePoller(session_factory, app, 1)
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        for value in (1.0, 2.0):
            with session_factory() as session:
                session.get(BacnetObject, object_id).queue_feed_write(value)
                session.commit()
            await poller.apply_due()

    warnings = [r.getMessage() for r in caplog.records if r.name == "bacpack.pending_writes"]
    assert len(warnings) == 1, warnings
    assert "AV-9" in warnings[0] and "live apply failed" in warnings[0]
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_feed_queued is False


@pytest.mark.asyncio
async def test_a_refused_proxy_forward_logs_one_line_not_a_traceback(session_factory, caplog):
    # A dead remote behind a proxy is an expected outcome.
    from bacpypes3.errors import ExecutionError

    object_id = _make_av(session_factory)
    app = _build(session_factory)
    live = app.get_object_id(ObjectIdentifier(("analog-value", 1)))

    async def dead_remote(value, priority):
        raise ExecutionError(errorClass="communication", errorCode="timeout")

    live._proxy_forward = dead_remote
    poller = PendingWritePoller(session_factory, app, 1)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_manual_write(5.0)
        session.commit()

    with caplog.at_level(logging.WARNING, logger="bacpack"):
        await poller.apply_due()

    records = [r for r in caplog.records if r.name == "bacpack.pending_writes"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "communication: timeout" in records[0].getMessage()
    with session_factory() as session:
        assert not session.get(BacnetObject, object_id).runtime.pending_manual_queued


from bacpack.models import ObjectRuntime


@pytest.mark.asyncio
async def test_only_the_queued_object_is_applied_and_cleared(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.objects = [
            BacnetObject(object_type="analog-value", instance_number=n, name=f"AV-{n}") for n in (1, 2, 3)
        ]
        session.add(device)
        session.commit()
        ids = {o.instance_number: o.id for o in device.objects}

    app = _build(session_factory)
    live = {n: app.get_object_id(ObjectIdentifier(("analog-value", n))) for n in (1, 2, 3)}
    before = {n: await live[n].read_property("presentValue") for n in (1, 3)}
    with session_factory() as session:
        session.get(BacnetObject, ids[2]).queue_feed_write(42.0)
        session.commit()

    await PendingWritePoller(session_factory, app, 1).apply_due()

    assert await live[2].read_property("presentValue") == 42.0
    assert {n: await live[n].read_property("presentValue") for n in (1, 3)} == before
    with session_factory() as session:
        runtime = session.get(ObjectRuntime, ids[2])
        assert (runtime.pending_feed_value, runtime.pending_feed_queued) == (None, False)


# --- Off the device's loop ---------------------------------------------------


def _device_with_pending_clock(session_factory, offset=7200.0):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.commit()
        device.runtime.pending_clock_offset = offset
        session.commit()


@pytest.mark.asyncio
async def test_a_ui_clock_change_saves_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    _device_with_pending_clock(session_factory)
    app = _build(session_factory)

    run = await run_while_locked(session_factory, PendingWritePoller(session_factory, app, 1).apply_due)

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.pending_clock_offset is None
        assert device.clock_offset_seconds == 7200.0


@pytest.mark.asyncio
async def test_clearing_a_ui_clock_change_waits_off_the_loop(session_factory, monkeypatch):
    # The offset save finds the lock free once an earlier save has waited it
    # out, so this pins the clear alone: the stand-in applies instantly.
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    _device_with_pending_clock(session_factory)
    app = _build(session_factory)

    async def instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(pending_writes, "apply_clock_offset", instant)

    run = await run_while_locked(session_factory, PendingWritePoller(session_factory, app, 1).apply_due)

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.pending_clock_offset is None


@pytest.mark.asyncio
async def test_an_applied_write_clears_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    object_id = _make_av(session_factory)
    app = _build(session_factory)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_feed_write(42.0)
        session.commit()

    run = await run_while_locked(session_factory, PendingWritePoller(session_factory, app, 1).apply_due)

    assert_waited_off_the_loop(run)
    live = app.get_object_id(ObjectIdentifier(("analog-value", 1)))
    assert await live.read_property("presentValue") == 42.0
    with session_factory() as session:
        runtime = session.get(BacnetObject, object_id).runtime
        assert (runtime.pending_feed_queued, runtime.pending_feed_value) == (False, None)


def _commit_while_holding_the_lock(session_factory, statement, args=(), hold_seconds=0.4):
    """Holds main's write lock, then commits `statement` and releases it,
    the way a device or the API does while another process's save waits."""
    path = session_factory.kw["bind"].url.database
    locked = threading.Event()

    def hold() -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(hold_seconds)
        connection.execute(statement, args)
        connection.execute("COMMIT")
        connection.close()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    return holder, locked


@pytest.mark.asyncio
async def test_a_newer_write_committed_while_the_clear_waits_stays(session_factory):
    object_id = _make_av(session_factory)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_feed_write(42.0)
        session.commit()
    holder, locked = _commit_while_holding_the_lock(
        session_factory,
        "UPDATE object_runtime SET pending_feed_value = '99.0', pending_feed_queued = 1 WHERE object_id = ?",
        (object_id,),
    )
    await asyncio.to_thread(locked.wait)

    await PendingWritePoller(session_factory, None, 1)._clear(object_id, "feed", 42.0)
    await asyncio.to_thread(holder.join)

    with session_factory() as session:
        runtime = session.get(BacnetObject, object_id).runtime
        assert (runtime.pending_feed_queued, runtime.pending_feed_value) == (True, 99.0)


@pytest.mark.asyncio
async def test_a_relinquish_is_cleared_by_its_own_value(session_factory):
    object_id = _make_av(session_factory)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_manual_write(None)
        session.commit()

    await PendingWritePoller(session_factory, None, 1)._clear(object_id, "manual", None)

    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_manual_queued is False


@pytest.mark.asyncio
async def test_a_newer_clock_offset_committed_while_the_clear_waits_stays(session_factory, monkeypatch):
    _device_with_pending_clock(session_factory, 10.0)

    async def instant(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(pending_writes, "apply_clock_offset", instant)
    holder, locked = _commit_while_holding_the_lock(
        session_factory, "UPDATE device_runtime SET pending_clock_offset = 42.0 WHERE device_id = 1"
    )
    await asyncio.to_thread(locked.wait)

    await PendingWritePoller(session_factory, _build(session_factory), 1)._apply_clock(10.0)
    await asyncio.to_thread(holder.join)

    with session_factory() as session:
        assert session.get(Device, 1).runtime.pending_clock_offset == 42.0
