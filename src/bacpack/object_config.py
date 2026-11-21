"""One answer to "can this object be built?"
Every save path and the build itself call check_object on the row as it will
be, and check_device on the device's resulting object list (see
check_saved_row for the cheaper BACnet write case). No field gating: a change to one field re-checks the whole row.

Callers use `object_config.check_object(...)`, not a from-import, so
tests/test_object_config_wiring.py can replace both checks in one place.
No web imports and no event loop: the device process imports this too."""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import inspect, select
from sqlalchemy.exc import DatabaseError

from .models import BINARY_TYPES, MAX_INSTANCE, MAX_TREND_SAMPLE_CAP, OBJECT_TYPES, BacnetObject, Device
from .object_references import dangling_references_of, find_dangling_references
from .objects.calendar_config import validate_calendar_config
from .objects.notification_class_config import validate_notification_class_config
from .objects.reserved_names import (
    find_reserved_name_clash,
    find_trend_log_identity_clash,
    find_trend_log_name_clash,
)
from .objects.schedule_config import validate_schedule_config
from .objects.trend_log_config import validate_trend_reporting_config
from .point_config import ALARM_VALIDATION_FIELDS, validate_alarming_config, validate_relinquish_default, validate_start_value, validate_units
from .simulation import normalize_simulation, parse_source

logger = logging.getLogger(__name__)



def row_from_object(obj: BacnetObject) -> dict:
    """Every mapped column of `obj`. A None on a column with a scalar
    default reads as that default -- what an insert would store -- so an
    unsaved BacnetObject() checks the way its saved row would."""
    row = {}
    for attr in inspect(BacnetObject).column_attrs:
        value = getattr(obj, attr.key)
        default = attr.columns[0].default
        if value is None and default is not None and default.is_scalar:
            value = default.arg
        row[attr.key] = value
    return row


def _or_default(row: dict, field: str, default):
    value = row.get(field)
    return default if value is None else value


def _check_binary_text(field: str, value) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError(f"{field} must be 1-64 characters of text")
    if value != value.strip():
        raise ValueError(f"{field} {value!r} must not start or end with a space")


def _check_trend_log_identity(row: dict) -> None:
    instance = row.get("trend_log_instance")
    if instance is not None and (
        isinstance(instance, bool) or not isinstance(instance, int) or not 0 <= instance <= MAX_INSTANCE
    ):
        raise ValueError(f"trend_log_instance must be an integer 0-{MAX_INSTANCE}")
    name = row.get("trend_log_name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("trend_log_name must not be blank")
        if name != name.strip():
            raise ValueError(f"trend_log_name {name!r} must not start or end with a space")


def check_object(row: dict) -> None:
    """ValueError naming the first thing that would stop `row` building.
    Keys it doesn't use are ignored; a missing key reads as None."""
    object_type = row.get("object_type")
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"{object_type!r} is not a valid object_type")
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if name != name.strip():
        raise ValueError(f"name {name!r} must not start or end with a space")
    instance_number = row.get("instance_number")
    if isinstance(instance_number, bool) or not isinstance(instance_number, int) or not (
        0 <= instance_number <= MAX_INSTANCE
    ):
        raise ValueError(f"instance_number must be an integer 0-{MAX_INSTANCE}")
    state_text = row.get("state_text")
    if "multi-state" in object_type and not state_text:
        raise ValueError(f"multi-state object {name!r} requires state_text")

    validate_units(object_type, row.get("units"))
    if object_type in BINARY_TYPES:
        for field in ("active_text", "inactive_text"):
            _check_binary_text(field, row.get(field))
    validate_relinquish_default(object_type, state_text, row.get("relinquish_default"))
    validate_start_value(object_type, state_text, row.get("start_value"))
    if object_type == "schedule":
        validate_schedule_config(
            row.get("schedule_value_type"),
            row.get("schedule_default"),
            row.get("weekly_schedule"),
            row.get("exception_schedule"),
            row.get("schedule_effective_start"),
            row.get("schedule_effective_end"),
            _or_default(row, "schedule_priority", 14),
            row.get("schedule_write_targets"),
            state_text,
        )
    elif object_type == "calendar":
        validate_calendar_config(row.get("date_list"))
    elif object_type == "notification-class":
        validate_notification_class_config(
            row.get("notification_priority"),
            row.get("notification_ack_required"),
            row.get("notification_recipient_list"),
        )

    alarming_enabled = bool(row.get("alarming_enabled"))
    if alarming_enabled and row.get("notification_class_instance") is None:
        raise ValueError("notification_class_instance is required when alarming_enabled=True")
    validate_alarming_config(
        object_type, alarming_enabled, **{field: row.get(field) for field in ALARM_VALIDATION_FIELDS}
    )
    cap = row.get("trend_sample_cap")
    if cap is not None and (
        isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_TREND_SAMPLE_CAP
    ):
        raise ValueError(f"trend_sample_cap must be an integer 1-{MAX_TREND_SAMPLE_CAP}")
    if row.get("trended") and object_type == "notification-class":
        raise ValueError("a Notification Class can't be trended: it has no Present_Value")
    if row.get("trended"):
        _check_trend_log_identity(row)
    validate_trend_reporting_config(
        trended=bool(row.get("trended")),
        enabled=bool(row.get("trend_reporting_enabled")),
        notification_class_instance=row.get("trend_notification_class_instance"),
        threshold=_or_default(row, "trend_notification_threshold", 10),
        notify_type=_or_default(row, "trend_notify_type", "event"),
        event_enable=row.get("trend_event_enable"),
    )

    simulation = row.get("simulation")
    if simulation is not None:
        normalized = normalize_simulation(object_type, simulation, len(state_text) if state_text else None)
        if normalized["kind"] == "follow" and parse_source(normalized["source"]) == (object_type, instance_number):
            raise ValueError("simulation: an object can't follow itself")


def trend_log_identity_problem(rows: Iterable[dict]) -> Optional[str]:
    """The Trend Log instance clash among rows that all have their ids, or
    None. check_device cannot see a defaulted instance (the row id) of a row
    not flushed yet, so a save path that creates trended rows runs this after
    the flush."""
    return find_trend_log_identity_clash(rows)


def check_device(device_name: str, rows: Iterable[dict]) -> Optional[str]:
    """The first problem with one device's complete object list that would
    stop it building, or None. Rows are check_object-shaped dicts."""
    rows = list(rows)
    identifiers: set = set()
    names: set = set()
    for row in rows:
        identifier = (row["object_type"], row["instance_number"])
        if identifier in identifiers:
            return f"{identifier[0]}:{identifier[1]} already exists on this device"
        identifiers.add(identifier)
        if row["name"] in names:
            return f"an object named {row['name']!r} already exists on this device"
        names.add(row["name"])
    clash = (
        find_trend_log_name_clash(rows)
        or find_reserved_name_clash(device_name, rows)
        or find_trend_log_identity_clash(rows)
    )
    if clash is not None:
        return clash
    dangling = find_dangling_references(rows)
    if dangling:
        return "; ".join(dangling)
    return None


# The fields a device-wide check depends on. A BACnet write that changes
# none of them can only break the changed row's own references, so only
# those are checked; a write that changes one gets the full check.
_DEVICE_WIDE_FIELDS = frozenset(
    {"name", "object_type", "instance_number", "trended", "trend_log_instance", "trend_log_name"}
)


def check_saved_row(session_factory, object_id: Optional[int], changes: dict) -> None:
    """For a BACnet write that saves config: ValueError if the saved row
    with `changes` applied wouldn't build, or would leave its device unable
    to build -- checked as cheaply as `changes` allows (see
    _DEVICE_WIDE_FIELDS). With no saved row to check against (e.g. deleted
    from the UI while the device runs) the write applies live. A database
    error reading the saved config is logged and the write applies live
    too: the save that follows almost always fails the same way, and if a
    busy moment clears first, the device reports the unchecked row at its
    next apply or start."""
    if session_factory is None or object_id is None:
        return
    try:
        with session_factory() as session:
            saved = session.get(BacnetObject, object_id)
            if saved is None:
                return
            row = {**row_from_object(saved), **changes}
            check_object(row)
            if _DEVICE_WIDE_FIELDS & changes.keys():
                device = session.get(Device, saved.device_id)
                siblings = session.scalars(select(BacnetObject).filter_by(device_id=saved.device_id)).all()
                rows = [row if other.id == object_id else row_from_object(other) for other in siblings]
                problem = check_device(device.name, rows)
            else:
                present = {
                    (object_type, instance)
                    for object_type, instance in session.execute(
                        select(BacnetObject.object_type, BacnetObject.instance_number).filter_by(
                            device_id=saved.device_id
                        )
                    )
                }
                problem = "; ".join(dangling_references_of(row, present)) or None
    except DatabaseError as exc:  # OperationalError included; a programming error stays loud
        _log_unchecked(f"object id {object_id}", exc)
        return
    if problem is not None:
        raise ValueError(problem)


def check_saved_device_name(session_factory, device_id: int, new_name: str) -> None:
    """For a BACnet Object_Name write on the Device: ValueError if the
    device's saved objects wouldn't build under `new_name`."""
    if session_factory is None:
        return
    try:
        with session_factory() as session:
            objects = session.scalars(select(BacnetObject).filter_by(device_id=device_id)).all()
            problem = check_device(new_name, [row_from_object(obj) for obj in objects])
    except DatabaseError as exc:  # OperationalError included; a programming error stays loud
        _log_unchecked(f"device {device_id}'s name", exc)
        return
    if problem is not None:
        raise ValueError(problem)


def _log_unchecked(what: str, exc: Exception) -> None:
    logger.warning(
        "could not read the saved config to check a BACnet write to %s, applying it live: %s", what, exc
    )
