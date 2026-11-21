"""Runtime engine manager: one child OS process per running device,
coordinated only through SQLite.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import multiprocessing
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, TypeVar

import psutil
from bacpypes3.pdu import IPv4Address

from . import device_clock, timeutil
from .db import get_db_path
from .device_process import is_port_in_use_error, run_device_process
from .process_identity import read_boot_id, record_identity, started_since_boot
from .models import Device, DeviceRuntime, get_or_create_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class EngineError(Exception):
    pass


async def _save_off_loop(save: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a SQLite save in a worker thread: it may wait for another
    process's write lock, which must not freeze the web loop. A cancel (the
    shutdown cancels the health check and resume tasks) waits for the save to
    finish first; the CancelledError's __cause__ is the save's exception, or
    None when the save landed, so a caller can finish what the save began."""
    future = asyncio.ensure_future(asyncio.to_thread(save, *args, **kwargs))
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError from (None if future.cancelled() else future.exception())
    return future.result()


class ProcessLike(Protocol):
    """The subset of multiprocessing.Process EngineManager needs, injectable
    so tests can use a fake (tests/fakes.py)."""

    pid: Optional[int]

    def start(self) -> None: ...
    def is_alive(self) -> bool: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def join(self, timeout: Optional[float] = None) -> None: ...


ProcessFactory = Callable[[str, int], ProcessLike]


DEVICE_EXECUTABLE_NAME = "bacpack-worker.exe"


def device_executable(executable: str = sys.executable, platform: str = sys.platform) -> Optional[str]:
    """The Windows bundle's copy of its interpreter that carries the name
    "BACpack device" (Task Manager shows an executable's own description), or
    None: a source install, a venv and Linux keep the interpreter itself."""
    if platform != "win32":
        return None
    candidate = Path(executable).with_name(DEVICE_EXECUTABLE_NAME)
    return str(candidate) if candidate.is_file() else None


def _default_process_factory(db_path: str, device_id: int) -> ProcessLike:
    # "spawn" explicitly: Linux defaults to "fork", which inherits every open
    # fd (uvicorn's listening socket included) and is unsafe with a running
    # asyncio loop. A surviving forked child (daemon=False) holding that
    # socket would block the parent from rebinding its web port on restart.
    #
    # daemon=False is the default but load-bearing: a device must keep
    # serving BACnet if the parent crashes or restarts.
    context = multiprocessing.get_context("spawn")
    executable = device_executable()
    if executable:
        context.set_executable(executable)
    # pid is a property on SpawnProcess; ProcessLike declares an attribute.
    return context.Process(target=run_device_process, args=(db_path, device_id), daemon=False)  # pyright: ignore[reportReturnType]


# How long start() waits for a spawned child to report "running" or die.
# Generous on purpose: the child re-imports bacpypes3/SQLAlchemy from scratch,
# so a Raspberry Pi or several devices starting at once can take seconds;
# start() returns as soon as the child reports running.
STARTUP_TIMEOUT_SECONDS = 30.0
STARTUP_POLL_INTERVAL_SECONDS = 0.05

# How long stop() waits for a graceful shutdown before terminate(). Covers the
# child's whole stop: noticing it (device_process.STOP_CHECK_TICK_SECONDS),
# relinquishing connector writes (bacnet_connectors.SHUTDOWN_RELINQUISH_SECONDS),
# then teardown; 5 s cut a slow machine off mid-relinquish. A test pins the margin.
STOP_GRACE_SECONDS = 10.0
STOP_POLL_INTERVAL_SECONDS = 0.05

HEALTH_CHECK_INTERVAL_SECONDS = 5.0

# After a parent restart, a "starting" row with no pid may belong to a child
# the old parent spawned just before it died (the pid is saved after the
# spawn). That child reports "running" within STARTUP_TIMEOUT_SECONDS of its own
# spawn, which is before this parent existed, so the row is waited on for that
# long from this parent's start, plus a margin for a slow save and the next
# health tick, before it is judged.
STARTUP_GRACE_MARGIN_SECONDS = 5.0

# A remembered device that won't start is retried this often, for this long,
# then left "crashed" and still remembered.
RESUME_RETRY_SECONDS = 10.0
RESUME_GIVE_UP_SECONDS = 300.0

# Rows that claim a live child. A parent that doesn't track one re-checks it
# at startup and on every health-check tick.
_UNTRACKED_STATUSES = ("running", "starting", "reinitializing")
# A child that dies in one of these crashed (or, found gone at startup, was
# cut off by a power-off and is restored).
_LIVE_STATUSES = ("running", "starting")


def _socket_of(bind_address: str) -> object:
    """The (host, port) a bind address binds, so one socket written two ways
    ("10.0.0.5/24", "10.0.0.5:47808") is one; the string itself if it
    doesn't parse (the child reports that)."""
    try:
        return IPv4Address(bind_address).addrTuple  # pyright: ignore[reportAttributeAccessIssue] - bacpypes3 types IPv4Address() as Address
    except Exception:
        return bind_address


def _fold_pending_clock(device: Device) -> None:
    """Fold a pending web-UI clock change into the saved clock once no child
    is left to apply it (stopped or crashed), so it isn't stuck "applying...".
    No time-change record: a device that isn't
    running isn't logging. The caller commits."""
    if device.runtime.pending_clock_offset is not None:
        device.clock_offset_seconds = device.runtime.pending_clock_offset
        device.runtime.pending_clock_offset = None

# For _is_same_process. The child records psutil's create_time() as
# process_started_at, so both sides come from the OS and this only absorbs
# float/clock noise. It must NOT be relied on to cover startup time, which on
# a cold box ran past 2 s and made reattach judge live children dead.
_PROCESS_IDENTITY_TOLERANCE_SECONDS = 2.0


def _is_same_process(
    pid: int,
    recorded_started_at: dt.datetime,
    recorded_boot_id: Optional[str] = None,
    recorded_since_boot: Optional[float] = None,
) -> bool:
    """True if `pid` is alive and still the process BACpack launched, not a
    reused PID (a bare is_alive()-by-PID check isn't enough).

    Same if its OS creation time matches `recorded_started_at`, or (Linux)
    if the boot id is unchanged and its start time since boot matches:
    psutil derives create_time() from the wall clock there, so an NTP step
    moves it. The boot id keeps a pid reused after a reboot from matching.
    Windows and rows without those columns get only the absolute check.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    # Stored naive UTC; a naive .timestamp() would assume local time.
    recorded_started_at = timeutil.from_db(recorded_started_at)
    if abs(proc.create_time() - recorded_started_at.timestamp()) < _PROCESS_IDENTITY_TOLERANCE_SECONDS:
        return True
    if recorded_boot_id is None or recorded_since_boot is None:
        return False
    if read_boot_id() != recorded_boot_id:
        return False
    since_boot = started_since_boot(pid)
    return since_boot is not None and abs(since_boot - recorded_since_boot) < _PROCESS_IDENTITY_TOLERANCE_SECONDS


class _PsutilProcessHandle:
    """ProcessLike for a device reattached after a parent restart. The child
    is no longer this process's OS child, so multiprocessing's
    waitpid()-based join() can't apply; psutil's wait() polls any PID."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._proc = psutil.Process(pid)

    def start(self) -> None:  # pragma: no cover - a reattached handle is never (re)started
        raise RuntimeError("a reattached process handle cannot be started")

    def is_alive(self) -> bool:
        return self._proc.is_running()

    # A child that exits before the signal is already gone, which is what the
    # caller wants (a multiprocessing handle tolerates it too).
    def terminate(self) -> None:
        with contextlib.suppress(psutil.NoSuchProcess):
            self._proc.terminate()

    def kill(self) -> None:
        # Reached by _terminate_and_confirm's kill() escalation when a
        # reattached device ignores terminate().
        with contextlib.suppress(psutil.NoSuchProcess):
            self._proc.kill()

    def join(self, timeout: Optional[float] = None) -> None:
        try:
            self._proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            pass


class EngineManager:
    def __init__(
        self,
        session_factory,
        *,
        process_factory: ProcessFactory = _default_process_factory,
        db_path: Optional[str] = None,
        startup_grace_seconds: float = STARTUP_TIMEOUT_SECONDS + STARTUP_GRACE_MARGIN_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self._grace_until = time.monotonic() + startup_grace_seconds
        # Devices whose pid-less "starting" row is waiting for its child to
        # report (see STARTUP_GRACE_MARGIN_SECONDS); in memory only.
        self._unjudged: Set[int] = set()
        self._process_factory = process_factory
        # A plain path for each child: a live engine/session factory can't
        # cross the spawn boundary.
        self._db_path = str(db_path) if db_path is not None else str(get_db_path())

        # For control (start/stop/health check) only; SQLite is the source of
        # truth for reporting (status()), including devices not tracked here.
        self.processes: Dict[int, ProcessLike] = {}
        # Devices between the bind check and being tracked: the "starting"
        # save awaits in between, and another device's start must see them.
        self._starting: Set[int] = set()
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock_for(self, device_id: int) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    def is_running(self, device_id: int) -> bool:
        return device_id in self.processes

    def status(self) -> List[dict]:
        """Per-device status, read from SQLite (the source of truth)
        rather than self.processes, ordered by device id."""
        with self.session_factory() as session:
            devices = session.query(Device).order_by(Device.id).all()
            return [
                {
                    "device_id": d.id,
                    "process_status": d.runtime.process_status,
                    "process_pid": d.runtime.process_pid,
                    "process_started_at": (
                        timeutil.iso_utc(d.runtime.process_started_at) if d.runtime.process_started_at else None
                    ),
                    "process_error": d.runtime.process_error,
                    "port_in_use": d.runtime.process_status == "crashed"
                    and is_port_in_use_error(d.runtime.process_error),
                    "apply_error": d.runtime.apply_error,
                    "restart_pending": bool(d.runtime.restart_pending),
                    "bind_address": d.bind_address,
                    "dcc_state": d.dcc_state,
                    "database_revision": d.database_revision,
                    "backup_failure_timeout": d.backup_failure_timeout,
                    "last_restore_time": (
                        device_clock.device_local_iso(d.last_restore_time) if d.last_restore_time else None
                    ),
                    **device_clock.clock_fields(d.clock_offset_seconds, d.runtime.pending_clock_offset),
                }
                for d in devices
            ]

    async def start(self, device_id: int) -> None:
        """An explicit start that fails forgets the device too, ending the
        startup resume task's retries. A device
        already running is left alone: a second Start queued behind the
        first must not restart it (restart() does that)."""
        async with self._lock_for(device_id):
            self._refuse_while_unjudged(device_id)
            process = self.processes.get(device_id)
            if process is not None and process.is_alive():
                return
            try:
                await self._start_locked(device_id)
            except Exception:
                await self._set_resume(device_id, False)
                raise

    async def _start_locked(self, device_id: int) -> None:
        if device_id in self.processes:
            await self._stop_locked(device_id)

        others = sorted((set(self.processes) | self._starting) - {device_id})
        self._starting.add(device_id)
        try:
            try:
                await _save_off_loop(self._begin_start_now, device_id, others)
            except asyncio.CancelledError as exc:
                if exc.__cause__ is not None:
                    raise
                # "starting" is saved: spawn and track the child before the
                # cancel goes on, so a shutdown stops and remembers it.
                self._spawn(device_id)
                raise
            process = self._spawn(device_id)
        finally:
            self._starting.discard(device_id)
        await self._record_starting_identity(device_id, process)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + STARTUP_TIMEOUT_SECONDS
        while loop.time() < deadline:
            with self.session_factory() as session:
                current = session.get(Device, device_id)
                current_status = current.runtime.process_status if current is not None else "stopped"
                current_error = current.runtime.process_error if current is not None else None

            if current_status == "running":
                await self._set_resume(device_id, False)
                return
            if current_status == "crashed":
                self.processes.pop(device_id, None)
                await self._fold_pending_clock_for(device_id)
                raise EngineError(
                    f"unable to start device {device_id}: "
                    f"{current_error or 'device process crashed during startup'}"
                )
            if not process.is_alive():
                self.processes.pop(device_id, None)
                error = current_error or "device process exited during startup"
                if current_status == "starting":
                    await self._mark_crashed(device_id, error)
                else:
                    await self._fold_pending_clock_for(device_id)
                raise EngineError(f"unable to start device {device_id}: {error}")
            await asyncio.sleep(STARTUP_POLL_INTERVAL_SECONDS)

        self.processes.pop(device_id, None)
        await self._terminate_and_confirm(device_id, process)
        error = f"did not report ready within {STARTUP_TIMEOUT_SECONDS}s"
        # Recorded like a crash, or the row would keep the pre-spawn reset
        # with no explanation. Accepted narrow race: the child could still
        # write "running" just before this lands.
        await self._mark_crashed(device_id, error)
        raise EngineError(f"unable to start device {device_id}: {error}")

    def _spawn(self, device_id: int) -> ProcessLike:
        process = self._process_factory(self._db_path, device_id)
        process.start()
        self.processes[device_id] = process
        return process

    def _begin_start_now(self, device_id: int, running: List[int]) -> None:
        """Check the device can start and write its "starting" row. `running`
        is a snapshot of the tracked ids: the loop changes self.processes
        while this waits on the database."""
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is None:
                raise EngineError(f"no device with id {device_id}")
            settings = get_or_create_settings(session)
            bind_address = device.bind_address or settings.bind_address
            if not bind_address:
                raise EngineError(
                    "no network bind address configured; set one on the "
                    "network settings screen, or on this device, first"
                )

            socket_address = _socket_of(bind_address)
            for other_id in running:
                other_device = session.get(Device, other_id)
                other_bind = (
                    (other_device.bind_address or settings.bind_address)
                    if other_device is not None
                    else None
                )
                if other_bind is not None and _socket_of(other_bind) == socket_address:
                    raise EngineError(
                        f"bind address {bind_address!r} is already in use by "
                        f"currently-running device {other_id}"
                    )

            # Reset any stale state from a previous attempt before spawning.
            device.runtime.process_status = "starting"
            device.runtime.process_pid = None
            device.runtime.process_error = None
            session.commit()

    async def stop(self, device_id: int, *, remember: bool = False) -> None:
        """An explicit stop forgets the device; the shutdown passes
        remember=True so the next startup restarts it."""
        async with self._lock_for(device_id):
            self._refuse_while_unjudged(device_id)
            await self._stop_locked(device_id, remember=remember)

    def _refuse_while_unjudged(self, device_id: int) -> None:
        """A device whose pid-less "starting" row is waiting for its child
        (STARTUP_GRACE_MARGIN_SECONDS) can't be started, restarted or
        stopped: a second child would fail on the port, and a stop has no
        pid to signal."""
        remaining = self._grace_until - time.monotonic()
        if remaining <= 0:
            self._unjudged.discard(device_id)
            return
        if device_id not in self._unjudged:
            return
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            still_waiting = (
                device is not None
                and device.runtime.process_status == "starting"
                and device.runtime.process_pid is None
            )
        if not still_waiting:
            self._unjudged.discard(device_id)
            return
        raise EngineError(
            f"device {device_id}: its process may still be starting after a server restart; "
            f"try again in about {int(remaining) + 1} s"
        )

    async def _terminate_and_confirm(self, device_id: int, process: ProcessLike) -> None:
        """terminate(), then kill() if still alive. An unresponsive child must
        not keep its bind socket while untracked, or a later start() would
        pass the bind-collision check and fail in a fresh child ("UDP port
        ... is already in use", device_process._wait_for_udp_port).
        """
        loop = asyncio.get_event_loop()
        process.terminate()
        # join() blocks; keep it off the event loop.
        await loop.run_in_executor(None, process.join, STOP_GRACE_SECONDS)
        if process.is_alive():
            logger.warning(
                "device %d: process %s did not exit after terminate(), "
                "escalating to kill()",
                device_id,
                process.pid,
                extra={"device_id": device_id},
            )
            process.kill()
            await loop.run_in_executor(None, process.join, STOP_GRACE_SECONDS)

    async def _record_starting_identity(self, device_id: int, process: ProcessLike) -> None:
        await _save_off_loop(self._record_starting_identity_now, device_id, process)

    def _record_starting_identity_now(self, device_id: int, process: ProcessLike) -> None:
        """Record the new child's pid and identity on its "starting" row, so
        a parent restarted mid-start can tell it from a reused pid. Left out
        if the child already wrote its own or is gone."""
        if process.pid is None:
            return
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is None or device.runtime.process_status != "starting":
                return
            try:
                record_identity(device.runtime, process.pid)
            except psutil.Error:
                return
            session.commit()

    async def _fold_pending_clock_for(self, device_id: int) -> None:
        await _save_off_loop(self._fold_pending_clock_for_now, device_id)

    def _fold_pending_clock_for_now(self, device_id: int) -> None:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is not None:
                _fold_pending_clock(device)
                session.commit()

    async def _set_resume(self, device_id: int, value: bool) -> None:
        await _save_off_loop(self._set_resume_now, device_id, value)

    def _set_resume_now(self, device_id: int, value: bool) -> None:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is not None and device.runtime.resume_on_start != value:
                device.runtime.resume_on_start = value
                session.commit()

    async def _mark_stopped(self, device_id: int, *, remember: bool = False) -> None:
        await _save_off_loop(self._mark_stopped_now, device_id, remember=remember)

    def _mark_stopped_now(self, device_id: int, *, remember: bool = False) -> None:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is not None:
                device.runtime.process_status = "stopped"
                device.runtime.process_pid = None
                device.runtime.process_error = None
                device.runtime.resume_on_start = remember
                _fold_pending_clock(device)
                session.commit()

    async def _mark_crashed(self, device_id: int, error: str) -> None:
        await _save_off_loop(self._mark_crashed_now, device_id, error)

    def _mark_crashed_now(self, device_id: int, error: str) -> None:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is not None:
                device.runtime.process_status = "crashed"
                device.runtime.process_error = error
                device.runtime.process_pid = None
                _fold_pending_clock(device)
                session.commit()

    def _mark_stopping_now(self, device_id: int) -> None:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is not None:
                device.runtime.process_status = "stopping"
                session.commit()

    async def _stop_locked(self, device_id: int, *, remember: bool = False) -> None:
        process = self.processes.get(device_id)
        if process is None:
            # Nothing tracked, but the row may still say "crashed", which
            # nothing else would ever clear; converge it to "stopped".
            await self._mark_stopped(device_id, remember=remember)
            return

        await _save_off_loop(self._mark_stopping_now, device_id)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + STOP_GRACE_SECONDS
        # A child OS process can't set an asyncio.Event, so poll it.
        while loop.time() < deadline and process.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(STOP_POLL_INTERVAL_SECONDS)

        if process.is_alive():
            await self._terminate_and_confirm(device_id, process)

        await self._mark_stopped(device_id, remember=remember)
        self.processes.pop(device_id, None)

    async def restart(self, device_id: int) -> None:
        """Stop and re-start one device's child process. Holds the device's
        lock once around both halves: separate stop()/start() calls would let
        a queued caller (asyncio.Lock is FIFO-fair) run in between."""
        async with self._lock_for(device_id):
            self._refuse_while_unjudged(device_id)
            await self._stop_locked(device_id)
            await self._start_locked(device_id)

    async def restart_all(self) -> dict:
        """Restart every device whose row says "running", so settings only
        read at start (network role, BBMD peers, device identity) take effect
        without visiting each device. Stopped and crashed devices are left as
        they are: this never starts a device the user didn't have running.
        One failure doesn't stop the rest.

        Every device stops before any starts: a device's new port may be
        another's old one (ports shifted or swapped), which a device-by-device
        restart can't free in time. Starts go in id order, so of two devices
        given the same address the lower id gets it.

        The devices' locks are held until the last start ends (up to the
        startup timeout per device): a Start or Stop on one of them, and the
        health check's turn on it, wait until then."""
        restarted: List[int] = []
        failed: List[dict] = []

        def fail(device_id: int, exc: BaseException) -> None:
            if not isinstance(exc, EngineError):
                logger.error(
                    "restart of device %d failed: %s", device_id, exc,
                    exc_info=exc, extra={"device_id": device_id},
                )
            failed.append({"device_id": device_id, "error": str(exc)})

        held: Set[int] = set()
        async with contextlib.AsyncExitStack() as stack:
            # One lock per device in id order, held across both phases;
            # every other caller holds one lock at a time, so no deadlock.
            for device_id in self._device_ids_with_status("running"):
                await stack.enter_async_context(self._lock_for(device_id))
                # A row this parent doesn't track: adopt its live process (so
                # the stop below frees its port) or mark it crashed. psutil
                # can refuse (AccessDenied): skip that one device.
                try:
                    await self._reconcile_untracked(
                        device_id, missing_error="process was not found (no server was tracking it)"
                    )
                except Exception as exc:
                    fail(device_id, exc)
                else:
                    held.add(device_id)
            # Re-read under the locks: a device stopped, or found crashed,
            # since is left as it is now.
            to_restart: List[int] = []
            for device_id in self._device_ids_with_status("running"):
                if device_id not in held or device_id not in self.processes:
                    continue  # started after the first read, or judged above
                try:
                    self._refuse_while_unjudged(device_id)
                except EngineError as exc:
                    fail(device_id, exc)
                else:
                    to_restart.append(device_id)
            # remember=True: a parent that dies before the starts below
            # brings these devices back at its next startup.
            results = await asyncio.gather(
                *(self._stop_locked(device_id, remember=True) for device_id in to_restart),
                return_exceptions=True,
            )
            stopped: List[int] = []
            for device_id, result in zip(to_restart, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    fail(device_id, result)
                    await self._forget_resume(device_id)
                else:
                    stopped.append(device_id)
            for device_id in stopped:
                try:
                    await self._start_locked(device_id)
                except Exception as exc:
                    fail(device_id, exc)
                    await self._forget_resume(device_id)
                else:
                    restarted.append(device_id)
        failed.sort(key=lambda f: f["device_id"])
        return {"restarted": restarted, "failed": failed}

    def _device_ids_with_status(self, status: str) -> List[int]:
        with self.session_factory() as session:
            return [
                d.id
                for d in session.query(Device).order_by(Device.id)
                if d.runtime.process_status == status
            ]

    async def _forget_resume(self, device_id: int) -> None:
        """A device restart_all couldn't bring back isn't restarted
        unasked at the next startup either."""
        try:
            await self._set_resume(device_id, False)
        except Exception:
            logger.exception(
                "device %d: could not clear its restart-at-startup flag", device_id,
                extra={"device_id": device_id},
            )

    async def stop_all_for_shutdown(self) -> None:
        """Stop every tracked device in parallel and remember it, for a
        requested parent shutdown (service stop, SIGTERM, Ctrl+C). Each stop
        takes the device's lock, so one mid-start finishes first. Every
        device is flagged before any stop begins, so a shutdown cut short
        (stop timeout, OS shutdown window, power cut) still restores it."""
        device_ids = list(self.processes)
        for device_id in device_ids:
            await self._set_resume(device_id, True)
        results = await asyncio.gather(
            *(self.stop(device_id, remember=True) for device_id in device_ids),
            return_exceptions=True,
        )
        for device_id, result in zip(device_ids, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "device %d could not be stopped at shutdown: %s", device_id, result,
                    extra={"device_id": device_id},
                )

    def _remembered_device_ids(self) -> List[int]:
        with self.session_factory() as session:
            return [
                row[0]
                for row in session.query(DeviceRuntime.device_id)
                .filter(DeviceRuntime.resume_on_start.is_(True))
                .order_by(DeviceRuntime.device_id)
                .all()
            ]

    async def resume_remembered_devices(
        self,
        *,
        retry_seconds: float = RESUME_RETRY_SECONDS,
        give_up_seconds: float = RESUME_GIVE_UP_SECONDS,
    ) -> None:
        """Startup task: start every remembered device that isn't running,
        retrying failures (an address that isn't up yet after a boot) until
        give_up_seconds; a device that never starts stays "crashed" with its
        last error and remembered for the next startup."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + give_up_seconds
        while True:
            for device_id in self._remembered_device_ids():
                async with self._lock_for(device_id):
                    if (
                        device_id in self.processes
                        or device_id in self._unjudged
                        or device_id not in self._remembered_device_ids()
                    ):
                        continue
                    try:
                        await self._start_locked(device_id)
                    except Exception as exc:
                        await self._mark_crashed(device_id, str(exc))
                        logger.warning(
                            "device %d could not be restarted: %s", device_id, exc,
                            extra={"device_id": device_id},
                        )
            pending = [d for d in self._remembered_device_ids() if d not in self.processes]
            if not pending or loop.time() >= deadline:
                return
            await asyncio.sleep(retry_seconds)

    async def _reconcile_untracked(
        self, device_id: int, *, missing_error: str, remember_missing: bool = False
    ) -> None:
        """One row that claims a live child this parent doesn't track: alive
        and the same process -> track it; gone and "reinitializing" -> respawn
        (it didn't crash, its parent did); gone and "running" or "starting"
        with remember_missing -> stopped and remembered (a power-off: no clean
        stop happened); gone otherwise -> crashed with `missing_error`. Caller
        holds this device's lock."""
        if device_id in self.processes:
            return
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is None or device.runtime.process_status not in _UNTRACKED_STATUSES:
                return
            pid = device.runtime.process_pid
            started_at = device.runtime.process_started_at
            status = device.runtime.process_status
            boot_id = device.runtime.process_boot_id
            since_boot = device.runtime.process_started_since_boot

        if pid is not None and started_at is not None and _is_same_process(pid, started_at, boot_id, since_boot):
            self._unjudged.discard(device_id)
            self.processes[device_id] = _PsutilProcessHandle(pid)
            logger.info(
                "reattached to already-running device %d (pid %d)",
                device_id,
                pid,
                extra={"device_id": device_id},
            )
            return
        if status == "starting" and pid is None and time.monotonic() < self._grace_until:
            if device_id not in self._unjudged:
                logger.info(
                    "device %d: starting row without a process id; waiting for its child to report",
                    device_id,
                    extra={"device_id": device_id},
                )
            self._unjudged.add(device_id)
            return
        was_unjudged = device_id in self._unjudged
        self._unjudged.discard(device_id)
        if status == "reinitializing":
            try:
                await self._start_locked(device_id)
            except Exception as exc:
                # Any failure, not just EngineError; re-raised so the
                # caller's guard logs it.
                await self._mark_crashed(device_id, str(exc))
                raise
            return
        if (remember_missing or was_unjudged) and status in _LIVE_STATUSES:
            await self._mark_stopped(device_id, remember=True)
            logger.info(
                "device %d: no live process found, will be restarted",
                device_id,
                extra={"device_id": device_id},
            )
            if was_unjudged:
                # The resume task may be done by now, so restart it here.
                try:
                    await self._start_locked(device_id)
                except Exception as exc:
                    await self._mark_crashed(device_id, str(exc))
                    raise
            return
        await self._mark_crashed(device_id, missing_error)
        logger.warning(
            "device %d marked crashed: no live process found",
            device_id,
            extra={"device_id": device_id},
        )

    async def _adopt_untracked_devices(self) -> None:
        """Track a child that wrote "running" after the one-time reattach pass
        (the parent restarted while it was starting). Untracked, Stop would
        just mark it stopped, leaving it serving and holding its port. A
        device whose lock is held is mid-start/stop and is left alone."""
        with self.session_factory() as session:
            device_ids = [
                row[0]
                for row in session.query(DeviceRuntime.device_id)
                .filter(DeviceRuntime.process_status.in_(_UNTRACKED_STATUSES))
                .all()
            ]
        self._unjudged.intersection_update(device_ids)
        for device_id in device_ids:
            lock = self._lock_for(device_id)
            if device_id in self.processes or lock.locked():
                continue
            try:
                async with lock:
                    await self._reconcile_untracked(
                        device_id, missing_error="process was not found (no server was tracking it)"
                    )
            except Exception:
                logger.exception("health check failed for device %d", device_id, extra={"device_id": device_id})

    def _record_death_now(self, device_id: int) -> Optional[str]:
        """Mark a dead child's row crashed if it still claimed to be live;
        returns the status it had."""
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            status_at_death = device.runtime.process_status if device is not None else None
            if device is not None and status_at_death in _LIVE_STATUSES:
                device.runtime.process_status = "crashed"
                device.runtime.process_error = (
                    device.runtime.process_error or "process exited unexpectedly"
                )
                device.runtime.process_pid = None
                _fold_pending_clock(device)
                session.commit()
        return status_at_death

    async def run_health_check_task(
        self, *, interval_seconds: float = HEALTH_CHECK_INTERVAL_SECONDS
    ) -> None:
        """Parent-lifetime task (started from the FastAPI lifespan). Marks a
        device "crashed" the tick after its process dies outside stop();
        the per-device lock keeps each pass from racing a start()/stop().
        Also adopts untracked running children (_adopt_untracked_devices).
        """
        while True:
            await asyncio.sleep(interval_seconds)
            for device_id in list(self.processes):
                try:
                    async with self._lock_for(device_id):
                        process = self.processes.get(device_id)
                        if process is None or process.is_alive():
                            continue
                        # Commit BEFORE popping self.processes: the commit can
                        # fail (N children plus the parent write one SQLite
                        # file), and popping first would strand the row at
                        # "running" with no tick left to retry it.
                        try:
                            status_at_death = await _save_off_loop(self._record_death_now, device_id)
                        except asyncio.CancelledError as exc:
                            if exc.__cause__ is None:
                                self.processes.pop(device_id, None)
                            raise
                        self.processes.pop(device_id, None)
                        # A device that wrote "reinitializing" before exiting
                        # (ReinitializeDevice, application.py) is respawned.
                        # _start_locked, not restart(): the lock is already
                        # held and restart() would deadlock on it.
                        if status_at_death == "reinitializing":
                            # Any failure (no or colliding bind address, an
                            # OSError from the spawn, SQLite) must be recorded:
                            # there's no caller to return it to and the row is
                            # untracked, so it would sit at "reinitializing"
                            # forever. Re-raised so the guard below logs it.
                            try:
                                await self._start_locked(device_id)
                            except Exception as exc:
                                await self._mark_crashed(device_id, str(exc))
                                raise
                except Exception:
                    logger.exception("health check failed for device %d", device_id, extra={"device_id": device_id})
            # Guarded on its own: an exception escaping here (a locked
            # database) would end the health check task for good.
            try:
                await self._adopt_untracked_devices()
            except Exception:
                logger.exception("health check could not look for untracked devices")

    async def reattach_running_devices(self) -> None:
        """Called once at parent startup: for every row left "running",
        resume tracking its still-alive child (daemon=False), or, if it's gone,
        mark it "stopped" and remembered so resume_remembered_devices restarts
        it.

        Also covers "reinitializing" rows, orphaned when the parent dies
        mid-respawn in run_health_check_task: alive is reattached, gone is
        respawned rather than marked crashed -- it didn't crash, its parent did.
        """
        with self.session_factory() as session:
            device_ids = [
                row[0]
                for row in session.query(DeviceRuntime.device_id)
                .filter(DeviceRuntime.process_status.in_(_UNTRACKED_STATUSES))
                .all()
            ]

        for device_id in device_ids:
            # Per-device guard: psutil can raise AccessDenied (not a
            # NoSuchProcess), or the process can exit between the identity
            # check and _PsutilProcessHandle's lookup. One bad row must not
            # abort reattaching the others.
            try:
                async with self._lock_for(device_id):
                    await self._reconcile_untracked(
                        device_id,
                        missing_error="process was not found on parent restart",
                        remember_missing=True,
                    )
            except Exception:
                logger.exception("reattach failed for device %d", device_id, extra={"device_id": device_id})
