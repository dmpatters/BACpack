"""One read a second per running device.

Each tick DeviceWatch reads the device's runtime row once and from that alone
decides whether to keep serving, applies object edits (live_config.py),
applies queued writes (in a task beside the stop check), and tells the
runners to reload. SQLite triggers (migrations.TRIGGERS) bump change_seq on
config changes and queued writes, config_seq on config changes only, so a
queued write doesn't reload objects or runners. No IPC.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Iterable, NamedTuple, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# LEFT JOIN from devices: no row at all means the device was deleted.
_READ = text(
    "SELECT r.process_status, r.process_pid, r.change_seq, r.config_seq, r.pending_clock_offset "
    "FROM main.devices d LEFT JOIN main.device_runtime r ON r.device_id = d.id "
    "WHERE d.id = :device_id"
)


class WatchState(NamedTuple):
    status: Optional[str]
    pid: Optional[int]
    change_seq: Optional[int]
    config_seq: Optional[int]
    pending_clock: Optional[float]


class DeviceWatch:
    def __init__(
        self,
        session_factory,
        device_id: int,
        *,
        pending_writes,
        runners: Iterable = (),
        reloader=None,
        tick_seconds: float = 1.0,
        pid: Optional[int] = None,
        reloads_held: Callable[[], bool] = lambda: False,
    ) -> None:
        self.session_factory = session_factory
        self.device_id = device_id
        self.pending_writes = pending_writes
        self.runners = list(runners)
        self.reloader = reloader
        # True while a BACnet restore is being applied: the device restarts
        # into the restored config, so reloading it live first is wasted work.
        self.reloads_held = reloads_held
        self.tick_seconds = tick_seconds
        self.pid = os.getpid() if pid is None else pid
        # None until the first read, so the first tick applies anything
        # queued before the device started and loads both runners.
        self._seen: Optional[int] = None
        # The config_seq the reloader and runners were last told about, and
        # the apply_due in flight: it runs beside the stop check so a slow one
        # (proxy writes to an unreachable remote) can't hold up a stop.
        self._marked: Optional[int] = None
        self._apply: Optional[asyncio.Task] = None

    def read(self) -> Optional[WatchState]:
        """The device's runtime row, or None if the device is gone."""
        with self.session_factory() as session:
            row = session.execute(_READ, {"device_id": self.device_id}).first()
        return None if row is None else WatchState(*row)

    async def run(self) -> bool:
        """Serve until told to stop. True for a clean stop -- a user stop or
        a deleted device, the only ones that relinquish remote points."""
        try:
            while True:
                await asyncio.sleep(self.tick_seconds)
                outcome = await self.step()
                if outcome is not None:
                    return outcome
        finally:
            await self._cancel_apply()

    async def step(self) -> Optional[bool]:
        """One tick: None to keep serving, else run()'s return value."""
        try:
            state = self.read()
        except Exception:
            logger.exception("device %d: stop-status check failed", self.device_id)
            return None
        # A deleted Device row counts as "stopping". A ReinitializeDevice
        # restart doesn't relinquish: a real controller doesn't on reboot, and
        # the restarted process re-writes at once.
        if state is None or state.status in ("stopping", "reinitializing"):
            await self._cancel_apply()
            return state is None or state.status == "stopping"
        if state.change_seq is None:
            # A devices row without its runtime row (only a hand-edited
            # file; init_db restores it at the next start): nothing to act on.
            return None
        # The row names another process (crashed, or reset for a replacement):
        # serving on would answer on the replacement's address/port. Stop and
        # leave the row alone.
        if state.pid != self.pid:
            logger.warning(
                "device %d: row no longer names this process (pid %d), exiting",
                self.device_id,
                self.pid,
            )
            await self._cancel_apply()
            return False
        if state.config_seq != self._marked and not self.reloads_held():
            # Objects first, so the runners
            # and the queued-write task find an object added in the same change.
            handled = self.reloader is None or await self.reloader.apply(state.change_seq)
            for runner in self.runners:
                runner.mark_changed()
            if handled:
                self._marked = state.config_seq
        if self._apply is not None:
            if not self._apply.done():
                return None  # one at a time; a change seen meanwhile is applied after it
            self._finish_apply()
        if state.change_seq != self._seen or state.pending_clock is not None:
            self._apply = asyncio.ensure_future(self._apply_for(state.change_seq))
            await asyncio.wait({self._apply}, timeout=self.tick_seconds)
            if self._apply.done():
                self._finish_apply()
        return None

    async def _apply_for(self, change_seq: int) -> int:
        await self.pending_writes.apply_due()
        return change_seq

    def _finish_apply(self) -> None:
        task, self._apply = self._apply, None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Not remembered as seen, so the next tick retries.
            logger.error("pending-write poller tick failed for device %d", self.device_id, exc_info=exc)
            return
        self._seen = task.result()

    async def _cancel_apply(self) -> None:
        """Stopping: a write still being applied is abandoned. Its slot stays
        queued (compare-and-clear never ran), harmless for a stopping device.
        One that already finished is collected, so its failure is logged."""
        task = self._apply
        if task is None:
            return
        if task.done():
            self._finish_apply()
            return
        self._apply = None
        task.cancel()
        await asyncio.wait({task}, timeout=self.tick_seconds)
