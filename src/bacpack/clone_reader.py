"""What a clone reads from a live controller: its Device object and every
object's properties, through the BACnet client.
Read-only: only ReadProperty and ReadPropertyMultiple go to the remote.

The result is the raw read, JSON-able, for clone_plan to turn into rows."""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, List

from .bacnet_client import ClientError, Point, read_object_list, to_jsonable
from .models import (
    ALARMABLE_TYPES,
    ANALOG_TYPES,
    BINARY_TYPES,
    COMMANDABLE_TYPES,
    INPUT_TYPES,
    MULTISTATE_TYPES,
    WILDCARD_DEVICE_INSTANCE,
)
from . import timeutil

# Points per ReadPropertyMultiple stretch handed to the client, which splits
# further to fit the remote (BacnetClient.read_many).
POINTS_PER_CHUNK = 100

_DEVICE_PROPERTIES = ("object-name", "description", "location", "vendor-name", "vendor-identifier")

_ALARM_COMMON = (
    "notification-class", "time-delay", "time-delay-normal", "event-enable", "notify-type", "event-detection-enable",
)


def _properties_for(object_type: str) -> tuple:
    props: List[str] = ["object-name", "description"]
    if object_type in ANALOG_TYPES:
        props.append("units")
    if object_type in BINARY_TYPES:
        props += ["active-text", "inactive-text"]
    if object_type in MULTISTATE_TYPES:
        props += ["state-text", "number-of-states"]
    if object_type in INPUT_TYPES:
        props.append("present-value")
    if object_type in COMMANDABLE_TYPES:
        props.append("relinquish-default")
    if object_type in ALARMABLE_TYPES:
        props += _ALARM_COMMON
        if object_type in ANALOG_TYPES:
            props += ["high-limit", "low-limit", "deadband", "limit-enable"]
        elif object_type in BINARY_TYPES:
            props.append("alarm-value")
        else:
            props.append("alarm-values")
    elif object_type == "schedule":
        props += [
            "weekly-schedule", "exception-schedule", "effective-period", "schedule-default",
            "priority-for-writing", "list-of-object-property-references",
        ]
    elif object_type == "calendar":
        props.append("date-list")
    elif object_type == "notification-class":
        props += ["notification-class", "priority", "ack-required", "recipient-list"]
    elif object_type == "trend-log":
        props += ["log-device-object-property", "logging-type", "log-interval", "buffer-size", "trigger"]
    return tuple(props)


# The properties read per object type. Any other type (one BACpack cannot
# simulate) reads only its name, so the plan can list it. Whether a
# commandable type's Priority_Array exists is asked separately (index 0).
PROPERTIES_BY_TYPE: Dict[str, tuple] = {
    object_type: _properties_for(object_type)
    for object_type in (
        *sorted(ANALOG_TYPES), *sorted(BINARY_TYPES), *sorted(MULTISTATE_TYPES),
        "schedule", "calendar", "notification-class", "trend-log",
    )
}
_NAME_ONLY = ("object-name",)
# The only answer that means a property is not set. A Priority_Array that is
# not an array of this object (or not there) means it is not commandable.
_NOT_SET = "unknown-property"
BAD_ARRAY_LENGTH = "bad-array-length"
PHASE_OBJECT_LIST = "object-list"
PHASE_OBJECTS = "objects"
_NOT_AN_ARRAY_PRIORITY = ("unknown-property", "property-is-not-an-array")


def _points_of(object_type: str, instance: int) -> List[Point]:
    points = [Point(object_type, instance, prop) for prop in PROPERTIES_BY_TYPE.get(object_type, _NAME_ONLY)]
    if object_type in COMMANDABLE_TYPES:
        points.append(Point(object_type, instance, "priority-array", 0))
    return points


def _text(value) -> str:
    return str(value)


async def read_controller(
    client, binding, instance: int, timeout: float, report: Callable[[int, int, str], None], cancelled: Callable[[], bool]
) -> dict:
    """The raw read of the controller at `binding`, JSON-able. `report(done,
    total, phase)` is called as the Object_List is read entry by entry
    (phase PHASE_OBJECT_LIST, when it is too big for one reply) and then as
    objects are read (PHASE_OBJECTS); `cancelled()` is true once the user
    cancelled, and the read then raises ClientError("cancelled"). A failure of
    the link itself (ClientError "transport") ends the read; anything the
    controller answers per property or per object is recorded and the read
    goes on."""
    device_points = [Point("device", instance, prop) for prop in _DEVICE_PROPERTIES]
    values = await client.read_many(binding, device_points, timeout=timeout)

    def device_value(prop: str):
        value = values[Point("device", instance, prop)]
        return None if isinstance(value, ClientError) else to_jsonable(value)

    identifiers = await read_object_list(
        client, binding, instance, timeout, lambda done, total: report(done, total, PHASE_OBJECT_LIST), cancelled
    )
    listed = [(str(oid[0]), int(oid[1])) for oid in identifiers if str(oid[0]) != "device"]
    # The plan tells the controller's own references from another device's by
    # this instance, so a wildcard read records the one the controller listed.
    listed_devices = [int(oid[1]) for oid in identifiers if str(oid[0]) == "device"]
    device = {
        "instance": listed_devices[0]
        if instance == WILDCARD_DEVICE_INSTANCE and len(listed_devices) == 1
        else instance,
        "name": device_value("object-name"),
        "description": device_value("description"),
        "location": device_value("location"),
        "vendor_name": device_value("vendor-name"),
        "vendor_identifier": device_value("vendor-identifier"),
        "object_list_count": len(identifiers),
    }

    objects: List[dict] = []
    total = len(listed)
    report(0, total, PHASE_OBJECTS)
    start = 0
    while start < total:
        if cancelled():
            raise ClientError("cancelled")
        chunk, points = [], []
        while start < total and len(points) < POINTS_PER_CHUNK:
            object_type, object_instance = listed[start]
            chunk.append((object_type, object_instance))
            points.extend(_points_of(object_type, object_instance))
            start += 1
        read = await client.read_many(binding, points, timeout=timeout)
        for object_type, object_instance in chunk:
            objects.append(
                await _object_entry(client, binding, object_type, object_instance, read, timeout, cancelled)
            )
        report(len(objects), total, PHASE_OBJECTS)

    return {"device": device, "objects": objects, "read_at": timeutil.iso_utc()}


def _recipient_list_json(value) -> list:
    """Recipient_List as JSON. An address recipient's MAC is hex text (the
    generic conversion gives the text of a bytes value)."""
    out = to_jsonable(value)
    for destination, item in zip(value, out, strict=True):
        address = getattr(getattr(destination, "recipient", None), "address", None)
        if address is not None and isinstance(item.get("recipient"), dict):
            item["recipient"]["macAddress"] = bytes(address.macAddress).hex()
    return out


def _failure_code(error: ClientError) -> str:
    if error.error_code:
        return error.error_code
    return "too-big" if error.kind == "too-big" else _text(error)


async def _read_by_index(client, binding, point: Point, timeout: float, cancelled: Callable[[], bool]):
    """An array property too big for one reply, read an element at a time:
    its list of values, or None when the remote can't give it that way. A
    length that is not a number is a failed read (BAD_ARRAY_LENGTH)."""
    try:
        count = await client.read(binding, replace(point, array_index=0), timeout=timeout)
        try:
            length = int(count)
        except (ValueError, TypeError):
            return BAD_ARRAY_LENGTH
        values = []
        for index in range(1, length + 1):
            if cancelled():
                raise ClientError("cancelled")
            values.append(
                to_jsonable(await client.read(binding, replace(point, array_index=index), timeout=timeout))
            )
        return values
    except ClientError as exc:
        if exc.transport or exc.kind == "dcc" or str(exc) == "cancelled":
            raise
        return None


async def _object_entry(
    client, binding, object_type: str, instance: int, read: dict, timeout: float, cancelled: Callable[[], bool]
) -> dict:
    """One object's entry. Only unknown-property means "not set" (`missing`);
    any other refusal of a property is kept in `failed` with its code, for the
    plan to say so."""
    props: Dict[str, object] = {}
    missing: List[str] = []
    failed: Dict[str, str] = {}
    name_error = None
    for prop in PROPERTIES_BY_TYPE.get(object_type, _NAME_ONLY):
        point = Point(object_type, instance, prop)
        value = read[point]
        if isinstance(value, ClientError) and value.kind == "too-big":
            if cancelled():
                raise ClientError("cancelled")
            by_index = await _read_by_index(client, binding, point, timeout, cancelled)
            if by_index == BAD_ARRAY_LENGTH:
                failed[prop] = BAD_ARRAY_LENGTH
                continue
            if by_index is not None:
                props[prop] = by_index
                continue
        if not isinstance(value, ClientError):
            props[prop] = _recipient_list_json(value) if prop == "recipient-list" else to_jsonable(value)
        elif value.error_code == _NOT_SET:
            missing.append(prop)
        else:
            failed[prop] = _failure_code(value)
        if prop == "object-name" and isinstance(value, ClientError):
            name_error = _text(value)
    if object_type in COMMANDABLE_TYPES:
        answer = read[Point(object_type, instance, "priority-array", 0)]
        if not isinstance(answer, ClientError):
            props["commandable"] = True
        elif answer.error_code in _NOT_AN_ARRAY_PRIORITY:
            props["commandable"] = False
        else:
            failed["priority-array"] = _failure_code(answer)
    entry = {
        "object_type": object_type,
        "instance": instance,
        "props": props,
        "missing": missing,
        "error": name_error,
    }
    if failed:
        entry["failed"] = failed
    return entry
