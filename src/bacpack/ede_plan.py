"""One EDE device's rows planned like a clone's read.

to_raw turns the rows into the shape clone_reader gives, and EdePlanner
extends clone_plan.Planner, so the device rules, name trimming, skipped lists,
notes and skipped_types all come from clone_plan; this module only holds what
is particular to a file: no alarming without a notification class, empty
stand-ins, duplicate names, text-typed defaults.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from bacpypes3.basetypes import ObjectType

from .clone_plan import ClonePlan, Planner, as_number
from .csv_io import DEFAULT_STATE_TEXT
from .ede_io import EdeRow, parse_number
from .models import ANALOG_TYPES, BINARY_TYPES, COMMANDABLE_TYPES, INPUT_TYPES, MULTISTATE_TYPES, OBJECT_TYPES
from .objects.reserved_names import trend_log_name

_VALUE_TYPES = ("analog-value", "binary-value", "multi-state-value")
_UNSUPPORTED = "unsupported:"
_NOT_COMMANDABLE_IN_FILE = "not commandable in the file; the simulated object still shows a Priority_Array"
# What an EDE row of these types leaves for the user to fill in.
_STAND_IN_NOTES = {
    "schedule": "created empty: set its value type, times and targets",
    "calendar": "created empty: add its dates",
    "notification-class": "created empty: add its recipients",
}


def unsupported_type_name(text: str) -> str:
    """`event-enrollment (9)` for a standard type number, else the text as given."""
    if text.isascii() and text.isdigit():
        try:
            name = str(ObjectType(int(text)))
        except ValueError:
            return text
        if name != text:
            return f"{name} ({text})"
    return text


def device_name_of(rows: List[EdeRow], device_instance: int) -> str:
    """The device row's name, else `Device <instance>`."""
    for row in rows:
        if row.device_instance == device_instance and row.object_type == "device":
            return row.name
    return f"Device {device_instance}"


def _props(row: EdeRow) -> Dict[str, Any]:
    props: Dict[str, Any] = {"object-name": row.name}
    if row.description is not None:
        props["description"] = row.description
    object_type = row.object_type
    if object_type in ANALOG_TYPES:
        if row.units is not None:
            props["units"] = row.units
        for key, value in (("high-limit", row.hi_limit), ("low-limit", row.low_limit)):
            if value is not None:
                props[key] = value
    if object_type in BINARY_TYPES and row.state_texts:
        props["inactive-text"] = row.state_texts[0]
        if len(row.state_texts) > 1:
            props["active-text"] = row.state_texts[1]
    if object_type in MULTISTATE_TYPES and row.state_texts:
        props["state-text"] = list(row.state_texts)
        props["number-of-states"] = len(row.state_texts)
    if row.default is not None:
        if object_type in INPUT_TYPES:
            props["present-value"] = row.default
        elif object_type in COMMANDABLE_TYPES:
            props["relinquish-default"] = row.default
    if object_type in _VALUE_TYPES and False in row.writable:
        props["commandable"] = False
    if row.notification_class is not None:
        props["notification-class"] = row.notification_class
    return props


def to_raw(rows: List[EdeRow], *, device_instance: int) -> dict:
    """The clone planner's raw-read shape for one device of the file."""
    mine = [row for row in rows if row.device_instance == device_instance]
    device = {"instance": device_instance, "name": device_name_of(mine, device_instance), "description": None}
    for row in mine:
        if row.object_type == "device":
            device["description"] = row.description
            break
    else:
        device["notes"] = [f"no device row; named {device['name']}"]
    objects = []
    for row in mine:
        if row.object_type == "device":
            continue
        object_type = row.object_type
        if object_type.startswith(_UNSUPPORTED):
            object_type = unsupported_type_name(object_type[len(_UNSUPPORTED):])
        objects.append({"object_type": object_type, "instance": row.instance, "props": _props(row), "ede": row})
    return {"device": device, "objects": objects}


_NAME_SEPARATORS = ".-_ :"


def _starts_with(text: str, prefix: str) -> bool:
    """`text` is `prefix`, or `prefix` followed by a separator and more."""
    return text == prefix or (text.startswith(prefix) and text[len(prefix)] in _NAME_SEPARATORS)


def _typed_default(object_type: str, text: str) -> Optional[Any]:
    """A present-value-default cell as the value the planner checks, or None
    when it is not a value of that object type."""
    text = text.strip()
    if object_type in ANALOG_TYPES:
        return parse_number(text)
    if object_type in BINARY_TYPES:
        word = text.lower()
        if word in ("1", "active", "on", "true"):
            return 1
        if word in ("0", "inactive", "off", "false"):
            return 0
        return None
    return int(text) if text.isascii() and text.isdigit() else None


@dataclass
class ExistingDevice:
    """The device an EDE file is imported into: how (merge only adds, sync
    makes the file the device's object list) and its objects as saved."""

    mode: Literal["merge", "sync"]
    rows: Dict[Tuple[str, int], dict]  # object_config.row_from_object dicts


def _effective_log_name(row: dict) -> str:
    return row.get("trend_log_name") or trend_log_name(row["name"])


class EdePlanner(Planner):
    not_commandable_note = _NOT_COMMANDABLE_IN_FILE
    done = "imported"

    def __init__(
        self,
        raw: dict,
        device_id: int,
        device_name: str,
        *,
        ede_rows: List[EdeRow],
        existing: Optional[ExistingDevice] = None,
    ) -> None:
        super().__init__(raw, device_id, device_name)
        self.ede_rows: Dict[Tuple[str, int], EdeRow] = {}
        for row in ede_rows:
            self.ede_rows.setdefault((row.object_type, row.instance), row)
        self.existing = existing
        # Sync, objects already on the device: only what the file gave, as the sync is fed it.
        self.partials: Dict[Tuple[str, int], dict] = {}
        self._logged: dict = {}  # points a trend log of this file turned trended -> (log entry, settings before)
        self._class_of: Dict[Tuple[str, int], int] = {}  # a point -> the notification class its row names
        self._noted_class: set = set()
        self._noted_trended: set = set()  # kept trend settings already noted
        if existing is not None and existing.mode == "merge":
            # A merge changes no existing object, so their names are taken.
            self._names.update(row["name"] for row in existing.rows.values())
            self._log_names.update(_effective_log_name(row) for row in existing.rows.values() if row.get("trended"))

    def _ede(self, entry: dict) -> Optional[EdeRow]:
        """The file row an entry was made from (two rows may share a type and instance)."""
        if "ede" in entry:
            return entry["ede"]
        return self.ede_rows.get((entry["object_type"], entry["instance"]))

    def _on_device(self, entry: dict) -> Optional[dict]:
        """The saved row of the object this entry is, in a sync."""
        if self.existing is None or self.existing.mode != "sync":
            return None
        return self.existing.rows.get((entry["object_type"], entry["instance"]))

    def _class_in_import(self, number: int) -> bool:
        if ("notification-class", number) in self.rows:
            return True
        return (
            self.existing is not None
            and self.existing.mode == "merge"
            and ("notification-class", number) in self.existing.rows
        )

    # --- points -------------------------------------------------------------

    def _add_point(self, entry: dict) -> None:
        key = (entry["object_type"], entry["instance"])
        if self.existing is not None and self.existing.mode == "merge" and key in self.existing.rows:
            self._skip(entry, "already-present", "already on the device")
            return
        before = self.rows.get(key)
        super()._add_point(entry)
        row = self.rows.get(key)
        ede = self._ede(entry)
        if row is None or row is before or ede is None:
            return  # refused, or skipped as a repeat of an earlier row
        if row["object_type"] in _STAND_IN_NOTES and self._on_device(entry) is None:
            self._note(entry, _STAND_IN_NOTES[row["object_type"]], row["name"])
        for note in ede.notes:
            self._note(entry, note, row["name"])
        if row["object_type"] not in ANALOG_TYPES and (ede.hi_limit is not None or ede.low_limit is not None):
            self._note(entry, "hi-limit and low-limit not imported: only analog objects keep limits", row["name"])
        if ede.notification_class is not None:
            self._class_of[key] = ede.notification_class

    def _admit(self, entry: dict, row: dict) -> None:
        if (row["object_type"], row["instance_number"]) in self.rows:
            self._skip(
                entry, "refused", f"another object in the file is also {row['object_type']}:{row['instance_number']}"
            )
            return
        name = row["name"]
        key = (row["object_type"], row["instance_number"])
        renamed = None
        if name in self._names or name in self._log_names:
            row["name"] = self._free_name(name)
            renamed = f"named {row['name']!r}; {self._taken_because(name, key)}"
        present = self._on_device(entry)
        if present is None:
            super()._admit(entry, row)
            if renamed is not None and self.rows.get(key) is row:
                self._note(entry, renamed, row["name"])
            return
        # Checked as it would be saved: the stored row with what the file gave.
        fields = self._file_fields(entry, row)
        overlay = {**present, **fields, "name": row["name"]}
        super()._admit(entry, overlay)
        if self.rows.get(key) is not overlay:
            self._keep_name(key, present["name"])
            return
        if renamed is not None:
            self._note(entry, renamed, row["name"])
        self.partials[key] = {"object_type": key[0], "instance_number": key[1], "name": overlay["name"], **fields}
        if overlay["trended"]:
            log_name = _effective_log_name(overlay)
            self._log_names.add(log_name)
            for other, earlier in list(self.rows.items()):
                if other != key and earlier["name"] == log_name:
                    self._rename_row(other, f"the trend log of {overlay['name']!r} is named {log_name!r}")

    def _free_name(self, name: str) -> str:
        number = 2
        while f"{name} ({number})" in self._names or f"{name} ({number})" in self._log_names:
            number += 1
        return f"{name} ({number})"

    def _log_owner(self, log_name: str) -> Optional[str]:
        """The name of the object whose trend log is called `log_name`."""
        rows = list(self.rows.values())
        if self.existing is not None and self.existing.mode == "merge":
            rows += list(self.existing.rows.values())
        for row in rows:
            if row.get("trended") and _effective_log_name(row) == log_name:
                return row["name"]
        return None

    def _taken_because(self, name: str, key: Tuple[str, int]) -> str:
        if name in self._names:
            if self.existing is not None and any(
                other["name"] == name for other_key, other in self.existing.rows.items() if other_key != key
            ):
                return f"the device already has an object named {name!r}"
            return f"the file has another object named {name!r}"
        owner = self._log_owner(name)
        return f"the trend log of {owner!r} is named {name!r}" if owner else f"a trend log is named {name!r}"

    def _rename_row(self, key: Tuple[str, int], why: str) -> None:
        """Give a planned row the next free numbered name, and say why."""
        row = self.rows[key]
        trended = row.get("trended") and not row.get("trend_log_name")
        if trended:
            self._log_names.discard(_effective_log_name(row))
        row["name"] = self._free_name(row["name"])
        if key in self.partials:
            self.partials[key]["name"] = row["name"]
        self._names.add(row["name"])
        if trended:
            self._log_names.add(_effective_log_name(row))
        self._note(self._entry_of(row), f"named {row['name']!r}; {why}", row["name"])

    def _drop_log(self, key: Tuple[str, int], why: str) -> None:
        """Take back the trending a file's log gave a point, and list the log."""
        entry, before = self._logged.pop(key)
        row = self.rows[key]
        self._log_names.discard(row["trend_log_name"])
        row.update(before)
        for field in before:
            if key in self.partials:
                self.partials[key].pop(field, None)
        self._skip(entry, "refused", why)

    def _keep_name(self, key: Tuple[str, int], name: str) -> None:
        """An object a rule refused stays on the device under its saved name:
        a row of the file that took the name, or whose trend log is called
        that, is renamed."""
        for other, row in list(self.rows.items()):
            if other == key:
                continue
            if row["name"] == name:
                self._rename_row(other, f"the device already has an object named {name!r}")
            elif other in self._logged and row.get("trend_log_name") == name:
                self._drop_log(other, f"its name {name!r} is the name of an object that stays on the device")
            elif row.get("trended") and _effective_log_name(row) == name:
                self._rename_row(other, f"the device already has an object named {name!r}, the name of its trend log")
        self._names.add(name)

    def _file_fields(self, entry: dict, row: dict) -> dict:
        """The planned row's values for the fields the file gave a usable cell
        for. Anything else of an object already on the device stays."""
        ede = self._ede(entry)
        object_type = row["object_type"]
        if ede is None:
            return {}
        supplied, out = ede.supplied, {}
        if "description" in supplied:
            out["description"] = row["description"]
        if "units" in supplied and object_type in ANALOG_TYPES:
            out["units"] = row["units"]
        if "default" in supplied:
            field = "start_value" if object_type in INPUT_TYPES else "relinquish_default"
            if object_type in (INPUT_TYPES | COMMANDABLE_TYPES) and row[field] is not None:
                out[field] = row[field]
        if "writable" in supplied and object_type in _VALUE_TYPES:
            out["bacnet_writable"] = row["bacnet_writable"]
        if object_type in ANALOG_TYPES:
            for attr, field in (("hi_limit", "alarm_high_limit"), ("low_limit", "alarm_low_limit")):
                if attr in supplied and row[field] is not None:
                    out[field] = row[field]
        if "state_texts" in supplied:
            if object_type in BINARY_TYPES:
                out.update({f: row[f] for f in ("active_text", "inactive_text") if row[f] is not None})
            elif object_type in MULTISTATE_TYPES:
                out["state_text"] = row["state_text"]
        return out

    def _point_value(self, entry, row, value, what: str, check) -> Optional[str]:
        if isinstance(value, str):
            typed = _typed_default(row["object_type"], value)
            if typed is None:
                self._note(
                    entry, f"{what} not imported: {value!r} is not a value of a {row['object_type']}", row["name"]
                )
                return None
            value = typed
        return super()._point_value(entry, row, value, what, check)

    def _fill_binary(self, entry, row) -> None:
        super()._fill_binary(entry, row)
        ede = self._ede(entry)
        if ede is not None and ede.state_text_missing:
            kept = "kept as they are" if self._on_device(entry) is not None else "kept On and Off"
            self._note(entry, f"state texts {ede.state_text_reference!r} not found; {kept}", row["name"])

    def _fill_multistate(self, entry, row) -> None:
        props = entry["props"]
        present = self._on_device(entry)
        if present is not None and not props.get("state-text"):
            # The file gave no texts: the object's own are the ones a default is checked against.
            row["state_text"] = present["state_text"]
            self._fill_value(entry, row)
            ede = self._ede(entry)
            if ede is not None and ede.state_text_missing:
                self._note(entry, f"state texts {ede.state_text_reference!r} not found; kept as they are", row["name"])
            return
        if not props.get("state-text"):
            ede = self._ede(entry)
            why = (
                f"state texts {ede.state_text_reference!r} not found"
                if ede is not None and ede.state_text_missing
                else "the file gives no state texts"
            )
            props["state-text"] = list(DEFAULT_STATE_TEXT)
            props["number-of-states"] = len(DEFAULT_STATE_TEXT)
            self._note(entry, f"{why}; named {', '.join(DEFAULT_STATE_TEXT)}", row["name"])
        super()._fill_multistate(entry, row)

    def plan(self) -> ClonePlan:
        plan = super().plan()
        if self.existing is None:
            return plan
        # An object the file lists but a rule refused stays as it is, under its saved name.
        kept = [
            (key, saved)
            for key, saved in self.existing.rows.items()
            if self.existing.mode == "sync" and key in self.ede_rows and key not in self.rows and key[0] in OBJECT_TYPES
        ]
        for key, saved in kept:
            self._keep_name(key, saved["name"])
        # What the merge or sync is fed: new objects whole, objects already on
        # the device with only what the file gave.
        plan.objects = [self.partials.get(key, row) for key, row in self.rows.items()]
        plan.counts = dict(Counter(row["object_type"] for key, row in self.rows.items() if key not in self.partials))
        if self.existing.mode == "sync":
            # A sync deletes what it is not fed: an object the file lists but
            # a rule refused stays as it is.
            for key, saved in kept:
                plan.objects.append({"object_type": key[0], "instance_number": key[1], "name": saved["name"]})
        return plan

    # --- stand-ins: a file row names these objects but carries nothing of them ---------

    def _fill_schedule(self, entry, row) -> None:
        # Analog is the type the Add object form starts with; a Null default
        # needs no time value to say otherwise.
        row["schedule_value_type"] = "analog"

    def _fill_calendar(self, entry, row) -> Optional[str]:
        """No dates: an EDE file never carries them."""
        row["date_list"] = None
        return None

    def _fill_notification_class(self, entry, row) -> None:
        """BACpack's defaults and no recipients."""

    # --- trend logs: a log row names a log, not what it records ---------------------

    def _apply_log(self, entry: dict) -> None:
        """Turn trending on for the one point the log's name, keyname or
        instance number points to. A log that cannot be tied to exactly one
        point is skipped and listed, never attached to a likely one."""
        ede = self._ede(entry)
        name = ede.name.strip() if ede is not None else ""
        found = self._match_log(entry, name, ede, self._created_points())
        if found is None:
            self._skip_unmatched_log(entry, name, ede)
            return
        key, how = found
        row = self.rows[key]
        if row["trended"] and key not in self._logged:
            if key in self._noted_trended:
                self._skip(entry, "standin-duplicate", f"{row['name']!r} is already trended")
                return
            self._noted_trended.add(key)
            self._note(self._entry_of(row), "already trended; its trend settings were kept", row["name"])
            return
        if row["trended"]:
            self._skip(
                entry, "standin-duplicate", f"{row['name']!r} already has the trend log {row['trend_log_name']!r}"
            )
            return
        clash = self._log_instance_clash(key, entry["instance"]) or self._log_name_clash(row, name)
        if clash is not None:
            self._skip(entry, "refused", clash)
            return
        settings = {
            "trended": True,
            "trend_mode": "interval",
            "trend_log_instance": entry["instance"],
            "trend_log_name": name,
        }
        before = {field: row[field] for field in settings}
        row.update(settings)
        problem = self._refusal(row)
        if problem is not None:
            row.update(before)
            self._skip(entry, "refused", problem)
            return
        self._log_names.add(name)
        self._logged[key] = (entry, before)
        if key in self.partials:
            self.partials[key].update(settings)
        self._note(self._entry_of(row), f"trending guessed from the trend log's {how}", row["name"])

    def _log_instance_clash(self, key: Tuple[str, int], instance: int) -> Optional[str]:
        """Why Trend Log `instance` is taken, or None. Every trended row of the
        device counts: the planned ones (a kept object's saved settings
        included) and, in a merge, the ones already on it."""
        rows = list(self.rows.items())
        if self.existing is not None and self.existing.mode == "merge":
            rows += [(k, r) for k, r in self.existing.rows.items() if k not in self.rows]
        for other, row in rows:
            explicit = row.get("trend_log_instance")
            if other != key and row.get("trended") and (row.get("id") if explicit is None else explicit) == instance:
                return f"its instance {instance} is already the trend log of {row['name']!r}"
        return None

    def _created_points(self) -> List[Tuple[Tuple[str, int], str, Optional[str]]]:
        """(key, name, keyname) of each point a log may record: the planned
        ones, which in a merge are only the ones it creates."""
        return [
            (key, row["name"], getattr(self.ede_rows.get(key), "keyname", None))
            for key, row in self.rows.items()
            if key[0] not in _STAND_IN_NOTES
        ]

    def _skip_unmatched_log(self, entry: dict, name: str, ede: Optional[EdeRow]) -> None:
        """In a merge, a log whose point is already on the device says so."""
        if self.existing is not None and self.existing.mode == "merge":
            present = [
                (key, point.name.strip(), point.keyname)
                for key, point in self.ede_rows.items()
                if key in self.existing.rows and key[0] not in _STAND_IN_NOTES
            ]
            found = self._match_log(entry, name, ede, present)
            if found is not None:
                point = self.ede_rows[found[0]].name.strip()
                self._skip(entry, "already-present", f"its point {point!r} is already on the device")
                return
        self._skip(entry, "standin-unmatched", "an EDE file doesn't say which point this trend log records")

    def _match_log(
        self,
        entry: dict,
        name: str,
        ede: Optional[EdeRow],
        points: List[Tuple[Tuple[str, int], str, Optional[str]]],
    ) -> Optional[Tuple[Tuple[str, int], str]]:
        """((type, instance) of the point, what matched), or None. The first
        rule that leaves exactly one candidate decides."""
        keyname = ede.keyname if ede is not None else None
        rules = (
            ("name", [(len(point_name), k) for k, point_name, _ in points if _starts_with(name, point_name)]),
            (
                "keyname",
                [
                    (len(point_keyname), k)
                    for k, _, point_keyname in points
                    if keyname and point_keyname and _starts_with(keyname, point_keyname)
                ],
            ),
            ("instance number", [(0, k) for k, _, _ in points if k[1] == entry["instance"]]),
        )
        for how, candidates in rules:
            if not candidates:
                continue
            longest = max(length for length, _ in candidates)
            best = [k for length, k in candidates if length == longest]
            if len(best) == 1:
                return best[0], how
        return None

    # --- alarms: limits are saved, alarming is never turned on ---------------------

    def _fill_alarm(self, entry, row) -> None:
        if row["object_type"] in ANALOG_TYPES:
            props = entry["props"]
            row["alarm_high_limit"] = as_number(props.get("high-limit"))
            row["alarm_low_limit"] = as_number(props.get("low-limit"))

    _STRIPPABLE = ("alarming_enabled", "notification_class_instance", "exception_schedule", "schedule_write_targets")

    def _strip_dependents(self) -> None:
        before = {key: {f: row[f] for f in self._STRIPPABLE} for key, row in self.rows.items() if key in self.partials}
        super()._strip_dependents()
        # The sync is fed the partial: what was stripped from the saved row
        # (it names an object the sync removes) has to be stripped there too.
        for key, was in before.items():
            row = self.rows.get(key)
            if row is not None:
                self.partials[key].update({f: row[f] for f in self._STRIPPABLE if row[f] != was[f]})
        for key, number in self._class_of.items():
            row = self.rows.get(key)
            if row is None:
                continue
            partial = self.partials.get(key)
            if self._class_in_import(number):
                row["notification_class_instance"] = number
                if partial is not None:
                    partial["notification_class_instance"] = number
                continue
            if partial is not None and partial.get("notification_class_instance", 0) is None:
                continue  # the alarm settings were stripped, which said so
            on_device = self._on_device(self._entry_of(row))
            row["notification_class_instance"] = None if on_device is None else on_device["notification_class_instance"]
            if partial is not None:
                partial.pop("notification_class_instance", None)
            if key not in self._noted_class:
                self._noted_class.add(key)
                self._note(
                    self._entry_of(row), f"notification class {number} is not in the import; not saved", row["name"]
                )


def plan_digest(plans: List[Tuple[int, str, ClonePlan]]) -> str:
    """A fingerprint of what Create would save, for (device id, name, plan)
    triples: a create given the preview's digest is refused if it changed."""
    payload = sorted(
        [device_id, name, sorted(f"{o['object_type']}:{o['instance_number']}:{o['name']}" for o in plan.objects)]
        for device_id, name, plan in plans
    )
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def plan_for_existing(
    rows: List[EdeRow], *, device_instance: int, existing: ExistingDevice, device_name: str, extra_notes=()
) -> ClonePlan:
    """The rows a merge or sync into `existing` is fed. `device_name` is the
    device's own name, so the reserved-name rules see it; the file's device
    row is ignored, and `plan.device` is not meant to be used."""
    raw = to_raw(rows, device_instance=device_instance)
    mine = [row for row in rows if row.device_instance == device_instance]
    plan = EdePlanner(raw, 0, device_name, ede_rows=mine, existing=existing).plan()
    plan.notes[:0] = [
        {"object_type": "device", "instance": device_instance, "name": device_name, "note": note}
        for note in extra_notes
    ]
    return plan


def plan_device(
    rows: List[EdeRow], *, device_instance: int, device_id: int, device_name: str, extra_notes=()
) -> ClonePlan:
    raw = to_raw(rows, device_instance=device_instance)
    mine = [row for row in rows if row.device_instance == device_instance]
    plan = EdePlanner(raw, device_id, device_name, ede_rows=mine).plan()
    device_notes = [*raw["device"].get("notes", ()), *extra_notes]
    plan.notes[:0] = [
        {"object_type": "device", "instance": device_instance, "name": device_name, "note": note}
        for note in device_notes
    ]
    return plan
