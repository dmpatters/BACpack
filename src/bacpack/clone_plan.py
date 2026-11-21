"""A raw controller read (clone_reader) turned into the rows a clone saves,
and the lists the user sees before anything is saved.

Pure: no BACnet traffic, no event loop, no database. build_plan is
deterministic, so one stored read can be planned again after the user changes
the new device's id or name. Every row goes through object_config.check_object
and the device through check_device, like any other save; an object a rule
refuses is left out and listed, and what depended on it is stripped and noted.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import ValidationError

from . import object_config
from .choices import canonicalize_object_fields
from .config_io import OBJECT_FIELD_DEFAULTS
from .config_schemas import UNSIGNED32_MAX, ObjectCreate
from .models import (
    ALARMABLE_TYPES,
    ANALOG_TYPES,
    BINARY_TYPES,
    COMMANDABLE_TYPES,
    INPUT_TYPES,
    MAX_TREND_SAMPLE_CAP,
    MULTISTATE_TYPES,
    OBJECT_TYPES,
)
from .objects.calendar_config import validate_calendar_entry
from .objects.notification_class import address_string
from .objects.notification_class_config import validate_notification_class_config
from .objects.reserved_names import (
    NETWORK_PORT_NAME,
    config_file_name,
    find_reserved_name_clash,
    trend_log_name,
)
from .point_config import validate_relinquish_default, validate_start_value

# BACpack builds these itself on every device, so a controller's are neither
# cloned nor a reason to ask for a new object type.
_BUILT_BY_BACPACK = ("network-port", "file")

NOT_COMMANDABLE_NOTE = "not commandable on the controller; the clone still shows a Priority_Array"
_MAX_TEXT = 64
_TREND_INTERVAL_MAX = UNSIGNED32_MAX // 100

# The text a read gives a BACnet Date ("2026-12-25 *"; * is a wildcard) and a WeekNDay.
_DATE = re.compile(r"^(\*|\d+)-(\*|\d+)-(\*|\d+) (\*|\d+)$")
_WEEK_N_DAY = re.compile(r"^WeekNDay\((\d+), (\d+), (\d+)\)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})\.(\d{2})$")


@dataclass
class ClonePlan:
    device: dict  # id, name, description, location, vendor_name, vendor_identifier
    objects: List[dict] = field(default_factory=list)  # ObjectCreate-shaped rows
    counts: Dict[str, int] = field(default_factory=dict)  # object_type -> number created
    skipped: List[dict] = field(default_factory=list)  # object_type, instance, name, reason, code
    notes: List[dict] = field(default_factory=list)  # object_type, instance, name, note
    skipped_types: Dict[str, int] = field(default_factory=dict)  # unsupported type -> count
    # A refusal no object can fix (the chosen device name); the plan can't be created.
    problem: Optional[str] = None


def build_plan(raw: dict, *, device_id: int, device_name: str) -> ClonePlan:
    return Planner(raw, device_id, device_name).plan()


class Planner:
    """Plans one controller's read. Another source of objects (an EDE file)
    subclasses it and overrides the _fill_*, _apply_log and _admit steps, so
    every rule, note and refusal stays in this module."""

    not_commandable_note = NOT_COMMANDABLE_NOTE
    done = "cloned"  # the word notes use for what was carried over

    def __init__(self, raw: dict, device_id: int, device_name: str) -> None:
        self.raw = raw
        self.device_id = device_id
        self.device_name = device_name
        self.controller = raw["device"].get("instance")
        self.rows: Dict[Tuple[str, int], dict] = {}
        self._names: Set[str] = set()  # the names of self.rows
        self._log_names: Set[str] = set()  # the trend log names the trended rows will serve
        self.skipped: List[dict] = []
        self.notes: List[dict] = []
        self.skipped_types: Counter = Counter()
        self.problem: Optional[str] = find_reserved_name_clash(device_name, [])

    # --- the plan ---------------------------------------------------------

    def plan(self) -> ClonePlan:
        logs = []
        for entry in self.raw["objects"]:
            object_type = entry["object_type"]
            if object_type not in OBJECT_TYPES and object_type not in _BUILT_BY_BACPACK and object_type != "trend-log":
                self.skipped_types[object_type] += 1
                self._skip(entry, "unsupported-type", f"{object_type} objects are not supported")
            elif entry.get("error"):
                self._skip(entry, "read-failed", entry["error"])
            elif object_type in _BUILT_BY_BACPACK:
                self._note(entry, f"not {self.done}: BACpack builds its own {object_type} object")
            elif object_type == "trend-log":
                logs.append(entry)
            else:
                self._add_point(entry)
        self._strip_dependents()
        for entry in logs:
            self._apply_log(entry)
        self._settle()
        device = self.raw["device"]
        vendor_id = device.get("vendor_identifier")
        return ClonePlan(
            device={
                "id": self.device_id,
                "name": self.device_name,
                "description": device.get("description"),
                "location": device.get("location"),
                "vendor_name": device.get("vendor_name"),
                "vendor_identifier": vendor_id if is_int(vendor_id) and 0 <= vendor_id <= 65535 else None,
            },
            objects=list(self.rows.values()),
            counts=dict(Counter(row["object_type"] for row in self.rows.values())),
            skipped=self.skipped,
            notes=self.notes,
            skipped_types=dict(self.skipped_types),
            problem=self.problem,
        )

    def _label(self, entry: dict) -> str:
        name = as_text(entry["props"].get("object-name"))
        return name.strip() if name and name.strip() else f"{entry['object_type']}:{entry['instance']}"

    def _skip(self, entry: dict, code: str, reason: str) -> None:
        self.skipped.append(
            {
                "object_type": entry["object_type"],
                "instance": entry["instance"],
                "name": self._label(entry),
                "reason": reason,
                "code": code,
            }
        )

    def _note(self, entry: dict, note: str, name: Optional[str] = None) -> None:
        self.notes.append(
            {
                "object_type": entry["object_type"],
                "instance": entry["instance"],
                "name": name if name is not None else self._label(entry),
                "note": note,
            }
        )

    def _note_failed_reads(self, entry: dict, name: str) -> None:
        """Every property the controller refused for a reason other than not
        having it: left unset, so the user is told."""
        for prop, code in (entry.get("failed") or {}).items():
            self._note(entry, f"{prop} could not be read: {code}", name)

    def _refusal(self, row: dict) -> Optional[str]:
        """The message of the first rule that refuses `row`, or None."""
        try:
            object_config.check_object(row)
            ObjectCreate.model_validate(row)
        except ValidationError as exc:
            problem = exc.errors()[0]
            return f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
        except ValueError as exc:
            return str(exc)
        return None

    # --- points -------------------------------------------------------------

    def _add_point(self, entry: dict) -> None:
        object_type, props = entry["object_type"], entry["props"]
        name = as_text(props.get("object-name")) or ""
        if name.strip() != name and name.strip():
            self._note(entry, f"name trimmed (was {name!r})", name.strip())
        row = {
            **OBJECT_FIELD_DEFAULTS,
            "object_type": object_type,
            "instance_number": entry["instance"],
            "name": name.strip(),
            "description": as_text(props.get("description")) or None,
        }
        if object_type == "schedule" and "schedule-default" in (entry.get("failed") or {}):
            # Cloning it as Null would silently change what the schedule does.
            self._skip(entry, "read-failed", f"schedule default could not be read: {entry['failed']['schedule-default']}")
            return
        self._note_failed_reads(entry, row["name"])
        if object_type in ANALOG_TYPES:
            self._fill_analog(entry, row)
        elif object_type in BINARY_TYPES:
            self._fill_binary(entry, row)
        elif object_type in MULTISTATE_TYPES:
            self._fill_multistate(entry, row)
        elif object_type == "schedule":
            self._fill_schedule(entry, row)
        elif object_type == "calendar":
            problem = self._fill_calendar(entry, row)
            if problem is not None:
                self._skip(entry, "refused", problem)
                return
        else:
            self._fill_notification_class(entry, row)
        if object_type in COMMANDABLE_TYPES and props.get("commandable") is False:
            row["bacnet_writable"] = False
            self._note(entry, self.not_commandable_note, row["name"])
        if object_type in ALARMABLE_TYPES:
            self._fill_alarm(entry, row)
        row = canonicalize_object_fields(row)
        self._admit(entry, row)

    def _admit(self, entry: dict, row: dict) -> None:
        problem = self._refusal(row)
        if problem is not None and row["alarming_enabled"]:
            # The alarm settings are the controller's, not what makes the
            # point: keep the point without them, if that is what cures it.
            plain = {**row, "alarming_enabled": False, "notification_class_instance": None}
            remaining = self._refusal(plain)
            if remaining is None:
                row.update(alarming_enabled=False, notification_class_instance=None)
                self._note(entry, f"alarm settings not {self.done}: {problem}", row["name"])
            problem = remaining
        if problem is None and row["name"] in self._names:
            problem = f"an object named {row['name']!r} already exists on this device"
        if problem is None and self.problem is None:
            problem = find_reserved_name_clash(self.device_name, [row])
        if problem is not None:
            self._skip(entry, "refused", problem)
            return
        self.rows[(row["object_type"], row["instance_number"])] = row
        self._names.add(row["name"])

    def _point_value(self, entry, row, value, what: str, check) -> Optional[str]:
        """A present value or relinquish default as the saved text, or None
        (noted) when the controller's value can't be stored."""
        text = _stored_point_value(row["object_type"], value)
        if text is None:
            return None
        try:
            check(row["object_type"], row.get("state_text"), text)
        except ValueError as exc:
            self._note(entry, f"{what} not {self.done}: {exc}", row["name"])
            return None
        return text

    def _fill_analog(self, entry, row) -> None:
        props = entry["props"]
        row["units"] = as_text(props.get("units"))
        self._fill_value(entry, row)

    def _fill_value(self, entry, row) -> None:
        """Input start value, or the Relinquish_Default of an output or value."""
        props, object_type = entry["props"], row["object_type"]
        if object_type in INPUT_TYPES:
            if "present-value" in props:
                row["start_value"] = self._point_value(
                    entry, row, props["present-value"], "start value", _check_start_value
                )
        elif "relinquish-default" in props:
            row["relinquish_default"] = self._point_value(
                entry, row, props["relinquish-default"], "relinquish default", _check_relinquish_default
            )

    def _fill_binary(self, entry, row) -> None:
        props = entry["props"]
        for field_name, prop in (("active_text", "active-text"), ("inactive_text", "inactive-text")):
            text = as_text(props.get(prop))
            if text is None:
                continue
            cleaned = text.strip()[:_MAX_TEXT].strip()
            if cleaned and cleaned != text:
                self._note(entry, f"{prop} {text!r} was changed to fit: {cleaned!r}", row["name"])
            text = cleaned
            row[field_name] = text or None
        self._fill_value(entry, row)

    def _fill_multistate(self, entry, row) -> None:
        props = entry["props"]
        state_text = props.get("state-text")
        states = [as_text(item) or "" for item in state_text] if isinstance(state_text, list) else []
        count = props.get("number-of-states")
        count = count if is_int(count) and 0 < count <= 4096 else 0
        if len(states) < count:
            if not states:
                self._note(entry, "the controller gave no state texts; named State 1, State 2, ...", row["name"])
            states += [f"State {number}" for number in range(len(states) + 1, count + 1)]
        row["state_text"] = states or None
        self._fill_value(entry, row)

    # --- alarms and notification classes --------------------------------------

    def _fill_alarm(self, entry, row) -> None:
        props, object_type = entry["props"], row["object_type"]
        notification_class = props.get("notification-class")
        if not is_int(notification_class):
            return  # no intrinsic reporting
        row["alarming_enabled"] = True
        row["notification_class_instance"] = notification_class
        if is_int(props.get("time-delay")):
            row["alarm_time_delay"] = props["time-delay"]
        if is_int(props.get("time-delay-normal")):
            row["alarm_time_delay_normal"] = props["time-delay-normal"]
        if props.get("notify-type") in ("alarm", "event"):
            row["alarm_notify_type"] = props["notify-type"]
        row["alarm_event_enable"] = as_bits(props.get("event-enable"), 3)
        if isinstance(props.get("event-detection-enable"), bool):
            row["alarm_event_detection_enable"] = props["event-detection-enable"]
        if object_type in ANALOG_TYPES:
            row["alarm_high_limit"] = as_number(props.get("high-limit"))
            row["alarm_low_limit"] = as_number(props.get("low-limit"))
            deadband = as_number(props.get("deadband"))
            row["alarm_deadband"] = 0.0 if deadband is None else deadband
            row["alarm_limit_enable"] = as_bits(props.get("limit-enable"), 2)
        elif object_type in BINARY_TYPES:
            row["alarm_value"] = {"active": True, "inactive": False}.get(props.get("alarm-value"))
        else:
            values = props.get("alarm-values")
            row["alarm_values"] = [v for v in values if is_int(v)] if isinstance(values, list) else None

    def _fill_notification_class(self, entry, row) -> None:
        props = entry["props"]
        priority = props.get("priority")
        if isinstance(priority, list) and len(priority) == 3 and all(is_int(p) for p in priority):
            row["notification_priority"] = list(priority)
        row["notification_ack_required"] = as_bits(props.get("ack-required"), 3)
        entries = []
        for number, item in enumerate(props.get("recipient-list") or [], start=1):
            try:
                entries.append(_recipient_entry(item))
            except (ValueError, KeyError, TypeError) as exc:
                self._note(entry, f"recipient {number} not cloned: {exc}", row["name"])
        row["notification_recipient_list"] = entries or None

    # --- schedules and calendars ---------------------------------------------

    def _fill_calendar(self, entry, row) -> Optional[str]:
        """A refusal when the controller has dates and none could be cloned:
        with none, the copy would never be in effect where the original is."""
        source = entry["props"].get("date-list") or []
        entries = []
        for item in source:
            try:
                entries.append(_calendar_entry(item))
            except ValueError as exc:
                self._note(entry, f"date list entry {item!r} not cloned: {exc}", row["name"])
        if source and not entries:
            return "none of its dates could be cloned"
        row["date_list"] = entries or None
        return None

    def _fill_schedule(self, entry, row) -> None:
        props, label = entry["props"], row["name"]
        value_type, default = _schedule_default(props.get("schedule-default"))
        if props.get("schedule-default") is None:
            if "schedule-default" not in props:
                self._note(entry, "the controller does not have a schedule default; cloned as not set", label)
            value_type = self._inferred_value_type(props)
            if value_type is None:
                self._note(
                    entry,
                    "the value type could not be told: the default is not set and no time value or "
                    "write target says whether it is analog, binary or multi-state",
                    label,
                )
        row["schedule_value_type"], row["schedule_default"] = value_type, default
        if is_int(props.get("priority-for-writing")):
            row["schedule_priority"] = props["priority-for-writing"]
        if value_type is None:
            return  # check_object refuses it, naming the value type
        cut = []

        def time_values(values, where: str) -> List[dict]:
            out, seen = [], set()
            for item in values or []:
                try:
                    stored = _time_value(item, value_type, cut)
                except ValueError as exc:
                    self._note(entry, f"{where}: time value not cloned: {exc}", label)
                    continue
                if stored["time"] in seen:
                    self._note(entry, f"{where}: a second value at {stored['time']} not cloned", label)
                    continue
                seen.add(stored["time"])
                out.append(stored)
            return out

        weekly = props.get("weekly-schedule")
        if isinstance(weekly, list) and len(weekly) == 7:
            row["weekly_schedule"] = [time_values((day or {}).get("daySchedule"), f"weekly day {n}")
                                      for n, day in enumerate(weekly, start=1)]
        elif weekly:
            self._note(entry, "the weekly schedule did not have 7 days and was not cloned", label)
        exceptions = []
        for number, event in enumerate(props.get("exception-schedule") or [], start=1):
            priority = event.get("eventPriority")
            try:
                if not is_int(priority):
                    raise ValueError("no event priority")
                period = _period(event.get("period"))
            except ValueError as exc:
                self._note(entry, f"exception {number} not cloned: {exc}", label)
                continue
            exceptions.append(
                {"period": period, "priority": priority,
                 "time_values": time_values(event.get("listOfTimeValues"), f"exception {number}")}
            )
        row["exception_schedule"] = exceptions or None
        row["schedule_effective_start"], row["schedule_effective_end"] = self._effective_period(
            entry, props.get("effective-period"), label
        )
        row["schedule_write_targets"] = self._write_targets(entry, props.get("list-of-object-property-references"), label)
        if cut:
            self._note(entry, "times were cut to the minute (BACpack schedules have minute resolution)", label)

    def _inferred_value_type(self, props: dict) -> Optional[str]:
        """The value type of a schedule whose Schedule_Default is Null: that
        of its first non-null weekly, then exception, time value, else of its
        first write target on this controller."""
        values = [
            item.get("value")
            for day in (props.get("weekly-schedule") or [])
            for item in ((day or {}).get("daySchedule") or [])
        ]
        values += [
            item.get("value")
            for event in (props.get("exception-schedule") or [])
            for item in (event.get("listOfTimeValues") or [])
        ]
        for value in values:
            if value is not None and (found := _schedule_default(value)[0]) is not None:
                return found
        for ref in props.get("list-of-object-property-references") or []:
            device = ref.get("deviceIdentifier")
            if device is not None and _identifier(device)[1] != self.controller:
                continue
            object_type = _identifier(ref.get("objectIdentifier"))[0]
            if object_type in ANALOG_TYPES:
                return "analog"
            if object_type in BINARY_TYPES:
                return "binary"
            if object_type in MULTISTATE_TYPES:
                return "multi-state"
        return None

    def _effective_period(self, entry, period, label) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(period, dict):
            return None, None
        ends = []
        for key in ("startDate", "endDate"):
            text = period.get(key)
            if not isinstance(text, str) or text.startswith("*-*-*"):
                ends.append(None)
                continue
            try:
                ends.append(_iso_date(text))
            except ValueError as exc:
                self._note(entry, f"effective period not cloned: {exc}", label)
                return None, None
        return ends[0], ends[1]

    def _write_targets(self, entry, references, label) -> Optional[list]:
        """The targets on this controller's own Present_Value. Whether the
        object is cloned is known later (_strip_dependents)."""
        targets = []
        for ref in references or []:
            identifier = ref.get("objectIdentifier")
            device = ref.get("deviceIdentifier")
            if device is not None and _identifier(device)[1] != self.controller:
                self._note(entry, f"write target {identifier} not cloned: it is on another device ({device})", label)
            elif ref.get("propertyIdentifier") != "present-value":
                self._note(
                    entry,
                    f"write target {identifier} not cloned: a schedule here can only write Present_Value, "
                    f"not {ref.get('propertyIdentifier')}",
                    label,
                )
            elif _identifier(identifier)[0] not in OBJECT_TYPES or _identifier(identifier)[0] in ("schedule", "calendar"):
                self._note(entry, f"write target {identifier} not cloned: its object type is not cloned or written", label)
            else:
                object_type, instance = _identifier(identifier)
                targets.append({"object_type": object_type, "instance_number": instance,
                                "property_identifier": "presentValue"})
        return targets or None

    # --- what depends on something that was left out ------------------------------

    def _entry_of(self, row: dict) -> dict:
        return {"object_type": row["object_type"], "instance": row["instance_number"], "props": {}}

    def _strip_dependents(self) -> None:
        present = set(self.rows)
        for row in self.rows.values():
            entry = self._entry_of(row)
            if row["alarming_enabled"] and ("notification-class", row["notification_class_instance"]) not in present:
                self._note(
                    entry,
                    f"alarm settings not {self.done}: its notification class {row['notification_class_instance']} "
                    f"was not {self.done}",
                    row["name"],
                )
                row["alarming_enabled"] = False
                row["notification_class_instance"] = None
            if row["object_type"] != "schedule":
                continue
            kept = []
            for event in row["exception_schedule"] or []:
                period = event["period"]
                if period["kind"] == "calendar-reference" and ("calendar", period["instance_number"]) not in present:
                    self._note(
                        entry,
                        f"exception {event['priority']} not {self.done}: its calendar {period['instance_number']} "
                        f"was not {self.done}",
                        row["name"],
                    )
                else:
                    kept.append(event)
            row["exception_schedule"] = kept or None
            targets = []
            for target in row["schedule_write_targets"] or []:
                key = (target["object_type"], target["instance_number"])
                if key in present and key[0] not in ("schedule", "calendar"):
                    targets.append(target)
                else:
                    self._note(
                        entry,
                        f"write target {key[0]}:{key[1]} not {self.done}: the object was not {self.done}",
                        row["name"],
                    )
            row["schedule_write_targets"] = targets or None

    # --- trends ---------------------------------------------------------------------

    def _apply_log(self, entry: dict) -> None:
        """Mark the point a real Trend Log records as trended, keeping the
        log's instance and name. A log that can't be matched is noted."""
        props = entry["props"]
        label = self._label(entry)
        self._note_failed_reads(entry, label)

        def refuse(why: str) -> None:
            self._note(entry, f"trend log {label!r} not cloned: {why}", label)

        target = props.get("log-device-object-property")
        if not isinstance(target, dict):
            return refuse("it does not say what it records")
        device = target.get("deviceIdentifier")
        if device is not None and _identifier(device)[1] != self.controller:
            return refuse(f"it records another device ({device})")
        if target.get("propertyIdentifier") != "present-value":
            return refuse(f"it records {target.get('propertyIdentifier')}, not Present_Value")
        if props.get("trigger") is True or props.get("logging-type") not in ("polled", "cov"):
            return refuse(f"{props.get('logging-type')} (triggered) logging is not supported")
        object_type, instance = _identifier(target.get("objectIdentifier"))
        row = None if instance is None else self.rows.get((object_type, instance))
        if row is None:
            return refuse(f"the object it records ({object_type}:{instance}) was not cloned")
        if row["trended"]:
            return refuse(f"{row['name']!r} already has the trend log {row['trend_log_name'] or ''!r}".replace(" ''", ""))
        interval = props.get("log-interval")
        if props.get("logging-type") == "polled" and not (is_int(interval) and interval > 0):
            return refuse("it samples at no interval")

        name = (as_text(props.get("object-name")) or "").strip() or None
        effective_name = name or trend_log_name(row["name"])
        clash = self._log_name_clash(row, effective_name)
        if clash is not None:
            return refuse(clash)

        settings = {
            "trended": True,
            "trend_mode": "cov" if props.get("logging-type") == "cov" else "interval",
            "trend_log_instance": entry["instance"],
            "trend_log_name": name,
        }
        if settings["trend_mode"] == "interval":
            settings["trend_interval_seconds"] = min(max(1, interval // 100), _TREND_INTERVAL_MAX)
        size = props.get("buffer-size")
        if is_int(size) and size >= 1:
            settings["trend_sample_cap"] = min(size, MAX_TREND_SAMPLE_CAP)
        before = {key: row[key] for key in settings}
        row.update(settings)
        problem = self._refusal(row)
        if problem is not None:
            row.update(before)
            refuse(problem)
        else:
            self._log_names.add(effective_name)

    def _log_name_clash(self, row: dict, log_name: str) -> Optional[str]:
        if log_name in self._names:
            return f"its name {log_name!r} is already an object's name"
        if log_name == self.device_name:
            return f"its name {log_name!r} is the new device's name"
        if log_name in (NETWORK_PORT_NAME, config_file_name(self.device_name)):
            return f"its name {log_name!r} is reserved"
        if log_name in self._log_names:
            return f"its name {log_name!r} is another trend log's name"
        return None

    # --- the whole device ------------------------------------------------------------

    def _settle(self) -> None:
        """check_device over what is left. The per-object rules have run, so
        this should pass; if it names an object, that object goes."""
        if self.problem is not None:
            return
        for _ in range(len(self.rows) + 1):
            problem = object_config.check_device(self.device_name, list(self.rows.values()))
            if problem is None:
                return
            culprits = [key for key, row in self.rows.items() if repr(row["name"]) in problem]
            if not culprits:
                self.problem = problem
                return
            row = self.rows.pop(culprits[-1])
            self._skip(
                {"object_type": row["object_type"], "instance": row["instance_number"],
                 "props": {"object-name": row["name"]}},
                "refused",
                problem,
            )
            self._strip_dependents()
        self.problem = "the objects could not be made to pass the device check"


# --- value conversions ---------------------------------------------------------------


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def as_text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def as_bits(value: Any, length: int) -> Optional[List[bool]]:
    if isinstance(value, list) and len(value) == length:
        return [bool(bit) for bit in value]
    return None


def _check_start_value(object_type, state_text, value) -> None:
    validate_start_value(object_type, state_text, value)


def _check_relinquish_default(object_type, state_text, value) -> None:
    validate_relinquish_default(object_type, state_text, value)


def _stored_point_value(object_type: str, value: Any) -> Optional[str]:
    """A read value in the stored text of start_value/relinquish_default
    (the form validate_relinquish_default documents)."""
    if object_type in ANALOG_TYPES:
        number = as_number(value)
        return None if number is None else str(number)
    if object_type in BINARY_TYPES:
        return _binary_word(value)
    return str(value) if is_int(value) else None


def _binary_word(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "active" if value else "inactive"
    return {"active": "active", "inactive": "inactive", "1": "active", "0": "inactive", 1: "active", 0: "inactive"}.get(
        value
    )


def _identifier(text: Any) -> Tuple[str, Optional[int]]:
    """("analog-input", 3) from "analog-input:3"."""
    object_type, _, instance = str(text).rpartition(":")
    return object_type, int(instance) if instance.isdigit() else None


def _schedule_default(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """The schedule's value type and Schedule_Default text. A read gives the
    value untyped (an AnyAtomic): a real is analog, an unsigned multi-state,
    and a binary arrives as "0"/"1" or a boolean."""
    if isinstance(value, bool) or value in ("0", "1", "active", "inactive"):
        return "binary", _binary_word(value)
    if is_int(value):
        return "multi-state", str(value)
    number = as_number(value)
    if number is not None:
        return "analog", str(number)
    return None, None


def _time_value(item: dict, value_type: str, cut: list) -> dict:
    """{"time": "08:00", "value": "50.0" | None} from a read time value."""
    match = _TIME.match(str(item.get("time")))
    if match is None:
        raise ValueError(f"unusable time {item.get('time')!r}")
    hour, minute, second, hundredth = (int(part) for part in match.groups())
    if second or hundredth:
        cut.append(True)
    value = item.get("value")
    if value is None:
        stored = None
    elif value_type == "analog":
        number = as_number(value)
        if number is None:
            raise ValueError(f"{value!r} is not an analog value")
        stored = str(number)
    elif value_type == "binary":
        stored = _binary_word(value)
        if stored is None:
            raise ValueError(f"{value!r} is not a binary value")
    else:
        if not is_int(value):
            raise ValueError(f"{value!r} is not a multi-state value")
        stored = str(value)
    return {"time": f"{hour:02d}:{minute:02d}", "value": stored}


def _iso_date(text: str) -> str:
    match = _DATE.match(text)
    if match is None or "*" in match.groups()[:3]:
        raise ValueError(f"{text!r} is not a concrete date")
    year, month, day = (int(part) for part in match.groups()[:3])
    return f"{year:04d}-{month:02d}-{day:02d}"


def _calendar_entry(item: Any) -> dict:
    """A calendar entry in the stored form, from the text or dict a read gives
    for each of the three kinds (the read drops which kind it was, so the
    shape tells). ValueError for one BACpack cannot store."""
    if isinstance(item, dict) and "startDate" in item:
        entry = {"kind": "date-range", "start": _iso_date(str(item["startDate"])), "end": _iso_date(str(item["endDate"]))}
    elif isinstance(item, str) and _WEEK_N_DAY.match(item):
        month, week, day = (int(part) for part in _WEEK_N_DAY.match(item).groups())  # pyright: ignore[reportOptionalMemberAccess] -- matched above
        entry = {
            "kind": "week-and-day",
            "month": "*" if month == 255 else str(month),
            "week_of_month": "*" if week == 255 else ("last" if week == 6 else str(week)),
            "day_of_week": "*" if day == 255 else str(day),
        }
    elif isinstance(item, str) and _DATE.match(item):
        year, month, day, weekday = _DATE.match(item).groups()  # pyright: ignore[reportOptionalMemberAccess] -- matched above
        entry = {"kind": "date", "year": year, "month": month, "day": day, "day_of_week": weekday}
    else:
        raise ValueError("an entry BACpack can't store")
    validate_calendar_entry(entry)
    return entry


def _period(period: Any) -> dict:
    if isinstance(period, str) and period.startswith("calendar:"):
        instance = _identifier(period)[1]
        if instance is None:
            raise ValueError(f"unusable calendar reference {period!r}")
        return {"kind": "calendar-reference", "instance_number": instance}
    return _calendar_entry(period)


def _recipient_entry(item: dict) -> dict:
    recipient = item["recipient"]
    if isinstance(recipient, str) and recipient.startswith("device:"):
        stored = {"kind": "device", "instance_number": _identifier(recipient)[1]}
    elif isinstance(recipient, dict):
        stored = {
            "kind": "address",
            "address": address_string(int(recipient["networkNumber"]), bytes.fromhex(str(recipient["macAddress"]))),
        }
    else:
        raise ValueError("unknown recipient")
    entry = {
        "recipient": stored,
        "valid_days": [bool(day) for day in item["validDays"]],
        "from_time": item["fromTime"],
        "to_time": item["toTime"],
        "transitions": [bool(t) for t in item["transitions"]],
        "confirmed": bool(item["issueConfirmedNotifications"]),
        "process_identifier": item["processIdentifier"],
    }
    validate_notification_class_config(None, None, [entry])
    return entry

