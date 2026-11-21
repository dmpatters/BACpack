"""A calendar entry's saved form: validation, and building it to and from a
BACnet CalendarEntry. Shared by calendars and schedule exceptions; no object
classes, so object_config can import it."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from bacpypes3.basetypes import CalendarEntry, DateRange, WeekNDay

from .. import timeutil


def parse_schedule_date(value: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a valid YYYY-MM-DD date") from None
    # A BACnet Date can't hold other years: 2155 would build as the year
    # wildcard, which python_date then refuses on every later wire write.
    if not timeutil.BACNET_MIN_YEAR <= parsed.year <= timeutil.BACNET_MAX_YEAR:
        raise ValueError(
            f"{value!r}: the year must be between {timeutil.BACNET_MIN_YEAR} and {timeutil.BACNET_MAX_YEAR}"
        )
    return parsed


def _validate_wildcard_date_component(value, lo: int, hi: int, label: str, extra: tuple = ()) -> None:
    """`value` is "*" (or an `extra` word like "last"), None (an explicit
    JSON null, same as "*"), or an int in [lo, hi]."""
    if value is None or value == "*" or value in extra:
        return
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is None or not (lo <= numeric <= hi):
        raise ValueError(f"{label} {value!r} must be between {lo} and {hi}, or '*'")


def validate_calendar_entry(entry: Optional[dict]) -> None:
    kind = entry.get("kind") if entry else None
    if kind == "date":
        _validate_wildcard_date_component(
            entry.get("year", "*"), timeutil.BACNET_MIN_YEAR, timeutil.BACNET_MAX_YEAR, "year"
        )
        _validate_wildcard_date_component(entry.get("month", "*"), 1, 12, "month")
        _validate_wildcard_date_component(entry.get("day", "*"), 1, 31, "day")
        _validate_wildcard_date_component(entry.get("day_of_week", "*"), 1, 7, "day_of_week")
    elif kind == "date-range":
        parse_schedule_date(entry.get("start"))  # pyright: ignore[reportArgumentType] -- a missing date is the ValueError it raises
        parse_schedule_date(entry.get("end"))  # pyright: ignore[reportArgumentType] -- a missing date is the ValueError it raises
    elif kind == "week-and-day":
        _validate_wildcard_date_component(entry.get("month", "*"), 1, 12, "week-and-day month")
        _validate_wildcard_date_component(
            entry.get("week_of_month", "*"), 1, 5, "week-and-day week_of_month", extra=("last",)
        )
        _validate_wildcard_date_component(entry.get("day_of_week", "*"), 1, 7, "week-and-day day_of_week")
    else:
        raise ValueError(f"{kind!r} is not a valid date_list/period entry kind")


def validate_calendar_config(date_list: Optional[list]) -> None:
    """Raises ValueError for any structurally invalid calendar config. No
    dates is valid: the calendar is simply never in effect (Clause 12.9)."""
    for entry in date_list or []:
        validate_calendar_entry(entry)


def _component(entry: dict, key: str):
    """A date component, null read as "*" (as validation does): the API
    stores null for every component a body leaves out."""
    value = entry.get(key)
    return "*" if value is None else value


def build_calendar_entry(entry: dict) -> CalendarEntry:
    kind = entry.get("kind")
    if kind == "date":
        year, month, day = _component(entry, "year"), _component(entry, "month"), _component(entry, "day")
        day_of_week = _component(entry, "day_of_week")
        return CalendarEntry(
            date=timeutil.bacnet_date_fields(
                None if year == "*" else int(year),
                None if month == "*" else int(month),
                None if day == "*" else int(day),
                None if day_of_week == "*" else int(day_of_week),
            )
        )
    if kind == "date-range":
        start = parse_schedule_date(entry["start"])
        end = parse_schedule_date(entry["end"])
        return CalendarEntry(
            dateRange=DateRange(
                startDate=timeutil.bacnet_date(start),
                endDate=timeutil.bacnet_date(end),
            )
        )
    if kind == "week-and-day":
        month, week, day_of_week = (
            _component(entry, "month"),
            _component(entry, "week_of_month"),
            _component(entry, "day_of_week"),
        )
        month_v = timeutil.WILDCARD if month == "*" else int(month)
        week_v = 6 if week == "last" else (timeutil.WILDCARD if week == "*" else int(week))
        dow_v = timeutil.WILDCARD if day_of_week == "*" else int(day_of_week)
        return CalendarEntry(weekNDay=WeekNDay(bytes([month_v, week_v, dow_v])))
    raise ValueError(f"{kind!r} is not a valid calendar entry kind")


def _range_end_iso(value) -> str:
    """A calendar date range's saved form needs both dates; a wildcard
    (whole or partial) is refused, and the write answers value-out-of-range."""
    parsed = timeutil.python_date(value)
    if parsed is None:
        raise ValueError("a calendar date range needs a concrete start and end date")
    return parsed.isoformat()


def unbuild_calendar_entry(entry: CalendarEntry) -> dict:
    wildcard = timeutil.WILDCARD
    if entry.date is not None:
        year, month, day, day_of_week = entry.date
        saved = {
            "kind": "date",
            "year": "*" if year == wildcard else str(timeutil.python_year(year)),
            "month": "*" if month == wildcard else str(month),
            "day": "*" if day == wildcard else str(day),
        }
        # Left out when wildcarded, as an entry written without one is saved.
        if day_of_week != wildcard:
            saved["day_of_week"] = str(day_of_week)
        return saved
    if entry.dateRange is not None:
        return {
            "kind": "date-range",
            "start": _range_end_iso(entry.dateRange.startDate),
            "end": _range_end_iso(entry.dateRange.endDate),
        }
    if entry.weekNDay is not None:
        month_v, week_v, dow_v = entry.weekNDay
        return {
            "kind": "week-and-day",
            "month": "*" if month_v == wildcard else str(month_v),
            "week_of_month": "last" if week_v == 6 else ("*" if week_v == wildcard else str(week_v)),
            "day_of_week": "*" if dow_v == wildcard else str(dow_v),
        }
    raise ValueError(f"unrecognized CalendarEntry choice: {entry!r}")
