"""Applying a device clock change: the one path every change takes, from a
BACnet time sync or the web UI. Runs inside the device's own process.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import weakref
from typing import Optional

from . import config_save, device_clock, timeutil
from .models import Device
from .objects.schedule import reschedule_all_schedules
from .trending import record_time_change
from .trending import write_lock as trend_write_lock

logger = logging.getLogger(__name__)

# BACpack's own choice, not ASHRAE 135's: a jump smaller than this still
# moves the clock but writes no Trend Log time-change record, so a BMS's
# periodic sub-second re-syncs don't flood every log.
TIME_CHANGE_RECORD_THRESHOLD_SECONDS = 1.0


# One per loop, like config_save's: an asyncio.Lock binds to the loop that
# first waits on it.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


async def settle() -> None:
    """Returns once every clock change applied so far has recorded and saved."""
    async with _lock():
        pass


async def apply_clock_offset(session_factory, app, device_id: int, new_offset: float, source: str) -> float:
    """Move this device's clock to `new_offset` seconds from host time and
    return the jump. The clock and the schedules (otherwise each keeps a
    stale value until a timer armed on the old clock fires) move before any
    save is awaited. A Trend Log time-change record is written for a big
    enough jump, and the offset is saved, both in worker threads, one change
    at a time in the order applied: a later change must not save its offset
    first. The record also takes trending's write lock, as every sample
    write does: two records in parallel collide on the log's sequence
    numbers. It is a history-file write, so it never joins config_save's
    queue.
    `app` or `session_factory` None (tests) skips those parts."""
    old_offset = device_clock.offset()
    delta = new_offset - old_offset
    device_clock.set_offset(new_offset)

    if app is not None:
        reschedule_all_schedules(app)

    if session_factory is not None:
        async with _lock():
            if abs(delta) >= TIME_CHANGE_RECORD_THRESHOLD_SECONDS:
                async with trend_write_lock():
                    await asyncio.to_thread(record_time_change, session_factory, device_id, delta, new_offset)
                if app is not None:
                    app.trend_records_added()

            def save_offset(session) -> None:
                device = session.get(Device, device_id)
                if device is not None:
                    device.clock_offset_seconds = new_offset

            await config_save.save_or_raise(session_factory, save_offset)

    logger.info(
        "device %d clock set by %s: offset %+.2fs -> %+.2fs (jump %+.2fs)",
        device_id, source, old_offset, new_offset, delta,
    )
    return delta


def bacnet_datetime_fields(value) -> Optional[tuple]:
    """(year, month, day, hour, minute, second, hundredths) from a BACnet
    DateTime, or None if anything the clock needs is unspecified (255), a
    special value (month 13/14, day 32-34), or not a real instant (February
    30th, hour 24), which mktime/timegm would silently roll forward. An
    unspecified hundredths reads as 0."""
    year, month, day, _day_of_week = tuple(value.date)
    hour, minute, second, hundredths = tuple(value.time)
    if timeutil.WILDCARD in (year, month, day, hour, minute, second):
        return None
    if month > 12 or day > 31:
        return None
    try:
        _dt.datetime(timeutil.python_year(year), month, day, hour, minute, second)
    except ValueError:
        return None
    return (
        timeutil.python_year(year), month, day, hour, minute, second,
        0 if hundredths == timeutil.WILDCARD else hundredths,
    )
