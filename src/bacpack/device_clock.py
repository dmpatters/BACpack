"""The per-process device clock.

Each running device is its own OS process (see engine.py), so "the device's
clock" is process-wide state: host time plus one offset in seconds.

install() swaps the `time` module attribute in bacpypes3's primitivedata
and basetypes (bacpypes3_compat.swap_clock), the two places its Date/Time/
DateTime.now bottom out in time.time(). That makes Local_Date/Local_Time,
schedule and calendar evaluation and every event time stamp follow the
device clock without overriding any bacpypes3 method; everything else
(asyncio, logging, SQLite, the traffic log) keeps real host time.
"""
from __future__ import annotations

import calendar
import datetime as dt
import time as _time
from typing import Optional

from . import bacpypes3_compat, timeutil

# A BACnet Date stores year-1900 in one octet with 255 reserved as the
# wildcard, so 1900+254 is the last real year.
MIN_YEAR = 1901
MAX_YEAR = 2154

_offset = 0.0


class _ShiftedTime:
    """The `time` module with .time() shifted by the device clock offset."""

    def time(self) -> float:
        return _time.time() + _offset

    def __getattr__(self, name):
        return getattr(_time, name)


_SHIFTED_TIME = _ShiftedTime()


def offset() -> float:
    return _offset


def set_offset(seconds: float) -> None:
    global _offset
    _offset = float(seconds)


def now() -> float:
    return _time.time() + _offset


def device_utc_now() -> dt.datetime:
    """The device's clock as aware UTC: host time plus the offset."""
    return timeutil.utc_from_timestamp(now())


def device_local_iso(stored: dt.datetime) -> str:
    """A stored device time (naive UTC, e.g. Device.last_restore_time) as
    host-local ISO 8601 with its offset -- the same form as clock_fields'
    device_time, which the UI shows with wallTime()."""
    return timeutil.from_db(stored).astimezone().isoformat(timespec="seconds")


def is_installed() -> bool:
    return bacpypes3_compat.clock_swapped()


def install() -> None:
    bacpypes3_compat.swap_clock(_SHIFTED_TIME)


def uninstall() -> None:
    """Restore the real `time` module and reset the offset. Also called
    after every test, since some tests run _serve_device in-process."""
    global _offset
    bacpypes3_compat.restore_clock()
    _offset = 0.0


def offset_for_local(year, month, day, hour, minute, second, hundredths=0) -> float:
    """The offset that makes the device's local wall time read the given
    time right now. Local means the host zone, which is the device's own
    zone (see utc_offset_minutes). ValueError if the host can't represent
    it (e.g. Windows' mktime rejects years before 1970)."""
    try:
        target = _time.mktime((year, month, day, hour, minute, second, 0, 0, -1))
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"device time {year}-{month}-{day} is not representable here") from exc
    return target + hundredths / 100 - _time.time()


def offset_for_utc(year, month, day, hour, minute, second, hundredths=0) -> float:
    """Same as offset_for_local, for a UTC wall time."""
    try:
        target = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"device time {year}-{month}-{day} is not representable here") from exc
    return target + hundredths / 100 - _time.time()


def is_representable(offset_seconds: float) -> bool:
    """Whether host time + offset_seconds is a device time this host can
    compute and a BACnet Date can hold. A clock outside this would make
    every later Date.now() in the device raise."""
    try:
        year = _time.localtime(_time.time() + offset_seconds).tm_year
    except (OverflowError, OSError, ValueError):
        return False
    return MIN_YEAR <= year <= MAX_YEAR


def utc_offset_minutes() -> int:
    """BACnet UTC_Offset: local *standard* time's offset from UTC in
    minutes, positive west of Greenwich -- the same sign convention as
    time.timezone (seconds west of UTC)."""
    return _time.timezone // 60


def is_dst() -> bool:
    """Whether the host zone's DST rule is in effect at the *device's*
    current time -- a device clock set into July reports DST even while
    the host is in January."""
    return _time.localtime(now()).tm_isdst > 0


def clock_fields(clock_offset_seconds: float, pending_clock_offset: Optional[float]) -> dict:
    """The clock half of the engine status payload and of PUT .../clock's
    response. device_time is host-local ISO 8601 with its UTC offset,
    computed from the given offset (not this process's own), so the parent
    process can report any device's clock."""
    offset_seconds = clock_offset_seconds or 0.0
    device_time = dt.datetime.fromtimestamp(
        _time.time() + offset_seconds, tz=dt.timezone.utc
    ).astimezone()
    return {
        "clock_offset_seconds": offset_seconds,
        "clock_change_pending": pending_clock_offset is not None,
        "device_time": device_time.isoformat(timespec="seconds"),
    }
