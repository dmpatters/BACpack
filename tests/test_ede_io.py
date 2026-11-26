"""ede_io: reading an EDE file."""
from __future__ import annotations

import pytest

from bacpack.ede_io import EdeError, looks_like_ede, read_ede, read_state_texts, resolve_state_texts, unit_name
from bacpack.point_config import validate_units

from tests.ede_fixtures import COLUMNS, device_row, ede_bytes, ede_row, ede_text, state_texts_text


def _read(rows=None, **kw):
    rows = rows if rows is not None else [device_row(), ede_row()]
    enc = kw.pop("encoding", "utf-8")
    bom = kw.pop("bom", False)
    crlf = kw.pop("crlf", False)
    return read_ede(ede_bytes(ede_text(rows, **kw), enc, bom, crlf))


def test_semicolon_and_comma_files_read_the_same_rows():
    rows = [device_row(), ede_row(name="AI 1", description="Room, north")]
    semi = _read(rows)
    comma = _read(rows, sep=",")
    assert [(r.object_type, r.instance, r.name, r.description) for r in semi.rows] == [
        (r.object_type, r.instance, r.name, r.description) for r in comma.rows
    ]
    assert comma.rows[1].description == "Room, north"


# header_lines=0 puts the BOM on the title line itself, as Excel's "CSV UTF-8" does.
@pytest.mark.parametrize("header_lines,hashes", [(5, True), (0, False), (0, True)])
def test_utf8_bom_is_stripped_from_the_first_title(header_lines, hashes):
    file = _read(bom=True, header_lines=header_lines, hashes=hashes)
    assert [r.name for r in file.rows] == ["Plant controller", "AI 1"]
    assert file.columns_not_used and not any("﻿" in c for c in file.columns_not_used)
    assert looks_like_ede(ede_bytes(ede_text([ede_row()], header_lines=header_lines, hashes=hashes), bom=True))


def test_a_quoted_cell_with_a_line_break_is_one_row_and_later_rows_keep_their_line_numbers():
    rows = [device_row(), ede_row(name="Two lines", description="first\nsecond"), ede_row(instance=-1, name="Bad")]
    file = _read(rows)
    assert [r.name for r in file.rows] == ["Plant controller", "Two lines"]
    assert file.rows[1].description == "first\nsecond"
    # Five header lines, the title, the device row, then two lines of the second row.
    assert any("row 10 (Bad)" in note for note in file.file_notes)


def test_windows_1252_file_is_read_with_a_note():
    file = _read([device_row(), ede_row(name="Büro")], encoding="cp1252", crlf=True)
    assert file.rows[1].name == "Büro"
    assert any("Windows-1252" in note for note in file.file_notes)


@pytest.mark.parametrize("header_lines,hashes", [(4, True), (4, False), (6, True), (6, False), (0, False)])
def test_header_block_with_and_without_hashes_and_4_or_6_lines(header_lines, hashes):
    file = _read(header_lines=header_lines, hashes=hashes)
    assert len(file.rows) == 2
    assert (file.project_name == "Test project") == (header_lines > 0)


def test_project_name_and_layout_version_are_read():
    file = _read(header_lines=5)
    assert (file.project_name, file.version_of_layout) == ("Test project", "2.1")


def test_comment_and_blank_rows_are_skipped():
    text = ede_text([device_row(), ede_row()]) + "# a comment;;;;\n;;;;;\n\n"
    file = read_ede(text.encode())
    assert len(file.rows) == 2


def test_quoted_cell_with_separator_inside():
    file = _read([device_row(), ede_row(name="Room; north")])
    assert file.rows[1].name == "Room; north"


@pytest.mark.parametrize(
    "titles",
    [
        ["keyname", "Device Obj.-Instance", "OBJECT_NAME", "Object-Type", "object instance"],
        ["keyname", "device-obj-instance", "object-name", "object-type", "object-instance"],
    ],
)
def test_titles_match_with_spaces_dots_dashes_underscores_and_case(titles):
    text = "#" + ";".join(titles) + "\nk;1000;Name A;0;1\n"
    file = read_ede(text.encode())
    assert [(r.device_instance, r.name, r.instance) for r in file.rows] == [(1000, "Name A", 1)]


def test_missing_mandatory_columns_are_named_in_the_error():
    text = "#keyname;object-name;object-type\nk;A;0\n"
    with pytest.raises(EdeError) as exc:
        read_ede(text.encode())
    assert "device obj.-instance" in str(exc.value) and "object-instance" in str(exc.value)
    assert "object-name" not in str(exc.value)


def test_unknown_columns_are_listed_not_refused():
    file = _read(columns=COLUMNS + ["my own column"])
    assert "my own column" in file.columns_not_used
    assert "keyname" not in file.columns_not_used and "object-name" not in file.columns_not_used


@pytest.mark.parametrize(
    "cell,expected",
    [
        (0, "analog-input"),
        ("analog-input", "analog-input"),
        ("AI", "analog-input"),
        (19, "multi-state-value"),
        ("MSV", "multi-state-value"),
        (8, "device"),
        (17, "schedule"),
        (6, "calendar"),
        (15, "notification-class"),
        (20, "trend-log"),
    ],
)
def test_object_type_numbers_names_and_short_forms(cell, expected):
    file = _read([ede_row(type=cell, instance=3, name="X")])
    assert file.rows[0].object_type == expected


def test_unsupported_type_number_is_kept_as_unsupported():
    file = _read([ede_row(type=9, instance=3, name="Loop"), ede_row(type=600, instance=1, name="Prop")])
    assert [r.object_type for r in file.rows] == ["unsupported:9", "unsupported:600"]


def test_file_and_network_port_keep_their_names_so_they_are_noted_not_skipped():
    file = _read([ede_row(type=10, instance=1, name="F"), ede_row(type=56, instance=1, name="NP")])
    assert [r.object_type for r in file.rows] == ["file", "network-port"]


@pytest.mark.parametrize("instance", ["-1", "4194304", "abc"])
def test_non_numeric_or_negative_or_huge_instance_row_is_noted_and_dropped(instance):
    file = _read([device_row(), ede_row(instance=instance, name="Bad"), ede_row(instance=2, name="Good")])
    assert [r.name for r in file.rows] == ["Plant controller", "Good"]
    assert any("row" in note and "Bad" in note for note in file.file_notes)


def test_blank_name_row_is_dropped_with_a_note():
    file = _read([device_row(), ede_row(name="  ", instance=5), ede_row(name="OK", instance=6)])
    assert [r.instance for r in file.rows] == [1000, 6]
    assert any("name" in note for note in file.file_notes)


@pytest.mark.parametrize(
    "cell,expected",
    [("Y", True), ("N", False), ("yes", True), ("no", False), ("true", True), ("false", False),
     ("1", True), ("0", False), ("maybe", None), ("", None)],
)
def test_yes_no_cells_in_all_spellings(cell, expected):
    file = _read([ede_row(settable=cell)])
    assert file.rows[0].writable == ([] if expected is None else [expected])


def test_writable_collects_settable_commandable_and_command_value():
    columns = COLUMNS + ["commandable", "commandValue"]
    file = _read([ede_row(type=2, settable="Y", commandable="N", command_value="")], columns=columns)
    assert file.rows[0].writable == [True, False]


def test_decimal_comma_numbers():
    file = _read([ede_row(hi="21,5", low="-3.25")])
    assert (file.rows[0].hi_limit, file.rows[0].low_limit) == (21.5, -3.25)


def test_unparseable_limit_is_dropped_with_a_row_note():
    file = _read([ede_row(hi="abc")])
    assert file.rows[0].hi_limit is None and file.rows[0].notes


def test_dash_dash_dash_unit_is_dropped_with_a_row_note():
    file = _read([ede_row(unit="---")])
    assert file.rows[0].unit_code is None
    assert any("unit-code" in n for n in file.rows[0].notes)


def test_inline_state_text_split_on_pipe():
    columns = COLUMNS + ["state-text"]
    file = _read([ede_row(type=3, state_text="[Normal|Alarm]")], columns=columns)
    assert file.rows[0].inline_state_text == ["Normal", "Alarm"]


def test_state_text_reference_kept_as_text():
    file = _read([ede_row(type=3, state_ref="7")])
    assert file.rows[0].state_text_reference == "7"


def test_notification_class_and_default_and_description_cells():
    columns = COLUMNS + ["notification-class"]
    file = _read([ede_row(nc="3", default="21,5", description="Desc")], columns=columns)
    row = file.rows[0]
    assert (row.notification_class, row.default, row.description) == (3, "21,5", "Desc")


def test_supplied_fields_are_recorded_per_row():
    file = _read([ede_row(description="d", settable="Y"), ede_row(instance=2, name="B")])
    assert {"description", "writable"} <= file.rows[0].supplied
    assert "description" not in file.rows[1].supplied


def test_not_ede_is_refused():
    with pytest.raises(EdeError):
        read_ede(b"object_type,instance_number,name\nanalog-value,1,AV\n")


def test_looks_like_ede_true_only_for_ede():
    assert looks_like_ede(ede_bytes(ede_text([ede_row()])))
    assert not looks_like_ede(b"object_type,instance_number,name\nanalog-value,1,AV\n")
    assert not looks_like_ede(b"")
    assert not looks_like_ede(bytes(range(256)) * 20)


def test_keyname_after_line_20_is_not_ede():
    text = "\n" * 20 + ede_text([ede_row()], header_lines=0)
    assert not looks_like_ede(text.encode())
    with pytest.raises(EdeError):
        read_ede(text.encode())


def test_keyname_with_no_object_type_column_is_not_ede():
    assert not looks_like_ede(b"keyname;a;b\nx;1;2\n")


def test_header_only_file_has_no_rows_and_is_refused_with_a_message():
    with pytest.raises(EdeError) as exc:
        _read([])
    assert "no objects" in str(exc.value)


def test_upload_of_a_file_with_only_the_device_row_reads():
    file = _read([device_row()])
    assert [r.object_type for r in file.rows] == ["device"]
    assert file.devices() == [1000]


def test_devices_are_distinct_in_first_seen_order():
    file = _read([ede_row(device=2000, name="A"), ede_row(device=1000, name="B"), ede_row(device=2000, instance=2, name="C")])
    assert file.devices() == [2000, 1000]


# --- state texts and units ----------------------------------------------------------


def test_state_texts_file_by_reference():
    table = read_state_texts(state_texts_text([["1", "Off", "On"], ["2", "Low", "Mid", "High"]]).encode())
    assert table == {"1": ["Off", "On"], "2": ["Low", "Mid", "High"]}


def test_state_texts_file_trailing_empty_texts_dropped_and_header_lines_ignored():
    text = "#header;x\n#Reference;Text 1\n\n5;A;B;;;\n"
    assert read_state_texts(text.encode()) == {"5": ["A", "B"]}
    assert read_state_texts(b"") == {}


def test_state_texts_cp1252_and_comma():
    data = "9,Fenêtre ouverte,Fermée\n".encode("cp1252")
    assert read_state_texts(data) == {"9": ["Fenêtre ouverte", "Fermée"]}


def test_inline_text_wins_over_reference():
    columns = COLUMNS + ["state-text"]
    file = _read([ede_row(type=3, state_ref="1", state_text="[A|B]")], columns=columns)
    resolve_state_texts(file, {"1": ["X", "Y"]})
    assert file.rows[0].state_texts == ["A", "B"] and not file.rows[0].state_text_missing


def test_reference_is_looked_up_in_the_table():
    file = _read([ede_row(type=3, state_ref="1")])
    resolve_state_texts(file, {"1": ["X", "Y"]})
    assert file.rows[0].state_texts == ["X", "Y"]
    assert "state_texts" in file.rows[0].supplied


def test_missing_reference_marks_state_text_missing():
    file = _read([ede_row(type=3, state_ref="2")])
    resolve_state_texts(file, {"1": ["X", "Y"]})
    assert file.rows[0].state_texts is None and file.rows[0].state_text_missing


def test_no_state_texts_file_marks_referenced_rows_missing():
    file = _read([ede_row(type=3, state_ref="2"), ede_row(type=0, instance=2, name="no ref")])
    resolve_state_texts(file, None)
    assert [r.state_text_missing for r in file.rows] == [True, False]


@pytest.mark.parametrize("code,name", [(95, "noUnits"), (62, "degreesCelsius"), (98, "percent")])
def test_unit_code_to_bacpack_name(code, name):
    assert unit_name(code) == name
    validate_units("analog-input", name)
    assert _read([ede_row(unit=code)]).rows[0].units == name


def test_unknown_unit_number_is_dropped_with_a_note():
    row = _read([ede_row(unit=99999)]).rows[0]
    assert row.units is None and "units" not in row.supplied
    assert any("99999" in note for note in row.notes)


def test_inline_state_text_drops_trailing_empty_texts_like_the_file_does():
    columns = COLUMNS + ["state-text"]
    file = _read(
        [ede_row(type=19, state_text="[A|B|]"), ede_row(type=19, instance=2, name="Gap", state_text="[A||C]")],
        columns=columns,
    )
    assert file.rows[0].inline_state_text == ["A", "B"]
    assert file.rows[1].inline_state_text == ["A", "", "C"]


def test_state_texts_file_read_as_windows_1252_is_noted_on_the_file():
    table = read_state_texts("9,Fenêtre ouverte,Fermée\n".encode("cp1252"))
    file = _read([ede_row(type=3, state_ref="9")])
    resolve_state_texts(file, table)
    assert any("state-texts file" in note and "Windows-1252" in note for note in file.file_notes)


def test_state_texts_file_in_utf8_adds_no_file_note():
    file = _read([ede_row(type=3, state_ref="9")])
    resolve_state_texts(file, read_state_texts("9,Off,On\n".encode()))
    assert file.file_notes == []


def _too_long() -> str:
    import csv

    return "x" * (csv.field_size_limit() + 1)


def test_a_cell_over_the_csv_limit_is_an_ede_error_with_its_line():
    big = _too_long()
    with pytest.raises(EdeError, match="line 8:"):
        _read([device_row(), ede_row(description=big)])
    text = ede_text([device_row(), ede_row()])
    with pytest.raises(EdeError, match="longer than"):
        read_ede(("#project;" + big + "\n" + text).encode())
    with pytest.raises(EdeError, match="longer than"):
        read_ede(text.replace("#keyname", "#keyname;" + big, 1).encode())


def test_looks_like_ede_is_true_for_a_title_line_that_is_broken_not_absent():
    text = ede_text([device_row()]).replace("#keyname", "#keyname;" + _too_long(), 1)
    assert looks_like_ede(text.encode())


def test_a_state_texts_cell_over_the_csv_limit_is_an_ede_error():
    with pytest.raises(EdeError, match="state-texts file"):
        read_state_texts(state_texts_text([["1", _too_long()]]).encode())


def test_an_ede_csv_error_that_is_not_the_field_limit_keeps_its_own_text():
    import csv

    from bacpack.ede_io import _too_long

    assert "longer than" in str(_too_long("line 3", csv.Error("field larger than field limit (131072)")))
    problem = str(_too_long("line 3", csv.Error("new-line character seen in unquoted field")))
    assert problem == "line 3: new-line character seen in unquoted field"
