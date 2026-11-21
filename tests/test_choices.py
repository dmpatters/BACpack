"""choices.py: fixed-choice values are case-insensitive on input."""
from __future__ import annotations

import pytest
from bacpypes3.primitivedata import ObjectType, PropertyIdentifier

from bacpack.point_config import list_engineering_units
from bacpack.choices import (
    AUTH_TYPES,
    BINARY_WORDS,
    CALENDAR_ENTRY_KINDS,
    CONNECTOR_TYPES,
    NETWORK_ROLES,
    NOTIFY_TYPES,
    RESPONSE_FORMATS,
    SCHEDULE_VALUE_TYPES,
    TREND_MODES,
    canonical,
    canonicalize_object_fields,
    choice_key,
    enum_name,
)
from bacpack.models import OBJECT_TYPES


@pytest.mark.parametrize("text", ["analog-value", "Analog-Value", "ANALOG_VALUE", "analog value", "analogValue", " Analog-value "])
def test_canonical_matches_case_and_separator_variants(text):
    assert canonical(text, OBJECT_TYPES) == "analog-value"


def test_canonical_passes_an_unknown_value_through():
    assert canonical("not-a-type", OBJECT_TYPES) == "not-a-type"


def test_canonical_units_keep_camel_case():
    units = list_engineering_units()
    assert canonical("Degrees-Fahrenheit", units) == "degreesFahrenheit"
    assert canonical("PERCENT", units) == "percent"


@pytest.mark.parametrize(
    "choices",
    [OBJECT_TYPES, tuple(list_engineering_units()), TREND_MODES, NOTIFY_TYPES, SCHEDULE_VALUE_TYPES,
     CALENDAR_ENTRY_KINDS, BINARY_WORDS, NETWORK_ROLES, CONNECTOR_TYPES, RESPONSE_FORMATS, AUTH_TYPES],
)
def test_no_two_choices_collapse_to_the_same_key(choices):
    assert len({choice_key(c) for c in choices}) == len(set(choices))


def test_enum_name_kebab_and_camel():
    assert enum_name(ObjectType, "Analog-Value") == "analog-value"
    assert enum_name(PropertyIdentifier, "PRESENT_VALUE") == "present-value"
    assert enum_name(PropertyIdentifier, "present-value", camel=True) == "presentValue"
    assert enum_name(ObjectType, "bogus") is None


def test_enum_name_ignores_digits():
    # A proprietary type given as a number has no name.
    assert enum_name(ObjectType, "200") is None


def test_canonicalize_object_fields_whole_row():
    row = {
        "object_type": "Binary-Value",
        "name": "Fan Status",
        "description": "Mixed Case Text",
        "state_text": ["Off", "On"],
        "relinquish_default": "INACTIVE",
        "trend_mode": "COV",
        "trend_notify_type": "Event",
        "alarm_notify_type": "ALARM",
    }
    out = canonicalize_object_fields(row)
    assert out == {
        **row,
        "object_type": "binary-value",
        "relinquish_default": "inactive",
        "trend_mode": "cov",
        "trend_notify_type": "event",
        "alarm_notify_type": "alarm",
    }
    assert row["object_type"] == "Binary-Value"  # the input isn't mutated


def test_canonicalize_schedule_structures():
    row = {
        "object_type": "schedule",
        "schedule_value_type": "Binary",
        "schedule_default": "Active",
        "weekly_schedule": [[{"time": "08:00", "value": "ACTIVE"}], []],
        "exception_schedule": [
            {"period": {"kind": "Week-And-Day", "week_of_month": "LAST"}, "priority": 1,
             "time_values": [{"time": "00:00", "value": "Inactive"}]}
        ],
        "schedule_write_targets": [
            {"object_type": "Binary-Value", "instance_number": 1, "property_identifier": "Present_Value"},
            {"device_instance": 5, "object_type": "Binary-Output", "instance_number": 2, "property_identifier": "PresentValue"},
            {"device_instance": 5, "object_type": "200", "instance_number": 2, "property_identifier": "present-value"},
        ],
    }
    out = canonicalize_object_fields(row)
    assert out["schedule_value_type"] == "binary"
    assert out["schedule_default"] == "active"
    assert out["weekly_schedule"] == [[{"time": "08:00", "value": "active"}], []]
    assert out["exception_schedule"][0]["period"] == {"kind": "week-and-day", "week_of_month": "last"}
    assert out["exception_schedule"][0]["time_values"] == [{"time": "00:00", "value": "inactive"}]
    local, remote, proprietary = out["schedule_write_targets"]
    assert (local["object_type"], local["property_identifier"]) == ("binary-value", "presentValue")
    assert (remote["object_type"], remote["property_identifier"]) == ("binary-output", "present-value")
    assert proprietary["object_type"] == "200"


def test_canonicalize_date_list():
    out = canonicalize_object_fields({"date_list": [{"kind": "DATE", "year": "*"}, {"kind": "Date-Range"}]})
    assert out["date_list"] == [{"kind": "date", "year": "*"}, {"kind": "date-range"}]


def test_canonicalize_leaves_non_strings_alone():
    # Shape errors are left for the real validation.
    row = {"object_type": ["analog-value"], "units": 5, "weekly_schedule": "nonsense",
           "schedule_write_targets": [None, "x"], "relinquish_default": 1.5}
    assert canonicalize_object_fields(row) == row


def test_canonicalize_leaves_absent_keys_absent():
    assert canonicalize_object_fields({"name": "AV-1"}) == {"name": "AV-1"}



def test_simulation_kind_and_follow_source_are_canonical():
    from bacpack.choices import canonicalize_object_fields

    row = canonicalize_object_fields(
        {"simulation": {"kind": "Random-Walk", "start": 1}, "object_type": "analog-value"}
    )
    assert row["simulation"] == {"kind": "random_walk", "start": 1}
    row = canonicalize_object_fields({"simulation": {"kind": "FOLLOW", "source": " Analog-Value : 3 "}})
    assert row["simulation"] == {"kind": "follow", "source": "analog-value:3"}
    assert canonicalize_object_fields({"simulation": None})["simulation"] is None


def test_recipient_kind_is_canonical():
    # "Device" was refused.
    row = canonicalize_object_fields({"notification_recipient_list": [
        {"recipient": {"kind": "Device", "instance_number": 5}},
        {"recipient": {"kind": "ADDRESS", "address": "*"}},
    ]})
    kinds = [entry["recipient"]["kind"] for entry in row["notification_recipient_list"]]
    assert kinds == ["device", "address"]


def test_time_values_are_saved_in_time_order():
    # The device runs each day sorted, so the saved row must match it.
    out = canonicalize_object_fields({
        "weekly_schedule": [[{"time": "17:00", "value": "1"}, {"time": "8:00", "value": "2"}], []],
        "exception_schedule": [
            {"period": {"kind": "date"}, "priority": 1,
             "time_values": [{"time": "12:30", "value": "3"}, {"time": "09:15", "value": "4"}]}
        ],
    })
    assert [e["time"] for e in out["weekly_schedule"][0]] == ["8:00", "17:00"]
    assert [e["time"] for e in out["exception_schedule"][0]["time_values"]] == ["09:15", "12:30"]


@pytest.mark.parametrize("bad", [
    [{"time": "17:00", "value": "1"}, {"time": "25:00", "value": "2"}],
    [{"time": "17:00", "value": "1"}, {"value": "2"}],
    [{"time": "17:00", "value": "1"}, "x"],
])
def test_time_values_left_unsorted_when_a_time_is_invalid(bad):
    # Validation reports the bad entry; canonicalizing doesn't reorder around it.
    assert canonicalize_object_fields({"weekly_schedule": [bad]})["weekly_schedule"] == [bad]
