"""Real multiprocessing.Process smoke test for run_device_process -- proves
the actual OS-process/pickling boundary works (this dev machine is Windows,
where multiprocessing's default 'spawn' start method re-imports the target
module fresh in the child; the Linux deployment box uses 'fork' by default,
a different but equally real boundary). Everything else about
device_process.py is covered by tests/test_device_process.py's fast
in-process tests -- this file exists only to prove the subprocess spawn
itself genuinely works, matching this project's "verify empirically, don't
just trust it" culture. Binding to 127.0.0.1 in a test is already an
established safe pattern here (see tests/test_engine.py's pre-existing
tests) -- this just moves that same real bind into a real child process.
"""
from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import time

import pytest

from bacpack.db import init_db, make_engine, make_session_factory
from bacpack.device_process import run_device_process
from bacpack.engine import _is_same_process
from bacpack.models import BacnetObject, Device, LogEntry, get_or_create_settings


def test_run_device_process_really_spawns_binds_and_stops(tmp_path):
    db_path = str(tmp_path / "subprocess-test.db")
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        )
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    process = multiprocessing.Process(target=run_device_process, args=(db_path, 1))
    process.start()
    try:
        deadline = time.monotonic() + 10.0
        status = None
        while time.monotonic() < deadline:
            with session_factory() as session:
                status = session.get(Device, 1).runtime.process_status
            if status == "running":
                break
            time.sleep(0.1)
        assert status == "running", f"child never reported running (last status: {status})"

        with session_factory() as session:
            device = session.get(Device, 1)
            assert device.runtime.process_pid == process.pid
            assert device.runtime.process_started_at is not None
            # What a restarted parent's reattach relies on -- this real
            # child's recorded start time is its OS creation time, however
            # long its startup took (or a duplicate process could be started).
            assert _is_same_process(process.pid, device.runtime.process_started_at)

        with session_factory() as session:
            session.get(Device, 1).runtime.process_status = "stopping"
            session.commit()

        process.join(timeout=10.0)
        assert not process.is_alive(), "child did not exit after being told to stop"

        with session_factory() as session:
            device = session.get(Device, 1)
            assert device.runtime.process_status == "stopped"
            assert device.runtime.process_pid is None
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)


def test_real_child_that_fails_to_start_leaves_its_log_line(tmp_path):
    db_path = str(tmp_path / "failed-start.db")
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))  # no bind address anywhere: the start fails
        session.commit()

    process = multiprocessing.Process(target=run_device_process, args=(db_path, 1))
    process.start()
    process.join(timeout=15.0)
    try:
        assert not process.is_alive(), "child did not exit after failing to start"
        with session_factory() as session:
            messages = [r.message for r in session.query(LogEntry).filter_by(device_id=1)]
        assert any("device 1 failed to start" in m for m in messages)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)


def test_real_child_applies_a_pending_clock_offset(tmp_path):
    """The one path the in-process tests can't prove: a real spawned child
    picks up the web UI's pending_clock_offset and saves it."""
    db_path = str(tmp_path / "subprocess-clock.db")
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        )
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    process = multiprocessing.Process(target=run_device_process, args=(db_path, 1))
    process.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with session_factory() as session:
                if session.get(Device, 1).runtime.process_status == "running":
                    break
            time.sleep(0.1)

        with session_factory() as session:
            session.get(Device, 1).runtime.pending_clock_offset = 5400.0
            session.commit()

        deadline = time.monotonic() + 10.0
        applied = None
        while time.monotonic() < deadline:
            with session_factory() as session:
                device = session.get(Device, 1)
                if device.runtime.pending_clock_offset is None:
                    applied = device.clock_offset_seconds
                    break
            time.sleep(0.1)
        assert applied == 5400.0, "child never applied the pending clock offset"

        with session_factory() as session:
            session.get(Device, 1).runtime.process_status = "stopping"
            session.commit()
        process.join(timeout=10.0)
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)


def test_real_child_applies_an_added_object_without_a_restart(tmp_path):
    """Live object edits through a real spawned child: an object row added
    while it runs is applied by the same process, with no error."""
    db_path = str(tmp_path / "subprocess-live.db")
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        )
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    process = multiprocessing.Process(target=run_device_process, args=(db_path, 1))
    process.start()
    try:
        for _ in range(100):
            with session_factory() as session:
                if session.get(Device, 1).runtime.process_status == "running":
                    break
            time.sleep(0.1)

        with session_factory() as session:
            session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=2, name="AV-2"))
            session.commit()
            target = session.get(Device, 1).runtime.change_seq

        runtime = None
        for _ in range(50):
            with session_factory() as session:
                runtime = session.get(Device, 1).runtime
                if runtime.applied_seq is not None and runtime.applied_seq >= target:
                    break
            time.sleep(0.1)
        assert runtime.applied_seq is not None and runtime.applied_seq >= target, "child never applied the change"
        assert runtime.apply_error is None
        assert runtime.process_pid == process.pid
        assert runtime.process_status == "running"

        with session_factory() as session:
            session.get(Device, 1).runtime.process_status = "stopping"
            session.commit()
        process.join(timeout=10.0)
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to one process only exists on POSIX")
def test_real_child_survives_sigint(tmp_path):
    db_path = str(tmp_path / "subprocess-sigint.db")
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    process = multiprocessing.get_context("spawn").Process(target=run_device_process, args=(db_path, 1))
    process.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with session_factory() as session:
                if session.get(Device, 1).runtime.process_status == "running":
                    break
            time.sleep(0.1)
        os.kill(process.pid, signal.SIGINT)
        time.sleep(1.0)
        assert process.is_alive(), "device died on SIGINT"
        with session_factory() as session:
            session.get(Device, 1).runtime.process_status = "stopping"
            session.commit()
        process.join(timeout=10.0)
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
