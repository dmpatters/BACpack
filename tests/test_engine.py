"""EngineManager -- orchestrates one child OS process per running device. Uses
tests/fakes.py's FakeProcess throughout: real socket binds and subprocess
spawning aren't practical to fully exercise here --
tests/test_device_process.py and tests/test_device_process_subprocess.py cover
the real device-serving logic and the real OS-process boundary respectively.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import gc
import os
import sys
import threading
import time

import pytest

from sqlalchemy import event
from sqlalchemy.orm import Session

from bacpack import engine as engine_module
from bacpack import timeutil
from bacpack.device_process import _bind_error_message
from bacpack.engine import EngineError, EngineManager, _save_off_loop
from bacpack.models import Device, DeviceRuntime, get_or_create_settings
from tests.fakes import FakeProcess
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

_ENGINE_FILE = os.path.normcase(engine_module.__file__)


@pytest.fixture(autouse=True)
def engine_commits_off_the_loop():
    """Fails a test in which EngineManager commits on the event loop's
    thread: a commit can wait out another process's write lock, freezing
    the web loop. A lock-holding timing test pins only the first save
    of a call; this sees every one the file's tests reach."""
    on_loop: list = []

    def before_commit(session) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        frame = sys._getframe(1)
        while frame is not None:
            if os.path.normcase(frame.f_code.co_filename) == _ENGINE_FILE:
                on_loop.append(frame.f_code.co_name)
                return
            frame = frame.f_back

    event.listen(Session, "before_commit", before_commit)
    try:
        yield on_loop
    finally:
        event.remove(Session, "before_commit", before_commit)
    if on_loop:
        pytest.fail(f"EngineManager committed on the event loop in {sorted(set(on_loop))}")


def _factory(processes: dict):
    def factory(db_path: str, device_id: int) -> FakeProcess:
        process = FakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    return factory


async def _mark_running(session_factory, device_id: int) -> None:
    with session_factory() as session:
        device = session.get(Device, device_id)
        device.runtime.process_status = "running"
        device.runtime.process_pid = 100000 + device_id
        session.commit()


@pytest.mark.asyncio
async def test_start_requires_bind_address(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    with pytest.raises(EngineError):
        await engine.start(1)


@pytest.mark.asyncio
async def test_start_requires_existing_device(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    with pytest.raises(EngineError):
        await engine.start(999)


@pytest.mark.asyncio
async def test_start_succeeds_once_the_child_reports_running(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_running_shortly_after_spawn():
        # A real child's own _mark_running would do this from inside its own
        # process, after building successfully -- simulate that here since
        # FakeProcess does no real work at all.
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_to_running_shortly_after_spawn())

    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    assert engine.is_running(1)
    status = engine.status()
    assert len(status) == 1
    device_time = status[0].pop("device_time")
    assert dt.datetime.fromisoformat(device_time).tzinfo is not None
    assert status == [
        {
            "device_id": 1,
            "process_status": "running",
            "process_pid": 100001,
            "process_started_at": None,
            "process_error": None,
            "port_in_use": False,
            "apply_error": None,
            "restart_pending": False,
            "bind_address": None,
            "dcc_state": "enable",
            "database_revision": 1,
            "backup_failure_timeout": 60,
            "last_restore_time": None,
            "clock_offset_seconds": 0.0,
            "clock_change_pending": False,
        }
    ]


@pytest.mark.asyncio
async def test_start_raises_a_clean_engine_error_when_the_child_reports_crashed(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_crashed_shortly_after_spawn():
        while 1 not in processes:
            await asyncio.sleep(0)
        with session_factory() as session:
            device = session.get(Device, 1)
            device.runtime.process_status = "crashed"
            device.runtime.process_error = "bad object config: Bad-AO"
            session.commit()

    asyncio.ensure_future(flip_to_crashed_shortly_after_spawn())

    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    with pytest.raises(EngineError, match="Bad-AO"):
        await engine.start(1)
    assert not engine.is_running(1)


@pytest.mark.asyncio
async def test_start_raises_a_clean_engine_error_on_a_startup_timeout(session_factory, monkeypatch):
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "STARTUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(engine_module, "STARTUP_POLL_INTERVAL_SECONDS", 0.01)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    with pytest.raises(EngineError, match="did not report ready"):
        await engine.start(1)

    # The row must be left self-describing, the same way a real crash is
    # recorded -- not silently left exactly as the pre-spawn reset left it
    # (see the "crashed"-on-timeout fix in engine.py's startup-timeout path).
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error is not None
        assert "did not report ready" in device.runtime.process_error


@pytest.mark.asyncio
async def test_start_timeout_escalates_to_kill_if_terminate_does_not_take(session_factory, monkeypatch):
    """The startup-timeout branch used to call a bare process.terminate() with no
    join()/kill()-escalation, unlike _stop_locked's own escalation. An
    unresponsive child on this path would keep its bind socket while
    untracked (popped from self.processes), so a later start() would sail
    past the bind-collision check and die with a raw OS bind error inside a
    fresh child -- same failure class as the already-fixed _stop_locked gap.
    Mirrors test_stop_escalates_to_kill_if_terminate_does_not_take's shape.
    """
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "STARTUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(engine_module, "STARTUP_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(engine_module, "STOP_GRACE_SECONDS", 0.05)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    class StubbornFakeProcess(FakeProcess):
        """terminate() (SIGTERM) does nothing -- only kill() actually stops
        it, forcing _terminate_and_confirm's escalation path to fire."""

        def terminate(self) -> None:
            pass

    processes: dict = {}

    def factory(db_path: str, device_id: int) -> StubbornFakeProcess:
        process = StubbornFakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    # Never flips to "running" or "crashed", and is always alive (real
    # is_alive()) -- forces the startup-timeout branch to fire.
    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")
    with pytest.raises(EngineError, match="did not report ready"):
        await engine.start(1)

    assert processes[1].alive is False  # only kill() could have done this
    assert not engine.is_running(1)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert "did not report ready" in device.runtime.process_error


@pytest.mark.asyncio
async def test_start_rejects_a_bind_address_collision_with_a_running_device(session_factory):
    with session_factory() as session:
        d1 = Device(id=1, name="D1")
        d2 = Device(id=2, name="D2")
        session.add_all([d1, d2])
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"  # both devices fall back to this
        session.commit()

    processes: dict = {}

    async def flip_running(device_id: int):
        while device_id not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, device_id)

    asyncio.ensure_future(flip_running(1))
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    with pytest.raises(EngineError, match="already in use"):
        await engine.start(2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first, second",
    [
        ("127.0.0.1/8", "127.0.0.1/8:47808"),
        ("127.0.0.1/8", "127.0.0.1"),
        ("127.0.0.1:47809", "127.0.0.1/24:47809"),
    ],
)
async def test_start_rejects_the_same_socket_written_another_way(session_factory, first, second):
    with session_factory() as session:
        session.add_all([Device(id=1, name="D1", bind_address=first), Device(id=2, name="D2", bind_address=second)])
        session.commit()

    processes: dict = {}

    async def flip_running():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_running())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    with pytest.raises(EngineError, match="already in use"):
        await engine.start(2)
    assert 2 not in processes


@pytest.mark.asyncio
async def test_start_allows_the_same_host_on_another_port(session_factory):
    with session_factory() as session:
        session.add_all(
            [Device(id=1, name="D1", bind_address="127.0.0.1/8"), Device(id=2, name="D2", bind_address="127.0.0.1/8:47809")]
        )
        session.commit()

    processes: dict = {}

    async def flip_running(device_id):
        while device_id not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, device_id)

    asyncio.ensure_future(flip_running(1))
    asyncio.ensure_future(flip_running(2))
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    await engine.start(2)

    assert engine.is_running(2)


@pytest.mark.asyncio
async def test_start_allows_two_devices_with_different_bind_addresses(session_factory):
    with session_factory() as session:
        d1 = Device(id=1, name="D1", bind_address="127.0.0.1/8")
        d2 = Device(id=2, name="D2", bind_address="127.0.0.2/8")
        session.add_all([d1, d2])
        session.commit()

    processes: dict = {}

    async def flip_running(device_id: int):
        while device_id not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, device_id)

    asyncio.ensure_future(flip_running(1))
    asyncio.ensure_future(flip_running(2))
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    await engine.start(2)

    assert engine.is_running(1)
    assert engine.is_running(2)


@pytest.mark.asyncio
async def test_stop_requests_a_graceful_shutdown_and_waits_for_it(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_running_then_honor_stop():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        # Simulate the real child's own stop-status watch loop.
        while True:
            await asyncio.sleep(0.01)
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.process_status == "stopping":
                    device.runtime.process_status = "stopped"
                    device.runtime.process_pid = None
                    session.commit()
                    processes[1].alive = False
                    return

    asyncio.ensure_future(flip_to_running_then_honor_stop())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    assert engine.is_running(1)

    await engine.stop(1)
    assert not engine.is_running(1)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "stopped"
        assert device.runtime.process_pid is None


@pytest.mark.asyncio
async def test_stop_folds_a_left_over_pending_clock_offset_into_the_saved_clock(session_factory):
    """A PUT .../clock arriving while the device is already 'stopping' sets
    pending_clock_offset with no child left to ever apply it -- _mark_stopped
    must fold it into clock_offset_seconds itself, or the UI shows
    "applying..." forever until the device is next started."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_running_then_honor_stop():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        while True:
            await asyncio.sleep(0.01)
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.process_status == "stopping":
                    # Simulate a PUT .../clock landing after the child was
                    # told to stop but before this engine's own
                    # _mark_stopped runs -- no child left to ever apply it.
                    device.runtime.pending_clock_offset = 1800.0
                    device.runtime.process_status = "stopped"
                    device.runtime.process_pid = None
                    session.commit()
                    processes[1].alive = False
                    return

    asyncio.ensure_future(flip_to_running_then_honor_stop())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    await engine.stop(1)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "stopped"
        assert device.clock_offset_seconds == 1800.0
        assert device.runtime.pending_clock_offset is None


@pytest.mark.asyncio
async def test_stop_falls_back_to_terminate_if_the_child_never_confirms(session_factory, monkeypatch):
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_module, "STOP_POLL_INTERVAL_SECONDS", 0.01)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_running_and_never_honor_stop():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        # deliberately never reacts to "stopping" -- exercises the
        # terminate() hard-fallback path.

    asyncio.ensure_future(flip_running_and_never_honor_stop())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    await engine.stop(1)
    assert processes[1].alive is False  # terminate() was called
    assert not engine.is_running(1)


@pytest.mark.asyncio
async def test_stop_escalates_to_kill_if_terminate_does_not_take(session_factory, monkeypatch, caplog):
    """A real multiprocessing.Process can survive SIGTERM (terminate()).
    _stop_locked must re-check is_alive() after the terminate()+join()
    fallback and escalate to kill() rather than silently recording
    "stopped" for a process that's still actually alive and still holding
    its bind socket (see the kill()-escalation fix in engine.py)."""
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_module, "STOP_POLL_INTERVAL_SECONDS", 0.01)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    class StubbornFakeProcess(FakeProcess):
        """terminate() (SIGTERM) does nothing -- only kill() actually stops
        it, forcing _stop_locked's escalation path to fire."""

        def terminate(self) -> None:
            pass

    processes: dict = {}

    def factory(db_path: str, device_id: int) -> StubbornFakeProcess:
        process = StubbornFakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    async def flip_running_and_never_honor_stop():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        # deliberately never reacts to "stopping", and terminate() itself
        # (StubbornFakeProcess above) does not clear .alive either --
        # only kill() does.

    asyncio.ensure_future(flip_running_and_never_honor_stop())
    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")
    await engine.start(1)

    await engine.stop(1)
    assert processes[1].alive is False  # only kill() could have done this
    assert not engine.is_running(1)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "stopped"

    matching = [r for r in caplog.records if "did not exit after terminate()" in r.message]
    assert matching and matching[0].device_id == 1


@pytest.mark.asyncio
async def test_stop_of_a_device_that_is_not_running_is_a_no_op(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.stop(1)  # must not raise


@pytest.mark.asyncio
async def test_restart_stops_then_starts_the_same_device(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def honor_lifecycle():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        while True:
            await asyncio.sleep(0.01)
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.process_status == "stopping":
                    device.runtime.process_status = "stopped"
                    device.runtime.process_pid = None
                    session.commit()
                    processes[1].alive = False
                    return

    asyncio.ensure_future(honor_lifecycle())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    old_process = processes[1]

    async def honor_lifecycle_after_restart():
        # Waits for restart()'s own stop()-then-start() to actually replace
        # the tracked process object, not just for "some" process to be
        # present -- the old one is still there until stop() pops it, so
        # gating on mere presence (like honor_lifecycle() above) would race
        # ahead of restart()'s stop() and clobber its "stopping" write with
        # a premature "running" before stop() ever observes it.
        while processes.get(1) is None or processes.get(1) is old_process:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        while True:
            await asyncio.sleep(0.01)
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.process_status == "stopping":
                    device.runtime.process_status = "stopped"
                    device.runtime.process_pid = None
                    session.commit()
                    processes[1].alive = False
                    return

    asyncio.ensure_future(honor_lifecycle_after_restart())
    await engine.restart(1)
    assert engine.is_running(1)


@pytest.mark.asyncio
async def test_status_reports_every_device_from_the_database(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D1"))
        session.add(Device(id=2, name="D2", bind_address="127.0.0.2/8"))
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    status = engine.status()
    assert [row["device_id"] for row in status] == [1, 2]
    assert status[0]["process_status"] == "stopped"
    assert status[1]["bind_address"] == "127.0.0.2/8"


@pytest.mark.asyncio
async def test_start_on_an_already_running_device_is_a_no_op(session_factory):
    """A second Start (a double click queued behind the first) must not
    restart the device: Restart exists for that."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_running():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_running())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    old_process = processes[1]

    await asyncio.wait_for(engine.start(1), timeout=1.0)

    assert processes[1] is old_process
    assert old_process.alive
    assert engine.processes[1] is old_process
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"


@pytest.mark.asyncio
async def test_start_on_a_tracked_device_whose_process_died_starts_a_fresh_one(session_factory):
    """The health check hasn't noticed the death yet; start() must not take
    the dead handle for a running device."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_running():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_running())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    old_process = processes[1]
    old_process.alive = False

    async def flip_running_after_second_start():
        while processes.get(1) is old_process:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_running_after_second_start())
    await engine.start(1)

    assert engine.is_running(1)
    assert processes[1] is not old_process
    assert engine.processes[1] is processes[1]


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_start_and_stop_of_the_same_device(session_factory):
    """Proves the per-device lock actually serializes overlapping calls
    (rather than merely existing, unused) by instrumenting the locked
    methods to record how many are ever in flight at once for the same
    device, then firing a concurrent stop()/start() pair at it via
    asyncio.gather."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def honor_lifecycle():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        while True:
            await asyncio.sleep(0.01)
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.process_status == "stopping":
                    device.runtime.process_status = "stopped"
                    device.runtime.process_pid = None
                    session.commit()
                    processes[1].alive = False
                    return

    asyncio.ensure_future(honor_lifecycle())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)
    old_process = processes[1]

    in_flight = 0
    max_in_flight = 0
    original_stop_locked = engine._stop_locked
    original_start_locked = engine._start_locked

    async def tracked_stop_locked(device_id, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await original_stop_locked(device_id)
        finally:
            in_flight -= 1

    async def tracked_start_locked(device_id):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await original_start_locked(device_id)
        finally:
            in_flight -= 1

    engine._stop_locked = tracked_stop_locked
    engine._start_locked = tracked_start_locked

    async def honor_lifecycle_after_restart():
        while processes.get(1) is None or processes.get(1) is old_process:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(honor_lifecycle_after_restart())

    # engine.stop(1) and engine.start(1) both contend for the SAME
    # per-device lock -- if it genuinely serializes them, tracked_*_locked
    # must never observe more than one in flight at once, no matter how the
    # event loop interleaves the two outer coroutines.
    await asyncio.gather(engine.stop(1), engine.start(1))

    assert max_in_flight == 1
    assert engine.is_running(1)


import os

import psutil

from bacpack.engine import _is_same_process


def test_is_same_process_true_for_the_current_process():
    import datetime as dt

    proc = psutil.Process(os.getpid())
    started_at = dt.datetime.fromtimestamp(proc.create_time(), tz=dt.timezone.utc)
    assert _is_same_process(os.getpid(), started_at) is True


def test_is_same_process_false_for_a_mismatched_start_time():
    import datetime as dt

    far_off = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    assert _is_same_process(os.getpid(), far_off) is False


def test_is_same_process_false_for_a_pid_that_does_not_exist():
    import datetime as dt

    # A pid essentially guaranteed not to exist.
    assert _is_same_process(2**30 - 1, dt.datetime.now(dt.timezone.utc)) is False


# --- A Linux clock step moves psutil's create_time -------------------------


def _fake_linux_identity(monkeypatch, boot_id="boot-A", since_boot=1234.5):
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "read_boot_id", lambda: boot_id)
    monkeypatch.setattr(engine_module, "started_since_boot", lambda pid: since_boot)


def _an_hour_off():
    import datetime as dt

    started = dt.datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), tz=dt.timezone.utc)
    return started + dt.timedelta(hours=1)


def test_is_same_process_true_after_a_clock_step_when_boot_id_and_since_boot_match(monkeypatch):
    _fake_linux_identity(monkeypatch)
    assert _is_same_process(os.getpid(), _an_hour_off(), "boot-A", 1234.5) is True


def test_is_same_process_false_for_a_different_boot_id(monkeypatch):
    _fake_linux_identity(monkeypatch)
    assert _is_same_process(os.getpid(), _an_hour_off(), "boot-B", 1234.5) is False


def test_is_same_process_false_when_the_since_boot_time_differs(monkeypatch):
    _fake_linux_identity(monkeypatch)
    assert _is_same_process(os.getpid(), _an_hour_off(), "boot-A", 1300.0) is False


def test_is_same_process_null_boot_columns_use_the_absolute_check_only(monkeypatch):
    _fake_linux_identity(monkeypatch)
    assert _is_same_process(os.getpid(), _an_hour_off(), None, None) is False


def test_is_same_process_still_false_for_a_dead_pid_with_matching_boot_columns(monkeypatch):
    _fake_linux_identity(monkeypatch)
    assert _is_same_process(2**30 - 1, _an_hour_off(), "boot-A", 1234.5) is False


@pytest.mark.asyncio
async def test_reattach_survives_a_clock_step(session_factory, monkeypatch):
    _fake_linux_identity(monkeypatch)
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.runtime.process_status = "running"
        device.runtime.process_pid = os.getpid()
        device.runtime.process_started_at = _an_hour_off()
        device.runtime.process_boot_id = "boot-A"
        device.runtime.process_started_since_boot = 1234.5
        session.add(device)
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert engine.is_running(1)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"


@pytest.mark.asyncio
async def test_health_check_marks_a_dead_process_crashed(session_factory, monkeypatch):
    import bacpack.engine as engine_module

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_running_then_die():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        await asyncio.sleep(0.02)
        processes[1].alive = False  # simulates an unexpected crash, no self-report

    asyncio.ensure_future(flip_to_running_then_die())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.1)
    health_check.cancel()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error == "process exited unexpectedly"
    assert not engine.is_running(1)


@pytest.mark.asyncio
async def test_health_check_folds_a_left_over_pending_clock_offset_when_marking_crashed(session_factory):
    """The crash counterpart of the stop test
    above: a pending clock change with no child left alive to ever apply it
    must be folded into the saved clock when the health check marks the
    device crashed, not left stuck pending forever."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def flip_to_running_set_pending_then_die():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        with session_factory() as session:
            session.get(Device, 1).runtime.pending_clock_offset = -600.0
            session.commit()
        await asyncio.sleep(0.02)
        processes[1].alive = False  # simulates an unexpected crash, no self-report

    asyncio.ensure_future(flip_to_running_set_pending_then_die())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.start(1)

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.1)
    health_check.cancel()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.clock_offset_seconds == -600.0
        assert device.runtime.pending_clock_offset is None


@pytest.mark.asyncio
async def test_health_check_does_not_touch_a_device_this_instance_is_not_tracking(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.05)
    health_check.cancel()

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "stopped"  # untouched


@pytest.mark.asyncio
async def test_reattach_resumes_tracking_a_still_running_device(session_factory, caplog):
    import logging
    caplog.set_level(logging.INFO)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.commit()

    # Simulate a device this test itself is standing in for -- a process
    # that's genuinely alive and whose create_time really does match what's
    # recorded, using this test process's own pid.
    import datetime as dt

    real_pid = os.getpid()
    real_started_at = dt.datetime.fromtimestamp(
        psutil.Process(real_pid).create_time(), tz=dt.timezone.utc
    )
    with session_factory() as session:
        device = session.get(Device, 1)
        device.runtime.process_status = "running"
        device.runtime.process_pid = real_pid
        device.runtime.process_started_at = real_started_at
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert engine.is_running(1)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"  # untouched, not re-marked

    matching = [r for r in caplog.records if "reattached to already-running device" in r.message]
    assert matching and matching[0].device_id == 1


@pytest.mark.asyncio
async def test_reattach_remembers_a_running_row_whose_process_is_gone(session_factory, caplog):
    import datetime as dt

    caplog.set_level("INFO", logger="bacpack.engine")

    with session_factory() as session:
        device = Device(id=1, name="D")
        device.runtime.process_status = "running"
        device.runtime.process_pid = 2**30 - 1  # essentially guaranteed not to exist
        device.runtime.process_started_at = dt.datetime.now(dt.timezone.utc)
        session.add(device)
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert not engine.is_running(1)
    with session_factory() as session:
        runtime = session.get(Device, 1).runtime
        assert runtime.process_status == "stopped"
        assert runtime.resume_on_start is True
        assert runtime.process_pid is None

    matching = [r for r in caplog.records if "will be restarted" in r.message]
    assert matching and matching[0].device_id == 1


@pytest.mark.asyncio
async def test_reattach_leaves_a_stopped_device_alone(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))  # process_status defaults to "stopped"
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert not engine.is_running(1)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "stopped"


@pytest.mark.asyncio
async def test_reattach_respawns_a_device_orphaned_mid_reinitialize(session_factory):
    """reattach_running_devices only ever queried process_status="running"
    rows. A device stuck at "reinitializing" because the PARENT itself died
    between popping self.processes and finishing the respawn (see
    run_health_check_task's own comment on this exact race) was invisible to
    this query too -- so even a full app restart could never self-heal it; it
    just sat at "reinitializing" forever. Here the old process is genuinely
    gone (no matching pid), so this must trigger a fresh spawn, not merely mark
    it crashed -- it didn't crash, its parent did."""
    import datetime as dt

    with session_factory() as session:
        device = Device(id=1, name="D")
        device.runtime.process_status = "reinitializing"
        device.runtime.process_pid = 2**30 - 1  # essentially guaranteed not to exist
        device.runtime.process_started_at = dt.datetime.now(dt.timezone.utc)
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def simulate_fresh_spawn_reports_running():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(simulate_fresh_spawn_reports_running())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert engine.is_running(1)
    assert 1 in processes
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running"
        assert device.runtime.process_error is None  # not the crashed path


@pytest.mark.asyncio
async def test_reattach_resumes_tracking_a_still_alive_reinitializing_device(session_factory, caplog):
    """The other half of the same gap: if the OLD process is still alive
    (the parent died before the child even finished its own clean exit),
    reattach it exactly like a "running" row -- the existing health-check
    loop's own reinitializing-respawn branch (see the test above this
    class of test in the "running" case) already handles noticing its
    eventual exit correctly; this must not race ahead and spawn a second
    process while the first one is still up."""
    import logging

    caplog.set_level(logging.INFO)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.commit()

    import datetime as dt

    real_pid = os.getpid()
    real_started_at = dt.datetime.fromtimestamp(
        psutil.Process(real_pid).create_time(), tz=dt.timezone.utc
    )
    with session_factory() as session:
        device = session.get(Device, 1)
        device.runtime.process_status = "reinitializing"
        device.runtime.process_pid = real_pid
        device.runtime.process_started_at = real_started_at
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert engine.is_running(1)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "reinitializing"  # untouched

    matching = [r for r in caplog.records if "reattached to already-running device" in r.message]
    assert matching and matching[0].device_id == 1


@pytest.mark.asyncio
async def test_health_check_restarts_a_device_that_marked_itself_reinitializing(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}
    spawn_count = 0

    def factory(db_path: str, device_id: int) -> FakeProcess:
        nonlocal spawn_count
        spawn_count += 1
        process = FakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    async def simulate_reinitialize():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        await asyncio.sleep(0.1)  # Allow startup to complete and detect "running"
        with session_factory() as session:
            device = session.get(Device, 1)
            device.runtime.process_status = "reinitializing"
            session.commit()
        processes[1].alive = False  # the child's own clean exit, marker already written
        while spawn_count < 2:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)  # the fresh child self-reports

    asyncio.ensure_future(simulate_reinitialize())
    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")
    await engine.start(1)
    assert spawn_count == 1

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))

    def _respawned_and_running() -> bool:
        with session_factory() as session:
            return spawn_count >= 2 and session.get(Device, 1).runtime.process_status == "running"

    # Polled, not a fixed sleep -- a fixed 0.15 s flaked under full-suite load.
    deadline = asyncio.get_running_loop().time() + 5
    while not _respawned_and_running() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    health_check.cancel()

    assert spawn_count == 2
    assert engine.is_running(1)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running"
        assert device.runtime.process_error is None  # not the crashed path


@pytest.mark.asyncio
async def test_health_check_still_marks_a_real_crash_crashed_not_reinitializing(session_factory):
    """Regression guard: a genuine crash (row still "running" at death) must
    still be marked "crashed" with no auto-restart -- the new branch above
    must not accidentally widen to cover this case too."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}
    spawn_count = 0

    def factory(db_path: str, device_id: int) -> FakeProcess:
        nonlocal spawn_count
        spawn_count += 1
        process = FakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    async def flip_to_running_then_die():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)
        await asyncio.sleep(0.02)
        processes[1].alive = False

    asyncio.ensure_future(flip_to_running_then_die())
    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")
    await engine.start(1)

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.1)
    health_check.cancel()

    assert spawn_count == 1  # no respawn
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
    assert not engine.is_running(1)


def test_status_includes_dcc_state(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D", dcc_state="disableInitiation"))
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    statuses = engine.status()

    assert statuses[0]["dcc_state"] == "disableInitiation"


def test_status_reports_the_live_apply_fields(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.flush()
        session.get(Device, 1).runtime.apply_error = "boom"
        session.commit()
    [entry] = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite").status()
    assert entry["apply_error"] == "boom"
    assert entry["restart_pending"] is False


@pytest.mark.asyncio
async def test_stop_converges_an_untracked_device_to_stopped(session_factory):
    """Every path that marks a device "crashed" also drops it from
    self.processes, so _stop_locked's "nothing tracked" early return left the
    row stuck at "crashed" forever with no API-reachable way out. stop() must
    converge an untracked device to the terminal stopped state instead.
    """
    with session_factory() as session:
        session.add(
            Device(
                id=1,
                name="D",
                runtime=DeviceRuntime(
                    process_status="crashed",
                    process_error="process was not found on parent restart",
                    process_pid=424242,
                ),
            )
        )
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.stop(1)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "stopped"
        assert device.runtime.process_pid is None
        assert device.runtime.process_error is None


@pytest.mark.asyncio
async def test_health_check_marks_a_failed_reinitialize_respawn_crashed(session_factory):
    """The reported bug: a ReinitializeDevice whose respawn fails its
    pre-spawn checks (here, the device's bind address now collides with
    another running device -- editing a bind address while running is a
    supported action) left the row at "reinitializing" forever, with a stale
    pid, no process_error, and popped from self.processes so no later health
    check tick ever revisited it.
    """
    with session_factory() as session:
        session.add_all(
            [
                Device(id=1, name="D1", bind_address="127.0.0.1/8"),
                Device(id=2, name="D2", bind_address="127.0.0.2/8"),
            ]
        )
        session.commit()

    processes: dict = {}
    spawns: list[int] = []

    def factory(db_path: str, device_id: int) -> FakeProcess:
        spawns.append(device_id)
        process = FakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    async def bring_up(device_id: int):
        while device_id not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, device_id)

    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")
    asyncio.ensure_future(bring_up(2))
    await engine.start(2)
    asyncio.ensure_future(bring_up(1))
    await engine.start(1)

    # Device 2's address is repointed at the one device 1 is already serving,
    # then device 2 reinitializes itself -- so its respawn must fail.
    with session_factory() as session:
        device = session.get(Device, 2)
        device.bind_address = "127.0.0.1/8"
        device.runtime.process_status = "reinitializing"
        session.commit()
    processes[2].alive = False

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.15)
    health_check.cancel()

    assert spawns == [2, 1]  # the failed respawn never reached a spawn
    with session_factory() as session:
        device = session.get(Device, 2)
        assert device.runtime.process_status == "crashed"
        assert "already in use" in device.runtime.process_error
        assert device.runtime.process_pid is None


def test_status_includes_backup_fields(session_factory):
    with session_factory() as session:
        session.add(
            Device(id=1, name="D", database_revision=5, last_restore_time=dt.datetime(2026, 9, 26, 10, 30, 0, 123456))
        )
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    status = engine.status()[0]

    assert status["database_revision"] == 5
    expected = dt.datetime(2026, 9, 26, 10, 30, 0, tzinfo=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    assert status["last_restore_time"] == expected
    assert expected[-6] in "+-"


def test_startup_timeout_leaves_room_for_slow_hardware():
    # A child re-imports bacpypes3/SQLAlchemy from scratch (~0.55 s
    # on the 2-core test VM); 5 s was too tight for a Raspberry Pi or several
    # devices starting at once. start() returns as soon as the child reports
    # running, so a longer ceiling costs a fast machine nothing.
    import bacpack.engine as engine_module

    assert engine_module.STARTUP_TIMEOUT_SECONDS >= 30


# --- A respawn failing with anything, not just EngineError ------------------


def _factory_failing_after(processes: dict, good_spawns: int):
    spawns = 0

    def factory(db_path: str, device_id: int) -> FakeProcess:
        nonlocal spawns
        spawns += 1
        if spawns > good_spawns:
            raise OSError("spawn failed: out of handles")
        process = FakeProcess(db_path, device_id)
        processes[device_id] = process
        return process

    return factory


@pytest.mark.asyncio
async def test_health_check_marks_a_respawn_failing_with_any_error_crashed(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D", bind_address="127.0.0.1/8"))
        session.commit()

    processes: dict = {}
    engine = EngineManager(
        session_factory, process_factory=_factory_failing_after(processes, 1), db_path="db.sqlite"
    )

    async def bring_up():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(bring_up())
    await engine.start(1)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "reinitializing"
        session.commit()
    processes[1].alive = False

    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    await asyncio.sleep(0.1)
    health_check.cancel()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert "spawn failed" in device.runtime.process_error
        assert device.runtime.process_pid is None


@pytest.mark.asyncio
async def test_reattach_marks_a_respawn_failing_with_any_error_crashed(session_factory):
    import datetime as dt

    with session_factory() as session:
        device = Device(id=1, name="D", bind_address="127.0.0.1/8")
        device.runtime.process_status = "reinitializing"
        device.runtime.process_pid = 2**30 - 1  # gone
        device.runtime.process_started_at = dt.datetime.now(dt.timezone.utc)
        session.add(device)
        session.commit()

    engine = EngineManager(
        session_factory, process_factory=_factory_failing_after({}, 0), db_path="db.sqlite"
    )
    await engine.reattach_running_devices()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert "spawn failed" in device.runtime.process_error


# --- Rows that turn "running" after reattach ran ----------------------------


def _running_row(session_factory, pid, started_at):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.runtime.process_status = "running"
        device.runtime.process_pid = pid
        device.runtime.process_started_at = started_at
        session.add(device)
        session.commit()


async def _one_health_check_pass(engine, until=None, what="the health check's result"):
    """Runs the health check; with `until`, waits for that condition (a pass
    that writes in steps can still be mid-way after a fixed sleep on a loaded
    machine), otherwise a fixed 0.1 s."""
    health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
    try:
        if until is None:
            await asyncio.sleep(0.1)
        else:
            await _until(until, what)
    finally:
        health_check.cancel()


@pytest.mark.asyncio
async def test_health_check_adopts_a_running_child_it_is_not_tracking(session_factory):
    import datetime as dt

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()  # nothing to reattach yet
    real_started_at = dt.datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), tz=dt.timezone.utc)
    _running_row(session_factory, os.getpid(), real_started_at)  # the child wrote "running" late

    await _one_health_check_pass(engine)

    assert engine.is_running(1)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"


@pytest.mark.asyncio
async def test_health_check_marks_an_untracked_running_row_with_no_process_crashed(session_factory):
    import datetime as dt

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    _running_row(session_factory, 2**30 - 1, dt.datetime.now(dt.timezone.utc))

    await _one_health_check_pass(engine)

    assert not engine.is_running(1)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error == "process was not found (no server was tracking it)"


@pytest.mark.asyncio
async def test_health_check_leaves_a_device_mid_start_alone(session_factory):
    import datetime as dt

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    _running_row(session_factory, 2**30 - 1, dt.datetime.now(dt.timezone.utc))
    lock = engine._lock_for(1)
    await lock.acquire()  # a start() in progress holds it
    try:
        await _one_health_check_pass(engine)
    finally:
        lock.release()

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"  # untouched


def test_stop_grace_covers_the_childs_whole_stop_budget():
    # The child notices a stop within STOP_CHECK_TICK_SECONDS,
    # relinquishes connector writes for up to SHUTDOWN_RELINQUISH_SECONDS,
    # then tears down. 3 s of margin for teardown on a slow machine, so tuning
    # one constant can't silently undercut the other.
    import bacpack.engine as engine_module
    from bacpack.bacnet_connectors import SHUTDOWN_RELINQUISH_SECONDS
    from bacpack.device_process import STOP_CHECK_TICK_SECONDS

    assert engine_module.STOP_GRACE_SECONDS > STOP_CHECK_TICK_SECONDS + SHUTDOWN_RELINQUISH_SECONDS + 3


def test_status_sends_process_started_at_with_its_utc_offset(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.flush()
        device.runtime.process_started_at = dt.datetime(2026, 10, 5, 12, 0, 0)
        session.commit()

    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    assert engine.status()[0]["process_started_at"] == "2026-10-05T12:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["crashed", "exited", "timeout"])
async def test_a_failed_start_folds_a_clock_change_made_while_starting(session_factory, monkeypatch, failure):
    import bacpack.engine as engine_module

    monkeypatch.setattr(engine_module, "STARTUP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(engine_module, "STARTUP_POLL_INTERVAL_SECONDS", 0.01)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    processes: dict = {}

    async def clock_change_then_fail():
        while 1 not in processes:
            await asyncio.sleep(0)
        with session_factory() as session:
            device = session.get(Device, 1)
            device.runtime.pending_clock_offset = 1800.0
            if failure == "crashed":
                device.runtime.process_status = "crashed"
            session.commit()
        if failure == "exited":
            processes[1].alive = False

    asyncio.ensure_future(clock_change_then_fail())
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    with pytest.raises(EngineError):
        await engine.start(1)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.pending_clock_offset is None
        assert device.clock_offset_seconds == 1800.0


def _add_device(session_factory, device_id, *, bind="127.0.0.1/8", status="stopped", resume=False):
    with session_factory() as session:
        if bind is not None:
            get_or_create_settings(session).bind_address = bind
        device = Device(id=device_id, name=f"D{device_id}")
        device.runtime.process_status = status
        device.runtime.resume_on_start = resume
        session.add(device)
        session.commit()


def _runtime(session_factory, device_id):
    with session_factory() as session:
        runtime = session.get(Device, device_id).runtime
        return runtime.process_status, runtime.resume_on_start, runtime.process_error


async def _start_tracked(engine, session_factory, processes, device_id):
    async def flip():
        while device_id not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, device_id)

    asyncio.ensure_future(flip())
    await engine.start(device_id)


@pytest.mark.asyncio
async def test_stop_all_for_shutdown_stops_and_remembers_every_tracked_device(session_factory, monkeypatch):
    monkeypatch.setattr("bacpack.engine.STOP_GRACE_SECONDS", 0.05)
    _add_device(session_factory, 1)
    _add_device(session_factory, 2)
    with session_factory() as session:
        session.get(Device, 2).bind_address = "127.0.0.2/8"  # one device per bind address
        session.commit()
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await _start_tracked(engine, session_factory, processes, 1)
    await _start_tracked(engine, session_factory, processes, 2)

    await engine.stop_all_for_shutdown()

    assert engine.processes == {}
    assert _runtime(session_factory, 1)[:2] == ("stopped", True)
    assert _runtime(session_factory, 2)[:2] == ("stopped", True)


@pytest.mark.asyncio
async def test_stop_all_for_shutdown_remembers_a_device_whose_stop_is_cut_short(session_factory, monkeypatch):
    """A shutdown cut short (the service manager's stop timeout, an OS
    shutdown window, a power cut mid-stop) must still bring the device back."""
    monkeypatch.setattr("bacpack.engine.STOP_GRACE_SECONDS", 0.05)
    _add_device(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await _start_tracked(engine, session_factory, processes, 1)

    async def cut_short(device_id, *, remember=False):
        raise RuntimeError("killed by the service manager mid-stop")

    monkeypatch.setattr(engine, "_stop_locked", cut_short)
    await engine.stop_all_for_shutdown()

    assert _runtime(session_factory, 1)[1] is True


@pytest.mark.asyncio
async def test_stop_all_for_shutdown_waits_for_a_device_mid_start(session_factory, monkeypatch):
    monkeypatch.setattr("bacpack.engine.STOP_GRACE_SECONDS", 0.05)
    _add_device(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")

    start = asyncio.ensure_future(_start_tracked(engine, session_factory, processes, 1))
    while 1 not in processes:
        await asyncio.sleep(0)
    await engine.stop_all_for_shutdown()
    await start

    assert 1 not in engine.processes
    assert _runtime(session_factory, 1)[:2] == ("stopped", True)


@pytest.mark.asyncio
async def test_an_explicit_stop_forgets_the_device(session_factory, monkeypatch):
    monkeypatch.setattr("bacpack.engine.STOP_GRACE_SECONDS", 0.05)
    _add_device(session_factory, 1, resume=True)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await _start_tracked(engine, session_factory, processes, 1)
    with session_factory() as session:
        session.get(Device, 1).runtime.resume_on_start = True
        session.commit()

    await engine.stop(1)

    assert _runtime(session_factory, 1)[:2] == ("stopped", False)


@pytest.mark.asyncio
async def test_a_successful_start_clears_the_flag(session_factory):
    _add_device(session_factory, 1, resume=True)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await _start_tracked(engine, session_factory, processes, 1)
    assert _runtime(session_factory, 1)[:2] == ("running", False)


@pytest.mark.asyncio
async def test_resume_starts_remembered_devices_and_leaves_others(session_factory):
    _add_device(session_factory, 1, resume=True)
    _add_device(session_factory, 2, resume=False)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")

    async def flip():
        while 1 not in processes:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip())
    await engine.resume_remembered_devices(retry_seconds=0.01, give_up_seconds=1.0)

    assert engine.is_running(1) and not engine.is_running(2)
    assert _runtime(session_factory, 1)[:2] == ("running", False)
    assert 2 not in processes


@pytest.mark.asyncio
async def test_resume_retries_a_failing_device_until_it_starts(session_factory):
    _add_device(session_factory, 1, resume=True)
    attempts: list = []

    def factory(db_path, device_id):
        process = FakeProcess(db_path, device_id)
        attempts.append(process)
        if len(attempts) < 3:
            process.start = lambda: None  # never alive: "exited during startup"
        return process

    engine = EngineManager(session_factory, process_factory=factory, db_path="db.sqlite")

    async def flip_third():
        while len(attempts) < 3:
            await asyncio.sleep(0)
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(flip_third())
    await engine.resume_remembered_devices(retry_seconds=0.01, give_up_seconds=5.0)

    assert len(attempts) == 3
    assert _runtime(session_factory, 1)[:2] == ("running", False)


@pytest.mark.asyncio
async def test_resume_gives_up_but_keeps_the_device_remembered(session_factory):
    _add_device(session_factory, 1, bind=None, resume=True)  # no bind address anywhere
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")

    await engine.resume_remembered_devices(retry_seconds=0.01, give_up_seconds=0.05)

    status, resume, error = _runtime(session_factory, 1)
    assert (status, resume) == ("crashed", True)
    assert "bind address" in error


@pytest.mark.asyncio
async def test_resume_skips_a_device_stopped_explicitly_meanwhile(session_factory):
    _add_device(session_factory, 1, bind=None, resume=True)
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    task = asyncio.ensure_future(engine.resume_remembered_devices(retry_seconds=0.05, give_up_seconds=5.0))
    await asyncio.sleep(0.02)
    await engine.stop(1)  # explicit stop clears the flag
    await asyncio.wait_for(task, timeout=1.0)
    assert _runtime(session_factory, 1)[:2] == ("stopped", False)


@pytest.mark.asyncio
async def test_an_explicit_start_that_fails_forgets_the_device(session_factory):
    _add_device(session_factory, 1, bind=None, resume=True)  # no bind address anywhere
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")

    with pytest.raises(EngineError, match="bind address"):
        await engine.start(1)

    assert _runtime(session_factory, 1)[1] is False


@pytest.mark.asyncio
async def test_resume_ends_after_an_explicit_start_fails_meanwhile(session_factory):
    _add_device(session_factory, 1, bind=None, resume=True)
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    task = asyncio.ensure_future(engine.resume_remembered_devices(retry_seconds=0.05, give_up_seconds=5.0))
    await asyncio.sleep(0.02)
    with pytest.raises(EngineError):
        await engine.start(1)
    await asyncio.wait_for(task, timeout=1.0)
    assert _runtime(session_factory, 1)[1] is False


class _SelfPidProcess(FakeProcess):
    """A fake whose pid is this test process, so psutil can read its identity."""

    def start(self) -> None:
        super().start()
        self.pid = os.getpid()


def _self_pid_factory(processes: dict):
    def factory(db_path: str, device_id: int) -> _SelfPidProcess:
        process = _SelfPidProcess(db_path, device_id)
        processes[device_id] = process
        return process

    return factory


@pytest.mark.asyncio
async def test_a_device_reads_starting_until_its_child_reports_running(session_factory):
    _add_device(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_self_pid_factory(processes), db_path="db.sqlite")
    seen = []

    async def watch_then_flip():
        while 1 not in processes:
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        with session_factory() as session:
            runtime = session.get(Device, 1).runtime
            seen.append((runtime.process_status, runtime.process_pid, runtime.process_started_at))
        await _mark_running(session_factory, 1)

    asyncio.ensure_future(watch_then_flip())
    await engine.start(1)

    status, pid, started_at = seen[0]
    assert status == "starting"
    # Recorded so a restarted parent can tell this child from a reused pid.
    assert pid == os.getpid()
    expected = psutil.Process(os.getpid()).create_time()
    assert abs(timeutil.from_db(started_at).timestamp() - expected) < 0.01
    assert _runtime(session_factory, 1)[0] == "running"


@pytest.mark.asyncio
async def test_a_child_that_exits_during_startup_without_a_word_is_left_crashed(session_factory):
    _add_device(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")

    async def die():
        while 1 not in processes:
            await asyncio.sleep(0)
        processes[1].alive = False

    asyncio.ensure_future(die())
    with pytest.raises(EngineError, match="exited during startup"):
        await engine.start(1)

    status, _resume, error = _runtime(session_factory, 1)
    assert status == "crashed"
    assert "exited during startup" in error


@pytest.mark.asyncio
async def test_a_child_that_stops_itself_during_startup_keeps_its_own_status(session_factory):
    _add_device(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")

    async def write_stopped_and_exit():
        while 1 not in processes:
            await asyncio.sleep(0)
        with session_factory() as session:
            session.get(Device, 1).runtime.process_status = "stopped"
            session.commit()
        processes[1].alive = False

    asyncio.ensure_future(write_stopped_and_exit())
    with pytest.raises(EngineError):
        await engine.start(1)

    assert _runtime(session_factory, 1)[0] == "stopped"


def _starting_row(session_factory, pid, started_at):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.runtime.process_status = "starting"
        device.runtime.process_pid = pid
        device.runtime.process_started_at = started_at
        session.add(device)
        session.commit()


def _own_started_at():
    return dt.datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), tz=dt.timezone.utc)


@pytest.mark.asyncio
async def test_reattach_tracks_a_device_still_starting_after_a_parent_restart(session_factory):
    _starting_row(session_factory, os.getpid(), _own_started_at())
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert engine.is_running(1)
    assert _runtime(session_factory, 1)[0] == "starting"  # the child writes "running" itself


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [2**30 - 1, None])
async def test_reattach_remembers_a_starting_row_whose_process_is_gone(session_factory, pid):
    _starting_row(session_factory, pid, dt.datetime.now(dt.timezone.utc))
    # A pid-less row is waited for first (below); with no wait left it is judged at once.
    engine = EngineManager(
        session_factory, process_factory=_factory({}), db_path="db.sqlite", startup_grace_seconds=0
    )
    await engine.reattach_running_devices()

    assert not engine.is_running(1)
    assert _runtime(session_factory, 1)[:2] == ("stopped", True)


@pytest.mark.asyncio
async def test_health_check_marks_an_untracked_starting_row_with_no_process_crashed(session_factory):
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    _starting_row(session_factory, 2**30 - 1, dt.datetime.now(dt.timezone.utc))

    await _one_health_check_pass(engine)

    status, _resume, error = _runtime(session_factory, 1)
    assert status == "crashed"
    assert "not found" in error


@pytest.mark.asyncio
async def test_health_check_leaves_a_starting_row_alone_while_its_start_holds_the_lock(session_factory):
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    _starting_row(session_factory, None, None)
    lock = engine._lock_for(1)
    await lock.acquire()
    try:
        await _one_health_check_pass(engine)
    finally:
        lock.release()

    assert _runtime(session_factory, 1)[0] == "starting"
    assert not engine._unjudged  # the parent's own start is not a row waiting for a child


@pytest.mark.asyncio
async def test_health_check_marks_a_reattached_starting_device_that_died_crashed(session_factory):
    _starting_row(session_factory, os.getpid(), _own_started_at())
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()
    engine.processes[1] = FakeProcess("db.sqlite", 1)  # the reattached child, now dead

    await _one_health_check_pass(engine)

    assert not engine.is_running(1)
    assert _runtime(session_factory, 1)[0] == "crashed"


async def test_save_off_loop_returns_the_result():
    assert await _save_off_loop(lambda a, b=0: a + b, 2, b=3) == 5


async def test_save_off_loop_raises_a_failed_save():
    def boom():
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        await _save_off_loop(boom)


async def test_a_cancel_waits_for_the_save_to_finish():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def save():
        started.set()
        release.wait(5)
        finished.set()

    task = asyncio.ensure_future(_save_off_loop(save))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # a second cancel doesn't cut the wait short either
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_a_cancel_wins_over_a_failed_save(caplog):
    started, release = threading.Event(), threading.Event()

    def save():
        started.set()
        release.wait(5)
        raise RuntimeError("locked")

    task = asyncio.ensure_future(_save_off_loop(save))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert isinstance(raised.value.__cause__, RuntimeError)
    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


async def test_a_cancel_after_a_save_that_landed_has_no_cause():
    started, release = threading.Event(), threading.Event()

    def save():
        started.set()
        release.wait(5)

    task = asyncio.ensure_future(_save_off_loop(save))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.__cause__ is None


async def _until(condition, what: str, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    # In a thread: a condition that reads the database on the loop would add
    # its own stalls to a loop-gap measurement it runs inside.
    while not await asyncio.to_thread(condition):
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def _status(session_factory, device_id: int) -> str:
    with session_factory() as session:
        return session.get(Device, device_id).runtime.process_status


def _resume(session_factory, device_id: int) -> bool:
    with session_factory() as session:
        return session.get(Device, device_id).runtime.resume_on_start


def _add_devices(session_factory, *ids: int) -> None:
    with session_factory() as session:
        for device_id in ids:
            session.add(Device(id=device_id, name=f"D{device_id}", bind_address=f"127.0.0.{device_id}/8"))
        session.commit()


def _track_running(engine, session_factory, processes: dict, device_id: int) -> None:
    process = FakeProcess("db.sqlite", device_id)
    process.start()
    processes[device_id] = process
    engine.processes[device_id] = process
    with session_factory() as session:
        runtime = session.get(Device, device_id).runtime
        runtime.process_status = "running"
        runtime.process_pid = process.pid
        session.commit()


async def _start_until_spawned(engine, processes: dict, device_id: int) -> None:
    task = asyncio.ensure_future(engine.start(device_id))
    await _until(lambda: device_id in processes and processes[device_id].started, "the spawn")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_start_saves_off_the_loop(session_factory):
    # "starting" is written before the spawn; the fake child never reports
    # running, so the start is cut short once it has spawned.
    _add_devices(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    run = await run_while_locked(session_factory, lambda: _start_until_spawned(engine, processes, 1))
    assert_waited_off_the_loop(run)
    assert _status(session_factory, 1) == "starting"


@pytest.mark.asyncio
async def test_stop_saves_off_the_loop(session_factory):
    _add_devices(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    _track_running(engine, session_factory, processes, 1)
    processes[1].alive = False  # exits at once, so only the saves wait
    run = await run_while_locked(session_factory, lambda: engine.stop(1))
    assert_waited_off_the_loop(run)
    assert _status(session_factory, 1) == "stopped"


@pytest.mark.asyncio
async def test_a_crash_mark_saves_off_the_loop(session_factory):
    _add_devices(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    _track_running(engine, session_factory, processes, 1)
    processes[1].alive = False

    async def one_pass() -> None:
        health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
        await _until(lambda: _status(session_factory, 1) == "crashed" and 1 not in engine.processes, "the crash mark")
        health_check.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_check

    run = await run_while_locked(session_factory, one_pass)
    assert_waited_off_the_loop(run)
    assert _status(session_factory, 1) == "crashed"
    assert 1 not in engine.processes


@pytest.mark.asyncio
async def test_a_shutdown_stop_saves_off_the_loop(session_factory):
    _add_devices(session_factory, 1, 2)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    for device_id in (1, 2):
        _track_running(engine, session_factory, processes, device_id)
        processes[device_id].alive = False
    run = await run_while_locked(session_factory, engine.stop_all_for_shutdown)
    assert_waited_off_the_loop(run)
    assert all(_resume(session_factory, d) for d in (1, 2))
    assert all(_status(session_factory, d) == "stopped" for d in (1, 2))


@pytest.mark.asyncio
async def test_two_starts_on_one_address_spawn_one(session_factory):
    # Each start waits in a thread for its "starting" save; the second must
    # still see the first as taking the address.
    with session_factory() as session:
        get_or_create_settings(session).bind_address = "127.0.0.5/8"
        session.add_all([Device(id=1, name="D1"), Device(id=2, name="D2")])
        session.commit()
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    starts = [asyncio.ensure_future(engine.start(d)) for d in (1, 2)]
    done, pending = await asyncio.wait(starts, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(done) == 1 and len(pending) == 1
    with pytest.raises(EngineError, match="already in use by currently-running device"):
        done.pop().result()
    assert len(processes) == 1


@pytest.mark.asyncio
async def test_a_start_cancelled_after_its_starting_save_still_tracks_the_child(session_factory):
    # The shutdown cancels the resume task: a child whose "starting" row is
    # saved is spawned and tracked, so the shutdown stops and remembers it.
    _add_devices(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")

    async def cancel_mid_save() -> None:
        task = asyncio.ensure_future(engine.start(1))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await run_while_locked(session_factory, cancel_mid_save)
    assert processes[1].started
    assert 1 in engine.processes
    assert _status(session_factory, 1) == "starting"


@pytest.mark.asyncio
async def test_a_health_check_cancelled_after_its_crash_mark_untracks_the_device(session_factory):
    _add_devices(session_factory, 1)
    processes: dict = {}
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    _track_running(engine, session_factory, processes, 1)
    processes[1].alive = False

    async def cancel_mid_save() -> None:
        health_check = asyncio.ensure_future(engine.run_health_check_task(interval_seconds=0.01))
        await asyncio.sleep(0.3)
        health_check.cancel()
        with pytest.raises(asyncio.CancelledError):
            await health_check

    await run_while_locked(session_factory, cancel_mid_save)
    assert _status(session_factory, 1) == "crashed"
    assert 1 not in engine.processes


def test_status_flags_a_crash_from_a_port_in_use(session_factory):
    in_use = _bind_error_message(OSError(errno.EADDRINUSE, "in use"), "192.168.1.200", 47808)
    with session_factory() as session:
        session.add_all([Device(id=1, name="A"), Device(id=2, name="B"), Device(id=3, name="C")])
        session.flush()
        session.get(Device, 1).runtime.process_status = "crashed"
        session.get(Device, 1).runtime.process_error = in_use
        session.get(Device, 2).runtime.process_status = "crashed"
        session.get(Device, 2).runtime.process_error = "Address 192.168.1.200 is not assigned to any network interface"
        session.commit()
    rows = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite").status()
    assert [row["port_in_use"] for row in rows] == [True, False, False]


def test_status_does_not_flag_a_running_device_with_a_stale_in_use_error(session_factory):
    in_use = _bind_error_message(OSError(errno.EADDRINUSE, "in use"), "192.168.1.200", 47808)
    with session_factory() as session:
        session.add(Device(id=1, name="A"))
        session.flush()
        session.get(Device, 1).runtime.process_status = "running"
        session.get(Device, 1).runtime.process_error = in_use
        session.commit()
    [row] = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite").status()
    assert row["port_in_use"] is False


def test_a_reattached_handle_tolerates_a_child_that_already_exited():
    import subprocess

    from bacpack.engine import _PsutilProcessHandle

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    handle = _PsutilProcessHandle(child.pid)
    child.wait()
    handle.terminate()
    handle.kill()
    assert not handle.is_alive()


def _reattached(psutil_process):
    from bacpack.engine import _PsutilProcessHandle

    handle = _PsutilProcessHandle(os.getpid())
    handle._proc = psutil_process
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("ignores_terminate", [False, True])
async def test_stop_of_a_reattached_child_that_exits_before_the_signal_marks_it_stopped(
    session_factory, monkeypatch, ignores_terminate
):
    from tests.fakes import FakePsutilProcess

    monkeypatch.setattr(engine_module, "STOP_GRACE_SECONDS", 0.2)
    _add_device(session_factory, 1, status="running")
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    # Either terminate() finds the child gone, or it is ignored and kill() does.
    engine.processes[1] = _reattached(
        FakePsutilProcess(exits_before_signal=True, ignores_terminate=ignores_terminate)
    )

    await engine.stop(1)

    assert _runtime(session_factory, 1)[0] == "stopped"
    assert not engine.is_running(1)


# --- a "starting" row with no pid: the parent died before it saved the child's ----------


def _orphaned_start(session_factory, *, resume=False, bind="127.0.0.1/8"):
    _add_device(session_factory, 1, status="starting", resume=resume, bind=bind)


def _child_reports_running(session_factory):
    with session_factory() as session:
        runtime = session.get(Device, 1).runtime
        runtime.process_status = "running"
        runtime.process_pid = os.getpid()
        runtime.process_started_at = _own_started_at()
        session.commit()


@pytest.mark.asyncio
async def test_reattach_waits_for_a_pidless_starting_row_instead_of_remembering_it(session_factory):
    processes: dict = {}
    _orphaned_start(session_factory, resume=True)
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()

    assert _runtime(session_factory, 1)[0] == "starting"
    assert not engine.is_running(1)
    assert processes == {}


@pytest.mark.asyncio
async def test_a_waiting_child_that_reports_running_is_adopted_by_the_health_check(session_factory):
    _orphaned_start(session_factory)
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()
    _child_reports_running(session_factory)

    await _one_health_check_pass(engine)

    assert engine.is_running(1)
    assert _runtime(session_factory, 1)[0] == "running"
    assert not engine._unjudged


@pytest.mark.asyncio
async def test_resume_does_not_start_a_device_while_its_orphaned_child_is_awaited(session_factory):
    processes: dict = {}
    _orphaned_start(session_factory, resume=True)
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()

    await engine.resume_remembered_devices(retry_seconds=0.01, give_up_seconds=0.1)

    assert processes == {}
    assert _runtime(session_factory, 1)[0] == "starting"


async def _flip_to_running_when_spawned(session_factory, processes):
    while 1 not in processes:
        await asyncio.sleep(0)
    await _mark_running(session_factory, 1)


@pytest.mark.asyncio
async def test_when_the_wait_ends_in_a_health_pass_the_device_is_restarted(session_factory):
    processes: dict = {}
    _orphaned_start(session_factory)
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()
    engine._grace_until = time.monotonic() - 1
    flip = asyncio.ensure_future(_flip_to_running_when_spawned(session_factory, processes))

    await _one_health_check_pass(engine)
    flip.cancel()  # still waiting for a spawn that never came: the assertions say so

    assert list(processes) == [1]
    assert engine.is_running(1)
    assert _runtime(session_factory, 1)[0] == "running"
    assert not engine._unjudged


@pytest.mark.asyncio
async def test_when_the_wait_ends_and_the_restart_fails_the_row_is_crashed(session_factory):
    _orphaned_start(session_factory, bind=None)  # no bind address: the start is refused
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()
    engine._grace_until = time.monotonic() - 1

    await _one_health_check_pass(engine, lambda: _runtime(session_factory, 1)[0] == "crashed", "the crash mark")

    status, _resume, error = _runtime(session_factory, 1)
    assert status == "crashed"
    assert "bind address" in error


@pytest.mark.asyncio
async def test_a_pidless_starting_row_first_seen_after_the_wait_is_crashed_as_before(session_factory):
    _starting_row(session_factory, None, None)
    engine = EngineManager(
        session_factory, process_factory=_factory({}), db_path="db.sqlite", startup_grace_seconds=0
    )

    await _one_health_check_pass(engine)

    assert _runtime(session_factory, 1)[0] == "crashed"


@pytest.mark.asyncio
async def test_a_pidless_starting_row_first_seen_by_the_health_check_in_the_wait_is_left_alone(session_factory):
    _starting_row(session_factory, None, None)
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")

    await _one_health_check_pass(engine)

    assert _runtime(session_factory, 1)[0] == "starting"
    assert engine._unjudged == {1}


@pytest.mark.asyncio
async def test_start_restart_and_stop_are_refused_while_the_orphaned_child_is_awaited(session_factory):
    processes: dict = {}
    _orphaned_start(session_factory, resume=True)
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()

    for action in (engine.start, engine.restart, engine.stop):
        with pytest.raises(EngineError, match="may still be starting after a server restart; try again in"):
            await action(1)

    assert processes == {}
    assert _runtime(session_factory, 1)[:2] == ("starting", True)  # nothing was forgotten either


@pytest.mark.asyncio
async def test_stop_and_start_work_again_once_the_wait_is_over_or_the_row_moved_on(session_factory):
    _orphaned_start(session_factory)
    engine = EngineManager(session_factory, process_factory=_factory({}), db_path="db.sqlite")
    await engine.reattach_running_devices()
    engine._grace_until = time.monotonic() - 1

    await engine.stop(1)
    assert _runtime(session_factory, 1)[0] == "stopped"

    engine._unjudged.add(1)
    engine._grace_until = time.monotonic() + 60
    await engine.stop(1)  # the row is "stopped", not waiting: nothing to refuse
    assert not engine._unjudged


@pytest.mark.asyncio
async def test_a_start_after_the_wait_forgets_the_device_was_waiting(session_factory):
    processes: dict = {}
    _orphaned_start(session_factory)
    engine = EngineManager(session_factory, process_factory=_factory(processes), db_path="db.sqlite")
    await engine.reattach_running_devices()
    assert engine._unjudged == {1}
    engine._grace_until = time.monotonic() - 1
    flip = asyncio.ensure_future(_flip_to_running_when_spawned(session_factory, processes))

    await engine.start(1)
    flip.cancel()

    assert engine.is_running(1)
    assert not engine._unjudged
