"""Shared pieces of the scale tests: the fixed
finding lines, a loop-lag ticker, a statement recorder with a query-plan
scan check, and the collection switch."""
from __future__ import annotations

import asyncio
import gc
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

SCALE_DIR = Path(__file__).resolve().parent

# The user's fixed lines: a measured figure past one is a finding.
LOOP_BLOCK_LIMIT = 0.25
REQUEST_LIMIT = 2.0
BIG_JOB_LIMIT = 60.0


@contextmanager
def collector_off():
    """For a device-loop gap check. A device process freezes its heap
    (gc_freeze); this test process holds every earlier test's leftovers,
    unfrozen, and a full collection of them takes 0.3-0.5 s on a 1-vCPU Linux VM,
    alone enough to pass the line. gc_probe measures a device's own."""
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


class LoopLagMonitor:
    """Records the longest gap between ticks of a task on the running loop,
    beyond the tick interval: a call that blocks the loop shows up whole."""

    def __init__(self, tick: float = 0.01) -> None:
        self.tick = tick
        self.max_gap = 0.0
        self._last = 0.0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        self._last = time.perf_counter()
        while True:
            await asyncio.sleep(self.tick)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - self._last - self.tick)
            self._last = now

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            # A block that lasted until now hasn't been seen by a tick yet.
            self.max_gap = max(self.max_gap, time.perf_counter() - self._last - self.tick)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def __aenter__(self) -> "LoopLagMonitor":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    @classmethod
    def on_portal(cls, portal, tick: float = 0.01) -> "_PortalMonitor":
        return _PortalMonitor(cls(tick), portal)


class _PortalMonitor:
    """The same ticker on the loop a TestClient's portal runs (the web UI's)."""

    def __init__(self, monitor: LoopLagMonitor, portal) -> None:
        self._monitor = monitor
        self._portal = portal

    def __enter__(self) -> LoopLagMonitor:
        self._portal.call(self._monitor.start)
        return self._monitor

    def __exit__(self, *exc) -> None:
        self._portal.call(self._monitor.stop)


_STEP_EVERY = 100


class StatementRecorder:
    """The statements an engine runs, and `vm_steps`: SQLite virtual-machine
    instructions they executed (to within _STEP_EVERY per statement). Steps
    grow with the rows a statement reads, which a plan check can't see: a
    COUNT over an index range is a SEARCH, yet touches every row in it."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self.statements: list[tuple[str, Any]] = []
        self.vm_steps = 0
        self._connections: list = []

    def _tick(self) -> int:
        self.vm_steps += _STEP_EVERY
        return 0

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append((statement, parameters))
        raw = conn.connection.dbapi_connection
        if not any(raw is seen for seen in self._connections):
            self._connections.append(raw)
            raw.set_progress_handler(self._tick, _STEP_EVERY)

    def __enter__(self) -> "StatementRecorder":
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)
        for raw in self._connections:
            raw.set_progress_handler(None, 0)
        self._connections.clear()


_SCAN = re.compile(r"\bSCAN (?:TABLE )?(?:\w+\.)?(\w+)(?: [A-Za-z_]\w*)?$")
_PLANNED = ("SELECT", "DELETE", "UPDATE", "WITH")


def table_scans(engine, statements, tables: set[str]) -> list[str]:
    """Plan lines that scan a named table in full (a SCAN ... USING INDEX
    walks an index and is not one)."""
    found: list[str] = []
    with engine.connect() as connection:
        for sql, params in statements:
            if not sql.lstrip().upper().startswith(_PLANNED):
                continue
            if isinstance(params, list):
                continue
            rows = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params or ())
            for row in rows:
                detail = str(row[-1])
                match = _SCAN.search(detail)
                if match and match.group(1) in tables:
                    found.append(detail)
    return found


# A request whose database work, measured at a table's tenth and at its full
# size, rose by more than this factor (plus slack for fixed per-request work)
# does work that grows with the table.
WORK_GROWTH_FACTOR = 3
WORK_SLACK_STEPS = 5000


def grew_with_table(small_steps: int, big_steps: int) -> bool:
    return big_steps > WORK_GROWTH_FACTOR * small_steps + WORK_SLACK_STEPS


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - start, result


async def timed_async(coro):
    start = time.perf_counter()
    result = await coro
    return time.perf_counter() - start, result


def scale_collection(config, items) -> None:
    """Marks everything under tests/scale/ and skips it unless --scale; the
    timings are meaningless with xdist workers fighting for the disk."""
    scale = config.getoption("scale", False)
    if scale and config.pluginmanager.hasplugin("xdist") and config.getoption("numprocesses", None):
        pytest.exit("--scale runs sequentially: add -p no:xdist", returncode=4)
    skip = pytest.mark.skip(reason="scale test: run with --scale")
    for item in items:
        if SCALE_DIR in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.scale)
            if not scale:
                item.add_marker(skip)
