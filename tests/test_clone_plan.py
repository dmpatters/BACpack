"""clone_plan: a raw controller read turned into a device and object rows.
Fixtures use the shapes tests/test_clone_reader_network.py pins from a real
read."""
from __future__ import annotations

import pytest

from bacpack import object_config
from bacpack.clone_plan import build_plan
from bacpack.config_schemas import ObjectCreate
from bacpack.models import MAX_TREND_SAMPLE_CAP


def _obj(object_type, instance, name=None, *, missing=(), error=None, failed=None, **props):
    props = {key.replace("_", "-"): value for key, value in props.items()}
    if name is not None:
        props["object-name"] = name
    entry = {"object_type": object_type, "instance": instance, "props": props, "missing": list(missing), "error": error}
    if failed:
        entry["failed"] = failed
    return entry


def _raw(*objects, name="Boiler Room", instance=4001, **device):
    return {
        "device": {
            "instance": instance, "name": name, "description": "Plant", "location": "Basement",
            "vendor_name": "Acme", "vendor_identifier": 42, "object_list_count": len(objects) + 1, **device,
        },
        "objects": list(objects),
        "read_at": "2026-11-01T12:00:00+00:00",
    }


def _plan(raw, device_id=599050, device_name="Boiler Room"):
    plan = build_plan(raw, device_id=device_id, device_name=device_name)
    # Whatever the planner returns must be savable: every row passes the rule
    # every save path runs, the device passes its check, and the rows have
    # the shape of the object API's body.
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


def _skipped_names(plan):
    return {s["name"]: s for s in plan.skipped}


def _time_values(*pairs):
    return [{"time": time, "value": value} for time, value in pairs]


def _day(*pairs):
    return {"daySchedule": _time_values(*pairs)}


# --- Device and the simple points ---------------------------------------------


def test_the_device_comes_from_the_read_with_the_chosen_id_and_name():
    plan = _plan(_raw(_obj("analog-input", 1, "AI")), device_id=777, device_name="Clone")
    assert plan.device == {
        "id": 777, "name": "Clone", "description": "Plant", "location": "Basement",
        "vendor_name": "Acme", "vendor_identifier": 42,
    }


def test_maps_an_analog_input_with_units_and_start_value():
    plan = _plan(_raw(_obj(
        "analog-input", 7, "Zone Temp", description="North zone", units="degrees-fahrenheit", present_value=71.5,
    )))
    row = _row(plan, "Zone Temp")
    assert (row["object_type"], row["instance_number"]) == ("analog-input", 7)
    assert (row["description"], row["units"], row["start_value"]) == ("North zone", "degreesFahrenheit", "71.5")
    assert plan.counts == {"analog-input": 1}
    assert plan.skipped == [] and plan.notes == []


def test_a_property_the_controller_lacks_leaves_the_field_unset_not_skipped():
    plan = _plan(_raw(_obj("analog-input", 1, "AI", missing=["description", "units", "present-value"])))
    row = _row(plan, "AI")
    assert row["description"] is None and row["units"] is None and row["start_value"] is None
    assert plan.skipped == []


def test_a_blank_description_is_stored_as_none():
    assert _row(_plan(_raw(_obj("analog-input", 1, "AI", description=""))), "AI")["description"] is None


def test_binary_text_and_start_value():
    plan = _plan(_raw(
        _obj("binary-input", 1, "Fan Status", active_text="Running", inactive_text="Stopped", present_value="active"),
        _obj("binary-value", 2, "Occupied", active_text="Yes", inactive_text="No", relinquish_default="inactive",
             commandable=True),
    ))
    status = _row(plan, "Fan Status")
    assert (status["active_text"], status["inactive_text"], status["start_value"]) == ("Running", "Stopped", "active")
    occupied = _row(plan, "Occupied")
    assert (occupied["active_text"], occupied["relinquish_default"], occupied["start_value"]) == ("Yes", "inactive", None)


def test_binary_text_that_is_blank_or_too_long_is_fixed_with_a_note():
    plan = _plan(_raw(_obj("binary-input", 1, "BI", active_text="  ", inactive_text="x" * 80)))
    row = _row(plan, "BI")
    assert row["active_text"] is None and row["inactive_text"] == "x" * 64
    assert len(_notes(plan, "BI")) == 1


def test_multistate_state_text_and_values():
    plan = _plan(_raw(
        _obj("multi-state-value", 3, "Mode", state_text=["Off", "Heat", "Cool"], relinquish_default=2, commandable=True),
        _obj("multi-state-input", 4, "Stage", state_text=["Idle", "One", "Two"], present_value=3),
    ))
    mode, stage = _row(plan, "Mode"), _row(plan, "Stage")
    assert mode["state_text"] == ["Off", "Heat", "Cool"] and mode["relinquish_default"] == "2"
    assert stage["start_value"] == "3"


def test_state_text_is_generated_from_the_state_count_when_the_controller_has_none():
    plan = _plan(_raw(_obj("multi-state-input", 4, "Stage", number_of_states=3, present_value=2)))
    assert _row(plan, "Stage")["state_text"] == ["State 1", "State 2", "State 3"]
    assert _notes(plan, "Stage")


def test_a_multistate_object_with_no_states_at_all_is_refused():
    plan = _plan(_raw(_obj("multi-state-input", 4, "Stage")))
    assert plan.objects == [] and _skipped_names(plan)["Stage"]["code"] == "refused"


def test_a_value_outside_the_states_is_dropped_with_a_note_not_the_object():
    plan = _plan(_raw(_obj(
        "multi-state-input", 4, "Stage", state_text=["A", "B"], present_value=5,
    )))
    assert _row(plan, "Stage")["start_value"] is None
    assert any("start value" in note for note in _notes(plan, "Stage"))


def test_output_copies_only_relinquish_default():
    plan = _plan(_raw(_obj(
        "analog-output", 1, "Valve", units="percent", present_value=55.0, relinquish_default=10.0, commandable=True,
    )))
    row = _row(plan, "Valve")
    assert row["relinquish_default"] == "10.0" and row["start_value"] is None
    assert row["bacnet_writable"] is True


def test_noncommandable_value_is_cloned_not_writable_with_a_note():
    plan = _plan(_raw(_obj("analog-value", 2, "Readout", units="percent", commandable=False)))
    row = _row(plan, "Readout")
    assert row["bacnet_writable"] is False
    assert _notes(plan, "Readout") == [
        "not commandable on the controller; the clone still shows a Priority_Array"
    ]


def test_an_input_is_never_marked_not_writable():
    assert _row(_plan(_raw(_obj("analog-input", 1, "AI"))), "AI")["bacnet_writable"] is True


# --- Skips ---------------------------------------------------------------------


def test_unsupported_type_is_skipped_and_counted_in_skipped_types():
    plan = _plan(_raw(
        _obj("accumulator", 1, "kWh 1"), _obj("accumulator", 2, "kWh 2"), _obj("loop", 1, "PID"),
        _obj("trend-log-multiple", 1, "Multi"), _obj("event-log", 1, "Events"), _obj("analog-input", 1, "AI"),
    ))
    assert plan.skipped_types == {"accumulator": 2, "loop": 1, "trend-log-multiple": 1, "event-log": 1}
    assert {s["code"] for s in plan.skipped} == {"unsupported-type"}
    assert _skipped_names(plan)["kWh 1"]["object_type"] == "accumulator"
    assert plan.counts == {"analog-input": 1}


def test_a_proprietary_object_type_is_skipped_by_its_number():
    plan = _plan(_raw(_obj("128", 1, "Vendor thing")))
    assert plan.skipped_types == {"128": 1}


def test_the_network_port_and_config_file_are_noted_not_requested():
    plan = _plan(_raw(_obj("network-port", 1, "NetworkPort-1"), _obj("file", 1, "x-config.json")))
    assert plan.skipped_types == {} and plan.skipped == [] and plan.objects == []
    assert {n["object_type"] for n in plan.notes} == {"network-port", "file"}


def test_failed_object_is_skipped_with_its_reason():
    plan = _plan(_raw(
        _obj("analog-input", 1, error="analog-input:1 object-name: property: unknown-property"),
        _obj("analog-input", 2, "Fine"),
    ))
    [skipped] = plan.skipped
    assert (skipped["code"], skipped["object_type"], skipped["instance"]) == ("read-failed", "analog-input", 1)
    assert "unknown-property" in skipped["reason"]
    assert [row["name"] for row in plan.objects] == ["Fine"]


def test_a_unit_that_cannot_be_mapped_refuses_the_object_with_the_rules_message():
    plan = _plan(_raw(_obj("analog-input", 1, "AI", units="not-a-unit"), _obj("analog-input", 2, "Fine")))
    skipped = _skipped_names(plan)["AI"]
    assert skipped["code"] == "refused" and "engineering unit" in skipped["reason"]
    assert [row["name"] for row in plan.objects] == ["Fine"]


# --- Names ---------------------------------------------------------------------


def test_name_is_trimmed_and_noted():
    plan = _plan(_raw(_obj("analog-input", 1, "  Zone Temp ")))
    assert _row(plan, "Zone Temp")
    assert any("trimmed" in note for note in _notes(plan, "Zone Temp"))


def test_reserved_name_is_refused_and_listed():
    plan = _plan(_raw(_obj("analog-input", 1, "NetworkPort-1"), _obj("analog-input", 2, "Fine")))
    skipped = _skipped_names(plan)["NetworkPort-1"]
    assert skipped["code"] == "refused" and "network port" in skipped["reason"]
    assert [row["name"] for row in plan.objects] == ["Fine"]


def test_device_name_clash_with_an_object_name_is_refused():
    plan = _plan(_raw(_obj("analog-input", 1, "Clone"), _obj("analog-input", 2, "Fine")), device_name="Clone")
    skipped = _skipped_names(plan)["Clone"]
    assert skipped["code"] == "refused" and "device's own name" in skipped["reason"]
    assert [row["name"] for row in plan.objects] == ["Fine"]


def test_a_duplicate_name_after_trimming_keeps_the_first():
    plan = _plan(_raw(_obj("analog-input", 1, "Zone"), _obj("analog-input", 2, "Zone ")))
    assert [(r["name"], r["instance_number"]) for r in plan.objects] == [("Zone", 1)]
    assert "already exists" in _skipped_names(plan)["Zone"]["reason"]


def test_a_blank_name_is_refused():
    plan = _plan(_raw(_obj("analog-input", 1, "   ")))
    assert plan.objects == [] and plan.skipped[0]["code"] == "refused"


def test_a_reserved_device_name_is_reported_as_the_plans_problem():
    plan = build_plan(_raw(_obj("analog-input", 1, "AI")), device_id=1, device_name="NetworkPort-1")
    assert plan.problem is not None and "network port" in plan.problem
    assert build_plan(_raw(_obj("analog-input", 1, "AI")), device_id=1, device_name="Fine").problem is None


# --- Schedules and calendars -----------------------------------------------------


def _schedule(name="Occupancy", **props):
    base = dict(
        weekly_schedule=[_day(("08:00:00.00", 50.0), ("17:00:00.00", None))] * 7,
        exception_schedule=[], effective_period={"startDate": "*-*-* *", "endDate": "*-*-* *"},
        schedule_default=10.0, priority_for_writing=12, list_of_object_property_references=[],
    )
    base.update(props)
    return _obj("schedule", 5, name, **base)


def test_schedule_weekly_and_exception_are_converted():
    plan = _plan(_raw(
        _obj("calendar", 6, "Holidays", date_list=["2026-12-25 *"]),
        _schedule(
            exception_schedule=[
                {"period": "calendar:6", "listOfTimeValues": _time_values(("00:00:00.00", 0.0)), "eventPriority": 5},
                {"period": {"startDate": "2026-6-1 *", "endDate": "2026-6-30 *"},
                 "listOfTimeValues": _time_values(("09:30:00.00", 25.0)), "eventPriority": 6},
                {"period": "WeekNDay(255, 6, 5)", "listOfTimeValues": _time_values(("09:00:00.00", 30.0)),
                 "eventPriority": 7},
                {"period": "2026-12-24 *", "listOfTimeValues": [], "eventPriority": 8},
            ],
            effective_period={"startDate": "2026-1-1 *", "endDate": "2026-12-31 *"},
        ),
    ))
    row = _row(plan, "Occupancy")
    assert row["schedule_value_type"] == "analog" and row["schedule_default"] == "10.0"
    assert row["schedule_priority"] == 12
    assert row["weekly_schedule"][0] == [{"time": "08:00", "value": "50.0"}, {"time": "17:00", "value": None}]
    assert len(row["weekly_schedule"]) == 7
    assert (row["schedule_effective_start"], row["schedule_effective_end"]) == ("2026-01-01", "2026-12-31")
    periods = [(e["period"], e["priority"]) for e in row["exception_schedule"]]
    assert periods == [
        ({"kind": "calendar-reference", "instance_number": 6}, 5),
        ({"kind": "date-range", "start": "2026-06-01", "end": "2026-06-30"}, 6),
        ({"kind": "week-and-day", "month": "*", "week_of_month": "last", "day_of_week": "5"}, 7),
        ({"kind": "date", "year": "2026", "month": "12", "day": "24", "day_of_week": "*"}, 8),
    ]
    assert row["exception_schedule"][1]["time_values"] == [{"time": "09:30", "value": "25.0"}]
    assert plan.notes == []


def test_an_open_effective_period_is_unset():
    row = _row(_plan(_raw(_schedule())), "Occupancy")
    assert row["schedule_effective_start"] is None and row["schedule_effective_end"] is None


@pytest.mark.parametrize(
    "default, value_type, stored",
    [(21.5, "analog", "21.5"), (3, "multi-state", "3"), ("1", "binary", "active"), ("0", "binary", "inactive"),
     (True, "binary", "active")],
)
def test_the_value_type_follows_the_schedule_default(default, value_type, stored):
    row = _row(_plan(_raw(_schedule(
        schedule_default=default, weekly_schedule=[_day(("08:00:00.00", default))] * 7,
    ))), "Occupancy")
    assert row["schedule_value_type"] == value_type and row["schedule_default"] == stored
    assert row["weekly_schedule"][0][0]["value"] == stored


def test_a_schedule_default_of_no_known_type_is_refused():
    plan = _plan(_raw(_schedule(schedule_default="text")))
    assert _skipped_names(plan)["Occupancy"]["code"] == "refused"


def _without_default(**kw):
    entry = _schedule(**kw)
    del entry["props"]["schedule-default"]
    return entry


def test_a_schedule_default_that_could_not_be_read_skips_the_schedule_naming_the_error():
    entry = _without_default()
    entry["failed"] = {"schedule-default": "busy"}
    plan = _plan(_raw(entry))
    assert plan.objects == []
    skipped = _skipped_names(plan)["Occupancy"]
    assert skipped["reason"] == "schedule default could not be read: busy"


def test_a_schedule_default_the_controller_does_not_have_is_cloned_as_null_with_a_note():
    entry = _without_default()
    entry["missing"] = ["schedule-default"]
    plan = _plan(_raw(entry))
    assert _row(plan, "Occupancy")["schedule_default"] is None
    assert any("schedule default" in n and "not have" in n for n in _notes(plan, "Occupancy"))


def test_a_null_schedule_default_is_cloned_as_null_without_a_note():
    plan = _plan(_raw(_schedule(schedule_default=None)))
    assert _row(plan, "Occupancy")["schedule_default"] is None
    assert not any("schedule default" in n for n in _notes(plan, "Occupancy"))


def test_times_are_cut_to_the_minute_and_duplicates_dropped_with_a_note():
    plan = _plan(_raw(_schedule(
        weekly_schedule=[_day(("08:00:30.00", 50.0), ("08:00:45.00", 60.0), ("17:00:00.00", None))] + [_day()] * 6,
    )))
    row = _row(plan, "Occupancy")
    assert row["weekly_schedule"][0] == [{"time": "08:00", "value": "50.0"}, {"time": "17:00", "value": None}]
    assert len(_notes(plan, "Occupancy")) >= 1


def test_write_target_inside_the_controller_is_kept():
    plan = _plan(_raw(
        _obj("analog-value", 2, "Setpoint", units="percent", commandable=True),
        _schedule(list_of_object_property_references=[{
            "objectIdentifier": "analog-value:2", "propertyIdentifier": "present-value",
            "propertyArrayIndex": None, "deviceIdentifier": "device:4001",
        }]),
    ))
    assert _row(plan, "Occupancy")["schedule_write_targets"] == [
        {"object_type": "analog-value", "instance_number": 2, "property_identifier": "presentValue"}
    ]
    assert plan.notes == []


def test_write_target_on_another_device_or_skipped_object_is_dropped_and_noted():
    plan = _plan(_raw(
        _obj("accumulator", 9, "kWh"),
        _schedule(list_of_object_property_references=[
            {"objectIdentifier": "analog-value:2", "propertyIdentifier": "present-value",
             "propertyArrayIndex": None, "deviceIdentifier": "device:77"},
            {"objectIdentifier": "accumulator:9", "propertyIdentifier": "present-value",
             "propertyArrayIndex": None, "deviceIdentifier": None},
            {"objectIdentifier": "analog-value:3", "propertyIdentifier": "present-value",
             "propertyArrayIndex": None, "deviceIdentifier": None},
        ]),
    ))
    assert _row(plan, "Occupancy")["schedule_write_targets"] in (None, [])
    notes = _notes(plan, "Occupancy")
    assert len(notes) == 3
    assert any("another device" in note for note in notes)


def test_week_n_day_entry_it_cannot_represent_is_dropped_and_noted():
    plan = _plan(_raw(
        _obj("calendar", 6, "Holidays", date_list=["2026-12-25 *", "WeekNDay(13, 255, 255)", "2026-13-1 *"]),
    ))
    assert _row(plan, "Holidays")["date_list"] == [
        {"kind": "date", "year": "2026", "month": "12", "day": "25", "day_of_week": "*"}
    ]
    assert len(_notes(plan, "Holidays")) == 2


def test_a_calendar_with_every_kind_of_entry():
    plan = _plan(_raw(_obj("calendar", 6, "Holidays", date_list=[
        "*-12-25 *", {"startDate": "2026-6-1 *", "endDate": "2026-6-30 *"}, "WeekNDay(255, 255, 7)",
    ])))
    assert _row(plan, "Holidays")["date_list"] == [
        {"kind": "date", "year": "*", "month": "12", "day": "25", "day_of_week": "*"},
        {"kind": "date-range", "start": "2026-06-01", "end": "2026-06-30"},
        {"kind": "week-and-day", "month": "*", "week_of_month": "*", "day_of_week": "7"},
    ]


def test_a_calendar_with_nothing_representable_is_refused():
    plan = _plan(_raw(_obj("calendar", 6, "Holidays", date_list=["WeekNDay(13, 255, 255)"])))
    skipped = _skipped_names(plan)["Holidays"]
    assert skipped["code"] == "refused" and skipped["reason"] == "none of its dates could be cloned"


def test_a_calendar_with_no_dates_on_the_controller_is_cloned_with_none():
    plan = _plan(_raw(_obj("calendar", 6, "Holidays", date_list=[])))
    assert not _row(plan, "Holidays")["date_list"]
    assert "Holidays" not in _skipped_names(plan)


def test_exception_referencing_a_skipped_calendar_is_dropped():
    plan = _plan(_raw(
        _obj("calendar", 6, "Holidays", error="read failed"),
        _schedule(exception_schedule=[
            {"period": "calendar:6", "listOfTimeValues": [], "eventPriority": 5},
            {"period": "2026-12-24 *", "listOfTimeValues": [], "eventPriority": 6},
        ]),
    ))
    row = _row(plan, "Occupancy")
    assert [e["priority"] for e in row["exception_schedule"]] == [6]
    assert any("calendar" in note for note in _notes(plan, "Occupancy"))


# --- Alarms and notification classes ---------------------------------------------


def _recipient(recipient, **extra):
    return {
        "validDays": [1] * 7, "fromTime": "00:00:00.00", "toTime": "23:59:59.00", "recipient": recipient,
        "processIdentifier": 7, "issueConfirmedNotifications": True, "transitions": [1, 0, 1], **extra,
    }


def test_notification_class_copies_recipients_as_they_are():
    plan = _plan(_raw(_obj(
        "notification-class", 7, "Alarms", notification_class=7, priority=[10, 20, 30], ack_required=[1, 0, 1],
        recipient_list=[
            _recipient("device:99"),
            _recipient({"networkNumber": 0, "macAddress": "c0a8014dbac0"}, validDays=[1] * 5 + [0] * 2,
                       issueConfirmedNotifications=False, fromTime="06:00:00.00", toTime="18:00:00.00"),
        ],
    )))
    row = _row(plan, "Alarms")
    assert row["notification_priority"] == [10, 20, 30] and row["notification_ack_required"] == [True, False, True]
    assert row["notification_recipient_list"] == [
        {"recipient": {"kind": "device", "instance_number": 99}, "valid_days": [True] * 7,
         "from_time": "00:00:00.00", "to_time": "23:59:59.00", "transitions": [True, False, True],
         "confirmed": True, "process_identifier": 7},
        {"recipient": {"kind": "address", "address": "192.168.1.77"}, "valid_days": [True] * 5 + [False] * 2,
         "from_time": "06:00:00.00", "to_time": "18:00:00.00", "transitions": [True, False, True],
         "confirmed": False, "process_identifier": 7},
    ]


def test_a_recipient_it_cannot_store_is_dropped_with_a_note():
    plan = _plan(_raw(_obj(
        "notification-class", 7, "Alarms", notification_class=7, priority=[1, 2, 3], ack_required=[1, 1, 1],
        recipient_list=[_recipient("device:99"), _recipient("device:123456789"),
                        _recipient("device:5", fromTime="*:*:*.*")],
    )))
    assert len(_row(plan, "Alarms")["notification_recipient_list"]) == 1
    assert len(_notes(plan, "Alarms")) == 2


def _alarm_ai(**extra):
    return _obj(
        "analog-input", 1, "Supply Temp", units="degrees-fahrenheit", present_value=70.0, notification_class=7,
        time_delay=5, event_enable=[1, 1, 0], notify_type="event", event_detection_enable=True,
        high_limit=90.0, low_limit=40.0, deadband=2.0, limit_enable=[1, 0], **extra,
    )


def _nc():
    return _obj("notification-class", 7, "Alarms", notification_class=7, priority=[1, 2, 3], ack_required=[1, 1, 1],
                recipient_list=[])


def test_alarming_is_enabled_from_intrinsic_properties():
    row = _row(_plan(_raw(_nc(), _alarm_ai())), "Supply Temp")
    assert row["alarming_enabled"] is True and row["notification_class_instance"] == 7
    assert (row["alarm_high_limit"], row["alarm_low_limit"], row["alarm_deadband"]) == (90.0, 40.0, 2.0)
    assert row["alarm_limit_enable"] == [True, False]
    assert row["alarm_event_enable"] == [True, True, False] and row["alarm_notify_type"] == "event"
    assert row["alarm_time_delay"] == 5 and row["alarm_time_delay_normal"] is None


def test_binary_and_multistate_alarm_values():
    plan = _plan(_raw(
        _nc(),
        _obj("binary-input", 1, "Fault", notification_class=7, alarm_value="active", event_enable=[1, 1, 1],
             notify_type="alarm", time_delay=0),
        _obj("multi-state-input", 2, "Stage", state_text=["A", "B", "C"], notification_class=7, alarm_values=[2, 3],
             event_enable=[1, 1, 1], notify_type="alarm", time_delay=0, time_delay_normal=4),
    ))
    assert _row(plan, "Fault")["alarm_value"] is True
    stage = _row(plan, "Stage")
    assert stage["alarm_values"] == [2, 3] and stage["alarm_time_delay_normal"] == 4


def test_an_object_without_intrinsic_reporting_is_not_alarming():
    row = _row(_plan(_raw(_nc(), _obj("analog-input", 1, "AI", missing=["notification-class"]))), "AI")
    assert row["alarming_enabled"] is False and row["notification_class_instance"] is None


def test_alarm_on_a_skipped_notification_class_is_switched_off_and_noted():
    plan = _plan(_raw(_obj("notification-class", 7, "Alarms", error="read failed"), _alarm_ai()))
    row = _row(plan, "Supply Temp")
    assert row["alarming_enabled"] is False and row["notification_class_instance"] is None
    assert any("notification class" in note for note in _notes(plan, "Supply Temp"))


def test_alarm_settings_the_rules_refuse_are_switched_off_not_the_object():
    # An analog object that reports intrinsically but whose limits did not read.
    plan = _plan(_raw(_nc(), _obj("analog-input", 1, "AI", notification_class=7, time_delay=0,
                                  missing=["high-limit", "low-limit"])))
    row = _row(plan, "AI")
    assert row["alarming_enabled"] is False
    assert any("alarm" in note for note in _notes(plan, "AI"))


def test_low_limit_above_high_limit_is_accepted():
    plan = _plan(_raw(_nc(), _alarm_ai(), ))
    row = _row(plan, "Supply Temp")
    plan2 = _plan(_raw(_nc(), _obj(
        "analog-input", 1, "Odd", notification_class=7, time_delay=0, high_limit=10.0, low_limit=50.0, deadband=0.0,
        limit_enable=[1, 1], event_enable=[1, 1, 1], notify_type="alarm",
    )))
    assert _row(plan2, "Odd")["alarming_enabled"] is True and row["alarming_enabled"] is True


# --- Trends ----------------------------------------------------------------------


def _log(instance, name, target="analog-input:1", *, logging_type="polled", interval=30000, buffer_size=500,
         trigger=False, device=None, prop="present-value"):
    return _obj(
        "trend-log", instance, name, logging_type=logging_type, log_interval=interval, buffer_size=buffer_size,
        trigger=trigger, log_device_object_property={
            "objectIdentifier": target, "propertyIdentifier": prop, "propertyArrayIndex": None,
            "deviceIdentifier": device,
        },
    )


def test_cov_and_interval_logs_mark_the_point_trended_with_the_real_instance_and_name():
    plan = _plan(_raw(
        _obj("analog-input", 1, "AI-1"), _obj("analog-input", 2, "AI-2"),
        _log(11, "AI-1 History", "analog-input:1", device="device:4001"),
        _log(12, "AI-2 COV Log", "analog-input:2", logging_type="cov", interval=0),
    ))
    one, two = _row(plan, "AI-1"), _row(plan, "AI-2")
    assert (one["trended"], one["trend_mode"], one["trend_interval_seconds"]) == (True, "interval", 300)
    assert (one["trend_log_instance"], one["trend_log_name"]) == (11, "AI-1 History")
    assert (two["trended"], two["trend_mode"], two["trend_log_instance"]) == (True, "cov", 12)
    assert [row["name"] for row in plan.objects] == ["AI-1", "AI-2"]
    assert plan.counts == {"analog-input": 2}


def test_cap_comes_from_buffer_size_clamped():
    plan = _plan(_raw(
        _obj("analog-input", 1, "A"), _obj("analog-input", 2, "B"), _obj("analog-input", 3, "C"),
        _log(11, "A-Log", "analog-input:1", buffer_size=500),
        _log(12, "B-Log", "analog-input:2", buffer_size=100000),
        _log(13, "C-Log", "analog-input:3", buffer_size=0),
    ))
    assert _row(plan, "A")["trend_sample_cap"] == 500
    assert _row(plan, "B")["trend_sample_cap"] == MAX_TREND_SAMPLE_CAP
    assert _row(plan, "C")["trend_sample_cap"] is None


def test_a_log_interval_under_a_second_is_one_second():
    plan = _plan(_raw(_obj("analog-input", 1, "A"), _log(11, "A-Log", interval=40)))
    assert _row(plan, "A")["trend_interval_seconds"] == 1


def test_two_logs_on_one_point_first_wins_other_noted():
    plan = _plan(_raw(
        _obj("analog-input", 1, "AI"), _log(11, "First"), _log(12, "Second"),
    ))
    assert _row(plan, "AI")["trend_log_name"] == "First"
    assert any("Second" in note for note in _notes(plan))


def test_log_on_another_device_or_other_property_or_triggered_is_noted():
    plan = _plan(_raw(
        _obj("analog-input", 1, "AI-1"), _obj("analog-input", 2, "AI-2"), _obj("analog-input", 3, "AI-3"),
        _obj("accumulator", 4, "kWh"),
        _log(11, "Other Device", "analog-input:1", device="device:77"),
        _log(12, "Other Property", "analog-input:2", prop="units"),
        _log(13, "Triggered", "analog-input:3", logging_type="triggered", interval=0),
        _log(14, "Triggered Flag", "analog-input:3", trigger=True),
        _log(15, "Skipped Object", "accumulator:4"),
        _log(16, "Nowhere", "analog-input:99"),
    ))
    assert all(row["trended"] is False for row in plan.objects)
    assert len(_notes(plan)) == 6


def test_a_log_whose_name_clashes_with_an_object_refuses_the_log_not_the_point():
    plan = _plan(_raw(
        _obj("analog-input", 1, "AI-1"), _obj("analog-input", 2, "Taken"), _log(11, "Taken", "analog-input:1"),
    ))
    assert _row(plan, "AI-1")["trended"] is False and _row(plan, "AI-1")["trend_log_name"] is None
    assert any("Taken" in note for note in _notes(plan))
    assert plan.skipped == []


def test_a_log_named_like_the_new_device_refuses_the_log():
    plan = _plan(_raw(_obj("analog-input", 1, "AI-1"), _log(11, "Clone", "analog-input:1")), device_name="Clone")
    assert _row(plan, "AI-1")["trended"] is False


def test_a_log_name_blank_after_trimming_falls_back_to_the_default_name():
    plan = _plan(_raw(_obj("analog-input", 1, "AI-1"), _log(11, "  ", "analog-input:1")))
    row = _row(plan, "AI-1")
    assert row["trended"] is True and row["trend_log_name"] is None and row["trend_log_instance"] == 11


# --- Whole plan ------------------------------------------------------------------


def test_plan_is_rebuilt_with_another_id_and_name():
    raw = _raw(_obj("analog-input", 1, "AI"), _obj("analog-input", 2, "Clone"))
    first = build_plan(raw, device_id=1, device_name="Boiler Room")
    again = build_plan(raw, device_id=1, device_name="Boiler Room")
    other = build_plan(raw, device_id=2, device_name="Clone")
    assert first == again
    assert first.device["id"] == 1 and other.device["id"] == 2
    assert [r["name"] for r in first.objects] == ["AI", "Clone"]
    assert [r["name"] for r in other.objects] == ["AI"]
    assert [s["name"] for s in other.skipped] == ["Clone"]


def test_the_raw_read_is_not_changed_by_planning():
    import copy

    raw = _raw(_nc(), _alarm_ai(), _obj("analog-input", 2, " Spaced "), _log(11, "Log", "analog-input:2"))
    before = copy.deepcopy(raw)
    _plan(raw)
    assert raw == before


def test_a_whole_controller_plans_and_passes_the_device_check():
    plan = _plan(_raw(
        _nc(), _alarm_ai(),
        _obj("analog-value", 2, "Setpoint", units="percent", relinquish_default=20.0, commandable=True),
        _obj("binary-value", 3, "Occupied", relinquish_default="active", commandable=True),
        _obj("multi-state-value", 4, "Mode", state_text=["A", "B"], relinquish_default=1, commandable=True),
        _obj("calendar", 6, "Holidays", date_list=["2026-12-25 *"]),
        _schedule(),
        _log(21, "Supply History", "analog-input:1"),
        _obj("accumulator", 30, "kWh"),
    ))
    assert plan.counts == {
        "analog-input": 1, "analog-value": 1, "binary-value": 1, "multi-state-value": 1,
        "calendar": 1, "schedule": 1, "notification-class": 1,
    }
    assert plan.skipped_types == {"accumulator": 1}


# --- Size ------------------------------------------------------------------------


def test_planning_walks_the_rows_a_fixed_number_of_times_not_once_per_object():
    from bacpack import clone_plan

    class CountingRows(dict):
        walks = 0

        def values(self):
            CountingRows.walks += 1
            return super().values()

    count = 300
    raw = _raw(
        *[_obj("analog-input", n, f"AI-{n}") for n in range(1, count + 1)],
        *[_log(1000 + n, f"Log {n}", f"analog-input:{n}") for n in range(1, count + 1)],
    )
    planner = clone_plan.Planner(raw, 599050, "Boiler Room")
    planner.rows = CountingRows()
    plan = planner.plan()
    assert len(plan.objects) == count and all(row["trended"] for row in plan.objects)
    assert CountingRows.walks < 10


def test_planner_is_public_and_hooks_can_be_overridden():
    from bacpack import clone_plan

    class Hooked(clone_plan.Planner):
        def _fill_calendar(self, entry, row):
            super()._fill_calendar(entry, row)
            row["description"] = "set by the hook"

    raw = _raw(_obj("calendar", 1, "Holidays", date_list=["2026-12-25 *"]))
    assert clone_plan.Planner(raw, 599050, "Boiler Room").plan().objects[0]["description"] is None
    assert Hooked(raw, 599050, "Boiler Room").plan().objects[0]["description"] == "set by the hook"
    assert clone_plan.is_int(3) and not clone_plan.is_int(True)
    assert clone_plan.NOT_COMMANDABLE_NOTE == clone_plan.Planner.not_commandable_note


# --- Properties that failed to read ----------------------------------------------


def test_a_property_that_failed_for_another_reason_is_noted_with_its_code():
    plan = _plan(_raw(_obj("analog-input", 1, "AI", failed={"units": "operational-problem"})))
    assert "units could not be read: operational-problem" in _notes(plan, "AI")


def test_a_property_that_is_merely_missing_is_not_noted():
    plan = _plan(_raw(_obj("analog-input", 1, "AI", missing=["units"])))
    assert _notes(plan, "AI") == []


def test_a_failed_priority_array_read_keeps_the_point_writable_and_says_so():
    plan = _plan(_raw(_obj("analog-value", 1, "AV", failed={"priority-array": "busy"})))
    assert _row(plan, "AV")["bacnet_writable"] is True
    assert "priority-array could not be read: busy" in _notes(plan, "AV")


def test_a_trend_log_that_failed_to_read_a_property_is_noted():
    log = _obj("trend-log", 12, "Other Log", failed={"buffer-size": "busy"})
    plan = _plan(_raw(_obj("analog-input", 1, "AI"), log))
    assert any("buffer-size could not be read: busy" in note for note in _notes(plan))


# --- A schedule with a Null Schedule_Default -----------------------------------------


def test_a_null_default_is_cloned_as_a_null_default_with_the_type_from_a_weekly_value():
    row = _row(_plan(_raw(_schedule(schedule_default=None))), "Occupancy")
    assert row["schedule_default"] is None and row["schedule_value_type"] == "analog"


def test_a_null_default_takes_its_type_from_an_exception_value_when_the_week_has_none():
    exception = {"period": "2026-12-24 *", "listOfTimeValues": _time_values(("08:00:00.00", True)), "eventPriority": 8}
    row = _row(_plan(_raw(_schedule(
        schedule_default=None, weekly_schedule=[_day(("08:00:00.00", None))] * 7, exception_schedule=[exception],
    ))), "Occupancy")
    assert row["schedule_value_type"] == "binary" and row["schedule_default"] is None


@pytest.mark.parametrize(
    "target, value_type",
    [("analog-output:2", "analog"), ("binary-value:2", "binary"), ("multi-state-output:2", "multi-state")],
)
def test_a_null_default_with_no_values_takes_its_type_from_the_first_write_target(target, value_type):
    object_type = target.split(":")[0]
    plan = _plan(_raw(
        _obj(object_type, 2, "Target", commandable=True, **({"state_text": ["a", "b"]} if "multi" in object_type else {})),
        _schedule(
            schedule_default=None, weekly_schedule=[_day()] * 7,
            list_of_object_property_references=[
                {"objectIdentifier": target, "propertyIdentifier": "present-value", "deviceIdentifier": None},
            ],
        ),
    ))
    row = _row(plan, "Occupancy")
    assert row["schedule_value_type"] == value_type and row["schedule_default"] is None


def test_a_null_default_with_nothing_to_tell_its_type_is_refused_with_a_note():
    plan = _plan(_raw(_schedule(schedule_default=None, weekly_schedule=[_day()] * 7)))
    assert _skipped_names(plan)["Occupancy"]["code"] == "refused"
    assert any("value type" in note for note in _notes(plan, "Occupancy"))


# --- Alarm settings are blamed only when they are the problem ------------------------


def test_a_point_refused_for_another_reason_is_not_blamed_on_its_alarm_settings():
    plan = _plan(_raw(
        _obj("notification-class", 5, "NC"),
        _obj("multi-state-value", 3, "Mode", number_of_states=0, notification_class=5, alarm_values=[1]),
    ))
    assert "Mode" in _skipped_names(plan)
    assert not any("alarm settings not cloned" in note for note in _notes(plan, "Mode"))


def test_what_depends_on_an_object_the_whole_device_check_removes_is_stripped_not_refused(monkeypatch):
    real = object_config.check_device

    def refuse_the_class(device_name, rows):
        rows = list(rows)
        if any(row["name"] == "NC" for row in rows):
            return "object 'NC' cannot be built"
        return real(device_name, rows)

    monkeypatch.setattr(object_config, "check_device", refuse_the_class)
    plan = build_plan(
        _raw(
            _obj("notification-class", 5, "NC"),
            _obj("analog-input", 1, "AI", notification_class=5, time_delay=0, high_limit=10.0, low_limit=1.0,
                 deadband=0.0, limit_enable=[1, 1], event_enable=[1, 1, 1], notify_type="alarm"),
        ),
        device_id=599050, device_name="Boiler Room",
    )
    assert "NC" in _skipped_names(plan) and "AI" not in _skipped_names(plan)
    row = _row(plan, "AI")
    assert row["alarming_enabled"] is False and row["notification_class_instance"] is None
    assert any("notification class 5 was not cloned" in note for note in _notes(plan, "AI"))


def test_an_unsupported_type_counts_as_skipped_even_when_its_name_could_not_be_read():
    plan = _plan(_raw(_obj("accumulator", 3, error="property: unknown-property"), _obj("analog-input", 1, "AI")))
    assert plan.skipped_types == {"accumulator": 1}
    assert [s["code"] for s in plan.skipped] == ["unsupported-type"]
