"""Reading an EDE file (Engineering Data Exchange, the BIG-EU template): the
file's bytes become one EdeRow per object. Pure: no BACnet, no database.

Nothing here decides what BACpack can build. A cell that does not parse is
dropped with a note on its row, a row that cannot be identified is dropped
with a note on the file, and the planner (ede_plan) takes it from there.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from bacpypes3.basetypes import EngineeringUnits

from .csv_io import SHORT_OBJECT_TYPES
from .models import MAX_INSTANCE, OBJECT_TYPES
from .point_config import list_engineering_units

# The recognising rule looks only this far for the title line.
_TITLE_LINE_WINDOW = 20

_TYPE_BY_NUMBER = {
    0: "analog-input",
    1: "analog-output",
    2: "analog-value",
    3: "binary-input",
    4: "binary-output",
    5: "binary-value",
    6: "calendar",
    8: "device",
    10: "file",
    13: "multi-state-input",
    14: "multi-state-output",
    15: "notification-class",
    17: "schedule",
    19: "multi-state-value",
    20: "trend-log",
    56: "network-port",
}
_NAMED_TYPES = set(OBJECT_TYPES) | {"device", "trend-log", "file", "network-port"}

_MANDATORY = (
    ("deviceobjinstance", "device obj.-instance"),
    ("objectname", "object-name"),
    ("objecttype", "object-type"),
    ("objectinstance", "object-instance"),
)
# Normalised title -> what the reader does with that column.
_USED = {
    "keyname", "deviceobjinstance", "deviceinstance", "objectname", "objecttype", "objectinstance", "description",
    "presentvaluedefault", "settable", "commandable", "commandvalue", "hilimit", "lowlimit",
    "statetextreference", "statetext", "unitcode", "notificationclass",
}
_YES = {"y", "yes", "true", "1"}
_NO = {"n", "no", "false", "0"}


class EdeError(ValueError):
    """The file cannot be read as EDE; the message is for the user."""


def _too_long(where: str, exc: csv.Error) -> EdeError:
    """A csv.Error as the file's own error: a cell over the csv module's
    field limit says so, any other keeps the module's text."""
    if "field larger than field limit" in str(exc):
        return EdeError(f"{where}: a cell is longer than {csv.field_size_limit()} characters")
    return EdeError(f"{where}: {exc}")


@dataclass
class EdeRow:
    keyname: Optional[str]
    device_instance: int
    object_type: str  # a BACpack type, "device", or "unsupported:<number or text>"
    instance: int
    name: str
    description: Optional[str] = None
    default: Optional[str] = None  # the present-value-default cell as written
    writable: List[bool] = field(default_factory=list)  # settable/commandable/commandValue cells that said yes or no
    hi_limit: Optional[float] = None
    low_limit: Optional[float] = None
    state_text_reference: Optional[str] = None
    inline_state_text: Optional[List[str]] = None
    unit_code: Optional[int] = None
    units: Optional[str] = None  # BACpack's name for unit_code
    notification_class: Optional[int] = None
    state_texts: Optional[List[str]] = None  # resolve_state_texts
    state_text_missing: bool = False  # a reference whose texts could not be found
    row_number: int = 0
    notes: List[str] = field(default_factory=list)  # cells dropped, and why
    # The fields (by attribute name) whose cell the file had and that parsed:
    # an object already on a device keeps its stored value for the rest.
    supplied: set = field(default_factory=set)


@dataclass
class EdeFile:
    rows: List[EdeRow]
    project_name: Optional[str] = None
    version_of_layout: Optional[str] = None
    columns_not_used: List[str] = field(default_factory=list)
    file_notes: List[str] = field(default_factory=list)

    def devices(self) -> List[int]:
        """The device instances the file has objects for, first seen first."""
        return list(dict.fromkeys(row.device_instance for row in self.rows))


# --- text and the title line ---------------------------------------------------------


def _decode(data: bytes) -> Tuple[str, List[str]]:
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        # Excel's default on a European Windows machine.
        return data.decode("cp1252", errors="replace"), ["the file is not UTF-8; read as Windows-1252"]


def _normalise(title: str) -> str:
    return "".join(ch for ch in title.lower() if ch not in " .-_﻿")


def _title_line(lines: List[str]) -> Optional[Tuple[int, str, List[str]]]:
    """(index, delimiter, titles) of the `keyname` line, or None."""
    for index, line in enumerate(lines[:_TITLE_LINE_WINDOW]):
        text = line.strip().lstrip("#").strip()
        if not text.lower().startswith("keyname"):
            continue
        if text[len("keyname"):].lstrip()[:1] not in ("", ";", ","):
            continue
        delimiter = ";" if ";" in text else "," if "," in text else None
        if delimiter is None:
            continue
        try:
            titles = next(csv.reader([text], delimiter=delimiter), [])
        except csv.Error as exc:
            raise _too_long(f"line {index + 1}", exc) from None
        if "objecttype" in {_normalise(title) for title in titles}:
            return index, delimiter, [title.strip() for title in titles]
    return None


def looks_like_ede(data: bytes) -> bool:
    try:
        text, _ = _decode(data)
        return _title_line(text.splitlines(keepends=True)) is not None
    except EdeError:
        return True  # a title line, just a broken one: the EDE reader says why
    except Exception:  # a probe on arbitrary bytes: any failure means "not EDE"
        return False


# --- units and state texts --------------------------------------------------------------

_UNIT_NAMES: Dict[int, str] = {}


def unit_name(code: int) -> Optional[str]:
    """BACpack's name for a BACnet engineering-unit number, or None."""
    if not _UNIT_NAMES:
        for name in list_engineering_units():
            _UNIT_NAMES.setdefault(int(EngineeringUnits(name)), name)
    return _UNIT_NAMES.get(code)


class StateTextTable(Dict[str, List[str]]):
    """The state-texts file's rows by reference, and what reading it noted."""

    def __init__(self) -> None:
        super().__init__()
        self.notes: List[str] = []


def read_state_texts(data: bytes) -> StateTextTable:
    """The state-texts file: `reference;text 1;text 2;...` rows. Lines starting
    with # and blank lines are skipped; trailing empty texts are dropped. A
    file with no usable row gives {}."""
    text, notes = _decode(data)
    table = StateTextTable()
    table.notes = [f"the state-texts file {note[len('the file '):]}" for note in notes]
    lines = [line for line in text.splitlines(keepends=True) if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return table
    delimiter = ";" if ";" in lines[0] else ","
    reader = csv.reader(lines, delimiter=delimiter)
    while True:
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise _too_long(f"the state-texts file, entry {reader.line_num}", exc) from None
        reference = cells[0].strip() if cells else ""
        texts = [cell.strip() for cell in cells[1:]]
        while texts and not texts[-1]:
            texts.pop()
        if reference and texts:
            table.setdefault(reference, texts)
    return table


def resolve_state_texts(file: EdeFile, table: Optional[Dict[str, List[str]]]) -> None:
    """Give each row its state texts: its own inline ones, else those its
    reference names in `table`. A reference that finds none marks the row."""
    file.file_notes.extend(getattr(table, "notes", ()))
    for row in file.rows:
        if row.inline_state_text:
            row.state_texts = row.inline_state_text
        elif row.state_text_reference:
            found = (table or {}).get(row.state_text_reference)
            if found:
                row.state_texts = list(found)
            else:
                row.state_text_missing = True
        if row.state_texts:
            row.supplied.add("state_texts")


# --- cells -------------------------------------------------------------------------------


def parse_int(text: str) -> Optional[int]:
    text = text.strip()
    digits = text[1:] if text[:1] in "+-" else text
    return int(text) if digits.isdigit() and digits.isascii() else None


def parse_number(text: str) -> Optional[float]:
    try:
        number = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _object_type(text: str) -> str:
    text = text.strip()
    if parse_int(text) is not None:
        number = int(text)
        return _TYPE_BY_NUMBER.get(number, f"unsupported:{number}")
    if text.upper() in SHORT_OBJECT_TYPES:
        return SHORT_OBJECT_TYPES[text.upper()]
    if text.lower() in _NAMED_TYPES:
        return text.lower()
    return f"unsupported:{text}"


def _read_row(cells: List[str], columns: Dict[str, int], row_number: int, notes: List[str]) -> Optional[EdeRow]:
    def cell(key: str) -> str:
        index = columns.get(key)
        return cells[index].strip() if index is not None and index < len(cells) else ""

    name = cell("objectname")
    where = f"row {row_number} ({name or 'no name'})"
    type_text = cell("objecttype")
    if not type_text:
        notes.append(f"{where} skipped: it has no object-type")
        return None
    device = parse_int(cell("deviceobjinstance"))
    instance = parse_int(cell("objectinstance"))
    for number, what, text in (
        (device, "device obj.-instance", cell("deviceobjinstance")),
        (instance, "object-instance", cell("objectinstance")),
    ):
        if number is None or not 0 <= number <= MAX_INSTANCE:
            notes.append(f"{where} skipped: {what} {text!r} is not a number from 0 to {MAX_INSTANCE}")
            return None
    if not name:
        notes.append(f"{where} skipped: it has no object-name")
        return None
    assert device is not None and instance is not None

    row = EdeRow(
        keyname=cell("keyname") or None,
        device_instance=device,
        object_type=_object_type(type_text),
        instance=instance,
        name=name,
        row_number=row_number,
    )
    description = cells[columns["description"]] if "description" in columns and columns["description"] < len(cells) else ""
    if description.strip():
        row.description = description
        row.supplied.add("description")
    if cell("presentvaluedefault"):
        row.default = cell("presentvaluedefault")
        row.supplied.add("default")
    for key in ("settable", "commandable", "commandvalue"):
        text = cell(key).lower()
        if text in _YES or text in _NO:
            row.writable.append(text in _YES)
    if row.writable:
        row.supplied.add("writable")
    for key, attr, title in (("hilimit", "hi_limit", "hi-limit"), ("lowlimit", "low_limit", "low-limit")):
        if cell(key):
            number = parse_number(cell(key))
            if number is None:
                row.notes.append(f"{title} {cell(key)!r} is not a number; not used")
            else:
                setattr(row, attr, number)
                row.supplied.add(attr)
    row.state_text_reference = cell("statetextreference") or None
    inline = cell("statetext")
    if inline:
        body = inline[1:-1] if inline.startswith("[") and inline.endswith("]") else inline
        texts = [part.strip() for part in body.split("|")]
        while texts and not texts[-1]:
            texts.pop()
        row.inline_state_text = texts
    if cell("unitcode"):
        code = parse_int(cell("unitcode"))
        if code is None or code < 0:
            row.notes.append(f"unit-code {cell('unitcode')!r} is not a unit number; not used")
        elif (name_of_unit := unit_name(code)) is None:
            row.unit_code = code
            row.notes.append(f"unit-code {code} is not a BACnet engineering unit; not used")
        else:
            row.unit_code, row.units = code, name_of_unit
            row.supplied.add("units")
    if cell("notificationclass"):
        number = parse_int(cell("notificationclass"))
        if number is None or not 0 <= number <= MAX_INSTANCE:
            row.notes.append(f"notification-class {cell('notificationclass')!r} is not a number; not used")
        else:
            row.notification_class = number
            row.supplied.add("notification_class")
    return row


def read_ede(data: bytes) -> EdeFile:
    text, notes = _decode(data)
    lines = text.splitlines(keepends=True)
    found = _title_line(lines)
    if found is None:
        raise EdeError(
            "this is not an EDE file: no line starting with 'keyname' and having an object-type column "
            f"in its first {_TITLE_LINE_WINDOW} lines"
        )
    index, delimiter, titles = found
    columns: Dict[str, int] = {}
    for position, title in enumerate(titles):
        columns.setdefault(_normalise(title), position)
    if "deviceinstance" in columns:
        columns.setdefault("deviceobjinstance", columns["deviceinstance"])
    missing = [title for key, title in _MANDATORY if key not in columns]
    if missing:
        raise EdeError(f"the file lacks the required column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}")

    project_name = version_of_layout = None
    for line_number, line in enumerate(lines[:index], start=1):
        try:
            cells = next(csv.reader([line.strip()], delimiter=delimiter), [])
        except csv.Error as exc:
            raise _too_long(f"line {line_number}", exc) from None
        if len(cells) >= 2:
            key = _normalise(cells[0].lstrip("#"))
            if key == "projectname":
                project_name = cells[1].strip() or None
            elif key == "versionoflayout":
                version_of_layout = cells[1].strip() or None

    rows: List[EdeRow] = []
    reader = csv.reader(lines[index + 1:], delimiter=delimiter)
    while True:
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise _too_long(f"line {index + 1 + reader.line_num}", exc) from None
        if not any(cell.strip() for cell in cells) or cells[0].strip().startswith("#"):
            continue
        row = _read_row(cells, columns, index + 1 + reader.line_num, notes)
        if row is not None:
            rows.append(row)
    if not rows:
        raise EdeError("the file has no objects")

    used_titles = {title for title in titles if _normalise(title) in _USED}
    return EdeFile(
        rows=rows,
        project_name=project_name,
        version_of_layout=version_of_layout,
        columns_not_used=[title for title in titles if title and title not in used_titles],
        file_notes=notes,
    )
