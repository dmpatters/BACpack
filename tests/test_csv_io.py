from __future__ import annotations

import csv
import io
import json

import pytest

from bacpack.config_io import OBJECT_FIELDS
from bacpack.config_io import OBJECT_FIELD_DEFAULTS
from bacpack.csv_io import (
    CELL_FORMATS,
    CsvImportError,
    ParsedRow,
    export_objects_csv,
    fill_default_state_text,
    parse_objects_csv,
    read_objects_csv,
    validate_objects_csv,
)
from bacpack.models import BacnetObject


def test_export_then_parse_round_trips():
    objects = [
        BacnetObject(
            object_type="analog-input",
            instance_number=1,
            name="AI-1",
            units="degreesFahrenheit",
            description="zone temp",
        ),
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            bacnet_writable=False,
        ),
    ]

    csv_text = export_objects_csv(objects)
    rows = parse_objects_csv(csv_text)

    assert len(rows) == 2
    assert rows[0].values["object_type"] == "analog-input"
    assert rows[0].values["units"] == "degreesFahrenheit"
    assert rows[0].values["description"] == "zone temp"
    assert rows[1].values["state_text"] == ["Off", "Low", "High"]
    assert rows[1].values["bacnet_writable"] is False


def test_three_column_sheet_parses():
    assert parse_objects_csv("object_type,instance_number,name\nanalog-value,1,AV-1\n") == [
        ParsedRow(2, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"})
    ]


def test_missing_required_column_rejected():
    with pytest.raises(CsvImportError, match=r"missing required column: name"):
        parse_objects_csv("object_type,instance_number\nanalog-value,1\n")


def test_unknown_object_type_rejected():
    csv_text = export_objects_csv(
        [BacnetObject(object_type="analog-input", instance_number=1, name="AI-1")]
    )
    bad = csv_text.replace("analog-input", "not-a-type")
    with pytest.raises(CsvImportError):
        parse_objects_csv(bad)


def test_multistate_requires_state_text():
    csv_text = export_objects_csv(
        [
            BacnetObject(
                object_type="multi-state-value",
                instance_number=1,
                name="MSV-1",
                state_text=["A", "B"],
            )
        ]
    )
    # strip the state_text value out of the CSV row, leaving the column empty.
    # Blank it by column name (not position) since state_text is no longer
    # the last column now that schedule/calendar columns follow it.
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames
    row_dict = next(reader)
    row_dict["state_text"] = ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerow(row_dict)
    rows = parse_objects_csv(out.getvalue())
    with pytest.raises(CsvImportError):
        validate_objects_csv(rows, {})


def test_duplicate_identifier_in_file_rejected():
    csv_text = (
        "object_type,instance_number,name,description,units,bacnet_writable,"
        "out_of_service_default,relinquish_default,state_text\n"
        "analog-input,1,AI-1,,,false,false,,\n"
        "analog-input,1,AI-1-dup,,,false,false,,\n"
    )
    with pytest.raises(CsvImportError):
        parse_objects_csv(csv_text)


@pytest.mark.parametrize("instance_number", [-1, 4194303])
def test_out_of_range_instance_number_rejected(instance_number):
    """Same 0-4194302 range the object API and JSON config import enforce --
    outside it the device can't start."""
    csv_text = export_objects_csv(
        [BacnetObject(object_type="analog-input", instance_number=instance_number, name="AI-1")]
    )
    with pytest.raises(CsvImportError):
        parse_objects_csv(csv_text)


def test_mixed_case_choices_import_canonical():
    csv_text = export_objects_csv(
        [
            BacnetObject(object_type="analog-input", instance_number=1, name="Zone Temp", units="degreesFahrenheit"),
            BacnetObject(object_type="binary-value", instance_number=1, name="Fan Cmd", relinquish_default="active"),
        ]
    )
    mixed = (
        csv_text.replace("analog-input", "Analog-Input")
        .replace("degreesFahrenheit", "DEGREES-FAHRENHEIT")
        .replace("binary-value", "BINARY_VALUE")
        .replace(",active,", ",Active,")
    )
    rows = parse_objects_csv(mixed)
    assert (rows[0].values["object_type"], rows[0].values["units"], rows[0].values["name"]) == ("analog-input", "degreesFahrenheit", "Zone Temp")
    assert (rows[1].values["object_type"], rows[1].values["relinquish_default"], rows[1].values["name"]) == ("binary-value", "active", "Fan Cmd")


def test_unknown_object_type_message_unchanged():
    csv_text = export_objects_csv([BacnetObject(object_type="analog-input", instance_number=1, name="AI-1")])
    with pytest.raises(CsvImportError, match="unknown object_type 'Not-A-Type'"):
        parse_objects_csv(csv_text.replace("analog-input", "Not-A-Type"))


# --- every JSON config object field is a CSV column -----------------


def _exported_row(obj):
    return next(csv.DictReader(io.StringIO(export_objects_csv([obj]))))


_RECIPIENTS = [
    {
        "recipient": {"kind": "address", "address": "192.168.1.200"},
        "valid_days": [True] * 7,
        "from_time": "00:00",
        "to_time": "23:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 1,
    }
]


def test_every_json_config_object_field_has_a_csv_column():
    """The guard against drift: a field added to the JSON
    config (config_io.OBJECT_FIELDS) with no CSV column fails here."""
    assert set(CELL_FORMATS) == set(OBJECT_FIELDS), (
        f"no CSV column for {sorted(set(OBJECT_FIELDS) - set(CELL_FORMATS))}; "
        f"CSV column with no JSON field {sorted(set(CELL_FORMATS) - set(OBJECT_FIELDS))}"
    )


def test_export_header_is_the_json_config_field_order():
    assert export_objects_csv([]).splitlines()[0].split(",") == OBJECT_FIELDS


def test_export_encodes_each_cell_format():
    row = _exported_row(
        BacnetObject(
            object_type="analog-value",
            instance_number=3,
            name="AV-3",
            description='Zone "A", north; east',
            trended=True,
            trend_interval_seconds=60,
            alarming_enabled=True,
            notification_class_instance=1,
            alarm_high_limit=80.5,
            alarm_deadband=0.0,
            alarm_event_enable=[True, False, True],
            alarm_limit_enable=[True, True],
        )
    )
    assert row["instance_number"] == "3"
    assert row["description"] == 'Zone "A", north; east'
    assert row["trended"] == "true"
    assert row["trend_interval_seconds"] == "60"
    assert row["alarm_high_limit"] == "80.5"
    assert row["alarm_deadband"] == "0.0"
    assert row["alarm_low_limit"] == ""
    assert row["alarm_event_enable"] == "true;false;true"
    assert row["alarm_limit_enable"] == "true;true"


def test_export_writes_int_lists_and_json_cells():
    msv = _exported_row(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            alarm_values=[2, 3],
        )
    )
    assert msv["state_text"] == "Off;Low;High"
    assert msv["alarm_values"] == "2;3"

    nc = _exported_row(
        BacnetObject(
            object_type="notification-class",
            instance_number=1,
            name="NC-1",
            notification_priority=[50, 60, 70],
            notification_ack_required=[True, False, True],
            notification_recipient_list=_RECIPIENTS,
        )
    )
    assert nc["notification_priority"] == "50;60;70"
    assert nc["notification_ack_required"] == "true;false;true"
    assert json.loads(nc["notification_recipient_list"]) == _RECIPIENTS


def test_export_blanks_fields_that_belong_to_another_object_type():
    row = _exported_row(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            schedule_priority=14,
            date_list=[{"kind": "date", "year": None, "month": 12, "day": 25}],
            notification_priority=[1, 2, 3],
        )
    )
    assert (row["schedule_priority"], row["date_list"], row["notification_priority"]) == ("", "", "")
    schedule = _exported_row(BacnetObject(object_type="schedule", instance_number=1, name="S-1", schedule_priority=10))
    assert schedule["schedule_priority"] == "10"


def test_export_keeps_an_empty_json_list():
    row = _exported_row(
        BacnetObject(object_type="notification-class", instance_number=1, name="NC-1", notification_recipient_list=[])
    )
    assert row["notification_recipient_list"] == "[]"



def _stored(**fields):
    """An in-memory object as the DB holds it: every defaulted column set
    (a bare BacnetObject(...) leaves them None until flush)."""
    return BacnetObject(**{**OBJECT_FIELD_DEFAULTS, **fields})


def test_validate_accepts_a_plain_new_object():
    validate_objects_csv([ParsedRow(2, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"})], {})


def test_validate_multistate_needs_state_text_unless_the_object_has_it():
    row = ParsedRow(2, {"object_type": "multi-state-value", "instance_number": 1, "name": "MSV-1"})
    with pytest.raises(CsvImportError, match="row 2: multi-state object .* requires state_text"):
        validate_objects_csv([row], {})
    existing = _stored(object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["A", "B"])
    validate_objects_csv([row], {("multi-state-value", 1): existing})


def test_validate_refuses_alarm_config_json_import_refuses():
    row = ParsedRow(
        3,
        {
            "object_type": "analog-value",
            "instance_number": 1,
            "name": "AV-1",
            "alarming_enabled": True,
            "notification_class_instance": 1,
            "alarm_limit_enable": [True, True],
            "alarm_low_limit": 10.0,
        },
    )
    with pytest.raises(CsvImportError, match="row 3: A limit is enabled"):
        validate_objects_csv([row], {})


def test_validate_refuses_alarming_without_a_notification_class():
    # The object API refuses this; a CSV save must too, or the device can't start.
    row = ParsedRow(
        2,
        {
            "object_type": "analog-value",
            "instance_number": 1,
            "name": "AV-1",
            "alarming_enabled": True,
            "alarm_high_limit": 80.0,
        },
    )
    with pytest.raises(CsvImportError, match="row 2: notification_class_instance is required when alarming_enabled=True"):
        validate_objects_csv([row], {})


def test_validate_checks_a_partial_row_against_the_existing_object():
    existing = _stored(
        object_type="analog-value",
        instance_number=1,
        name="AV-1",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=80.0,
    )
    identifier = ("analog-value", 1)
    # Only a low limit given: merged with the existing high limit, fine.
    validate_objects_csv(
        [ParsedRow(2, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "alarm_low_limit": 10.0})],
        {identifier: existing},
    )
    # A blank high-limit cell clears the only limit of an alarming object.
    with pytest.raises(CsvImportError, match="row 7: Set a high limit or a low limit"):
        validate_objects_csv(
            [ParsedRow(7, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "alarm_high_limit": None})],
            {identifier: existing},
        )


def test_validate_refuses_notification_class_and_trend_reporting_errors():
    nc = ParsedRow(
        2, {"object_type": "notification-class", "instance_number": 1, "name": "NC-1", "notification_priority": [1, 2]}
    )
    with pytest.raises(CsvImportError, match="row 2: priority must have exactly 3 entries"):
        validate_objects_csv([nc], {})
    reporting = ParsedRow(
        4, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "trend_reporting_enabled": True}
    )
    with pytest.raises(CsvImportError, match="row 4: trend_reporting_enabled requires trended"):
        validate_objects_csv([reporting], {})


def test_validate_refuses_a_bad_choice_like_json_import():
    row = ParsedRow(2, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "trend_mode": "hourly"})
    with pytest.raises(CsvImportError, match="row 2: trend_mode"):
        validate_objects_csv([row], {})


_OLD_HEADER = (
    "object_type,instance_number,name,description,units,bacnet_writable,"
    "out_of_service_default,relinquish_default,state_text,"
    "schedule_value_type,schedule_default,weekly_schedule,exception_schedule,"
    "schedule_effective_start,schedule_effective_end,schedule_priority,"
    "schedule_write_targets,date_list"
)


def test_a_0_7_0_era_file_still_parses():
    (row,) = parse_objects_csv(_OLD_HEADER + "\nanalog-value,1,AV-1,,percent,false,false,,,,,,,,,,,\n")
    assert set(row.values) == set(_OLD_HEADER.split(","))
    assert row.values["units"] == "percent"
    assert row.values["bacnet_writable"] is False
    assert row.values["schedule_priority"] == 14


def test_duplicate_column_rejected():
    with pytest.raises(CsvImportError, match=r"duplicate column: name"):
        parse_objects_csv("object_type,instance_number,name,name\nanalog-value,1,AV-1,AV-2\n")


def test_unknown_columns_are_skipped_and_named():
    """A points list keeps its own columns (panel, notes) beside BACpack's;
    they are left out, and named so a misspelled real column shows in the
    preview."""
    sheet = read_objects_csv(
        "Panel,object_type,instance_number,,name,alarm_hi_limit,Panel\n"
        "P-1,analog-value,1,note,AV-1,80,P-1\n"
    )
    assert sheet.rows == [ParsedRow(2, {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"})]
    assert sheet.skipped_columns == ["Panel", "column 4 (no header)", "alarm_hi_limit"]


def test_nothing_skipped_from_an_export():
    csv_text = export_objects_csv([BacnetObject(object_type="analog-input", instance_number=1, name="AI-1")])
    assert read_objects_csv(csv_text).skipped_columns == []


def test_missing_required_column_names_the_skipped_ones():
    with pytest.raises(CsvImportError, match=r"missing required columns: object_type, name \(columns not imported: Type, Name\)"):
        parse_objects_csv("Type,instance_number,Name\nAI,1,AI-1\n")


@pytest.mark.parametrize(
    "short, full",
    [
        ("AI", "analog-input"),
        ("ao", "analog-output"),
        ("Av", "analog-value"),
        ("BI", "binary-input"),
        ("bo", "binary-output"),
        ("BV", "binary-value"),
        ("MSI", "multi-state-input"),
        ("mso", "multi-state-output"),
        (" MSV ", "multi-state-value"),
    ],
)
def test_short_object_types_import_as_the_full_name(short, full):
    (row,) = parse_objects_csv(f'object_type,instance_number,name\n"{short}",1,P-1\n')
    assert row.values["object_type"] == full


@pytest.mark.parametrize(
    "csv_text",
    [
        "object_type,instance_number,name\nMSV,1,Occ Mode\n",
        "object_type,instance_number,name,state_text\nmulti-state-input,1,Occ Mode,\n",
    ],
    ids=["no-column", "blank-cell"],
)
def test_a_new_multistate_row_without_states_gets_the_default_two(csv_text):
    parsed = parse_objects_csv(csv_text)
    fill_default_state_text(parsed, {})
    assert parsed[0].values["state_text"] == ["State 1", "State 2"]
    validate_objects_csv(parsed, {})


def test_default_states_leave_existing_and_other_objects_alone():
    existing_msv = BacnetObject(object_type="multi-state-value", instance_number=1, name="Occ", state_text=["Occ", "Unocc"])
    parsed = parse_objects_csv(
        "object_type,instance_number,name,state_text\n"
        "MSV,1,Occ,\n"
        "AV,1,Setpoint,\n"
    )
    fill_default_state_text(parsed, {("multi-state-value", 1): existing_msv})
    assert parsed[0].values["state_text"] is None
    assert parsed[1].values["state_text"] is None


def test_trailing_blank_header_columns_ignored():
    """A spreadsheet that ever touched a column past the data saves it as
    trailing empty columns, header included; the reader has always
    ignored them, so they stay ignored."""
    (row,) = parse_objects_csv("object_type,instance_number,name,,\nanalog-value,1,AV-1,,\n")
    assert row.values == {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"}


def test_empty_file_rejected():
    with pytest.raises(CsvImportError, match="header"):
        parse_objects_csv("")


def test_excel_bom_and_uppercase_booleans():
    (row,) = parse_objects_csv("﻿object_type,instance_number,name,trended\nanalog-value,1,AV-1,TRUE\n")
    assert row.values["trended"] is True


def test_blank_rows_skipped_and_short_rows_padded():
    rows = parse_objects_csv(
        "object_type,instance_number,name,description\n"
        "analog-value,1,AV-1\n"
        ",,,\n"
        "\n"
        "analog-value,2,AV-2,x\n"
    )
    assert [r.row_num for r in rows] == [2, 5]
    assert rows[0].values["description"] is None
    assert rows[1].values["description"] == "x"


def test_extra_non_blank_cell_rejected():
    assert len(parse_objects_csv("object_type,instance_number,name\nanalog-value,1,AV-1,,\n")) == 1
    with pytest.raises(CsvImportError, match="row 2: more cells than columns"):
        parse_objects_csv("object_type,instance_number,name\nanalog-value,1,AV-1,oops\n")


def test_blank_cells_mean_the_default():
    (row,) = parse_objects_csv(
        "object_type,instance_number,name,schedule_priority,alarm_deadband,bacnet_writable,alarm_high_limit,state_text\n"
        "analog-value,1,AV-1,,,,,\n"
    )
    assert row.values["schedule_priority"] == 14
    assert row.values["alarm_deadband"] == 0.0
    assert row.values["bacnet_writable"] is True
    assert row.values["alarm_high_limit"] is None
    assert row.values["state_text"] is None


def test_list_cells_decode():
    (row,) = parse_objects_csv(
        "object_type,instance_number,name,state_text,alarm_values,alarm_limit_enable\n"
        'multi-state-value,1,MSV-1,"Off;Low, slow;High","2; 3;",TRUE;false\n'
    )
    assert row.values["state_text"] == ["Off", "Low, slow", "High"]
    assert row.values["alarm_values"] == [2, 3]
    assert row.values["alarm_limit_enable"] == [True, False]


@pytest.mark.parametrize(
    "field, cell, message",
    [
        ("alarm_high_limit", "abc", "must be a number"),
        ("alarm_high_limit", "nan", "must be a number"),
        ("alarm_low_limit", "-inf", "must be a number"),
        ("trend_interval_seconds", "1.5", "must be an integer"),
        ("trended", "maybe", "must be true/false"),
        ("alarm_values", "2;x", "must be an integer"),
        ("alarm_event_enable", "true;sometimes;false", "must be true/false"),
        ("notification_recipient_list", "[{", "is not valid JSON"),
    ],
)
def test_bad_cells_name_row_and_column(field, cell, message):
    with pytest.raises(CsvImportError, match=f"row 2: '{field}' {message}"):
        parse_objects_csv(f'object_type,instance_number,name,{field}\nanalog-value,1,AV-1,"{cell}"\n')


def test_identity_cells_required():
    with pytest.raises(CsvImportError, match="row 2: object_type is required"):
        parse_objects_csv("object_type,instance_number,name\n,1,AV-1\n")
    with pytest.raises(CsvImportError, match="row 2: instance_number is required"):
        parse_objects_csv("object_type,instance_number,name\nanalog-value,,AV-1\n")
    with pytest.raises(CsvImportError, match="row 2: name is required"):
        parse_objects_csv("object_type,instance_number,name\nanalog-value,1,\n")


def test_export_then_parse_round_trips_every_format():
    original = BacnetObject(
        object_type="notification-class",
        instance_number=1,
        name="NC-1",
        description='Zone "A", north; east',
        notification_priority=[50, 60, 70],
        notification_ack_required=[True, False, True],
        notification_recipient_list=_RECIPIENTS,
    )
    (row,) = parse_objects_csv(export_objects_csv([original]))
    for field in ("description", "notification_priority", "notification_ack_required", "notification_recipient_list"):
        assert row.values[field] == getattr(original, field), field


@pytest.mark.parametrize("cell", ["true;;false;true", ";true;false;true", "true; ;false"])
def test_empty_item_inside_a_typed_list_refused(cell):
    """Dropping it would shift every later flag into the wrong position."""
    with pytest.raises(CsvImportError, match="row 2: 'alarm_event_enable' has an empty item"):
        parse_objects_csv(f"object_type,instance_number,name,alarm_event_enable\nanalog-value,1,AV-1,{cell}\n")


# --- State_Text that a ;-list can't hold is written as JSON ------------------


def _msv(state_text):
    return BacnetObject(object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=state_text)


@pytest.mark.parametrize(
    "state_text",
    [["Off", "Auto;Hand", "On"], ["Off", "", "On"], ["[x]", "y"], [" Off", "On "]],
)
def test_state_text_round_trips_exactly(state_text):
    [row] = parse_objects_csv(export_objects_csv([_msv(state_text)]))
    assert row.values["state_text"] == state_text


def test_state_text_a_semicolon_list_holds_is_still_written_plain():
    assert _exported_row(_msv(["Off", "On", "Auto"]))["state_text"] == "Off;On;Auto"


def test_state_text_with_a_semicolon_is_written_as_json():
    assert json.loads(_exported_row(_msv(["Off", "Auto;Hand"]))["state_text"]) == ["Off", "Auto;Hand"]


@pytest.mark.parametrize("cell", ['[1, 2]', '["a", null]', '[', '["a"'])
def test_state_text_json_cell_must_be_a_list_of_strings(cell):
    csv_text = "object_type,instance_number,name,state_text\n" + (
        "multi-state-value,1,MSV-1," + '"' + cell.replace('"', '""') + '"' + "\n"
    )
    with pytest.raises(CsvImportError, match="state_text"):
        parse_objects_csv(csv_text)


@pytest.mark.parametrize("risky", ["=1+1", "+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "\r=1+1", "'quoted"])
def test_export_escapes_a_text_cell_a_spreadsheet_would_run(risky):
    row = _exported_row(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            description=risky,
            state_text=[risky, "Off"],
        )
    )
    assert row["description"] == "'" + risky
    if risky == risky.strip():  # edge whitespace sends a label list to JSON
        assert row["state_text"] == "'" + risky + ";Off"


def test_export_leaves_plain_numbers_and_safe_text_unescaped():
    row = _exported_row(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            description="zone - north",
            relinquish_default="-5",
        )
    )
    assert row["description"] == "zone - north"
    assert row["relinquish_default"] == "-5"


@pytest.mark.parametrize("text", ["=1+1", "-1+1", "@x", "\t=x", "'quoted", "''x", "'plain"])
def test_escaped_text_cells_round_trip_exactly(text):
    obj = BacnetObject(
        object_type="multi-state-value",
        instance_number=1,
        name="MSV-1",
        description=text,
        state_text=[text, "Off"],
    )
    [parsed] = parse_objects_csv(export_objects_csv([obj]))
    assert parsed.values["description"] == text
    assert parsed.values["state_text"] == [text, "Off"]


def test_import_keeps_a_hand_written_leading_quote_before_safe_text():
    [parsed] = parse_objects_csv("object_type,instance_number,name,description\nanalog-value,1,AV-1,'tis cold\n")
    assert parsed.values["description"] == "'tis cold"


def test_csv_round_trip_carries_binary_text():
    obj = BacnetObject(
        object_type="binary-value", instance_number=1, name="BV-1", active_text="Running", inactive_text="Stopped"
    )
    [parsed] = parse_objects_csv(export_objects_csv([obj]))
    assert (parsed.values["active_text"], parsed.values["inactive_text"]) == ("Running", "Stopped")


def test_csv_round_trip_carries_start_value():
    obj = BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", start_value="21.5")
    [parsed] = parse_objects_csv(export_objects_csv([obj]))
    assert parsed.values["start_value"] == "21.5"


def test_csv_round_trip_carries_trend_log_identity():
    obj = BacnetObject(
        object_type="analog-input", instance_number=1, name="AI-1", trended=True,
        trend_log_instance=77, trend_log_name="Zone Temp Log",
    )
    [parsed] = parse_objects_csv(export_objects_csv([obj]))
    assert (parsed.values["trend_log_instance"], parsed.values["trend_log_name"]) == (77, "Zone Temp Log")


def test_a_cell_over_the_csv_field_limit_is_an_import_error_with_its_row():
    big = "x" * (csv.field_size_limit() + 1)
    header = "object_type,instance_number,name,description\n"
    with pytest.raises(CsvImportError, match="row 3"):
        read_objects_csv(header + "analog-value,1,A,fine\nanalog-value,2,B," + big + "\n")
    with pytest.raises(CsvImportError, match="row 1"):
        read_objects_csv("object_type,instance_number,name," + big + "\n")
    assert read_objects_csv(header + "analog-value,1,A," + "x" * csv.field_size_limit() + "\n")


def test_a_cr_only_csv_parses():
    text = "object_type,instance_number,name\ranalog-value,1,A\ranalog-value,2,B\r"
    assert [values["name"] for _, values in read_objects_csv(text).rows] == ["A", "B"]


def test_a_csv_error_that_is_not_the_field_limit_keeps_its_own_text():
    from bacpack.csv_io import _records

    class Reader:
        def __next__(self):
            raise csv.Error("new-line character seen in unquoted field")

    with pytest.raises(CsvImportError, match="row 1: new-line character seen in unquoted field"):
        next(_records(Reader()))


def test_validate_refuses_a_trended_notification_class():
    row = ParsedRow(3, {"object_type": "notification-class", "instance_number": 1, "name": "NC-1", "trended": True})
    with pytest.raises(CsvImportError, match="row 3: a Notification Class can't be trended"):
        validate_objects_csv([row], {})
