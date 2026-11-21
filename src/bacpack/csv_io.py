"""CSV import/export of a device's point list. Every JSON config object
field (config_io.OBJECT_FIELDS) is a column.
"""
from __future__ import annotations

import csv
import io
import json
import math
from typing import Iterable, Iterator, List, Mapping, NamedTuple, Tuple

from .choices import canonicalize_object_fields
from .config_io import (
    OBJECT_FIELDS,
    OBJECT_FIELD_DEFAULTS,
    object_to_dict,
    validate_object_row,
)
from .config_schemas import ConfigImportError
from .models import MAX_INSTANCE, MULTISTATE_TYPES, OBJECT_TYPES, BacnetObject
from .unicode_text import UNSTORABLE_TEXT, has_unpaired_surrogate

# Cell formats, one per config_io.OBJECT_FIELDS field. Short lists of
# scalars are ;-separated; only genuinely nested data is JSON in the cell.
TEXT = "text"
INT = "int"
FLOAT = "float"
BOOL = "bool"
TEXT_LIST = "text-list"
BOOL_LIST = "bool-list"
INT_LIST = "int-list"
JSON_CELL = "json"

CELL_FORMATS = {
    "object_type": TEXT,
    "instance_number": INT,
    "name": TEXT,
    "description": TEXT,
    "units": TEXT,
    "bacnet_writable": BOOL,
    "out_of_service_default": BOOL,
    "relinquish_default": TEXT,
    "active_text": TEXT,
    "inactive_text": TEXT,
    "start_value": TEXT,
    "state_text": TEXT_LIST,
    "trended": BOOL,
    "trend_mode": TEXT,
    "trend_interval_seconds": INT,
    "trend_sample_cap": INT,
    "trend_log_instance": INT,
    "trend_log_name": TEXT,
    "trend_reporting_enabled": BOOL,
    "trend_notification_threshold": INT,
    "trend_notification_class_instance": INT,
    "trend_event_enable": BOOL_LIST,
    "trend_notify_type": TEXT,
    "trend_event_detection_enable": BOOL,
    "schedule_value_type": TEXT,
    "schedule_default": TEXT,
    "weekly_schedule": JSON_CELL,
    "exception_schedule": JSON_CELL,
    "schedule_effective_start": TEXT,
    "schedule_effective_end": TEXT,
    "schedule_priority": INT,
    "schedule_write_targets": JSON_CELL,
    "date_list": JSON_CELL,
    "alarming_enabled": BOOL,
    "notification_class_instance": INT,
    "alarm_time_delay": INT,
    "alarm_time_delay_normal": INT,
    "alarm_notify_type": TEXT,
    "alarm_event_enable": BOOL_LIST,
    "alarm_high_limit": FLOAT,
    "alarm_low_limit": FLOAT,
    "alarm_deadband": FLOAT,
    "alarm_limit_enable": BOOL_LIST,
    "alarm_value": BOOL,
    "alarm_values": INT_LIST,
    "alarm_event_detection_enable": BOOL,
    "notification_priority": INT_LIST,
    "notification_ack_required": BOOL_LIST,
    "notification_recipient_list": JSON_CELL,
    "simulation": JSON_CELL,
}

# Fields that only mean something on one object type: exported blank on
# every other type. A blank re-imports as the default, so a round trip
# changes nothing.
_FIELDS_ONLY_FOR = {
    "schedule": (
        "schedule_value_type",
        "schedule_default",
        "weekly_schedule",
        "exception_schedule",
        "schedule_effective_start",
        "schedule_effective_end",
        "schedule_priority",
        "schedule_write_targets",
    ),
    "calendar": ("date_list",),
    "notification-class": ("notification_priority", "notification_ack_required", "notification_recipient_list"),
}
_OWNER_TYPE = {field: object_type for object_type, fields in _FIELDS_ONLY_FOR.items() for field in fields}

# Semicolons, since labels may contain commas.
_LIST_SEP = ";"


class CsvImportError(ValueError):
    """Raised with a human-readable message pointing at the offending row."""


class ParsedRow(NamedTuple):
    """One object from the file: its row number (the header is row 1) and
    its decoded values, holding only the columns the file has."""

    row_num: int
    values: dict


# What the Add object form starts a multi-state object with.
DEFAULT_STATE_TEXT = ("State 1", "State 2")


def fill_default_state_text(
    parsed: List[ParsedRow], existing: Mapping[Tuple[str, int], BacnetObject]
) -> None:
    """Give a multi-state row the import creates, and that has no state
    texts, the default two. A points list rarely carries them. An existing
    object (in `existing`) is left alone, so a blank cell never wipes its
    state names."""
    for _, values in parsed:
        key = (values["object_type"], values["instance_number"])
        if values["object_type"] in MULTISTATE_TYPES and key not in existing and not values.get("state_text"):
            values["state_text"] = list(DEFAULT_STATE_TEXT)


def validate_objects_csv(
    parsed: List[ParsedRow], existing: Mapping[Tuple[str, int], BacnetObject]
) -> None:
    """Check each row with the JSON config import's validation, so both
    accept the same settings. Each row is judged on the object it produces:
    the row overlaid on the existing object from `existing` (sync mode), or
    on the defaults. Pass `{}` when every row creates."""
    for row_num, values in parsed:
        current = existing.get((values["object_type"], values["instance_number"]))
        base = object_to_dict(current) if current is not None else dict(OBJECT_FIELD_DEFAULTS)
        try:
            validate_object_row({**base, **values}, context=f"row {row_num}")
        except ConfigImportError as exc:
            raise CsvImportError(str(exc)) from None


def _encode_cell(fmt: str, value) -> str:
    if value is None:
        return ""
    if fmt == BOOL:
        return "true" if value else "false"
    if fmt == BOOL_LIST:
        return _LIST_SEP.join("true" if item else "false" for item in value)
    if fmt == TEXT_LIST:
        # A State_Text label with a ";", an empty one, edge
        # whitespace or a leading "[" wouldn't read back as the same list,
        # so that list is written as JSON instead.
        plain = _LIST_SEP.join(str(item) for item in value)
        try:
            if _decode_text_list(plain.strip()) == list(value):
                return plain
        except ValueError:
            pass
        return json.dumps(value, ensure_ascii=False)
    if fmt == INT_LIST:
        return _LIST_SEP.join(str(item) for item in value)
    if fmt == JSON_CELL:
        return json.dumps(value)
    return str(value)


# A spreadsheet runs a cell starting with one of these as a formula, and
# text cells can hold anything a BACnet client wrote. A leading ' makes it
# text; it's escaped too, so import can strip exactly one.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "'")


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _escape_formula(text: str) -> str:
    if text.startswith(_FORMULA_STARTS) and not _is_number(text):
        return "'" + text
    return text


def _unescape_formula(text: str) -> str:
    if text.startswith("'") and text[1:].startswith(_FORMULA_STARTS):
        return text[1:]
    return text


def _export_cell(obj: BacnetObject, field: str) -> str:
    owner = _OWNER_TYPE.get(field)
    if owner is not None and owner != obj.object_type:
        return ""
    fmt = CELL_FORMATS[field]
    cell = _encode_cell(fmt, getattr(obj, field))
    if fmt in (TEXT, TEXT_LIST):
        return _escape_formula(cell)
    return cell


def export_objects_csv(objects: Iterable[BacnetObject]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OBJECT_FIELDS)
    writer.writeheader()
    for obj in objects:
        writer.writerow({field: _export_cell(obj, field) for field in OBJECT_FIELDS})
    return buf.getvalue()


REQUIRED_COLUMNS = ("object_type", "instance_number", "name")

_TRUE_WORDS = ("true", "1", "yes")
_FALSE_WORDS = ("false", "0", "no")


def _decode_scalar(fmt: str, text: str, *, row_num: int, field: str):
    if fmt == BOOL:
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise CsvImportError(f"row {row_num}: {field!r} must be true/false, got {text!r}")
    if fmt == INT:
        try:
            return int(text)
        except ValueError:
            raise CsvImportError(f"row {row_num}: {field!r} must be an integer, got {text!r}") from None
    # FLOAT. float() takes "nan"/"inf", but a NaN alarm limit never trips.
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise CsvImportError(f"row {row_num}: {field!r} must be a number, got {text!r}")
    return value


def _decode_text_list(text: str) -> list:
    """A ;-separated list (empty items dropped), or a JSON list of strings
    when the cell starts with "[" (what export writes when ;-separation
    can't hold the list)."""
    if not text.startswith("["):
        return [item for item in text.split(_LIST_SEP) if item]
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("starts with '[' but is not a JSON list") from None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("must be a JSON list of strings")
    if has_unpaired_surrogate(value):
        raise ValueError(UNSTORABLE_TEXT)
    return value


def _decode_cell(fmt: str, text: str, *, row_num: int, field: str):
    """One cell to its typed value; a blank cell is the field's default,
    like a JSON null (None for the required columns). A description is kept
    as written, so its edge spaces survive export and re-import."""
    if field == "description" and text.strip():
        return _unescape_formula(text)
    text = text.strip()
    if fmt in (TEXT, TEXT_LIST):
        text = _unescape_formula(text)
    if not text:
        return OBJECT_FIELD_DEFAULTS.get(field)
    if fmt == TEXT:
        return text
    if fmt == JSON_CELL:
        try:
            value = json.loads(text)
        except RecursionError:
            # json raises this, not ValueError, on deeply nested input.
            raise CsvImportError(f"row {row_num}: {field!r} is not valid JSON: nested too deeply") from None
        except ValueError as exc:
            raise CsvImportError(f"row {row_num}: {field!r} is not valid JSON: {exc}") from None
        if has_unpaired_surrogate(value):
            raise CsvImportError(f"row {row_num}: {field!r} {UNSTORABLE_TEXT}")
        return value
    if fmt == TEXT_LIST:
        try:
            items = _decode_text_list(text)
        except ValueError as exc:
            raise CsvImportError(f"row {row_num}: {field!r} {exc}") from None
        return items or OBJECT_FIELD_DEFAULTS.get(field)
    if fmt in (BOOL_LIST, INT_LIST):
        item_fmt = BOOL if fmt == BOOL_LIST else INT
        items = [item.strip() for item in text.split(_LIST_SEP)]
        if items[-1] == "":
            items.pop()  # a trailing separator ("2;3;")
        # An empty item can't just be dropped: every later flag or state
        # would shift into the wrong position.
        if "" in items:
            raise CsvImportError(f"row {row_num}: {field!r} has an empty item")
        return [_decode_scalar(item_fmt, item, row_num=row_num, field=field) for item in items]
    return _decode_scalar(fmt, text, row_num=row_num, field=field)


def _columns(names: List[str]) -> str:
    return "column" if len(names) == 1 else "columns"


def _skipped_columns(header: List[str]) -> List[str]:
    """The columns that aren't BACpack's, each named once in file order."""
    skipped: List[str] = []
    for position, column in enumerate(header, start=1):
        label = column or f"column {position} (no header)"
        if column not in CELL_FORMATS and label not in skipped:
            skipped.append(label)
    return skipped


def _check_header(header: List[str], skipped: List[str]) -> None:
    if not header:
        raise CsvImportError("file is empty: expected a header row")
    known = [column for column in header if column in CELL_FORMATS]
    duplicated = sorted({column for column in known if known.count(column) > 1})
    if duplicated:
        raise CsvImportError(f"duplicate {_columns(duplicated)}: {', '.join(duplicated)}")
    missing = [column for column in REQUIRED_COLUMNS if column not in known]
    if missing:
        hint = f" (columns not imported: {', '.join(skipped)})" if skipped else ""
        raise CsvImportError(f"missing required {_columns(missing)}: {', '.join(missing)}{hint}")


# The short forms points lists use.
SHORT_OBJECT_TYPES = {
    "AI": "analog-input",
    "AO": "analog-output",
    "AV": "analog-value",
    "BI": "binary-input",
    "BO": "binary-output",
    "BV": "binary-value",
    "MSI": "multi-state-input",
    "MSO": "multi-state-output",
    "MSV": "multi-state-value",
}


class CsvSheet(NamedTuple):
    """A parsed sheet: its objects, and the columns left out because they
    aren't BACpack's (a points list's own panel or notes columns)."""

    rows: List[ParsedRow]
    skipped_columns: List[str]


def parse_objects_csv(csv_text: str) -> List[ParsedRow]:
    return read_objects_csv(csv_text).rows


def _records(reader) -> Iterator[Tuple[int, List[str]]]:
    """(row number, cells) per record, the header being row 1. A cell over
    the csv module's field limit raises csv.Error mid-iteration; it becomes
    the import error that names its row."""
    row_num = 0
    while True:
        row_num += 1
        try:
            cells = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            if "field larger than field limit" in str(exc):
                problem = f"a cell is longer than {csv.field_size_limit()} characters"
            else:
                problem = str(exc)
            raise CsvImportError(f"row {row_num}: {problem}") from None
        yield row_num, cells


def read_objects_csv(csv_text: str) -> CsvSheet:
    """Parse CSV text into one ParsedRow per object. Only object_type,
    instance_number and name are required; a missing column means "not
    given", a column BACpack doesn't know is skipped. Checks cell formats
    and duplicates within the file; the values are checked by
    validate_objects_csv once existing objects are known."""
    # A spreadsheet's "CSV UTF-8" starts with a byte-order mark.
    # newline="": the reader does its own line ends, so a CR-only file (classic
    # Mac) parses, and a line end inside a quoted cell is kept as written.
    records = _records(csv.reader(io.StringIO(csv_text.removeprefix("﻿"), newline="")))
    header = [column.strip() for column in next(records, (1, []))[1]]
    # A spreadsheet that ever touched a column past the data saves it as
    # trailing empty columns; ignored without a mention.
    while header and not header[-1]:
        header.pop()
    skipped = _skipped_columns(header)
    _check_header(header, skipped)

    parsed: List[ParsedRow] = []
    seen_identifiers = set()
    seen_names = set()
    for row_num, cells in records:
        if not any(cell.strip() for cell in cells):
            continue  # a blank line, or a spreadsheet's trailing empty rows
        if any(cell.strip() for cell in cells[len(header):]):
            raise CsvImportError(f"row {row_num}: more cells than columns")
        # A spreadsheet may drop a row's trailing empty cells.
        cells = (cells + [""] * len(header))[: len(header)]
        decoded = {
            field: _decode_cell(CELL_FORMATS[field], text, row_num=row_num, field=field)
            for field, text in zip(header, cells, strict=True)
            if field in CELL_FORMATS
        }
        if isinstance(decoded["object_type"], str):
            short = decoded["object_type"].strip().upper()
            decoded["object_type"] = SHORT_OBJECT_TYPES.get(short, decoded["object_type"])
        # Fixed-choice values in any case, JSON cells included.
        values = canonicalize_object_fields(decoded)

        object_type = values["object_type"]
        if object_type is None:
            raise CsvImportError(f"row {row_num}: object_type is required")
        if object_type not in OBJECT_TYPES:
            raise CsvImportError(f"row {row_num}: unknown object_type {object_type!r}")
        instance_number = values["instance_number"]
        if instance_number is None:
            raise CsvImportError(f"row {row_num}: instance_number is required")
        if not (0 <= instance_number <= MAX_INSTANCE):
            raise CsvImportError(f"row {row_num}: instance_number must be 0-{MAX_INSTANCE}, got {instance_number}")
        name = values["name"]
        if name is None:
            raise CsvImportError(f"row {row_num}: name is required")

        identifier = (object_type, instance_number)
        if identifier in seen_identifiers:
            raise CsvImportError(f"row {row_num}: duplicate object identifier {identifier} in this file")
        seen_identifiers.add(identifier)
        if name in seen_names:
            raise CsvImportError(f"row {row_num}: duplicate name {name!r} in this file")
        seen_names.add(name)

        parsed.append(ParsedRow(row_num, values))
    return CsvSheet(parsed, skipped)
