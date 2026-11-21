"""Fixed-choice values are case-insensitive on input.

Every object entry path passes its row through canonicalize_object_fields
before validating, so "Analog-Value" or "PERCENT" is stored canonical. Only
user text (names, descriptions, State_Text) keeps its exact case. A
non-match passes through unchanged for the field's own validation to report.
The name is also trimmed: no saved name starts or ends with whitespace.
"""
from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import Any, Callable, Iterable, Optional, Tuple

from bacpypes3.primitivedata import ObjectType, PropertyIdentifier

from . import bacpypes3_compat
from .models import OBJECT_TYPES
from .point_config import list_engineering_units

TREND_MODES = ("interval", "cov")
# Every family's simulator kinds (which fit which type: simulation.KINDS_BY_FAMILY).
SIMULATION_KINDS = ("sine", "triangle", "sawtooth", "square", "random_walk", "noise", "follow", "random", "cycle")
NOTIFY_TYPES = ("alarm", "event")
SCHEDULE_VALUE_TYPES = ("analog", "binary", "multi-state")
CALENDAR_ENTRY_KINDS = ("date", "date-range", "week-and-day", "calendar-reference")
RECIPIENT_KINDS = ("device", "address")
BINARY_WORDS = ("active", "inactive")
NETWORK_ROLES = ("normal", "foreign", "bbmd")
CONNECTOR_TYPES = ("http-poll", "bacnet")
RESPONSE_FORMATS = ("json", "plain-text")
AUTH_TYPES = ("none", "basic", "bearer")
MAPPING_DIRECTIONS = ("read", "proxy")

_SEPARATORS = re.compile(r"[-_\s]")


def choice_key(text: str) -> str:
    """What two spellings must share to count as the same choice."""
    return _SEPARATORS.sub("", text).lower()


@lru_cache(maxsize=None)
def _lookup(choices: Tuple[str, ...]) -> dict:
    return {choice_key(choice): choice for choice in choices}


def canonical(value: Any, choices: Iterable[str]) -> Any:
    """`value`'s spelling from `choices`, ignoring case, '-', '_' and
    spaces. Anything else -- no match, or not a string -- is returned
    unchanged."""
    if not isinstance(value, str):
        return value
    return _lookup(tuple(choices)).get(choice_key(value), value)


@lru_cache(maxsize=None)
def _enum_lookup(enum_class) -> dict:
    # Every spelling of every name (kebab and camel).
    every_spelling, _, _ = bacpypes3_compat.enum_name_maps(enum_class)
    return {choice_key(name): number for name, number in every_spelling.items()}


def enum_name(enum_class, value: str, *, camel: bool = False) -> Optional[str]:
    """A bacpypes3 Enumerated name matched like canonical(): kebab-case
    ("analog-value") by default, camelCase ("analogValue") with camel=True.
    None when nothing matches, including a bare number."""
    number = _enum_lookup(enum_class).get(choice_key(value))
    if number is None:
        return None
    _, camel_names, kebab_names = bacpypes3_compat.enum_name_maps(enum_class)
    return (camel_names if camel else kebab_names)[number]


def _units() -> Tuple[str, ...]:
    return tuple(list_engineering_units())


def _binary_word(value: Any) -> Any:
    return canonical(value, BINARY_WORDS)


def _time_of(entry: Any) -> Optional[dt.time]:
    try:
        return dt.datetime.strptime(entry["time"], "%H:%M").time()
    except (TypeError, KeyError, ValueError):
        return None


def _time_values(entries: Any) -> Any:
    """Binary words canonical, and the entries in time order: the device
    runs each day sorted, so the saved row shows what it runs. Left in the
    order sent when any time is invalid, for validation to report."""
    if not isinstance(entries, list):
        return entries
    out = [
        {**entry, "value": _binary_word(entry["value"])} if isinstance(entry, dict) and "value" in entry else entry
        for entry in entries
    ]
    times = [_time_of(entry) for entry in out]
    if None in times:
        return out
    return [entry for _, entry in sorted(zip(times, out, strict=True), key=lambda pair: pair[0])]


def _weekly(days: Any) -> Any:
    if not isinstance(days, list):
        return days
    return [_time_values(day) for day in days]


def _calendar_entry(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    entry = dict(entry)
    if "kind" in entry:
        entry["kind"] = canonical(entry["kind"], CALENDAR_ENTRY_KINDS)
    if "week_of_month" in entry:
        entry["week_of_month"] = canonical(entry["week_of_month"], ("last",))
    return entry


def _entries(entries: Any) -> Any:
    if not isinstance(entries, list):
        return entries
    return [_calendar_entry(entry) for entry in entries]


def _exceptions(events: Any) -> Any:
    if not isinstance(events, list):
        return events
    out = []
    for event in events:
        if isinstance(event, dict):
            event = dict(event)
            if "period" in event:
                event["period"] = _calendar_entry(event["period"])
            if "time_values" in event:
                event["time_values"] = _time_values(event["time_values"])
        out.append(event)
    return out


def _enum_or_same(enum_class, value: Any, *, camel: bool = False) -> Any:
    if not isinstance(value, str):
        return value
    return enum_name(enum_class, value, camel=camel) or value


def _targets(targets: Any) -> Any:
    """A local target's object type is one of OBJECT_TYPES and its property
    stays camelCase ("presentValue", as the UI stores it); a remote one's
    names are kebab-case."""
    if not isinstance(targets, list):
        return targets
    out = []
    for target in targets:
        if isinstance(target, dict):
            target = dict(target)
            remote = target.get("device_instance") is not None
            if "object_type" in target:
                target["object_type"] = (
                    _enum_or_same(ObjectType, target["object_type"])
                    if remote
                    else canonical(target["object_type"], OBJECT_TYPES)
                )
            if "property_identifier" in target:
                target["property_identifier"] = _enum_or_same(
                    PropertyIdentifier, target["property_identifier"], camel=not remote
                )
        out.append(target)
    return out


def _simulation(value):
    """A simulation's kind and follow source type in canonical spelling."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if "kind" in out:
        out["kind"] = canonical(out["kind"], SIMULATION_KINDS)
    source = out.get("source")
    if isinstance(source, str) and ":" in source:
        object_type, _, instance = source.rpartition(":")
        out["source"] = f"{canonical(object_type.strip(), OBJECT_TYPES)}:{instance.strip()}"
    return out


def _recipients(entries: Any) -> Any:
    """A Recipient_List entry's recipient kind in canonical spelling."""
    if not isinstance(entries, list):
        return entries
    out = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("recipient"), dict):
            entry = dict(entry)
            recipient = dict(entry["recipient"])
            if "kind" in recipient:
                recipient["kind"] = canonical(recipient["kind"], RECIPIENT_KINDS)
            entry["recipient"] = recipient
        out.append(entry)
    return out


_FIELDS: Tuple[Tuple[str, Callable[[Any], Any]], ...] = (
    ("object_type", lambda v: canonical(v, OBJECT_TYPES)),
    ("units", lambda v: canonical(v, _units())),
    # A binary word is only ever valid where the type is binary, and no
    # other type's value can be spelled "active"/"inactive", so these don't
    # need to know the object type.
    ("relinquish_default", _binary_word),
    ("start_value", _binary_word),
    ("schedule_default", _binary_word),
    ("trend_mode", lambda v: canonical(v, TREND_MODES)),
    ("trend_notify_type", lambda v: canonical(v, NOTIFY_TYPES)),
    ("alarm_notify_type", lambda v: canonical(v, NOTIFY_TYPES)),
    ("schedule_value_type", lambda v: canonical(v, SCHEDULE_VALUE_TYPES)),
    ("weekly_schedule", _weekly),
    ("exception_schedule", _exceptions),
    ("date_list", _entries),
    ("schedule_write_targets", _targets),
    ("simulation", _simulation),
    ("notification_recipient_list", _recipients),
)


def canonicalize_object_fields(row: dict) -> dict:
    """A copy of an object row (API body, CSV row or JSON config object)
    with every fixed-choice value in its canonical spelling. Absent and
    null fields are left as they are."""
    out = dict(row)
    for field in ("name", "trend_log_name"):
        if isinstance(out.get(field), str):
            out[field] = out[field].strip()
    for field, canonicalize in _FIELDS:
        if out.get(field) is not None:
            out[field] = canonicalize(out[field])
    return out
