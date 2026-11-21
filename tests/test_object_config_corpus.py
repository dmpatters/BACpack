"""Validates, so builds: every
row in the corpus passes object_config.check_object and then really builds.
A row that passes the check but fails the build is a gap in check_object
-- fix check_object, never drop the row. The corpus is hand-written: a
minimal row per type, then one variant per edge of every field the build
reads."""
from __future__ import annotations

import pytest

from bacpack import object_config
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application, build_bacnet_object

_RECIPIENT = {
    "recipient": {"kind": "device", "instance_number": 599002},
    "valid_days": [True] * 7,
    "from_time": "00:00:00",
    "to_time": "23:59:59",
    "transitions": [True, True, True],
    "confirmed": True,
    "process_identifier": 1,
}

_BASE = {
    "analog-input": {"units": "degreesFahrenheit"},
    "analog-output": {"units": "percent"},
    "analog-value": {"units": "percent"},
    "binary-input": {},
    "binary-output": {},
    "binary-value": {},
    "multi-state-input": {"state_text": ["Off", "Low", "High"]},
    "multi-state-output": {"state_text": ["Off", "Low", "High"]},
    "multi-state-value": {"state_text": ["Off", "Low", "High"]},
    "schedule": {"schedule_value_type": "analog", "schedule_default": "1.0"},
    "calendar": {"date_list": [{"kind": "date", "year": "*", "month": 12, "day": 25}]},
    "notification-class": {},
}

# NC-90 and CAL-91 are the reference targets every whole-device case carries.
_TARGETS = [
    {"object_type": "notification-class", "instance_number": 90, "name": "NC-90"},
    {
        "object_type": "calendar",
        "instance_number": 91,
        "name": "CAL-91",
        "date_list": [{"kind": "date", "year": "*", "month": 1, "day": 1}],
    },
]

_ANALOG = ("analog-input", "analog-output", "analog-value")
_BINARY = ("binary-input", "binary-output", "binary-value")
_MULTI = ("multi-state-input", "multi-state-output", "multi-state-value")
_COMMANDABLE = (
    "analog-output",
    "analog-value",
    "binary-output",
    "binary-value",
    "multi-state-output",
    "multi-state-value",
)


def _variants():
    out = []

    def add(label, object_type, **fields):
        out.append((f"{object_type}:{label}", {"object_type": object_type, **_BASE[object_type], **fields}))

    for t in _BASE:
        add("minimal", t)
        add("not-writable", t, bacnet_writable=False)
        add("description", t, description="x" * 200)
        add("instance-max", t, instance_number=4194302)
        add("instance-zero", t, instance_number=0)
        if t != "calendar":
            add("out-of-service", t, out_of_service_default=True)
    for t in _ANALOG:
        add("no-units", t, units=None)
        add("alarm-high-only", t, alarming_enabled=True, notification_class_instance=90, alarm_high_limit=80.0)
        add("alarm-low-only", t, alarming_enabled=True, notification_class_instance=90, alarm_low_limit=-40.0)
        add(
            "alarm-full",
            t,
            alarming_enabled=True,
            notification_class_instance=90,
            alarm_high_limit=80.0,
            alarm_low_limit=10.0,
            alarm_deadband=2.5,
            alarm_time_delay=4294967295,
            alarm_time_delay_normal=0,
            alarm_notify_type="event",
            alarm_event_enable=[True, False, True],
            alarm_limit_enable=[True, True],
            alarm_event_detection_enable=False,
        )
        for kind, sim in [
            ("sine", {"kind": "sine", "center": 50, "amplitude": 10, "period_s": 60}),
            ("triangle", {"kind": "triangle", "low": 0, "high": 10, "period_s": 30}),
            ("sawtooth", {"kind": "sawtooth", "low": 0, "high": 10, "period_s": 30}),
            ("square", {"kind": "square", "low": 0, "high": 10, "period_s": 30, "duty": 0.25}),
            ("random-walk", {"kind": "random_walk", "start": 5, "max_step": 1, "low": 0, "high": 10}),
            ("noise", {"kind": "noise", "center": 5, "amplitude": 0}),
            (
                "clamped",
                {"kind": "sine", "center": 50, "amplitude": 10, "period_s": 60, "min": 45, "max": 55, "decimals": 6},
            ),
        ]:
            add(f"sim-{kind}", t, simulation=sim)
    for t in _BINARY:
        add("alarm-active", t, alarming_enabled=True, notification_class_instance=90, alarm_value=True)
        add("alarm-inactive", t, alarming_enabled=True, notification_class_instance=90, alarm_value=False)
        add("sim-square", t, simulation={"kind": "square", "period_s": 10})
        add("sim-random", t, simulation={"kind": "random", "min_dwell_s": 1, "max_dwell_s": 5})
    for t in _MULTI:
        add("one-state", t, state_text=["Only"])
        add("many-states", t, state_text=[f"S{i}" for i in range(1, 33)])
        add("empty-label", t, state_text=["Off", "", "On"])
        add("semicolon-label", t, state_text=["Off", "Auto;Hand", "On"])
        add("alarm-values", t, alarming_enabled=True, notification_class_instance=90, alarm_values=[2, 3])
        add("sim-cycle", t, simulation={"kind": "cycle", "dwell_s": 5})
        add("sim-cycle-states", t, simulation={"kind": "cycle", "dwell_s": 5, "states": [1, 3]})
        add(
            "sim-random-states",
            t,
            simulation={"kind": "random", "min_dwell_s": 1, "max_dwell_s": 2, "states": [2, 3]},
        )
    for t in _COMMANDABLE:
        if t in _ANALOG:
            rd = ["-1e30", "0.0", "42.5"]
        elif t in _BINARY:
            rd = ["active", "inactive"]
        else:
            rd = ["1", "3"]
        for value in rd:
            add(f"relinquish-{value}", t, relinquish_default=value)
    for t in _BASE:
        if t in ("calendar", "notification-class", "schedule"):
            continue
        add("trended-interval", t, trended=True, trend_mode="interval", trend_interval_seconds=1, trend_sample_cap=1)
        add("trended-cov", t, trended=True, trend_mode="cov")
        add(
            "trend-reporting",
            t,
            trended=True,
            trend_reporting_enabled=True,
            trend_notification_class_instance=90,
            trend_notification_threshold=4294967295,
            trend_notify_type="alarm",
            trend_event_enable=[True, True, True],
        )

    day = [{"time": "08:00", "value": "21.5"}, {"time": "17:00", "value": None}]
    add("week", "schedule", weekly_schedule=[day] * 7)
    add("empty-week", "schedule", weekly_schedule=[[] for _ in range(7)])
    add("priority-1", "schedule", schedule_priority=1)
    add("priority-16", "schedule", schedule_priority=16)
    add("effective", "schedule", schedule_effective_start="2026-01-01", schedule_effective_end="2026-12-31")
    for kind, period in [
        ("date", {"kind": "date", "year": "*", "month": 12, "day": 25}),
        ("range", {"kind": "date-range", "start": "2026-01-01", "end": "2026-01-31"}),
        ("week-and-day", {"kind": "week-and-day", "month": "*", "week_of_month": "last", "day_of_week": 5}),
        ("calendar-ref", {"kind": "calendar-reference", "instance_number": 91}),
    ]:
        add(
            f"exception-{kind}",
            "schedule",
            exception_schedule=[{"priority": 16, "period": period, "time_values": [{"time": "00:00", "value": "5"}]}],
        )
    add("binary", "schedule", schedule_value_type="binary", schedule_default="active")
    add("multistate", "schedule", schedule_value_type="multi-state", schedule_default="2", state_text=["A", "B", "C"])
    add(
        "local-target",
        "schedule",
        schedule_write_targets=[
            {"object_type": "analog-value", "instance_number": 0, "property_identifier": "presentValue"}
        ],
    )
    add(
        "external-target",
        "schedule",
        schedule_write_targets=[
            {
                "device_instance": 599002,
                "object_type": "analog-value",
                "instance_number": 3,
                "property_identifier": "present-value",
            }
        ],
    )
    for kind, entry in [
        ("range", {"kind": "date-range", "start": "2026-01-01", "end": "2026-12-31"}),
        ("week-and-day", {"kind": "week-and-day", "month": 3, "week_of_month": 2, "day_of_week": 1}),
        ("wildcards", {"kind": "date", "year": None, "month": None, "day": None}),
    ]:
        add(kind, "calendar", date_list=[entry])
    add(
        "priorities",
        "notification-class",
        notification_priority=[0, 128, 255],
        notification_ack_required=[True, False, True],
    )
    add("device-recipient", "notification-class", notification_recipient_list=[_RECIPIENT])
    for address in ["192.168.1.200", "192.168.1.200:47809", "0x0a0b", "*", "5:192.168.1.9", "5:12"]:
        add(
            f"address-{address}",
            "notification-class",
            notification_recipient_list=[{**_RECIPIENT, "recipient": {"kind": "address", "address": address}}],
        )
    add("many-recipients", "notification-class", notification_recipient_list=[_RECIPIENT] * 5)

    # Every row needs a name; a unique one per row keeps the whole-device case legal.
    for index, (label, row) in enumerate(out):
        row.setdefault("instance_number", 1)
        row["name"] = f"R{index}"
    return out


CORPUS = _variants()


@pytest.mark.parametrize("label, row", CORPUS, ids=[label for label, _ in CORPUS])
async def test_corpus_row_validates_and_builds(label, row):
    object_config.check_object(row)
    # Built with the column defaults an insert would store, as a saved row is.
    build_bacnet_object(BacnetObject(**object_config.row_from_object(BacnetObject(**row))))


async def test_corpus_rows_build_as_one_device(session_factory):
    # One device can't hold two rows with one identifier: renumber.
    rows = [{**row, "instance_number": index + 100} for index, (_, row) in enumerate(CORPUS)]
    rows += [dict(target) for target in _TARGETS]
    rows.append({"object_type": "analog-value", "instance_number": 0, "name": "AV-0", "units": "percent"})
    assert object_config.check_device("Corpus Device", rows) is None
    with session_factory() as session:
        device = Device(id=599001, name="Corpus Device", vendor_identifier=999)
        session.add(device)
        objects = [BacnetObject(device=device, **row) for row in rows]
        session.add_all(objects)
        session.commit()
        build_application(device, objects, trend_session_factory=session_factory)
