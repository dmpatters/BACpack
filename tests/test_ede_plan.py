"""ede_plan: an EDE device's rows planned like a clone."""
from __future__ import annotations

from bacpack import object_config
from bacpack.config_io import OBJECT_FIELD_DEFAULTS
from bacpack.config_schemas import ObjectCreate
from bacpack.ede_io import read_ede, resolve_state_texts
from bacpack.ede_plan import ExistingDevice, device_name_of, plan_device, plan_for_existing

from tests.ede_fixtures import WIDE, device_row, ede_bytes, ede_row, ede_text


def _file(rows, *, columns=WIDE, table=None):
    file = read_ede(ede_bytes(ede_text(rows, columns=columns)))
    resolve_state_texts(file, table)
    return file


def _plan(rows, *, columns=WIDE, table=None, device=1000, device_id=599050, device_name="Plant"):
    file = _file(rows, columns=columns, table=table)
    plan = plan_device(file.rows, device_instance=device, device_id=device_id, device_name=device_name)
    # Whatever the planner returns must be savable.
    for row in plan.objects:
        object_config.check_object(row)
        ObjectCreate.model_validate(row)
    assert object_config.check_device(device_name, plan.objects) is None
    return plan


def _row(plan, name):
    [row] = [row for row in plan.objects if row["name"] == name]
    return row


def _notes(plan, name=None):
    return [n["note"] for n in plan.notes if name is None or n["name"] == name]


# --- Points ---------------------------------------------------------------------


def test_points_become_rows_with_units_texts_and_defaults():
    plan = _plan(
        [
            device_row(),
            ede_row(type=0, instance=1, name="Temp", unit=62, default="21,5", description="Room"),
            ede_row(type=3, instance=1, name="Door", state_text="[Open|Closed]"),
            ede_row(type=19, instance=1, name="Mode", state_text="[Low|Mid|High]"),
            ede_row(type=2, instance=1, name="Setpoint", default="21,5"),
        ]
    )
    temp = _row(plan, "Temp")
    assert (temp["units"], temp["start_value"], temp["description"]) == ("degreesCelsius", "21.5", "Room")
    door = _row(plan, "Door")
    assert (door["inactive_text"], door["active_text"]) == ("Open", "Closed")
    assert _row(plan, "Mode")["state_text"] == ["Low", "Mid", "High"]
    assert _row(plan, "Setpoint")["relinquish_default"] == "21.5"


def test_device_row_gives_the_device_name_and_description():
    rows = [device_row(name="Boiler", description="Plant room"), ede_row()]
    file = _file(rows)
    assert device_name_of(file.rows, 1000) == "Boiler"
    plan = plan_device(file.rows, device_instance=1000, device_id=7, device_name="Boiler")
    assert plan.device["description"] == "Plant room" and plan.device["id"] == 7


def test_no_device_row_names_the_device_by_instance_and_notes_it():
    file = _file([ede_row()])
    assert device_name_of(file.rows, 1000) == "Device 1000"
    plan = plan_device(file.rows, device_instance=1000, device_id=7, device_name="Device 1000")
    assert any("no device row" in note for note in _notes(plan))


def test_limits_are_saved_but_alarming_stays_off():
    plan = _plan([ede_row(type=2, name="Pressure", hi="30", low="5,5")])
    row = _row(plan, "Pressure")
    assert (row["alarm_high_limit"], row["alarm_low_limit"], row["alarming_enabled"]) == (30.0, 5.5, False)


def test_limits_on_a_binary_are_dropped_with_a_note():
    plan = _plan([ede_row(type=3, name="Door", hi="1")])
    assert _row(plan, "Door")["alarm_high_limit"] is None
    assert any("limit" in note for note in _notes(plan, "Door"))


def test_notification_class_dropped_with_a_note_when_the_class_is_not_in_the_file():
    plan = _plan([ede_row(type=0, name="Temp", nc="3")])
    row = _row(plan, "Temp")
    assert row["notification_class_instance"] is None and row["alarming_enabled"] is False
    assert any("notification class 3" in note for note in _notes(plan, "Temp"))


def test_any_no_makes_a_value_object_not_writable_with_the_file_note():
    plan = _plan([ede_row(type=2, name="SP", settable="N", commandable="Y")])
    assert _row(plan, "SP")["bacnet_writable"] is False
    assert any("not commandable in the file" in note for note in _notes(plan, "SP"))


def test_all_blank_leaves_writable_default():
    plan = _plan([ede_row(type=2, name="SP")])
    assert _row(plan, "SP")["bacnet_writable"] is True and not _notes(plan, "SP")


def test_writable_columns_are_ignored_on_inputs_and_outputs():
    plan = _plan([ede_row(type=0, name="AI", settable="N"), ede_row(type=1, instance=2, name="AO", settable="N")])
    assert _row(plan, "AI")["bacnet_writable"] is True and _row(plan, "AO")["bacnet_writable"] is True


def test_missing_state_text_reference_keeps_on_off_for_binary_and_defaults_multistate_with_notes():
    plan = _plan([ede_row(type=3, name="Door", state_ref="9"), ede_row(type=19, name="Mode", state_ref="9")])
    door, mode = _row(plan, "Door"), _row(plan, "Mode")
    assert (door["active_text"], door["inactive_text"]) == (None, None)
    assert mode["state_text"] == ["State 1", "State 2"]
    assert any("state texts" in note for note in _notes(plan, "Door"))
    assert any("state texts" in note for note in _notes(plan, "Mode"))


def test_multistate_without_any_state_text_reference_gets_defaults_and_a_note():
    plan = _plan([ede_row(type=13, name="Mode")])
    assert _row(plan, "Mode")["state_text"] == ["State 1", "State 2"]
    assert _notes(plan, "Mode")


def test_bad_default_is_dropped_with_a_note_and_the_point_still_imports():
    plan = _plan([ede_row(type=2, name="SP", default="abc")])
    assert _row(plan, "SP")["relinquish_default"] is None
    assert any("'abc'" in note for note in _notes(plan, "SP"))


def test_default_a_rule_refuses_is_dropped_with_a_note():
    plan = _plan([ede_row(type=19, name="Mode", state_text="[A|B]", default="7")])
    assert _row(plan, "Mode")["relinquish_default"] is None and _notes(plan, "Mode")


def test_row_notes_from_the_reader_reach_the_object():
    plan = _plan([ede_row(type=0, name="AI", unit="---")])
    assert any("unit-code" in note for note in _notes(plan, "AI"))


def test_duplicate_names_get_numbered_suffixes_and_a_note():
    plan = _plan([ede_row(name="Damper", instance=1), ede_row(name="Damper", instance=2), ede_row(name="Damper", instance=3)])
    assert sorted(row["name"] for row in plan.objects) == ["Damper", "Damper (2)", "Damper (3)"]
    assert any("another object named 'Damper'" in note for note in _notes(plan, "Damper (2)"))


def test_a_second_object_with_the_same_identifier_is_skipped():
    plan = _plan([ede_row(name="A", instance=1), ede_row(name="B", instance=1)])
    assert [row["name"] for row in plan.objects] == ["A"]
    assert plan.skipped[0]["name"] == "B"


def test_unsupported_types_are_skipped_counted_and_listed():
    plan = _plan(
        [
            ede_row(type=0, name="AI"),
            ede_row(type=9, instance=1, name="Loop 1"),
            ede_row(type=600, instance=1, name="Prop"),
            ede_row(type=10, instance=1, name="File"),
            ede_row(type=56, instance=1, name="NP"),
        ]
    )
    assert plan.skipped_types == {"event-enrollment (9)": 1, "600": 1}
    assert {s["name"] for s in plan.skipped} == {"Loop 1", "Prop"}
    assert all(s["code"] == "unsupported-type" for s in plan.skipped)
    assert any("BACpack builds its own" in note for note in _notes(plan, "File"))
    assert [row["name"] for row in plan.objects] == ["AI"]


def test_a_row_check_object_refuses_is_skipped_and_the_rest_imports():
    plan = _plan([ede_row(name="NetworkPort-1", instance=1), ede_row(name="Fine", instance=2)])
    assert [row["name"] for row in plan.objects] == ["Fine"]
    assert plan.skipped and plan.skipped[0]["code"] == "refused"


def test_other_devices_rows_are_left_out():
    plan = _plan([ede_row(device=1000, name="Mine"), ede_row(device=2000, name="Theirs")])
    assert [row["name"] for row in plan.objects] == ["Mine"]


def test_plan_counts_per_type():
    plan = _plan([ede_row(type=0, instance=1, name="A"), ede_row(type=0, instance=2, name="B"), ede_row(type=3, instance=1, name="C")])
    assert plan.counts == {"analog-input": 2, "binary-input": 1}


# --- Stand-ins for schedules, calendars and notification classes ---------------------


def test_schedule_is_created_empty_analog_with_null_default():
    plan = _plan([ede_row(type=17, instance=1, name="Occupancy", description="Weekly")])
    row = _row(plan, "Occupancy")
    assert row["schedule_value_type"] == "analog" and row["schedule_default"] is None
    assert not row["weekly_schedule"] and not row["exception_schedule"] and not row["schedule_write_targets"]
    assert row["schedule_priority"] == OBJECT_FIELD_DEFAULTS["schedule_priority"] and row["description"] == "Weekly"
    assert "created empty: set its value type, times and targets" in _notes(plan, "Occupancy")


def test_calendar_is_created_with_no_dates():
    plan = _plan([ede_row(type=6, instance=1, name="Holidays", description="Closed"), ede_row(name="AI")])
    row = _row(plan, "Holidays")
    assert not row["date_list"] and row["description"] == "Closed"
    assert not plan.skipped
    assert "created empty: add its dates" in _notes(plan, "Holidays")


def test_notification_class_created_with_default_priority_and_no_recipients():
    plan = _plan([ede_row(type=15, instance=3, name="Alarms")])
    row = _row(plan, "Alarms")
    assert row["notification_priority"] == OBJECT_FIELD_DEFAULTS["notification_priority"]
    assert row["notification_recipient_list"] is None
    assert "created empty: add its recipients" in _notes(plan, "Alarms")


def test_point_notification_class_kept_when_the_class_is_in_the_file():
    plan = _plan([ede_row(type=15, instance=3, name="Alarms"), ede_row(type=0, instance=1, name="Temp", nc="3")])
    row = _row(plan, "Temp")
    assert row["notification_class_instance"] == 3 and row["alarming_enabled"] is False
    assert not any("notification class" in note for note in _notes(plan, "Temp"))


def test_schedule_name_clash_with_a_point_gets_a_numbered_suffix():
    plan = _plan([ede_row(type=0, instance=1, name="Zone"), ede_row(type=17, instance=1, name="Zone")])
    assert sorted(row["name"] for row in plan.objects) == ["Zone", "Zone (2)"]


# --- Trend logs ------------------------------------------------------------------------


def _log_row(name, instance=5, **cells):
    return ede_row(type=20, instance=instance, name=name, **cells)


def _skipped(plan, name):
    [found] = [s for s in plan.skipped if s["name"] == name]
    return found


def test_trend_log_attaches_by_name_prefix():
    plan = _plan([ede_row(name="Hall.mesure"), _log_row("Hall.mesure.Tendance1")])
    row = _row(plan, "Hall.mesure")
    assert row["trended"] is True and row["trend_mode"] == "interval"
    assert "trending guessed from the trend log's name" in _notes(plan, "Hall.mesure")
    assert plan.skipped == [] and plan.skipped_types == {}


def test_trend_log_keeps_its_instance_and_name():
    plan = _plan([ede_row(name="Hall.mesure"), _log_row("Hall.mesure.Tendance1", instance=77)])
    row = _row(plan, "Hall.mesure")
    assert (row["trend_log_instance"], row["trend_log_name"]) == (77, "Hall.mesure.Tendance1")


def test_longest_point_name_wins():
    plan = _plan([ede_row(instance=1, name="Temp"), ede_row(instance=2, name="Temp.Room"), _log_row("Temp.Room.Log")])
    assert _row(plan, "Temp.Room")["trended"] is True and _row(plan, "Temp")["trended"] is False


def test_a_longer_name_that_is_not_followed_by_a_separator_does_not_match():
    plan = _plan([ede_row(name="Temp"), _log_row("Temperature.Log")])
    assert _row(plan, "Temp")["trended"] is False
    assert _skipped(plan, "Temperature.Log")["code"] == "standin-unmatched"


def test_equal_prefix_candidates_are_a_tie_and_the_log_is_skipped():
    plan = _plan(
        [
            ede_row(instance=1, name="Alpha", keyname="K1"),
            ede_row(instance=2, name="Beta", keyname="K1"),
            _log_row("Gamma", instance=50, keyname="K1.Log"),
        ]
    )
    assert not any(row["trended"] for row in plan.objects)
    assert _skipped(plan, "Gamma")["code"] == "standin-unmatched"


def test_keyname_rule_used_when_the_name_rule_finds_nothing():
    plan = _plan([ede_row(name="Alpha", keyname="K7"), _log_row("Unrelated", instance=50, keyname="K7.Log")])
    assert _row(plan, "Alpha")["trended"] is True


def test_instance_number_rule_last_and_only_when_unique():
    plan = _plan([ede_row(instance=9, name="Alpha"), _log_row("Unrelated", instance=9)])
    assert _row(plan, "Alpha")["trended"] is True
    two = _plan([ede_row(type=0, instance=9, name="Alpha"), ede_row(type=3, instance=9, name="Beta"), _log_row("Unrelated", instance=9)])
    assert not any(row["trended"] for row in two.objects)
    assert _skipped(two, "Unrelated")["code"] == "standin-unmatched"


def test_no_candidate_is_skipped_with_the_reason():
    plan = _plan([ede_row(name="Alpha"), _log_row("Unrelated", instance=50)])
    assert _skipped(plan, "Unrelated")["reason"] == "an EDE file doesn't say which point this trend log records"
    assert _skipped(plan, "Unrelated")["object_type"] == "trend-log"


def test_two_logs_on_one_point_first_wins_second_listed():
    plan = _plan([ede_row(name="Alpha"), _log_row("Alpha.One", instance=5), _log_row("Alpha.Two", instance=6)])
    assert _row(plan, "Alpha")["trend_log_name"] == "Alpha.One"
    assert "Alpha.One" in _skipped(plan, "Alpha.Two")["reason"]


def test_trend_log_whose_name_clashes_is_skipped_with_the_planners_reason():
    plan = _plan([ede_row(instance=1, name="Alpha"), ede_row(type=2, instance=1, name="Alpha.Log"), _log_row("Alpha.Log")])
    assert not any(row["trended"] for row in plan.objects)
    assert "already an object's name" in _skipped(plan, "Alpha.Log")["reason"]


def _walks_with_logs(count, existing=None):
    """Whole-table walks of the planned rows and the file's rows when `count`
    points each get a log by name, and as many more logs match nothing."""
    from bacpack.ede_plan import EdePlanner, to_raw

    from tests.row_walks import WalkCountingRows

    rows = [ede_row(type=0, instance=i, name=f"P{i}", keyname=f"K{i}") for i in range(1, count + 1)]
    rows += [_log_row(f"P{i}.Log", instance=i) for i in range(1, count + 1)]
    rows += [_log_row(f"Q{i}.Log", instance=10000 + i) for i in range(1, count + 1)]
    mine = _file(rows).rows
    planner = EdePlanner(to_raw(mine, device_instance=1000), 0, "Plant", ede_rows=mine, existing=existing)
    planner.rows = WalkCountingRows()
    planner.ede_rows = WalkCountingRows(planner.ede_rows)
    planner.plan()
    assert all(planner.rows[("analog-input", i)]["trend_log_name"] == f"P{i}.Log" for i in range(1, count + 1))
    assert sum(s["code"] == "standin-unmatched" for s in planner.skipped) == count
    return planner.rows.walks, planner.ede_rows.walks


def test_matching_trend_logs_does_not_walk_every_row_once_per_log():
    # Per-log scans are O(logs x objects): minutes on a field-sized file.
    assert _walks_with_logs(300) == _walks_with_logs(30)
    assert _walks_with_logs(300, _existing("merge")) == _walks_with_logs(30, _existing("merge"))


def test_a_log_never_attaches_to_a_schedule():
    plan = _plan([ede_row(type=17, name="Occupancy"), _log_row("Occupancy.Trend", instance=50)])
    assert _skipped(plan, "Occupancy.Trend")["code"] == "standin-unmatched"
    assert _row(plan, "Occupancy")["trended"] is False


# --- Planning against an existing device ---------------------------------------------------


def _existing(mode, *rows):
    return ExistingDevice(
        mode, {(r["object_type"], r["instance_number"]): {**OBJECT_FIELD_DEFAULTS, **r} for r in rows}
    )


def _plan_into(rows, existing, *, columns=WIDE, device_name="Plant"):
    file = _file(rows, columns=columns)
    plan = plan_for_existing(file.rows, device_instance=1000, existing=existing, device_name=device_name)
    assert plan.problem is None
    return plan


def _by_name(plan):
    return {row["name"]: row for row in plan.objects}


def _av(instance, name, **fields):
    return {"object_type": "analog-value", "instance_number": instance, "name": name, **fields}


def test_merge_skips_objects_already_on_the_device_and_lists_them():
    existing = _existing("merge", _av(1, "Temp"))
    plan = _plan_into([ede_row(type=2, instance=1, name="Temp"), ede_row(type=2, instance=2, name="New")], existing)
    assert list(_by_name(plan)) == ["New"]
    [skipped] = plan.skipped
    assert (skipped["name"], skipped["code"], skipped["reason"]) == ("Temp", "already-present", "already on the device")


def test_merge_reimport_of_the_same_file_creates_nothing_and_is_not_a_problem():
    existing = _existing("merge", _av(1, "Temp"), _av(2, "New"))
    plan = _plan_into([ede_row(type=2, instance=1, name="Temp"), ede_row(type=2, instance=2, name="New")], existing)
    assert plan.objects == [] and len(plan.skipped) == 2


def test_merge_name_clash_with_an_existing_object_gets_a_suffix():
    existing = _existing("merge", _av(5, "Zone"))
    plan = _plan_into([ede_row(type=0, instance=1, name="Zone")], existing)
    assert list(_by_name(plan)) == ["Zone (2)"]
    assert any("the device already has an object named 'Zone'" in note for note in _notes(plan, "Zone (2)"))


def test_merge_notification_class_on_the_device_is_accepted():
    existing = _existing("merge", {"object_type": "notification-class", "instance_number": 3, "name": "Alarms"})
    plan = _plan_into([ede_row(type=0, instance=1, name="Temp", nc="3")], existing)
    assert _row(plan, "Temp")["notification_class_instance"] == 3


def test_sync_name_of_an_object_the_file_omits_is_not_taken():
    existing = _existing("sync", _av(9, "Zone"))
    plan = _plan_into([ede_row(type=0, instance=1, name="Zone")], existing)
    assert list(_by_name(plan)) == ["Zone"]


def test_sync_row_is_partial_only_fields_the_file_gave():
    existing = _existing("sync", _av(1, "SP", units="percent", description="keep me"))
    plan = _plan_into([ede_row(type=2, instance=1, name="SP", default="5")], existing)
    assert plan.objects == [
        {"object_type": "analog-value", "instance_number": 1, "name": "SP", "relinquish_default": "5.0"}
    ]


def test_sync_blank_writable_leaves_bacnet_writable_alone():
    existing = _existing("sync", _av(1, "SP", bacnet_writable=False))
    plan = _plan_into([ede_row(type=2, instance=1, name="SP", description="d")], existing)
    assert "bacnet_writable" not in plan.objects[0] and plan.objects[0]["description"] == "d"


def test_sync_supplied_value_equal_to_the_default_is_still_written():
    existing = _existing("sync", _av(1, "SP", bacnet_writable=False))
    plan = _plan_into([ede_row(type=2, instance=1, name="SP", settable="Y")], existing)
    assert plan.objects[0]["bacnet_writable"] is True


def test_sync_existing_untrended_point_with_a_matching_trend_log_is_trended():
    existing = _existing("sync", _av(1, "SP"))
    plan = _plan_into([ede_row(type=2, instance=1, name="SP"), _log_row("SP.Log", instance=40)], existing)
    row = plan.objects[0]
    assert (row["trended"], row["trend_log_instance"], row["trend_log_name"]) == (True, 40, "SP.Log")
    assert "trending guessed from the trend log's name" in _notes(plan, "SP")


def test_sync_existing_schedule_is_not_reset():
    existing = _existing(
        "sync",
        {
            "object_type": "schedule", "instance_number": 1, "name": "Occupancy", "schedule_value_type": "binary",
            "schedule_default": "inactive", "weekly_schedule": [[{"time": "08:00", "value": "active"}]] * 7,
        },
    )
    plan = _plan_into([ede_row(type=17, instance=1, name="Occupancy")], existing)
    assert plan.objects == [{"object_type": "schedule", "instance_number": 1, "name": "Occupancy"}]
    assert not any("created empty" in note for note in _notes(plan))


def test_sync_existing_trended_point_keeps_trend_settings_with_a_note():
    existing = _existing("sync", _av(1, "SP", trended=True, trend_interval_seconds=30))
    plan = _plan_into([ede_row(type=2, instance=1, name="SP"), _log_row("SP.Log", instance=40)], existing)
    assert "trended" not in plan.objects[0] and "trend_log_instance" not in plan.objects[0]
    assert "already trended; its trend settings were kept" in _notes(plan, "SP")
    assert plan.skipped == []


def test_sync_created_multistate_gets_default_state_text_existing_does_not():
    existing = _existing(
        "sync", {"object_type": "multi-state-value", "instance_number": 1, "name": "Mode", "state_text": ["A", "B"]}
    )
    plan = _plan_into([ede_row(type=19, instance=1, name="Mode"), ede_row(type=19, instance=2, name="New")], existing)
    by_name = _by_name(plan)
    assert "state_text" not in by_name["Mode"]
    assert by_name["New"]["state_text"] == ["State 1", "State 2"]
    assert not _notes(plan, "Mode")


def test_sync_row_checked_on_the_overlay_not_the_defaults():
    existing = _existing(
        "sync",
        {"object_type": "multi-state-value", "instance_number": 1, "name": "Mode", "state_text": ["A", "B", "C"]},
    )
    plan = _plan_into([ede_row(type=19, instance=1, name="Mode", default="3")], existing)
    assert plan.objects[0]["relinquish_default"] == "3" and plan.skipped == []


def test_sync_existing_object_a_rule_refuses_is_kept_unchanged_and_listed():
    existing = _existing("sync", _av(1, "SP"))
    plan = _plan_into([ede_row(type=2, instance=1, name="NetworkPort-1")], existing)
    assert plan.objects == [{"object_type": "analog-value", "instance_number": 1, "name": "SP"}]
    assert plan.skipped[0]["code"] == "refused"


# --- objects the device can't take ---------------------------------------------------------


def _msv_refused_on_the_device():
    # The saved default (4) is not one of the 3 texts the file gives, so the file's row is refused.
    return {
        "object_type": "multi-state-value", "instance_number": 1, "name": "Mode",
        "state_text": ["a", "b", "c", "d"], "relinquish_default": "4",
    }


def test_sync_name_of_an_existing_object_a_rule_refused_is_taken_by_a_later_row():
    existing = _existing("sync", _msv_refused_on_the_device())
    plan = _plan_into(
        [ede_row(type=19, instance=1, name="Mode", state_text="[x|y|z]"), ede_row(type=2, instance=9, name="Mode")],
        existing,
    )
    assert sorted(row["name"] for row in plan.objects) == ["Mode", "Mode (2)"]
    assert plan.skipped[0]["code"] == "refused"


def test_sync_name_of_an_existing_object_a_rule_refused_is_taken_from_an_earlier_row():
    existing = _existing("sync", _msv_refused_on_the_device())
    plan = _plan_into(
        [ede_row(type=2, instance=9, name="Mode"), ede_row(type=19, instance=1, name="Mode", state_text="[x|y|z]")],
        existing,
    )
    assert sorted(row["name"] for row in plan.objects) == ["Mode", "Mode (2)"]
    assert any("Mode (2)" in note for note in _notes(plan, "Mode (2)"))


def test_sync_existing_multistate_with_a_state_text_reference_not_found_is_noted():
    existing = _existing(
        "sync", {"object_type": "multi-state-value", "instance_number": 1, "name": "Mode", "state_text": ["A", "B"]}
    )
    plan = _plan_into([ede_row(type=19, instance=1, name="Mode", state_ref="7")], existing)
    assert any("state texts '7' not found" in note and "kept as they are" in note for note in _notes(plan, "Mode"))


def test_sync_two_logs_on_an_already_trended_point_note_once_and_list_the_second():
    existing = _existing("sync", _av(1, "Fan", trended=True))
    plan = _plan_into(
        [ede_row(type=2, instance=1, name="Fan"), _log_row("Fan.L1", instance=1), _log_row("Fan.L2", instance=2)],
        existing,
    )
    assert _notes(plan, "Fan").count("already trended; its trend settings were kept") == 1
    assert _skipped(plan, "Fan.L2")["code"] == "standin-duplicate"


def test_two_logs_with_one_instance_each_use_their_own_row():
    plan = _plan(
        [
            ede_row(instance=1, name="A"),
            ede_row(instance=2, name="B"),
            _log_row("A.Log", instance=7),
            _log_row("B.Log", instance=7),
        ]
    )
    assert _row(plan, "A")["trended"] is True and _row(plan, "B")["trended"] is False
    reason = _skipped(plan, "B.Log")["reason"]
    assert "instance 7" in reason and "'A'" in reason


def test_unsupported_types_are_named_when_the_standard_names_them():
    plan = _plan([ede_row(type=9, instance=1, name="Enroll"), ede_row(type=600, instance=1, name="Prop")])
    assert plan.skipped_types == {"event-enrollment (9)": 1, "600": 1}
    assert _skipped(plan, "Enroll")["reason"] == "event-enrollment (9) objects are not supported"
    assert _skipped(plan, "Prop")["object_type"] == "600"


def test_notes_do_not_say_cloned():
    existing = _existing(
        "sync",
        _av(1, "SP", alarming_enabled=True, notification_class_instance=4),
        {"object_type": "schedule", "instance_number": 1, "name": "Sch", "schedule_value_type": "analog", "schedule_default": "1.0", "schedule_write_targets": [
            {"object_type": "analog-value", "instance_number": 9, "property_identifier": "presentValue"}]},
    )
    plan = _plan_into(
        [
            ede_row(type=2, instance=1, name="SP"),
            ede_row(type=17, instance=1, name="Sch"),
            ede_row(type=10, instance=1, name="File"),
            ede_row(type=2, instance=3, name="Bad", default="abc"),
            ede_row(type=19, instance=2, name="Mode", state_text="[A|B]", default="7"),
        ],
        existing,
    )
    notes = _notes(plan)
    assert len(notes) >= 5 and not any("cloned" in note for note in notes), notes
    assert any("not imported: BACpack builds its own file object" in note for note in notes)


def test_merge_name_clash_with_an_existing_object_says_so():
    plan = _plan_into([ede_row(type=0, instance=1, name="Zone")], _existing("merge", _av(5, "Zone")))
    assert any("the device already has an object named 'Zone'" in note for note in _notes(plan, "Zone (2)"))


def test_a_refused_existing_object_is_not_noted_as_renamed():
    existing = _existing("sync", _msv_refused_on_the_device())
    plan = _plan_into(
        [ede_row(type=2, instance=9, name="Mode"), ede_row(type=19, instance=1, name="Mode", state_text="[x|y|z]")],
        existing,
    )
    renamed = [n for n in plan.notes if "named" in n["note"]]
    assert [(n["object_type"], n["name"]) for n in renamed] == [("analog-value", "Mode (2)")]


def test_sync_name_clash_with_only_a_trend_logs_name_says_whose_log():
    existing = _existing("sync", _av(1, "Fan", trended=True))
    plan = _plan_into(
        [ede_row(type=2, instance=1, name="Fan"), ede_row(type=2, instance=2, name="Fan-Trend")], existing
    )
    [note] = _notes(plan, "Fan-Trend (2)")
    assert note == "named 'Fan-Trend (2)'; the trend log of 'Fan' is named 'Fan-Trend'"


def test_sync_a_rows_own_saved_name_is_not_another_object_on_the_device():
    existing = _existing("sync", _av(1, "Zone"))
    plan = _plan_into(
        [ede_row(type=2, instance=9, name="Zone"), ede_row(type=2, instance=1, name="Zone")], existing
    )
    assert _notes(plan, "Zone (2)") == ["named 'Zone (2)'; the file has another object named 'Zone'"]


def test_a_kept_name_equal_to_a_guessed_logs_name_drops_the_log_not_the_points_name():
    # A kept object's name is only learned once the logs are applied (an overlay the
    # device check pops), so the late step is driven directly.
    from bacpack.ede_plan import EdePlanner, to_raw

    rows = [ede_row(type=2, instance=2, name="Pump"), _log_row("Pump-Trend", instance=5)]
    mine = _file(rows).rows
    planner = EdePlanner(to_raw(mine, device_instance=1000), 0, "Plant", ede_rows=mine, existing=_existing("sync"))
    planner.plan()
    pump = planner.rows[("analog-value", 2)]
    assert pump["trended"] is True and pump["trend_log_name"] == "Pump-Trend"
    planner._keep_name(("multi-state-value", 1), "Pump-Trend")
    assert pump["name"] == "Pump" and pump["trended"] is False and pump["trend_log_name"] is None
    assert pump["trend_log_instance"] is None
    [log] = [s for s in planner.skipped if s["name"] == "Pump-Trend"]
    assert log["code"] == "refused" and "stays on the device" in log["reason"]
    assert planner.partials.get(("analog-value", 2), {}).get("trended") is None
