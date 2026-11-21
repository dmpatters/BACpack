"""Test-only ProcessLike fakes for concurrent multi-device hosting: real socket
binds and real subprocess spawning aren't practical to fully exercise in most
unit tests.

FakeProcess: a bare, fully test-controlled double for EngineManager's own
orchestration-logic tests (Tasks 9-10) -- the test itself drives state by
writing to the DB directly (simulating what a real/fake child would do) and
toggling .alive; this class does no real work at all.

FakeDeviceProcess: high-fidelity -- .start() runs the REAL _serve_device
coroutine as an asyncio.Task in the current process/event loop instead of
spawning a real OS process. Used by test_web.py's web_client fixture (Task
12 onward) so API-level tests exercise the actual device-serving code
(build_application, TrendSampler, PendingWritePoller, the "stopping"-status
shutdown handshake) fast and deterministically, without real subprocess
spawn overhead -- only the OS-process boundary itself is faked.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import psutil

from bacpack.device_process import _serve_device
from bacpack.models import BacnetObject, Device


def _device_and_objects():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    objects = [
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit"
        ),
        BacnetObject(
            object_type="analog-output", instance_number=1, name="AO-1", units="percent"
        ),
        BacnetObject(object_type="binary-value", instance_number=1, name="BV-1"),
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
        ),
    ]
    return device, objects


class FakeProcess:
    def __init__(self, db_path: str, device_id: int) -> None:
        self.db_path = db_path
        self.device_id = device_id
        self.pid: Optional[int] = None
        self.alive = False
        self.started = False

    def start(self) -> None:
        self.started = True
        self.alive = True
        self.pid = 100000 + self.device_id  # any stable, distinguishable fake pid

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def join(self, timeout: Optional[float] = None) -> None:
        pass


class FakeDeviceProcess:
    def __init__(self, session_factory, device_id: int) -> None:
        self.session_factory = session_factory
        self.device_id = device_id
        self.pid: Optional[int] = device_id  # stable int; never compared to a real OS pid
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(_serve_device(self.session_factory, self.device_id))

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def kill(self) -> None:
        # Cancelling the task is the only "stop it harder" action available
        # for an in-process fake -- there's no real OS process to SIGKILL.
        # Required by ProcessLike and reachable via EngineManager's
        # kill()-escalation path in _stop_locked (see engine.py) if
        # terminate() doesn't take within the grace period.
        if self._task is not None:
            self._task.cancel()

    def join(self, timeout: Optional[float] = None) -> None:
        pass  # nothing to block on synchronously in-process; callers only ever poll is_alive()


class FakePsutilProcess:
    """The psutil.Process a reattached device's handle wraps. With
    `exits_before_signal` the child is already gone when terminate() or
    kill() reaches it, which psutil reports as NoSuchProcess."""

    def __init__(self, pid: int = 4242, *, exits_before_signal: bool = False, ignores_terminate: bool = False) -> None:
        self.pid = pid
        self.exits_before_signal = exits_before_signal
        self.ignores_terminate = ignores_terminate
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def _signal(self) -> None:
        self.running = False
        if self.exits_before_signal:
            raise psutil.NoSuchProcess(self.pid)

    def terminate(self) -> None:
        if self.ignores_terminate:
            return
        self._signal()

    def kill(self) -> None:
        self._signal()

    def wait(self, timeout=None) -> None:
        pass
