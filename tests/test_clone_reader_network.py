"""clone_reader against a real BACpack device over UDP loopback: device 1 reads
the objects device 2 serves. Static address, not Who-Is: loopback doesn't carry
the broadcast reliably."""
from __future__ import annotations

import pytest

from bacpack.bacnet_client import BacnetClient
from bacpack.clone_reader import read_controller
from bacpack.models import BacnetObject, Device
from tests.udp_bind import build_on_loopback


def _build(session_factory, device_id, bind_address):
    return build_on_loopback(session_factory, device_id, bind_address, trend_session_factory=session_factory)


def _remote(session):
    remote = Device(id=2, name="Remote", description="The plant", location="Basement", vendor_name="Acme")
    weekly = [[{"time": "08:00", "value": "50.0"}, {"time": "17:00", "value": None}] for _ in range(7)]
    rows = [
        BacnetObject(object_type="analog-input", instance_number=1, name="Zone Temp", description="North zone",
                     units="degreesFahrenheit", trended=True, trend_mode="interval", trend_interval_seconds=300,
                     trend_sample_cap=500),
        BacnetObject(object_type="analog-value", instance_number=2, name="Setpoint", units="percent",
                     relinquish_default="21.5"),
        BacnetObject(object_type="multi-state-value", instance_number=3, name="Mode",
                     state_text=["Off", "Heat", "Cool"], relinquish_default="2"),
        BacnetObject(object_type="binary-input", instance_number=4, name="Fan Status",
                     active_text="Running", inactive_text="Stopped"),
        BacnetObject(object_type="schedule", instance_number=5, name="Occupancy", schedule_value_type="analog",
                     schedule_default="10.0", weekly_schedule=weekly, schedule_priority=12,
                     exception_schedule=[
                         {"period": {"kind": "calendar-reference", "instance_number": 6}, "priority": 5,
                          "time_values": [{"time": "00:00", "value": "0.0"}]},
                         {"period": {"kind": "date-range", "start": "2026-06-01", "end": "2026-06-30"},
                          "priority": 6, "time_values": [{"time": "09:00", "value": "25.0"}]},
                         {"period": {"kind": "week-and-day", "month": "*", "week_of_month": "last", "day_of_week": "5"},
                          "priority": 7, "time_values": [{"time": "09:00", "value": "30.0"}]},
                     ],
                     schedule_effective_start="2026-01-01", schedule_effective_end="2026-12-31",
                     schedule_write_targets=[{"object_type": "analog-value", "instance_number": 2}]),
        BacnetObject(object_type="calendar", instance_number=6, name="Holidays",
                     date_list=[{"kind": "date", "year": 2026, "month": 12, "day": 25}]),
        BacnetObject(object_type="notification-class", instance_number=7, name="Alarms",
                     notification_priority=[10, 20, 30],
                     notification_recipient_list=[
                         {"recipient": {"kind": "device", "instance_number": 99}, "valid_days": [True] * 7,
                          "from_time": "00:00:00", "to_time": "23:59:59", "transitions": [True, False, True],
                          "confirmed": True, "process_identifier": 7},
                         {"recipient": {"kind": "address", "address": "192.168.1.77"}, "valid_days": [True] * 5 + [False] * 2,
                          "from_time": "06:00:00", "to_time": "18:00:00", "transitions": [True, True, True],
                          "confirmed": False, "process_identifier": 8},
                     ]),
        BacnetObject(object_type="analog-input", instance_number=8, name="Supply Temp", units="degreesFahrenheit",
                     alarming_enabled=True, notification_class_instance=7, alarm_high_limit=90.0,
                     alarm_low_limit=40.0, alarm_deadband=2.0),
    ]
    for row in rows:
        row.device = remote
    session.add_all([remote, *rows])


@pytest.mark.asyncio
async def test_reads_a_running_bacpack_device_over_udp(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="Local"))
        _remote(session)
        session.commit()
    remote_app = local_app = None
    try:
        remote_app = _build(session_factory, 2, "127.0.0.2/8")
        local_app = _build(session_factory, 1, "127.0.0.1/8")
        client = BacnetClient(local_app)
        binding = await client.bind(2, "127.0.0.2", timeout=3)
        reports = []
        result = await read_controller(
            client, binding, 2, 3.0, lambda done, total, phase: reports.append((done, total)), lambda: False
        )
    finally:
        for app in (local_app, remote_app):
            if app is not None:
                app.close()

    assert result["device"]["name"] == "Remote"
    assert result["device"]["vendor_name"] == "Acme" and result["device"]["location"] == "Basement"
    by_name = {o["props"]["object-name"]: o for o in result["objects"] if "object-name" in o["props"]}
    assert reports[-1][0] == reports[-1][1] == len(result["objects"])
    assert all(o["error"] is None for o in result["objects"])

    zone = by_name["Zone Temp"]
    assert zone["props"]["description"] == "North zone" and zone["props"]["units"] == "degrees-fahrenheit"
    assert zone["props"]["present-value"] == 0.0
    setpoint = by_name["Setpoint"]
    assert setpoint["props"]["relinquish-default"] == 21.5 and setpoint["props"]["commandable"] is True
    assert by_name["Mode"]["props"]["state-text"] == ["Off", "Heat", "Cool"]
    assert by_name["Mode"]["props"]["number-of-states"] == 3
    fan = by_name["Fan Status"]
    assert (fan["props"]["active-text"], fan["props"]["inactive-text"]) == ("Running", "Stopped")
    assert "commandable" not in fan["props"]
    assert by_name["Occupancy"]["props"]["priority-for-writing"] == 12
    assert by_name["Holidays"]["props"]["date-list"]
    # The shapes clone_plan reads (it builds its fixtures from these).
    occupancy = by_name["Occupancy"]["props"]
    assert occupancy["weekly-schedule"][0] == {
        "daySchedule": [{"time": "08:00:00.00", "value": 50.0}, {"time": "17:00:00.00", "value": None}]
    }
    assert [e["period"] for e in occupancy["exception-schedule"]] == [
        "calendar:6", {"startDate": "2026-6-1 *", "endDate": "2026-6-30 *"}, "WeekNDay(255, 6, 5)",
    ]
    assert occupancy["exception-schedule"][0]["eventPriority"] == 5
    assert occupancy["effective-period"] == {"startDate": "2026-1-1 *", "endDate": "2026-12-31 *"}
    assert occupancy["list-of-object-property-references"] == [{
        "objectIdentifier": "analog-value:2", "propertyIdentifier": "present-value",
        "propertyArrayIndex": None, "deviceIdentifier": None,
    }]
    recipients = by_name["Alarms"]["props"]["recipient-list"]
    assert recipients[0]["recipient"] == "device:99" and recipients[0]["transitions"] == [1, 0, 1]
    # A MAC arrives as hex text.
    assert recipients[1]["recipient"]["macAddress"] == "c0a8014dbac0"
    assert by_name["Alarms"]["props"]["priority"] == [10, 20, 30]
    supply = by_name["Supply Temp"]
    assert supply["props"]["high-limit"] == 90.0 and supply["props"]["notification-class"] == 7
    assert supply["props"]["deadband"] == 2.0
    log = by_name["Zone Temp-Trend"]
    assert log["object_type"] == "trend-log"
    assert log["props"]["buffer-size"] == 500 and log["props"]["logging-type"] == "polled"
    assert log["props"]["log-interval"] == 30000
