"""Schedule config helpers: parse, validate, normalize, build and unbuild a
schedule's stored config. Pure functions; the object classes are in
objects/schedule.py.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from bacpypes3.basetypes import (
    BinaryPV,
    DailySchedule,
    DateRange,
    DeviceObjectPropertyReference,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import Enumerated, Null, PropertyIdentifier, Real, Time, Unsigned

from .. import timeutil
from ..models import MAX_INSTANCE, OBJECT_TYPES
from ..point_config import finite_float
from ..bacnet_client import Point, normalize_object_type, normalize_property
from ..choices import SCHEDULE_VALUE_TYPES, enum_name
from .calendar_config import (
    build_calendar_entry,
    parse_schedule_date,
    unbuild_calendar_entry,
    validate_calendar_entry,
)


# --- Validation and construction --------------------------------------------
# validate_schedule_config is a pure structural check (no DB), run through
# object_config.check_object. Whether write targets and calendar references
# exist needs the whole device: that's object_config.check_device's job.


def _parse_schedule_time(value: str) -> Time:
    """Parse "HH:MM" (24-hour) into a Time; minute resolution only."""
    try:
        parsed = dt.datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a valid HH:MM schedule time") from None
    return Time((parsed.hour, parsed.minute, 0, 0))


def _validate_schedule_value_string(
    value_type: str, value: str, number_of_states: Optional[int]
) -> None:
    """Raises ValueError if `value` isn't a legal value string for
    `value_type` (the relinquish_default convention, keyed by value family).
    """
    if value_type == "analog":
        try:
            finite_float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{value!r} is not a valid analog schedule value") from None
    elif value_type == "binary":
        if value not in ("active", "inactive"):
            raise ValueError(
                f"{value!r} is not a valid binary schedule value (must be 'active' or 'inactive')"
            )
    elif value_type == "multi-state":
        try:
            state = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{value!r} is not a valid multi-state schedule value") from None
        if state < 1 or (number_of_states is not None and state > number_of_states):
            raise ValueError(f"{value!r} is out of range for this schedule's states")
    else:
        raise ValueError(f"{value_type!r} is not a valid schedule_value_type")


def schedule_atomic_value(value_type: str, value: Optional[str]):
    """Cast a validated value string to Real/BinaryPV/Unsigned; None is Null."""
    if value is None:
        return Null(())
    if value_type == "analog":
        return Real(float(value))
    if value_type == "binary":
        return BinaryPV(value)
    return Unsigned(int(value))


def _validate_time_value_list(
    entries: List[dict], value_type: str, number_of_states: Optional[int]
) -> None:
    seen: set = set()
    for entry in entries:
        parsed = _parse_schedule_time(entry.get("time"))
        key = (parsed[0], parsed[1])
        if key in seen:
            raise ValueError(f"duplicate schedule time {entry.get('time')!r}")
        seen.add(key)
        value = entry.get("value")
        if value is not None:
            _validate_schedule_value_string(value_type, value, number_of_states)


def _validate_schedule_period(period: Optional[dict]) -> None:
    if not period:
        raise ValueError("exception event requires a period")
    if period.get("kind") == "calendar-reference":
        if not isinstance(period.get("instance_number"), int):
            raise ValueError("calendar-reference period requires an integer instance_number")
        return
    validate_calendar_entry(period)


# --- External write targets -------------
# A target with a `device_instance` is in another device (ExternalScheduleWriter
# sends it). One naming the owning device is local, and every save path
# normalizes it away; safe once on save, as the device instance never changes.


def is_external_target(target: dict) -> bool:
    return target.get("device_instance") is not None


def normalize_write_targets(targets, own_device_instance: Optional[int]):
    """A copy of `targets`: local ones without `device_instance` and with a
    camelCase property; external ones' names in kebab-case (as status keys
    and Point use). Anything unparsable is left for validate_schedule_config
    to reject."""
    if not isinstance(targets, list):
        return targets
    normalized = []
    for target in targets:
        if not isinstance(target, dict):
            normalized.append(target)
            continue
        target = dict(target)
        if target.get("device_instance") is None or target["device_instance"] == own_device_instance:
            target.pop("device_instance", None)
            prop = target.get("property_identifier")
            if isinstance(prop, str):
                target["property_identifier"] = enum_name(PropertyIdentifier, prop, camel=True) or prop
        else:
            try:
                target["object_type"] = normalize_object_type(str(target.get("object_type")))
                target["property_identifier"] = normalize_property(
                    str(target.get("property_identifier") or "present-value")
                )
            except ValueError:
                pass
        normalized.append(target)
    return normalized


def _is_instance(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INSTANCE


def _validate_external_write_target(target: dict) -> None:
    device_instance = target.get("device_instance")
    if not _is_instance(device_instance):
        raise ValueError(f"write target device_instance {device_instance!r} must be an integer 0-{MAX_INSTANCE}")
    object_type = normalize_object_type(str(target.get("object_type")))  # ValueError: "unknown object type ..."
    normalize_property(str(target.get("property_identifier") or "present-value"))
    if object_type in ("schedule", "calendar"):
        # Their Present_Value is calculated; a write never takes.
        raise ValueError(f"{object_type!r} is not a valid schedule_write_targets object_type")
    if not _is_instance(target.get("instance_number")):
        raise ValueError(f"schedule_write_targets entries require an integer instance_number 0-{MAX_INSTANCE}")


def validate_schedule_config(
    value_type: Optional[str],
    schedule_default: Optional[str],
    weekly_schedule: Optional[list],
    exception_schedule: Optional[list],
    effective_start: Optional[str],
    effective_end: Optional[str],
    priority: int,
    write_targets: Optional[list],
    state_text: Optional[list] = None,
) -> None:
    """Raises ValueError for any structurally invalid schedule config."""
    if value_type not in SCHEDULE_VALUE_TYPES:
        raise ValueError(f"schedule_value_type must be one of {sorted(SCHEDULE_VALUE_TYPES)}")
    number_of_states = len(state_text) if state_text else None
    # No default is a Null Schedule_Default: outside the scheduled times the
    # schedule's value is Null and its targets are released.
    if schedule_default is not None:
        _validate_schedule_value_string(value_type, schedule_default, number_of_states)

    # Neither a weekly schedule nor exceptions is fine: the schedule just
    # reads Schedule_Default.
    if not (1 <= priority <= 16):
        raise ValueError(f"schedule_priority {priority!r} must be between 1 and 16")

    if weekly_schedule:
        if len(weekly_schedule) != 7:
            raise ValueError("weekly_schedule must have exactly 7 entries (Monday-Sunday)")
        for day in weekly_schedule:
            _validate_time_value_list(day, value_type, number_of_states)

    if exception_schedule:
        for event in exception_schedule:
            event_priority = event.get("priority")
            if not isinstance(event_priority, int) or not (1 <= event_priority <= 16):
                raise ValueError(f"exception event priority {event_priority!r} must be 1-16")
            _validate_schedule_period(event.get("period"))
            _validate_time_value_list(event.get("time_values") or [], value_type, number_of_states)

    if effective_start:
        parse_schedule_date(effective_start)
    if effective_end:
        parse_schedule_date(effective_end)

    if write_targets:
        for target in write_targets:
            if is_external_target(target):
                _validate_external_write_target(target)
                continue
            target_type = target.get("object_type")
            # A schedule/calendar target would silently never take (its
            # Present_Value is calculated), as connector mappings also refuse.
            if target_type not in OBJECT_TYPES or target_type in ("schedule", "calendar"):
                raise ValueError(f"{target_type!r} is not a valid schedule_write_targets object_type")
            if not isinstance(target.get("instance_number"), int):
                raise ValueError("schedule_write_targets entries require an integer instance_number")
            # Any case and separator, or a proprietary number. Present_Value
            # only: BACpack's own write still passes the target's allowlist,
            # where nothing else a schedule value fits is writable.
            prop = target.get("property_identifier") or "present-value"
            if normalize_property(str(prop)) != "present-value":
                raise ValueError(
                    f"a schedule can only write Present_Value of an object on this device, not {prop!r}"
                )


def _build_schedule_period(period: dict) -> SpecialEventPeriod:
    if period.get("kind") == "calendar-reference":
        return SpecialEventPeriod(calendarReference=("calendar", int(period["instance_number"])))
    return SpecialEventPeriod(calendarEntry=build_calendar_entry(period))


def _build_time_value(entry: dict, value_type: str) -> TimeValue:
    time_value = _parse_schedule_time(entry["time"])
    raw = entry.get("value")
    value = Null(()) if raw is None else schedule_atomic_value(value_type, raw)
    return TimeValue(time=time_value, value=value)


def _sorted_by_time(entries: list) -> list:
    """bacpypes3's eval() assumes ascending times within a day/event: it
    overwrites the running value on each match and stops at the first
    future entry."""
    return sorted(entries, key=lambda e: dt.datetime.strptime(e["time"], "%H:%M").time())


def build_daily_schedule(entries: list, value_type: str) -> DailySchedule:
    return DailySchedule(
        daySchedule=[_build_time_value(e, value_type) for e in _sorted_by_time(entries)]
    )


def build_special_event(event: dict, value_type: str) -> SpecialEvent:
    return SpecialEvent(
        period=_build_schedule_period(event["period"]),
        listOfTimeValues=[
            _build_time_value(e, value_type) for e in _sorted_by_time(event.get("time_values") or [])
        ],
        eventPriority=int(event["priority"]),
    )


def effective_period(start: Optional[str], end: Optional[str]) -> DateRange:
    """Both unset means always in effect: a fully wildcarded DateRange, the
    conventional form, which upstream's plain tuple comparison can't
    evaluate -- bacpypes3_compat._match_date_range_with_wildcards handles
    it. A single open side gets a concrete limit. (YABE's "Optional
    Exception Schedule not implemented" is a static label in its editor,
    not about this.)
    """
    if not start and not end:
        return DateRange(startDate=timeutil.wildcard_date(), endDate=timeutil.wildcard_date())
    start_date = parse_schedule_date(start) if start else dt.date(timeutil.BACNET_MIN_YEAR, 1, 1)
    end_date = parse_schedule_date(end) if end else dt.date(timeutil.BACNET_MAX_YEAR, 12, 31)
    return DateRange(startDate=timeutil.bacnet_date(start_date), endDate=timeutil.bacnet_date(end_date))


def build_schedule_write_targets(targets: Optional[list]) -> list:
    refs = []
    for t in targets or []:
        if is_external_target(t):
            point = Point(t["object_type"], int(t["instance_number"]), t.get("property_identifier") or "present-value")
            refs.append(
                DeviceObjectPropertyReference(
                    objectIdentifier=point.object_identifier,
                    propertyIdentifier=point.property_identifier,
                    deviceIdentifier=("device", int(t["device_instance"])),
                )
            )
        else:
            refs.append(
                DeviceObjectPropertyReference(
                    objectIdentifier=(t["object_type"], int(t["instance_number"])),
                    propertyIdentifier=t.get("property_identifier", "presentValue"),
                )
            )
    return refs


# --- Unbuild: live bacpypes3 values back to stored JSON ----------------------
# The reverse of the builders above, for persisting an accepted external
# write (ScheduleConfigPersisted, CalendarConfigPersisted).


# Binary is Enumerated, not BinaryPV: AnyAtomic.get_value() demotes it.
_SCHEDULE_DATATYPES = {"analog": Real, "binary": Enumerated, "multi-state": Unsigned}


def atomic_value_to_schedule_string(value_type: str, value) -> Optional[str]:
    """Inverse of schedule_atomic_value; a Null (unused slot) gives None.
    ValueError for a value not of the schedule's datatype (135 12.24: every
    value is Schedule_Default's type), rather than converting it."""
    if isinstance(value, AnyAtomic):
        value = value.get_value()
    if isinstance(value, Null):
        return None
    if not isinstance(value, _SCHEDULE_DATATYPES.get(value_type, Real)):
        raise ValueError(f"a {type(value).__name__} is not a {value_type} schedule value")
    if value_type == "binary":
        # AnyAtomic.get_value() demotes a BinaryPV to a bare Enumerated whose
        # str() is "0"/"1"; re-cast to get "inactive"/"active".
        return str(BinaryPV(int(value)))
    if value_type == "multi-state":
        return str(int(value))
    return str(float(value))


def _unbuild_time_value(time_value: TimeValue, value_type: str) -> dict:
    t = time_value.time
    # Stored to the minute: a write with seconds or hundredths (or 255,
    # unspecified) is refused rather than acknowledged and then cut.
    if t[2] or t[3]:  # pyright: ignore[reportIndexIssue]  # a Time is a 4-tuple; bacpypes3 types it loosely
        raise ValueError(f"schedule time {t} is finer than a minute")
    return {
        "time": f"{t[0]:02d}:{t[1]:02d}",
        "value": atomic_value_to_schedule_string(value_type, time_value.value),
    }


# Sorted, so the saved row matches the day the live object runs (a client
# can send time-values in any order; see _sorted_by_time).
def unbuild_daily_schedule(daily_schedule: DailySchedule, value_type: str) -> list:
    return _sorted_by_time([_unbuild_time_value(tv, value_type) for tv in daily_schedule.daySchedule])


def _unbuild_schedule_period(period: SpecialEventPeriod) -> dict:
    if period.calendarReference is not None:
        return {"kind": "calendar-reference", "instance_number": int(period.calendarReference[1])}
    return unbuild_calendar_entry(period.calendarEntry)


def unbuild_special_event(event: SpecialEvent, value_type: str) -> dict:
    return {
        "period": _unbuild_schedule_period(event.period),
        "priority": int(event.eventPriority),
        "time_values": _sorted_by_time([_unbuild_time_value(tv, value_type) for tv in event.listOfTimeValues]),
    }


def unbuild_schedule_write_targets(refs: list) -> list:
    targets = []
    for ref in refs:
        # A saved target has no array index, so one would be dropped silently.
        if ref.propertyArrayIndex is not None:
            raise ValueError("a write target can't name an array index")
        target = {
            "object_type": str(ref.objectIdentifier[0]),
            "instance_number": int(ref.objectIdentifier[1]),
            "property_identifier": str(ref.propertyIdentifier),
        }
        if ref.deviceIdentifier is not None:
            if str(ref.deviceIdentifier[0]) != "device":
                raise ValueError("a write target's deviceIdentifier must name a device")
            target["device_instance"] = int(ref.deviceIdentifier[1])
        targets.append(target)
    return targets


def date_range_to_effective_start_end(date_range: DateRange) -> tuple:
    """Inverse of effective_period. A client may wildcard one side only: a
    fully wildcarded side is an open end; a partly wildcarded one is refused
    by timeutil.python_date (ValueError -> value-out-of-range)."""
    start = timeutil.python_date(date_range.startDate)
    end = timeutil.python_date(date_range.endDate)
    return (
        None if start is None else start.isoformat(),
        None if end is None else end.isoformat(),
    )
