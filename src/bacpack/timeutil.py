"""Time helpers.

Two kinds of time live here; the third, the device clock (host time plus
an offset), is device_clock.py's.

Host time: every timestamp column is stored as naive UTC (SQLite's
CURRENT_TIMESTAMP writes that form, and so does BACpack). "db" in a name
means that stored form. Convert with to_db/from_db at the boundary, never
by adding or dropping tzinfo by hand: SQLAlchemy's SQLite DateTime drops
tzinfo on write without converting, and a naive value's .timestamp()
assumes local time.

BACnet encoding: a Date is (year - 1900, month, day, day_of_week) with 255
as the wildcard in any field.

tests/test_time_helpers_guard.py refuses the raw primitives these replace
anywhere outside this module and device_clock.py.
"""
from __future__ import annotations

import datetime as dt
import time as _time
from typing import Optional

from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time

UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    """Aware UTC now."""
    return dt.datetime.now(UTC)


def host_seconds() -> float:
    """Seconds since the epoch on the host clock, ignoring any device clock
    offset. Value simulators run on this on purpose: a device clock change
    (even a 2 s time sync) would otherwise jump every simulated wave."""
    return _time.time()


def db_utc_now() -> dt.datetime:
    """Naive UTC now, the stored form."""
    return to_db(utc_now())


def to_db(value: dt.datetime) -> dt.datetime:
    """An aware datetime as the stored form (naive UTC). A naive argument
    is refused: it could be local time, and storing it would shift it by
    the host's UTC offset."""
    if value.tzinfo is None:
        raise ValueError("to_db needs an aware datetime; a naive one may be local time")
    return value.astimezone(UTC).replace(tzinfo=None)


def from_db(value: dt.datetime) -> dt.datetime:
    """A stored value (naive UTC) as aware UTC. An aware value is converted
    to UTC, so callers needn't know which they hold."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def db_cutoff(age: dt.timedelta) -> dt.datetime:
    """Naive UTC `now - age`, for comparing against a stored column."""
    return db_utc_now() - age


def utc_from_timestamp(seconds: float) -> dt.datetime:
    """Aware UTC from a POSIX timestamp."""
    return dt.datetime.fromtimestamp(seconds, tz=UTC)


def iso_utc(value: Optional[dt.datetime] = None, timespec: str = "auto") -> str:
    """ISO 8601 with an explicit +00:00, for the API: a value without an
    offset is read as local time by the frontend's Date.parse(). A naive
    value is taken as stored UTC; no value means now."""
    return from_db(utc_now() if value is None else value).isoformat(timespec=timespec)


def host_local(value: dt.datetime) -> dt.datetime:
    """An aware datetime as naive host-local wall time, the device's own
    zone (device_clock.utc_offset_minutes) -- the form a BACnet DateTime
    carries."""
    if value.tzinfo is None:
        raise ValueError("host_local needs an aware datetime")
    return value.astimezone().replace(tzinfo=None)


# Every zone's UTC offset lies within this of zero (-12:00 to +14:00).
MAX_UTC_OFFSET = dt.timedelta(hours=14)


def host_utc_offsets_near(wall: dt.datetime, hours: int = 48) -> tuple[dt.timedelta, dt.timedelta]:
    """The smallest and largest UTC offset the host zone uses within `hours`
    of naive host-local `wall`, sampled hourly. An instant shown within a
    day of `wall` lies in that span, so it had one of these offsets. Where
    the platform can't convert (Windows refuses dates before 1970) it falls
    back to every offset in use."""
    centre = wall.replace(tzinfo=UTC)
    try:
        offsets = [
            (centre + dt.timedelta(hours=h)).astimezone().utcoffset() for h in range(-hours, hours + 1)
        ]
    except (OverflowError, OSError, ValueError):
        return -MAX_UTC_OFFSET, MAX_UTC_OFFSET
    return min(offsets), max(offsets)


# --- BACnet Date/Time encoding -------------------------------------------

WILDCARD = 255
# One octet holds year - 1900, with 255 reserved for the wildcard.
BACNET_MIN_YEAR = 1900
BACNET_MAX_YEAR = 2154
_YEAR_BASE = 1900


def bacnet_year(year: int) -> int:
    """A calendar year as a BACnet Date's year octet. No range check:
    callers validate the year (objects/calendar_config.py)."""
    return year - _YEAR_BASE


def python_year(octet: int) -> int:
    """A BACnet Date's (non-wildcard) year octet as a calendar year."""
    return octet + _YEAR_BASE


def bacnet_date(value: dt.date) -> Date:
    """A concrete date, day of week wildcarded."""
    return Date((bacnet_year(value.year), value.month, value.day, WILDCARD))


def bacnet_date_fields(
    year: Optional[int], month: Optional[int], day: Optional[int], day_of_week: Optional[int] = None
) -> Date:
    """A date pattern; None in any field is the wildcard (e.g. every
    December 25th is (None, 12, 25), every Monday (None, None, None, 1))."""
    return Date(
        (
            WILDCARD if year is None else bacnet_year(year),
            WILDCARD if month is None else month,
            WILDCARD if day is None else day,
            WILDCARD if day_of_week is None else day_of_week,
        )
    )


def python_date(value) -> Optional[dt.date]:
    """A BACnet Date as a date. None when year, month and day are all
    wildcarded (an open end of a range); ValueError when only some are,
    since a pattern isn't a date. Day of week is ignored."""
    year, month, day, _day_of_week = tuple(value)
    fields = (year, month, day)
    if all(field == WILDCARD for field in fields):
        return None
    if WILDCARD in fields:
        raise ValueError(f"date {year}/{month}/{day} is partly wildcarded")
    return dt.date(python_year(year), month, day)


def wildcard_date() -> Date:
    """An all-wildcard Date (a new instance each call)."""
    return Date((WILDCARD, WILDCARD, WILDCARD, WILDCARD))


def wildcard_datetime() -> DateTime:
    """An all-wildcard DateTime, BACnet's "never" (a new instance each call)."""
    return DateTime(date=wildcard_date(), time=Time((WILDCARD, WILDCARD, WILDCARD, WILDCARD)))


def python_datetime(value: DateTime) -> dt.datetime:
    """A concrete BACnet DateTime as a naive datetime (device-local, like
    the DateTime itself). Day of week is ignored and an unspecified
    hundredths reads as 0 (bacpypes3's own Date.date refuses any special
    value, which would fail a ReadRange referenceTime with day of week 255).
    A wildcard in any other field raises ValueError."""
    year, month, day, _day_of_week = tuple(value.date)
    hour, minute, second, hundredths = tuple(value.time)
    if WILDCARD in (year, month, day, hour, minute, second):
        raise ValueError(f"date/time {tuple(value.date)} {tuple(value.time)} is partly wildcarded")
    return dt.datetime(
        python_year(year), month, day, hour, minute, second, 0 if hundredths == WILDCARD else hundredths * 10000
    )
