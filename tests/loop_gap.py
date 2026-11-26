"""Runs a coroutine while a second connection holds a SQLite file's write
lock, the way a device process or a prune does, and measures the longest gap
in a ticker on the same event loop. A save made on the loop freezes it for
as long as the lock is held."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bacpack.db import history_db_path

LOCK_SECONDS = 1.0
# A save on the loop stalls it for nearly all of LOCK_SECONDS (about 0.95 s).
# A shared CI runner's VM can stall it 0.4 s on its own, so the bound sits
# between the two rather than near zero.
MAX_GAP_SECONDS = 0.6
TICK_SECONDS = 0.02


@dataclass
class LockedRun:
    worst_gap: float
    elapsed: float
    result: Any


async def run_while_locked(
    session_factory,
    make_coro: Callable[[], Awaitable[Any]],
    *,
    history: bool = False,
    seconds: float = LOCK_SECONDS,
) -> LockedRun:
    path = session_factory.kw["bind"].url.database
    if history:
        path = str(history_db_path(path))
    locked = threading.Event()

    def hold() -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(seconds)
            connection.execute("COMMIT")
        finally:
            connection.close()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    await asyncio.to_thread(locked.wait)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def tick() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(TICK_SECONDS)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.ensure_future(tick())
    # Let the ticker take its first reading, or a call that blocks at once
    # runs before it and the stall goes unmeasured.
    await asyncio.sleep(TICK_SECONDS * 2)
    started = time.perf_counter()
    try:
        result = await make_coro()
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        await ticker
        await asyncio.to_thread(holder.join)
    return LockedRun(max(gaps, default=0.0), elapsed, result)


def assert_waited_off_the_loop(run: LockedRun, seconds: float = LOCK_SECONDS) -> None:
    # The elapsed check proves the save really waited on the lock, so a
    # small gap means it waited off the loop rather than not at all.
    assert run.elapsed >= seconds * 0.8, f"finished in {run.elapsed:.2f} s: never waited on the lock"
    assert run.worst_gap < MAX_GAP_SECONDS, f"event loop stalled {run.worst_gap:.2f} s"
