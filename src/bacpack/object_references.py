"""Which objects on a device refer to which others, by (object_type,
instance_number): notification classes (alarming and trend reporting),
calendar-reference exceptions, local schedule write targets, and a
simulator's follow source. Saves refuse a reference to a missing object
(via object_config); the delete routes use find_references_to, since
deleting a referenced object can stop the device starting or silently
break a schedule.

Works on BacnetObject rows and plain config dicts alike.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from .simulation import parse_source

Identifier = Tuple[str, int]


def _field(obj, name: str):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name)


def _label(obj) -> str:
    return f"{_field(obj, 'name')!r} ({_field(obj, 'object_type')}:{_field(obj, 'instance_number')})"


def object_references(obj) -> List[Tuple[Identifier, str]]:
    """(target identifier, what the reference is) for each object `obj`
    refers to."""
    refs: List[Tuple[Identifier, str]] = []
    if _field(obj, "alarming_enabled") and _field(obj, "notification_class_instance") is not None:
        refs.append(
            (("notification-class", _field(obj, "notification_class_instance")), "as its notification class")
        )
    if (
        _field(obj, "trended")
        and _field(obj, "trend_reporting_enabled")
        and _field(obj, "trend_notification_class_instance") is not None
    ):
        refs.append(
            (
                ("notification-class", _field(obj, "trend_notification_class_instance")),
                "as its trend log's notification class",
            )
        )
    if _field(obj, "object_type") == "schedule":
        for event in _field(obj, "exception_schedule") or []:
            period = event.get("period") or {}
            if period.get("kind") == "calendar-reference":
                refs.append((("calendar", period.get("instance_number")), "in its exception schedule"))
        for target in _field(obj, "schedule_write_targets") or []:
            if target.get("device_instance") is not None:
                continue  # another device's point, not a reference on this device
            refs.append(((target["object_type"], target["instance_number"]), "as a write target"))
    simulation = _field(obj, "simulation")
    if isinstance(simulation, dict) and simulation.get("kind") == "follow":
        try:
            refs.append((parse_source(simulation.get("source")), "as its simulation source"))
        except ValueError:
            pass  # a malformed source is the settings check's to report
    return refs


def find_references_to(objects: Iterable, targets: set) -> List[str]:
    """Messages for every reference, among `objects`, to an identifier in
    `targets` -- used before deleting `targets`. `objects` should exclude
    the ones being deleted, so deleting a referrer together with its target
    is allowed."""
    messages = []
    for obj in objects:
        for identifier, how in object_references(obj):
            if identifier in targets:
                messages.append(f"{_label(obj)} uses {identifier[0]}:{identifier[1]} {how}")
    return messages


def dangling_references_of(obj, present: set) -> List[str]:
    """Messages for each reference `obj` makes to an identifier not in
    `present` -- for checking one changed object against its device's
    identifiers without loading the whole device."""
    return [
        f"{_label(obj)} uses {identifier[0]}:{identifier[1]} {how}, which does not exist"
        for identifier, how in object_references(obj)
        if identifier not in present
    ]


def find_dangling_references(objects: Iterable) -> List[str]:
    """Messages for every reference, among `objects` (one device's complete
    object list), to an object not in that list."""
    objects = list(objects)
    present = {(_field(o, "object_type"), _field(o, "instance_number")) for o in objects}
    return [message for obj in objects for message in dangling_references_of(obj, present)]
