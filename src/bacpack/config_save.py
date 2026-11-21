"""Saves a device process's main-file change off its event loop: a commit
can wait out another process's write lock for the busy timeout, and on the
loop that wait stops all BACnet service on the device.

`save` is for a BACnet write's config change: the live object already holds
it, so a failed save is logged and the write still succeeds.
`run_in_order` hands the result or the
failure to a caller that handles it itself. Both run one at a time in the
order made (asyncio.Lock wakes waiters in turn), so two quick writes to one
property can't commit swapped. A history-file save takes no part in this
queue: it would wait behind main-file saves for no reason."""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Callable, TypeVar

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One per loop: an asyncio.Lock is bound to the loop that first waits on it.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


async def run_in_order(save: Callable[..., T], *args: Any) -> T:
    """Runs `save(*args)` in a worker thread after every save started before
    it; a failure propagates."""
    async with _lock():
        return await asyncio.to_thread(save, *args)


async def save(session_factory, change: Callable[[Session], None], what: str) -> None:
    """`change` edits rows in the session it's given; the commit follows."""
    try:
        await save_or_raise(session_factory, change)
    except Exception:
        logger.exception("could not save %s", what)


async def save_or_raise(session_factory, change: Callable[[Session], None]) -> None:
    """`save` for a caller that handles the failure itself."""
    await run_in_order(_commit, session_factory, change)


async def save_columns(session_factory, model, row_id: int, columns: dict[str, Any], what: str) -> None:
    def change(session: Session) -> None:
        row = session.get(model, row_id)
        if row is not None:
            for column, value in columns.items():
                setattr(row, column, value)

    await save(session_factory, change, what)


def _commit(session_factory, change: Callable[[Session], None]) -> None:
    with session_factory() as session:
        change(session)
        session.commit()
