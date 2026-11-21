"""object_config: the one answer to "can this object be built?".
The corpus proving "validates, so builds" is tests/test_object_config_corpus.py."""
from __future__ import annotations

import math

import pytest

from bacpack import object_config
from bacpack.models import BacnetObject, Device


def _av(**extra):
    return {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent", **extra}


def _recipient(**recipient):
    return {
        "recipient": recipient,
        "valid_days": [True] * 7,
        "from_time": "00:00:00",
        "to_time": "23:59:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 1,
    }


def _nc(instance=1, recipients=None):
    return {
        "object_type": "notification-class",
        "instance_number": instance,
        "name": f"NC-{instance}",
        "notification_recipient_list": recipients,
    }


def test_row_from_object_reads_column_defaults_for_an_unsaved_object():
    row = object_config.row_from_object(BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"))
    assert row["bacnet_writable"] is True
    assert row["schedule_priority"] == 14
    assert row["trend_notification_threshold"] == 10
    assert row["object_type"] == "analog-value"


def test_a_minimal_analog_value_passes():
    object_config.check_object(_av())


@pytest.mark.parametrize(
    "row, message",
    [
        (_av(object_type="bogus"), "is not a valid object_type"),
        (_av(name=""), "name is required"),
        (_av(name="   "), "name is required"),
        (_av(instance_number=4194303), "instance_number must be an integer 0-4194302"),
        (_av(instance_number=True), "instance_number must be an integer 0-4194302"),
        ({"object_type": "multi-state-value", "instance_number": 1, "name": "MSV-1"}, "requires state_text"),
        (_av(alarming_enabled=True, alarm_high_limit=80.0), "notification_class_instance is required"),
        (_av(trended=False, trend_reporting_enabled=True, trend_notification_class_instance=1), "requires trended"),
        (_av(trend_sample_cap=32768), "trend_sample_cap must be an integer 1-32767"),
        (_av(trend_sample_cap=0), "trend_sample_cap must be an integer 1-32767"),
        (
            _av(simulation={"kind": "follow", "source": "analog-value:1"}),
            "can't follow itself",
        ),
        (
            {
                "object_type": "multi-state-value",
                "instance_number": 1,
                "name": "MSV-1",
                "state_text": ["A", "B"],
                "simulation": {"kind": "cycle", "dwell_s": 5, "states": [1, 3]},
            },
            "above this object's 2 states",
        ),
        # Recipients the device can't build.
        (_nc(recipients=[_recipient(kind="address", address="not-an-address")]), "isn't a BACnet address"),
        (_nc(recipients=[_recipient(kind="device", instance_number=99999999)]), "0-4194302"),
        (_nc(recipients=[{**_recipient(kind="device", instance_number=5), "from_time": "nope"}]), "from_time"),
        # An unknown local write-target property.
        (
            {
                "object_type": "schedule",
                "instance_number": 1,
                "name": "SCH-1",
                "schedule_value_type": "analog",
                "schedule_default": "1.0",
                "schedule_write_targets": [
                    {"object_type": "analog-value", "instance_number": 1, "property_identifier": "fooBar"}
                ],
            },
            "fooBar",
        ),
    ],
)
def test_check_object_refuses(row, message):
    with pytest.raises(ValueError, match=message):
        object_config.check_object(row)


def test_check_object_refuses_a_non_finite_relinquish_default():
    with pytest.raises(ValueError):
        object_config.check_object(_av(relinquish_default=str(math.inf)))


def test_check_device_passes_a_consistent_device():
    rows = [_av(alarming_enabled=True, notification_class_instance=1, alarm_high_limit=80.0), _nc(1)]
    assert object_config.check_device("Dev", rows) is None


@pytest.mark.parametrize(
    "device_name, rows, message",
    [
        ("Dev", [_av(), _av(name="AV-2")], "analog-value:1 already exists on this device"),
        ("Dev", [_av(), _av(instance_number=2)], "an object named 'AV-1' already exists on this device"),
        # The network port's fixed name.
        ("Dev", [_av(name="NetworkPort-1")], "NetworkPort-1"),
        # A device named after a trended object's trend log.
        ("AV-1-Trend", [_av(trended=True)], "AV-1-Trend"),
        ("Dev", [_av(alarming_enabled=True, notification_class_instance=7, alarm_high_limit=80.0)], "does not exist"),
    ],
)
def test_check_device_refuses(device_name, rows, message):
    problem = object_config.check_device(device_name, rows)
    assert problem is not None and message in problem


def _save(session_factory, *objects):
    with session_factory() as session:
        session.add(Device(id=599001, name="Dev", vendor_identifier=999))
        for obj in objects:
            obj.device_id = 599001
            session.add(obj)
        session.commit()
        return [obj.id for obj in objects]


def test_check_saved_row_applies_changes_to_the_saved_row(session_factory):
    (msv_id,) = _save(
        session_factory,
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["A", "B", "C"],
            simulation={"kind": "cycle", "dwell_s": 5, "states": [1, 3]},
        ),
    )
    object_config.check_saved_row(session_factory, msv_id, {"state_text": ["A", "B", "C", "D"]})
    with pytest.raises(ValueError, match="above this object's 2 states"):
        object_config.check_saved_row(session_factory, msv_id, {"state_text": ["A", "B"]})


def test_check_saved_row_checks_the_device_too(session_factory):
    (av_id, _) = _save(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"),
        BacnetObject(object_type="analog-value", instance_number=2, name="AV-2"),
    )
    with pytest.raises(ValueError, match="already exists"):
        object_config.check_saved_row(session_factory, av_id, {"name": "AV-2"})


def test_check_saved_row_without_a_row_or_factory_is_a_no_op(session_factory):
    object_config.check_saved_row(None, 1, {"name": ""})
    object_config.check_saved_row(session_factory, None, {"name": ""})
    object_config.check_saved_row(session_factory, 12345, {"name": ""})


def test_check_saved_device_name(session_factory):
    _save(session_factory, BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"))
    object_config.check_saved_device_name(session_factory, 599001, "Other")
    with pytest.raises(ValueError, match="AV-1"):
        object_config.check_saved_device_name(session_factory, 599001, "AV-1")


# --- A config-saving BACnet write checks what it can break -------------------


def _nc_row(instance):
    return BacnetObject(object_type="notification-class", instance_number=instance, name=f"NC-{instance}")


_ALARM = {"alarming_enabled": True, "notification_class_instance": 7, "alarm_high_limit": 80.0}


def test_check_saved_row_accepts_a_reference_to_an_existing_object(session_factory):
    (av_id, _) = _save(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent"),
        _nc_row(7),
    )
    object_config.check_saved_row(session_factory, av_id, _ALARM)


def test_check_saved_row_refuses_a_dangling_reference(session_factory):
    (av_id, _) = _save(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent"),
        _nc_row(8),
    )
    with pytest.raises(ValueError, match=r"notification-class:7 as its notification class, which does not exist"):
        object_config.check_saved_row(session_factory, av_id, _ALARM)


def test_check_saved_row_does_not_load_every_sibling(session_factory, monkeypatch):
    (av_id, _) = _save(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent"),
        _nc_row(7),
    )
    calls = []
    real = object_config.row_from_object
    monkeypatch.setattr(object_config, "row_from_object", lambda obj: calls.append(obj.id) or real(obj))
    object_config.check_saved_row(session_factory, av_id, _ALARM)
    assert calls == [av_id]


def test_check_saved_row_with_a_name_change_runs_the_full_device_check(session_factory):
    (av_id, _) = _save(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"),
        BacnetObject(object_type="analog-value", instance_number=2, name="AV-2"),
    )
    object_config.check_saved_row(session_factory, av_id, {"name": "AV-3"})
    with pytest.raises(ValueError, match="already exists"):
        object_config.check_saved_row(session_factory, av_id, {"name": "AV-2"})


def test_dangling_references_of_matches_find_dangling_references():
    from bacpack.object_references import dangling_references_of, find_dangling_references

    row = _av(**_ALARM)
    assert dangling_references_of(row, {("analog-value", 1)}) == find_dangling_references([row])
    assert dangling_references_of(row, {("analog-value", 1), ("notification-class", 7)}) == []


# --- A database error in the pre-save check lets the write through -----------------------

import logging
import sqlite3

from sqlalchemy.exc import OperationalError


def _locked_factory():
    raise OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked"))


def test_check_saved_row_lets_the_write_through_when_the_database_fails(caplog):
    # {"name": ""} would be refused if the check got as far as the row.
    with caplog.at_level(logging.WARNING, logger="bacpack.object_config"):
        object_config.check_saved_row(_locked_factory, 1, {"name": ""})
    assert "applying it live" in caplog.text
    assert "database is locked" in caplog.text


def test_check_saved_device_name_lets_the_write_through_when_the_database_fails(caplog):
    with caplog.at_level(logging.WARNING, logger="bacpack.object_config"):
        object_config.check_saved_device_name(_locked_factory, 599001, "")
    assert "applying it live" in caplog.text


def test_a_programming_error_in_the_pre_save_check_is_not_swallowed():
    # Only database errors are logged and let
    # through; a programming error stays loud.
    from sqlalchemy.exc import InvalidRequestError

    def broken():
        raise InvalidRequestError("not a database problem")

    with pytest.raises(InvalidRequestError):
        object_config.check_saved_row(broken, 1, {"description": "x"})
    with pytest.raises(InvalidRequestError):
        object_config.check_saved_device_name(broken, 1, "New")


def test_the_largest_trend_sample_cap_passes():
    object_config.check_object(_av(trend_sample_cap=32767))


@pytest.mark.parametrize("name", [" AV-1", "AV-1 ", "AV-1\t"])
def test_a_name_with_leading_or_trailing_whitespace_is_refused(name):
    # Every save path trims names first; this catches one that doesn't.
    with pytest.raises(ValueError, match="space"):
        object_config.check_object(_av(name=name))


def _bv(**extra):
    return {"object_type": "binary-value", "instance_number": 1, "name": "BV-1", **extra}


@pytest.mark.parametrize("field", ["active_text", "inactive_text"])
@pytest.mark.parametrize("value", ["", "   ", " On", "On ", "x" * 65, 5])
def test_binary_text_must_be_trimmed_and_not_blank(field, value):
    with pytest.raises(ValueError, match=field):
        object_config.check_object(_bv(**{field: value}))


def test_binary_text_of_64_characters_passes():
    object_config.check_object(_bv(active_text="x" * 64, inactive_text="y"))


def test_binary_text_is_ignored_on_other_types():
    object_config.check_object(_av(active_text="", inactive_text=" x "))


@pytest.mark.parametrize(
    "row",
    [
        {"object_type": "analog-input", "start_value": "abc"},
        {"object_type": "analog-input", "start_value": "nan"},
        {"object_type": "binary-input", "start_value": "maybe"},
        {"object_type": "multi-state-input", "start_value": "0", "state_text": ["a", "b"]},
        {"object_type": "multi-state-input", "start_value": "3", "state_text": ["a", "b"]},
        {"object_type": "multi-state-input", "start_value": "x", "state_text": ["a", "b"]},
    ],
)
def test_start_value_is_checked_like_relinquish_default(row):
    with pytest.raises(ValueError, match="start-value"):
        object_config.check_object({"instance_number": 1, "name": "IN-1", **row})


def test_a_good_start_value_passes_and_is_ignored_on_other_types():
    object_config.check_object({"object_type": "analog-input", "instance_number": 1, "name": "I", "start_value": "-3.5"})
    object_config.check_object({"object_type": "binary-input", "instance_number": 1, "name": "I", "start_value": "active"})
    object_config.check_object(_av(start_value="abc"))


# --- Trend Log identity (a point's fixed Trend Log instance and name) -------


def _trended(name, instance_number, **extra):
    return _av(name=name, instance_number=instance_number, trended=True, **extra)


def test_fixed_trend_log_name_is_checked_not_the_default():
    a_row = _trended("A", 1, trend_log_name="B-Trend")
    problem = object_config.check_device("Dev", [a_row, _av(name="B-Trend", instance_number=2)])
    assert problem is not None and "B-Trend" in problem and "'A'" in problem
    # The default name no longer exists, so an object called "A-Trend" is fine.
    assert object_config.check_device("Dev", [a_row, _av(name="A-Trend", instance_number=2)]) is None


def test_two_trend_logs_cannot_share_a_name():
    rows = [_trended("A", 1, trend_log_name="Log"), _trended("B", 2, trend_log_name="Log")]
    problem = object_config.check_device("Dev", rows)
    assert problem is not None and "'A'" in problem and "'B'" in problem and "'Log'" in problem


def test_two_points_cannot_share_a_trend_log_instance():
    rows = [_trended("A", 1, trend_log_instance=7), _trended("B", 2, trend_log_instance=7)]
    problem = object_config.check_device("Dev", rows)
    assert problem is not None and "'A'" in problem and "'B'" in problem and "Trend Log instance" in problem


def test_explicit_instance_clashes_with_a_defaulted_rows_id():
    rows = [_trended("A", 1, id=1, trend_log_instance=2), _trended("B", 2, id=2)]
    problem = object_config.check_device("Dev", rows)
    assert problem is not None and "'A'" in problem and "'B'" in problem
    assert object_config.check_device("Dev", [rows[0], _trended("B", 2, id=3)]) is None


def test_a_row_without_an_id_contributes_only_an_explicit_instance():
    assert object_config.check_device("Dev", [_trended("A", 1, id=1), _trended("B", 2)]) is None


@pytest.mark.parametrize("name", ["Dev", "NetworkPort-1", "Dev-config.json"])
def test_trend_log_name_equal_to_a_reserved_name_is_refused(name):
    assert object_config.check_device("Dev", [_trended("A", 1, trend_log_name=name)]) is not None


@pytest.mark.parametrize("value", [-1, 4194303, True, "7"])
def test_trend_log_instance_out_of_range_is_refused(value):
    with pytest.raises(ValueError, match="trend_log_instance"):
        object_config.check_object(_trended("A", 1, trend_log_instance=value))


@pytest.mark.parametrize("value", ["", "  ", " Log", "Log ", 5])
def test_trend_log_name_must_be_trimmed_and_not_blank(value):
    with pytest.raises(ValueError, match="trend_log_name"):
        object_config.check_object(_trended("A", 1, trend_log_name=value))


def test_trend_identity_is_ignored_when_not_trended():
    object_config.check_object(_av(trend_log_instance=-5, trend_log_name=""))
    rows = [_av(name="A", instance_number=1, trend_log_instance=7), _av(name="B", instance_number=2, trend_log_instance=7)]
    assert object_config.check_device("Dev", rows) is None


# --- A Notification Class has no Present_Value to trend ----------------------


def _trendless_nc(**extra):
    return {"object_type": "notification-class", "instance_number": 1, "name": "NC-1", **extra}


def test_a_notification_class_cannot_be_trended():
    with pytest.raises(ValueError, match="Notification Class can't be trended"):
        object_config.check_object(_trendless_nc(trended=True))


def test_a_notification_class_that_is_not_trended_passes():
    object_config.check_object(_trendless_nc())
    object_config.check_object(_trendless_nc(trended=False))


def test_a_trended_notification_class_with_reporting_still_names_the_trended_rule():
    with pytest.raises(ValueError, match="Notification Class can't be trended"):
        object_config.check_object(_trendless_nc(trended=True, trend_reporting_enabled=True, trend_notification_class_instance=1))
