"""DeviceWatch: one read of the device's runtime row per tick decides
stop/exit, applies queued writes and marks the runners changed."""
from __future__ import annotations

import asyncio
import logging
import os

import pytest
from sqlalchemy import delete

from bacpack.device_watch import DeviceWatch
from bacpack.models import BacnetObject, Device, DeviceRuntime


class _FakePending:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def apply_due(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")


class _FakeRunner:
    def __init__(self):
        self.marks = 0

    def mark_changed(self):
        self.marks += 1


def _device(session_factory, status="running", pid=None):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
        runtime = session.get(DeviceRuntime, 1)
        runtime.process_status = status
        runtime.process_pid = os.getpid() if pid is None else pid
        session.commit()


def _set_runtime(session_factory, **fields):
    with session_factory() as session:
        runtime = session.get(DeviceRuntime, 1)
        for key, value in fields.items():
            setattr(runtime, key, value)
        session.commit()


def _watch(session_factory, pending=None, runner=None):
    return DeviceWatch(
        session_factory,
        1,
        pending_writes=pending or _FakePending(),
        runners=[runner or _FakeRunner()],
        tick_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_the_first_tick_applies_writes_and_marks_runners(session_factory):
    _device(session_factory)
    pending, runner = _FakePending(), _FakeRunner()
    assert await _watch(session_factory, pending, runner).step() is None
    assert (pending.calls, runner.marks) == (1, 1)


@pytest.mark.asyncio
async def test_an_unchanged_counter_does_nothing(session_factory):
    _device(session_factory)
    pending, runner = _FakePending(), _FakeRunner()
    watch = _watch(session_factory, pending, runner)
    await watch.step()
    await watch.step()
    assert (pending.calls, runner.marks) == (1, 1)


@pytest.mark.asyncio
async def test_a_config_change_applies_writes_and_marks_runners(session_factory):
    _device(session_factory)
    pending, runner = _FakePending(), _FakeRunner()
    watch = _watch(session_factory, pending, runner)
    await watch.step()
    with session_factory() as session:
        session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()
    await watch.step()
    assert (pending.calls, runner.marks) == (2, 2)


@pytest.mark.asyncio
async def test_a_pending_clock_change_applies_without_marking(session_factory):
    _device(session_factory)
    pending, runner = _FakePending(), _FakeRunner()
    watch = _watch(session_factory, pending, runner)
    await watch.step()
    _set_runtime(session_factory, pending_clock_offset=5.0)
    await watch.step()
    assert (pending.calls, runner.marks) == (2, 1)


@pytest.mark.asyncio
async def test_a_failed_apply_is_retried_next_tick(session_factory, caplog):
    _device(session_factory)
    pending = _FakePending(fail=True)
    watch = _watch(session_factory, pending)
    with caplog.at_level(logging.ERROR, logger="bacpack"):
        assert await watch.step() is None
        assert await watch.step() is None
    assert pending.calls == 2
    assert "pending-write" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status, clean", [("stopping", True), ("reinitializing", False)])
async def test_a_stop_request_ends_the_watch(session_factory, status, clean):
    _device(session_factory, status=status)
    pending = _FakePending()
    assert await _watch(session_factory, pending).step() is clean
    assert pending.calls == 0


@pytest.mark.asyncio
async def test_a_deleted_device_is_a_clean_stop(session_factory):
    _device(session_factory)
    with session_factory() as session:
        session.delete(session.get(Device, 1))
        session.commit()
    assert await _watch(session_factory).step() is True


@pytest.mark.asyncio
async def test_a_row_naming_another_process_exits_unclean(session_factory, caplog):
    _device(session_factory, pid=os.getpid() + 1)
    pending = _FakePending()
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        assert await _watch(session_factory, pending).step() is False
    assert "no longer names this process" in caplog.text
    assert pending.calls == 0


@pytest.mark.asyncio
async def test_a_missing_runtime_row_keeps_serving_and_marks_nothing(session_factory):
    _device(session_factory)
    with session_factory() as session:
        session.execute(delete(DeviceRuntime).where(DeviceRuntime.device_id == 1))
        session.commit()
    pending, runner = _FakePending(), _FakeRunner()
    assert await _watch(session_factory, pending, runner).step() is None
    assert (pending.calls, runner.marks) == (0, 0)


@pytest.mark.asyncio
async def test_a_failed_read_keeps_serving_and_marks_nothing(session_factory, monkeypatch, caplog):
    _device(session_factory)
    pending, runner = _FakePending(), _FakeRunner()
    watch = _watch(session_factory, pending, runner)

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(watch, "read", broken)
    with caplog.at_level(logging.ERROR, logger="bacpack"):
        assert await watch.step() is None
    assert (pending.calls, runner.marks) == (0, 0)
    assert "stop-status check failed" in caplog.text


@pytest.mark.asyncio
async def test_run_returns_the_stop_outcome(session_factory):
    _device(session_factory)
    watch = _watch(session_factory)
    task = asyncio.ensure_future(watch.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    _set_runtime(session_factory, process_status="stopping")
    assert await asyncio.wait_for(task, timeout=1.0) is True


# --- A slow apply_due doesn't hold up a stop --------------------------------


class _SlowPending:
    """apply_due blocks until released; records cancellation."""

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()
        self.cancelled = False

    async def apply_due(self):
        self.calls += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_a_slow_apply_does_not_hold_up_a_stop(session_factory):
    _device(session_factory)
    pending = _SlowPending()
    watch = _watch(session_factory, pending)
    assert await asyncio.wait_for(watch.step(), timeout=1.0) is None  # returns after one tick
    _set_runtime(session_factory, process_status="stopping")
    assert await asyncio.wait_for(watch.step(), timeout=1.0) is True
    assert pending.cancelled


@pytest.mark.asyncio
async def test_one_apply_at_a_time_and_runners_marked_meanwhile(session_factory):
    _device(session_factory)
    pending, runner = _SlowPending(), _FakeRunner()
    watch = _watch(session_factory, pending, runner)
    await asyncio.wait_for(watch.step(), timeout=1.0)
    with session_factory() as session:
        session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()
    await asyncio.wait_for(watch.step(), timeout=1.0)
    assert (pending.calls, runner.marks) == (1, 2)  # still the first apply; the change marked at once
    pending.release.set()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(watch.step(), timeout=1.0)  # collects the first, starts one for the change seen meanwhile
    assert pending.calls == 2


@pytest.mark.asyncio
async def test_run_cancels_an_apply_in_flight_when_it_stops(session_factory):
    _device(session_factory)
    pending = _SlowPending()
    watch = _watch(session_factory, pending)
    task = asyncio.ensure_future(watch.run())
    await asyncio.sleep(0.05)
    _set_runtime(session_factory, process_status="stopping")
    assert await asyncio.wait_for(task, timeout=1.0) is True
    assert pending.cancelled


class _FailsWhenReleased:
    def __init__(self):
        self.release = asyncio.Event()

    async def apply_due(self):
        await self.release.wait()
        raise RuntimeError("late boom")


@pytest.mark.asyncio
async def test_an_apply_that_failed_just_before_a_stop_is_logged(session_factory, caplog):
    _device(session_factory)
    pending = _FailsWhenReleased()
    watch = _watch(session_factory, pending)
    await asyncio.wait_for(watch.step(), timeout=1.0)  # starts the apply, which waits
    pending.release.set()
    await asyncio.sleep(0.01)  # it fails; nothing has collected it yet
    _set_runtime(session_factory, process_status="stopping")
    with caplog.at_level(logging.ERROR, logger="bacpack"):
        assert await watch.step() is True
    # Logged by DeviceWatch itself -- not asyncio's "Task exception was never
    # retrieved" when the uncollected task is garbage-collected.
    assert any(
        record.name.startswith("bacpack") and record.exc_info and "late boom" in str(record.exc_info[1])
        for record in caplog.records
    )


class _FakeReloader:
    def __init__(self, order):
        self.order = order
        self.seqs = []

    async def apply(self, change_seq):
        self.seqs.append(change_seq)
        self.order.append("reloader")
        return True


@pytest.mark.asyncio
async def test_reloader_applies_before_runners_and_pending_writes(session_factory):
    order = []

    class _Runner:
        def mark_changed(self):
            order.append("runner")

    class _Pending:
        async def apply_due(self):
            order.append("pending")

    _device(session_factory)
    reloader = _FakeReloader(order)
    watch = DeviceWatch(
        session_factory, 1, pending_writes=_Pending(), runners=[_Runner()], reloader=reloader,
        tick_seconds=0.01, pid=os.getpid(),
    )
    await watch.step()
    await asyncio.sleep(0)  # apply_due runs as a task beside the stop check
    assert order[:3] == ["reloader", "runner", "pending"]
    seq = reloader.seqs[-1]
    await watch.step()
    assert reloader.seqs == [seq]  # not again while change_seq stays put


@pytest.mark.asyncio
async def test_a_queued_write_applies_without_reloading_or_marking(session_factory):
    _device(session_factory)
    reloader, pending, runner = _FakeReloader([]), _FakePending(), _FakeRunner()
    watch = DeviceWatch(
        session_factory, 1, pending_writes=pending, runners=[runner], reloader=reloader,
        tick_seconds=0.01, pid=os.getpid(),
    )
    with session_factory() as session:
        session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()
    await watch.step()
    seen = (len(reloader.seqs), runner.marks)
    with session_factory() as session:
        session.query(BacnetObject).one().queue_feed_write(5.0)
        session.commit()
    await watch.step()
    assert (len(reloader.seqs), runner.marks) == seen
    assert pending.calls == 2


@pytest.mark.asyncio
async def test_an_apply_that_was_not_handled_is_tried_again_next_tick(session_factory):
    class _Flaky(_FakeReloader):
        async def apply(self, change_seq):
            await super().apply(change_seq)
            return len(self.seqs) > 1

    _device(session_factory)
    reloader, runner = _Flaky([]), _FakeRunner()
    watch = DeviceWatch(
        session_factory, 1, pending_writes=_FakePending(), runners=[runner], reloader=reloader,
        tick_seconds=0.01, pid=os.getpid(),
    )
    await watch.step()
    await watch.step()
    await watch.step()
    assert len(reloader.seqs) == 2  # retried once, then handled


@pytest.mark.asyncio
async def test_a_held_config_change_waits_until_the_hold_ends(session_factory):
    # A restore commits before the device restarts into it: reloading it live
    # first would swap every object for nothing.
    _device(session_factory)
    held = [True]
    reloader, runner = _FakeReloader([]), _FakeRunner()
    watch = DeviceWatch(
        session_factory, 1, pending_writes=_FakePending(), runners=[runner], reloader=reloader,
        tick_seconds=0.01, pid=os.getpid(), reloads_held=lambda: held[0],
    )
    await watch.step()
    assert (reloader.seqs, runner.marks) == ([], 0)
    held[0] = False
    await watch.step()
    assert (len(reloader.seqs), runner.marks) == (1, 1)
