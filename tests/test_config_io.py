from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from bacpack import bacpack_version, device_clock, timeutil
from bacpack.config_schemas import ConfigImportError
from bacpack.config_io import (
    CONFIG_VERSION,
    apply_device_restore,
    export_config,
    export_device_backup,
    parse_config,
    sync_config,
    sync_objects_for_device,
)
from bacpack.db import lock_both_files
from bacpack.models import BacnetObject, Device
from bacpack.models import get_or_create_history_counters, trend_state_of, TrendState
from bacpack.object_builder import build_application

# --- Export / parse round trip -------------------------------------------


def test_export_then_parse_round_trips():
    device = Device(id=1, name="Dev-1", description="a device", vendor_identifier=42)
    device.objects.append(
        BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit")
    )
    device.objects.append(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "On"],
        )
    )

    doc = export_config([device])
    assert doc["version"] == 1
    assert len(doc["devices"]) == 1

    parsed = parse_config(doc)
    assert len(parsed) == 1
    assert parsed[0]["id"] == 1
    assert parsed[0]["name"] == "Dev-1"
    assert len(parsed[0]["objects"]) == 2
    assert parsed[0]["objects"][1]["state_text"] == ["Off", "On"]


def test_export_then_parse_round_trips_bind_address():
    # The per-device bind_address column used to be invisible to
    # export_config/parse_config entirely, so it never survived a config
    # export/import round trip.
    device = Device(id=1, name="Dev-1", bind_address="192.168.1.50/24")

    doc = export_config([device])
    assert doc["devices"][0]["bind_address"] == "192.168.1.50/24"

    parsed = parse_config(doc)
    assert parsed[0]["bind_address"] == "192.168.1.50/24"


def test_parse_config_defaults_missing_bind_address_to_none():
    doc = {"devices": [{"id": 1, "name": "Dev-1", "objects": []}]}
    parsed = parse_config(doc)
    assert parsed[0]["bind_address"] is None


def test_export_then_parse_round_trips_vendor_name():
    # The per-device vendor_name column was invisible to
    # export_config/parse_config, so it never survived a config export/import
    # round trip.
    device = Device(id=1, name="Dev-1", vendor_name="Example Controls")

    doc = export_config([device])
    assert doc["devices"][0]["vendor_name"] == "Example Controls"

    parsed = parse_config(doc)
    assert parsed[0]["vendor_name"] == "Example Controls"


def test_export_then_parse_round_trips_location():
    device = Device(id=1, name="Dev-1", location="Plant room")

    doc = export_config([device])
    assert doc["devices"][0]["location"] == "Plant room"

    parsed = parse_config(doc)
    assert parsed[0]["location"] == "Plant room"


def test_parse_config_defaults_missing_location_to_none():
    doc = {"devices": [{"id": 1, "name": "Dev-1", "objects": []}]}
    assert parse_config(doc)[0]["location"] is None


def test_parse_config_refuses_a_non_string_location():
    doc = {"devices": [{"id": 1, "name": "Dev-1", "location": 5, "objects": []}]}
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_import_sets_location_on_new_and_existing_devices(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="Old", location="before"))
        session.commit()
    doc = {"devices": [
        {"id": 1, "name": "Old", "location": "after", "objects": []},
        {"id": 2, "name": "New", "location": "Roof", "objects": []},
    ]}
    with session_factory() as session:
        sync_config(session, parse_config(doc), dry_run=False)
        session.commit()
    with session_factory() as session:
        assert session.get(Device, 1).location == "after"
        assert session.get(Device, 2).location == "Roof"


def test_export_then_parse_round_trips_backup_failure_timeout():
    device = Device(id=1, name="Dev-1", backup_failure_timeout=300)

    doc = export_config([device])
    assert doc["devices"][0]["backup_failure_timeout"] == 300

    parsed = parse_config(doc)
    assert parsed[0]["backup_failure_timeout"] == 300


@pytest.mark.parametrize("given", ["missing", None])
def test_parse_config_defaults_backup_failure_timeout_to_60(given):
    device = {"id": 1, "name": "Dev-1", "objects": []}
    if given != "missing":
        device["backup_failure_timeout"] = given
    assert parse_config({"devices": [device]})[0]["backup_failure_timeout"] == 60


@pytest.mark.parametrize("bad", [0, 65536, -1, "60", 60.0, True])
def test_parse_config_refuses_a_bad_backup_failure_timeout(bad):
    doc = {"devices": [{"id": 1, "name": "Dev-1", "backup_failure_timeout": bad, "objects": []}]}
    with pytest.raises(ConfigImportError, match="backup_failure_timeout"):
        parse_config(doc)


def test_import_sets_backup_failure_timeout_on_new_and_existing_devices(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="Old", backup_failure_timeout=120))
        session.commit()
    doc = {"devices": [
        {"id": 1, "name": "Old", "backup_failure_timeout": 900, "objects": []},
        {"id": 2, "name": "New", "backup_failure_timeout": 30, "objects": []},
    ]}
    with session_factory() as session:
        sync_config(session, parse_config(doc), dry_run=False)
        session.commit()
    with session_factory() as session:
        assert session.get(Device, 1).backup_failure_timeout == 900
        assert session.get(Device, 2).backup_failure_timeout == 30


def test_parse_config_defaults_missing_vendor_name_to_none():
    doc = {"devices": [{"id": 1, "name": "Dev-1", "objects": []}]}
    parsed = parse_config(doc)
    assert parsed[0]["vendor_name"] is None


# --- config_io.py never extended for
# alarming fields -- a full export/import silently dropped all 15
# alarming columns, and an imported notification-class object got no
# priority/ack-required/recipients. ---------------------------------------


def test_export_then_parse_round_trips_alarming_enabled_object():
    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="analog-input",
            instance_number=1,
            name="AI-1",
            units="degreesFahrenheit",
            alarming_enabled=True,
            notification_class_instance=1,
            alarm_time_delay=30,
            alarm_time_delay_normal=15,
            alarm_notify_type="event",
            alarm_event_enable=[True, False, True],
            alarm_high_limit=100.0,
            alarm_low_limit=0.0,
            alarm_deadband=2.0,
            alarm_limit_enable=[True, True],
            alarm_event_detection_enable=False,
        )
    )
    device.objects.append(BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"))

    doc = export_config([device])
    parsed = parse_config(doc)
    row = parsed[0]["objects"][0]

    assert row["alarming_enabled"] is True
    assert row["notification_class_instance"] == 1
    assert row["alarm_time_delay"] == 30
    assert row["alarm_time_delay_normal"] == 15
    assert row["alarm_notify_type"] == "event"
    assert row["alarm_event_enable"] == [True, False, True]
    assert row["alarm_high_limit"] == 100.0
    assert row["alarm_low_limit"] == 0.0
    assert row["alarm_deadband"] == 2.0
    assert row["alarm_event_detection_enable"] is False
    assert row["alarm_limit_enable"] == [True, True]


def test_export_then_parse_round_trips_binary_and_multistate_alarm_fields():
    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="binary-value",
            instance_number=1,
            name="BV-1",
            alarming_enabled=True,
            notification_class_instance=1,
            alarm_value=True,
        )
    )
    device.objects.append(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            alarming_enabled=True,
            notification_class_instance=1,
            alarm_values=[2, 3],
        )
    )
    device.objects.append(BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"))

    doc = export_config([device])
    parsed = parse_config(doc)
    objects_by_type = {o["object_type"]: o for o in parsed[0]["objects"]}

    assert objects_by_type["binary-value"]["alarm_value"] is True
    assert objects_by_type["multi-state-value"]["alarm_values"] == [2, 3]


def test_export_then_parse_round_trips_notification_class_object():
    device = Device(id=1, name="Dev-1")
    recipient_list = [
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
    device.objects.append(
        BacnetObject(
            object_type="notification-class",
            instance_number=1,
            name="NC-1",
            notification_priority=[50, 50, 50],
            notification_ack_required=[True, False, True],
            notification_recipient_list=recipient_list,
        )
    )

    doc = export_config([device])
    parsed = parse_config(doc)
    row = parsed[0]["objects"][0]

    assert row["notification_priority"] == [50, 50, 50]
    assert row["notification_ack_required"] == [True, False, True]
    assert row["notification_recipient_list"] == recipient_list


def test_parse_config_defaults_missing_alarming_fields():
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "Dev-1",
                "objects": [
                    {"object_type": "analog-input", "instance_number": 1, "name": "AI-1"}
                ],
            }
        ]
    }
    parsed = parse_config(doc)
    row = parsed[0]["objects"][0]
    assert row["alarming_enabled"] is False
    assert row["notification_class_instance"] is None
    assert row["alarm_time_delay"] == 0
    assert row["alarm_notify_type"] == "alarm"
    assert row["alarm_deadband"] == 0.0
    assert row["alarm_event_detection_enable"] is True


def test_parse_config_refuses_alarming_without_a_notification_class():
    # An older release saved this, then the device couldn't start.
    doc = {"devices": [{"id": 1, "name": "A", "objects": [{
        "object_type": "analog-value", "instance_number": 1, "name": "AV-1",
        "alarming_enabled": True, "alarm_high_limit": 80.0,
    }]}]}
    with pytest.raises(ConfigImportError, match="notification_class_instance is required when alarming_enabled=True"):
        parse_config(doc)


def test_parse_config_rejects_bad_notification_class_priority_length():
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "Dev-1",
                "objects": [
                    {
                        "object_type": "notification-class",
                        "instance_number": 1,
                        "name": "NC-1",
                        "notification_priority": [100, 100],
                    }
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_refuses_a_recipient_process_identifier_above_32_bits():
    # Only checked to be an integer before.
    recipient = {
        "recipient": {"kind": "address", "address": "192.168.1.200"},
        "valid_days": [True] * 7,
        "from_time": "00:00",
        "to_time": "23:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 4294967296,
    }
    doc = {"devices": [{"id": 1, "name": "Dev-1", "objects": [{
        "object_type": "notification-class", "instance_number": 1, "name": "NC-1",
        "notification_recipient_list": [recipient],
    }]}]}
    with pytest.raises(ConfigImportError, match="process_identifier"):
        parse_config(doc)


# --- parse_config validation ----------------------------------------------


def test_parse_config_requires_devices_list():
    with pytest.raises(ConfigImportError):
        parse_config({})
    with pytest.raises(ConfigImportError):
        parse_config({"devices": "not-a-list"})


def test_export_records_the_bacpack_release_beside_the_format_version():
    doc = export_config([])
    assert doc["version"] == CONFIG_VERSION
    assert doc["bacpack_version"] == bacpack_version()


@pytest.mark.parametrize(
    "extra", [{}, {"version": None}, {"version": CONFIG_VERSION}, {"version": 0}]
)
def test_parse_config_accepts_this_format_version_older_or_none(extra):
    doc = {**extra, "devices": [{"id": 1, "name": "A", "objects": []}]}
    assert parse_config(doc)[0]["id"] == 1


def test_parse_config_ignores_bacpack_version():
    # Informational only: a file from any release imports if its format
    # version is one this release reads.
    doc = {"version": 1, "bacpack_version": "9.9.9", "devices": []}
    assert parse_config(doc) == []


def test_parse_config_refuses_a_newer_format_version():
    doc = {"version": CONFIG_VERSION + 1, "bacpack_version": "9.9.9", "devices": []}
    with pytest.raises(ConfigImportError, match=r"format version 2 \(written by BACpack 9\.9\.9\).*can read \(up to 1\)"):
        parse_config(doc)


@pytest.mark.parametrize("bad", ["1", 1.0, True, -1])
def test_parse_config_refuses_a_version_that_is_not_a_whole_number(bad):
    with pytest.raises(ConfigImportError, match="'version' must be a whole number"):
        parse_config({"version": bad, "devices": []})


def test_parse_config_rejects_duplicate_device_id():
    doc = {
        "devices": [
            {"id": 1, "name": "A", "objects": []},
            {"id": 1, "name": "B", "objects": []},
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_rejects_bad_device_id():
    doc = {"devices": [{"id": "not-an-int", "name": "A", "objects": []}]}
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_requires_device_name():
    doc = {"devices": [{"id": 1, "objects": []}]}
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_rejects_unknown_object_type():
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [{"object_type": "not-a-type", "instance_number": 1, "name": "X"}],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_refuses_a_misspelled_object_field():
    # A full sync would otherwise reset the real field ("units") to its default.
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [
                    {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "unts": "percent"}
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError, match=r"devices\[0\] \(id 1\)\.objects\[0\].*'unts'"):
        parse_config(doc)


def test_parse_config_refuses_a_misspelled_device_field():
    doc = {"devices": [{"id": 1, "name": "A", "descripton": "x", "objects": []}]}
    with pytest.raises(ConfigImportError, match=r"devices\[0\] \(id 1\).*'descripton'"):
        parse_config(doc)


def test_parse_config_refuses_an_unknown_top_level_key():
    with pytest.raises(ConfigImportError, match="'device'"):
        parse_config({"devices": [], "device": []})


def test_parse_config_multistate_requires_state_text():
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [
                    {"object_type": "multi-state-value", "instance_number": 1, "name": "MSV-1"}
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_rejects_bad_units():
    # Same "F" vs "degreesFahrenheit" gap validate_units exists for -- see
    # bacnet.py.
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [
                    {
                        "object_type": "analog-input",
                        "instance_number": 1,
                        "name": "AI-1",
                        "units": "F",
                    }
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)


def test_parse_config_rejects_duplicate_object_identifier_and_name():
    doc = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [
                    {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"},
                    {"object_type": "analog-value", "instance_number": 1, "name": "AV-1-dup"},
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc)

    doc2 = {
        "devices": [
            {
                "id": 1,
                "name": "A",
                "objects": [
                    {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"},
                    {"object_type": "analog-value", "instance_number": 2, "name": "AV-1"},
                ],
            }
        ]
    }
    with pytest.raises(ConfigImportError):
        parse_config(doc2)


# --- sync_objects_for_device -----------------------------------------------


def test_sync_objects_for_device_creates_updates_and_deletes(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        keep = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        remove = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        session.add(device)
        session.commit()

        # Removing an object via this sync path (config import/apply) used to bypass
        # release_trend_samples_for_objects entirely, leaking
        # trend_total_sample_count high with no self-healing.
        from bacpack.trending import record_sample

        record_sample(session_factory, remove.id, 1.0)
        record_sample(session_factory, remove.id, 2.0)
        session.refresh(device)

        rows = [
            {
                "object_type": "analog-value",
                "instance_number": 1,
                "name": "AV-1-renamed",
                "description": None,
                "units": None,
                "bacnet_writable": True,
                "out_of_service_default": False,
                "relinquish_default": None,
                "state_text": None,
                "trended": False,
                "trend_mode": "interval",
                "trend_interval_seconds": None,
            },
            {
                "object_type": "analog-value",
                "instance_number": 3,
                "name": "AV-3-new",
                "description": None,
                "units": None,
                "bacnet_writable": True,
                "out_of_service_default": False,
                "relinquish_default": None,
                "state_text": None,
                "trended": False,
                "trend_mode": "interval",
                "trend_interval_seconds": None,
            },
        ]
        summary = sync_objects_for_device(session, device, rows, dry_run=False)
        session.commit()

        assert summary == {
            "created": ["analog-value:3"],
            "updated": ["analog-value:1"],
            "deleted": ["analog-value:2"],
        }

        remaining = {(o.object_type, o.instance_number): o for o in device.objects}
        assert set(remaining) == {("analog-value", 1), ("analog-value", 3)}
        assert remaining[("analog-value", 1)].name == "AV-1-renamed"

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 0


def test_sync_objects_for_device_dry_run_does_not_write(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(device)
        session.commit()

        summary = sync_objects_for_device(session, device, [], dry_run=True)
        session.commit()

        assert summary == {"created": [], "updated": [], "deleted": ["analog-value:1"]}
        assert len(device.objects) == 1  # untouched


# --- sync_config (whole-server) --------------------------------------------


def test_sync_config_creates_updates_and_deletes_devices(session_factory):
    with session_factory() as session:
        keep_device = Device(id=1, name="Keep")
        BacnetObject(device=keep_device, object_type="analog-value", instance_number=1, name="AV-1")
        drop_device = Device(id=2, name="Drop")
        drop_object = BacnetObject(
            device=drop_device, object_type="analog-value", instance_number=1, name="AV-Drop"
        )
        session.add_all([keep_device, drop_device])
        session.commit()

        # A device dropped entirely from an applied config file used to
        # bypass release_trend_samples_for_objects, leaking
        # trend_total_sample_count high with no self-healing.
        from bacpack.trending import record_sample

        record_sample(session_factory, drop_object.id, 1.0)
        record_sample(session_factory, drop_object.id, 2.0)

        parsed = [
            {
                "id": 1,
                "name": "Keep-Renamed",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": None,
                "bind_address": None,
                "backup_failure_timeout": 60,
                "objects": [
                    {
                        "object_type": "analog-value",
                        "instance_number": 1,
                        "name": "AV-1",
                        "description": None,
                        "units": None,
                        "bacnet_writable": True,
                        "out_of_service_default": False,
                        "relinquish_default": None,
                        "state_text": None,
                        "trended": False,
                        "trend_mode": "interval",
                        "trend_interval_seconds": None,
                    }
                ],
            },
            {
                "id": 3,
                "name": "New",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": None,
                "bind_address": None,
                "backup_failure_timeout": 60,
                "objects": [],
            },
        ]

        lock_both_files(session)  # as the import route does
        result = sync_config(session, parsed, dry_run=False)
        session.commit()

        assert result["devices_created"] == [3]
        assert result["devices_updated"] == [1]
        assert result["devices_deleted"] == [2]

        remaining_ids = {d.id for d in session.query(Device).all()}
        assert remaining_ids == {1, 3}
        assert session.get(Device, 1).name == "Keep-Renamed"

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 0


def test_sync_config_creates_and_updates_bind_address(session_factory):
    with session_factory() as session:
        keep_device = Device(id=1, name="Keep", bind_address="127.0.0.1/8")
        session.add(keep_device)
        session.commit()

        parsed = [
            {
                "id": 1,
                "name": "Keep",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": None,
                "bind_address": "192.168.1.50/24",
                "backup_failure_timeout": 60,
                "objects": [],
            },
            {
                "id": 2,
                "name": "New",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": None,
                "bind_address": "192.168.1.51/24",
                "backup_failure_timeout": 60,
                "objects": [],
            },
        ]

        sync_config(session, parsed, dry_run=False)
        session.commit()

        assert session.get(Device, 1).bind_address == "192.168.1.50/24"
        assert session.get(Device, 2).bind_address == "192.168.1.51/24"


def test_sync_config_creates_and_updates_vendor_name(session_factory):
    # The vendor_name round trip at the sync_config layer -- mirrors
    # test_sync_config_creates_and_updates_bind_address above.
    with session_factory() as session:
        keep_device = Device(id=1, name="Keep", vendor_name="Old Co")
        session.add(keep_device)
        session.commit()

        parsed = [
            {
                "id": 1,
                "name": "Keep",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": "Example Controls",
                "bind_address": None,
                "backup_failure_timeout": 60,
                "objects": [],
            },
            {
                "id": 2,
                "name": "New",
                "description": None,
                "location": None,
                "vendor_identifier": 999,
                "vendor_name": "Acme Devices",
                "bind_address": None,
                "backup_failure_timeout": 60,
                "objects": [],
            },
        ]

        sync_config(session, parsed, dry_run=False)
        session.commit()

        assert session.get(Device, 1).vendor_name == "Example Controls"
        assert session.get(Device, 2).vendor_name == "Acme Devices"


def test_sync_config_dry_run_does_not_write(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Keep")
        session.add(device)
        session.commit()

        result = sync_config(session, [], dry_run=True)
        session.commit()

        assert result["devices_deleted"] == [1]
        # Nothing actually deleted -- dry run only.
        assert session.get(Device, 1) is not None


# --- Re-typing and other sync edge cases --------------------------------------


def test_sync_re_typing_an_object_that_keeps_its_name(session_factory):
    """A file that turns AI:1 "Zone Temp" into AV:1 "Zone Temp" deletes one
    object and creates another with the same name. The INSERT used to be
    flushed before the DELETE, failing the per-device unique name."""
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        BacnetObject(device=device, object_type="analog-input", instance_number=1, name="Zone Temp")
        session.add(device)
        session.commit()

        parsed = parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [{"object_type": "analog-value", "instance_number": 1, "name": "Zone Temp"}],
                    }
                ]
            }
        )
        sync_objects_for_device(session, device, parsed[0]["objects"], dry_run=False)
        session.commit()

        assert [(o.object_type, o.name) for o in device.objects] == [("analog-value", "Zone Temp")]


def test_sync_rename_onto_a_deleted_objects_name(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        BacnetObject(device=device, object_type="analog-value", instance_number=1, name="Old")
        BacnetObject(device=device, object_type="analog-value", instance_number=2, name="Taken")
        session.add(device)
        session.commit()

        parsed = parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [{"object_type": "analog-value", "instance_number": 1, "name": "Taken"}],
                    }
                ]
            }
        )
        sync_objects_for_device(session, device, parsed[0]["objects"], dry_run=False)
        session.commit()

        assert [o.name for o in device.objects] == ["Taken"]


def test_sync_swaps_names_between_two_kept_objects(session_factory):
    """Renames are flushed row by row, so A->B while B->A used to fail the
    per-device unique name partway through (a 500 from the import route)."""
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        BacnetObject(device=device, object_type="analog-value", instance_number=1, name="A")
        BacnetObject(device=device, object_type="analog-value", instance_number=2, name="B")
        session.add(device)
        session.commit()

        parsed = parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [
                            {"object_type": "analog-value", "instance_number": 1, "name": "B"},
                            {"object_type": "analog-value", "instance_number": 2, "name": "A"},
                        ],
                    }
                ]
            }
        )
        sync_objects_for_device(session, device, parsed[0]["objects"], dry_run=False)
        session.commit()

        names = {o.instance_number: o.name for o in device.objects}
        assert names == {1: "B", 2: "A"}


def test_sync_new_object_takes_a_renamed_objects_old_name(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        BacnetObject(device=device, object_type="analog-value", instance_number=1, name="X")
        session.add(device)
        session.commit()

        parsed = parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [
                            {"object_type": "analog-value", "instance_number": 1, "name": "Y"},
                            {"object_type": "analog-value", "instance_number": 2, "name": "X"},
                        ],
                    }
                ]
            }
        )
        sync_objects_for_device(session, device, parsed[0]["objects"], dry_run=False)
        session.commit()

        names = {o.instance_number: o.name for o in device.objects}
        assert names == {1: "Y", 2: "X"}


def test_export_then_parse_round_trips_trend_sample_cap():
    device = Device(id=1, name="Dev-1", vendor_identifier=42)
    device.objects.append(
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", trended=True, trend_sample_cap=500
        )
    )
    parsed = parse_config(export_config([device]))
    assert parsed[0]["objects"][0]["trend_sample_cap"] == 500


@pytest.mark.parametrize("vendor_identifier", [-1, 65536, "acme"])
def test_parse_config_rejects_bad_vendor_identifier(vendor_identifier):
    with pytest.raises(ConfigImportError):
        parse_config({"devices": [{"id": 1, "name": "Dev-1", "vendor_identifier": vendor_identifier}]})


def test_parse_config_rejects_object_named_like_a_trend_log():
    with pytest.raises(ConfigImportError, match="AV-1-Trend"):
        parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [
                            {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "trended": True},
                            {"object_type": "analog-value", "instance_number": 2, "name": "AV-1-Trend"},
                        ],
                    }
                ]
            }
        )


def test_parse_config_rejects_reference_to_an_object_missing_from_the_file():
    """The file is the device's complete object list, so an alarming object
    whose notification class isn't in it would leave the device unable to
    start."""
    with pytest.raises(ConfigImportError, match="notification-class:5"):
        parse_config(
            {
                "devices": [
                    {
                        "id": 1,
                        "name": "Dev-1",
                        "objects": [
                            {
                                "object_type": "analog-input",
                                "instance_number": 1,
                                "name": "AI-1",
                                "units": "degreesFahrenheit",
                                "alarming_enabled": True,
                                "notification_class_instance": 5,
                                "alarm_high_limit": 100.0,
                            }
                        ],
                    }
                ]
            }
        )


# --- BACnet backup/restore (DM-BR-B) --------------------------------------




def _seed(session_factory, device_id=1, name="Plant", bind_address="10.0.0.5/24"):
    with session_factory() as session:
        device = Device(id=device_id, name=name, description="orig", bind_address=bind_address)
        device.objects = [
            BacnetObject(object_type="binary-value", instance_number=1, name="Pump"),
            BacnetObject(object_type="analog-value", instance_number=1, name="Setpoint", units="percent"),
        ]
        session.add(device)
        session.commit()


def test_backup_is_the_export_format_for_one_device(session_factory):
    _seed(session_factory)
    with session_factory() as session:
        device = session.get(Device, 1)
        data = export_device_backup(device)
        assert data == json.dumps(export_config([device], connectors=device.bacnet_connectors), indent=2).encode(
            "utf-8"
        )
    assert [d["id"] for d in json.loads(data)["devices"]] == [1]


def _restore(session_factory, device_id, document):
    data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
    with session_factory() as session:
        lock_both_files(session)  # as the device's BACnet restore does
        summary = apply_device_restore(session, session.get(Device, device_id), data)
        session.commit()
    return summary


def _backup_doc(session_factory, device_id=1):
    with session_factory() as session:
        return json.loads(export_device_backup(session.get(Device, device_id)))


def test_restore_applies_objects_and_device_fields(session_factory):
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    entry = doc["devices"][0]
    entry["description"] = "restored"
    entry["location"] = "restored room"
    entry["backup_failure_timeout"] = 240
    entry["objects"] = [o for o in entry["objects"] if o["name"] != "Pump"]
    entry["objects"].append(
        {"object_type": "binary-value", "instance_number": 7, "name": "Fan"}
    )

    summary = _restore(session_factory, 1, doc)

    assert summary["deleted"] == ["binary-value:1"]
    assert summary["created"] == ["binary-value:7"]
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.description == "restored"
        assert device.location == "restored room"
        assert device.backup_failure_timeout == 240
        assert sorted(o.name for o in device.objects) == ["Fan", "Setpoint"]
        assert device.last_restore_time is not None
        assert device.database_revision > 1


def test_restore_ignores_the_files_id_and_bind_address(session_factory):
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    doc["devices"][0]["id"] = 424242
    doc["devices"][0]["bind_address"] = "192.168.99.9/24"

    _restore(session_factory, 1, doc)

    with session_factory() as session:
        assert session.get(Device, 424242) is None
        assert session.get(Device, 1).bind_address == "10.0.0.5/24"


def test_backup_of_one_device_restores_onto_another(session_factory):
    _seed(session_factory, device_id=1, name="Source")
    with session_factory() as session:
        session.add(Device(id=2, name="Target", bind_address="10.0.0.6/24"))
        session.commit()

    _restore(session_factory, 2, _backup_doc(session_factory, 1))

    with session_factory() as session:
        target = session.get(Device, 2)
        assert target.name == "Source"
        assert target.bind_address == "10.0.0.6/24"
        assert sorted(o.name for o in target.objects) == ["Pump", "Setpoint"]


@pytest.mark.parametrize(
    "data, message",
    [
        (b"\xff\xfe not utf-8", "UTF-8 JSON"),
        (b"{not json", "UTF-8 JSON"),
        (b"[]", "top-level 'devices' list"),
        (json.dumps({"version": 1, "devices": []}).encode(), "exactly one device"),
        (
            json.dumps({"version": 1, "devices": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}).encode(),
            "exactly one device",
        ),
        (
            json.dumps({"version": 2, "devices": [{"id": 1, "name": "A", "objects": []}]}).encode(),
            "format version 2",
        ),
    ],
)
def test_malformed_restore_file_is_rejected(session_factory, data, message):
    _seed(session_factory)
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match=message):
            apply_device_restore(session, session.get(Device, 1), data)


def test_restore_refuses_alarming_without_a_notification_class(session_factory):
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    for obj in doc["devices"][0]["objects"]:
        if obj["name"] == "Setpoint":
            obj.update(alarming_enabled=True, alarm_high_limit=80.0)
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match="notification_class_instance is required"):
            apply_device_restore(session, session.get(Device, 1), json.dumps(doc).encode())


def test_restore_with_an_invalid_object_changes_nothing(session_factory):
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    doc["devices"][0]["name"] = "Should Not Apply"
    doc["devices"][0]["objects"].append(
        {"object_type": "analog-value", "instance_number": 2, "name": "Bad", "units": "notAUnit"}
    )

    with session_factory() as session:
        with pytest.raises(ConfigImportError):
            apply_device_restore(session, session.get(Device, 1), json.dumps(doc).encode())
        session.rollback()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.name == "Plant"
        assert sorted(o.name for o in device.objects) == ["Pump", "Setpoint"]
        assert device.last_restore_time is None
        assert device.database_revision == 1


def test_restore_naming_an_object_like_the_config_file_is_rejected(session_factory):
    # The device would refuse to start with it (object_builder reserves the
    # name for file,1) -- so the restore must fail, not ACK and then crash.
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    doc["devices"][0]["objects"].append(
        {"object_type": "binary-value", "instance_number": 8, "name": "Plant-config.json"}
    )
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match="reserved"):
            apply_device_restore(session, session.get(Device, 1), json.dumps(doc).encode())


def test_restore_renaming_the_device_onto_an_object_name_is_rejected(session_factory):
    _seed(session_factory)
    doc = _backup_doc(session_factory)
    doc["devices"][0]["name"] = "Pump"
    doc["devices"][0]["objects"][0]["name"] = "Pump-config.json"
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match="reserved"):
            apply_device_restore(session, session.get(Device, 1), json.dumps(doc).encode())


def test_parse_config_accepts_a_file_without_reporting_fields():
    from bacpack.config_io import parse_config

    doc = {"devices": [{"id": 1, "name": "D", "objects": [
        {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent", "trended": True},
    ]}]}
    obj = parse_config(doc)[0]["objects"][0]
    assert obj["trend_reporting_enabled"] is False
    assert obj["trend_notification_threshold"] == 10
    assert obj["trend_notify_type"] == "event"


def test_parse_config_refuses_bad_reporting_config():
    import pytest as _pytest

    from bacpack.config_io import parse_config
    from bacpack.config_schemas import ConfigImportError

    base = {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent", "trended": True,
            "trend_reporting_enabled": True, "trend_notification_class_instance": 1}
    nc = {"object_type": "notification-class", "instance_number": 1, "name": "NC-1"}
    for bad, match in [
        ({"trended": False}, "trended"),
        ({"trend_notification_threshold": -1}, "trend_notification_threshold"),
        ({"trend_notify_type": "x"}, "trend_notify_type"),
        ({"trend_event_enable": [True]}, "trend_event_enable"),
        ({"trend_notification_class_instance": 5}, "notification-class:5"),
    ]:
        doc = {"devices": [{"id": 1, "name": "D", "objects": [nc, {**base, **bad}]}]}
        with _pytest.raises(ConfigImportError, match=match):
            parse_config(doc)


def test_parse_config_normalizes_schedule_write_targets():
    schedule = {
        "object_type": "schedule",
        "instance_number": 1,
        "name": "Sched-1",
        "schedule_value_type": "analog",
        "schedule_default": "10.0",
        "weekly_schedule": [[{"time": "08:00", "value": "50.0"}] for _ in range(7)],
        "schedule_write_targets": [
            {"device_instance": 5, "object_type": "analogValue", "instance_number": 3},
            {"device_instance": 1, "object_type": "analog-value", "instance_number": 2},
        ],
    }
    av = {"object_type": "analog-value", "instance_number": 2, "name": "AV-2"}
    [device] = parse_config({"devices": [{"id": 1, "name": "D", "objects": [av, schedule]}]})
    targets = next(o for o in device["objects"] if o["name"] == "Sched-1")["schedule_write_targets"]
    assert targets == [
        {"device_instance": 5, "object_type": "analog-value", "instance_number": 3, "property_identifier": "present-value"},
        {"object_type": "analog-value", "instance_number": 2},
    ]


def test_parse_config_canonicalizes_fixed_choices():
    devices = parse_config(
        {
            "devices": [
                {
                    "id": 1,
                    "name": "Dev-1",
                    "objects": [
                        {"object_type": "Binary-Value", "instance_number": 1, "name": "Fan Cmd",
                         "description": "Keep My Case", "relinquish_default": "Active", "trend_mode": "COV"},
                        {"object_type": "ANALOG_VALUE", "instance_number": 1, "name": "SP", "units": "Percent"},
                    ],
                }
            ]
        }
    )
    bv, av = devices[0]["objects"]
    assert (bv["object_type"], bv["relinquish_default"], bv["trend_mode"]) == ("binary-value", "active", "cov")
    assert (bv["name"], bv["description"]) == ("Fan Cmd", "Keep My Case")
    assert (av["object_type"], av["units"]) == ("analog-value", "percent")


def test_parse_config_reads_null_as_the_default_for_a_not_null_field():
    # A null bacnet_writable/schedule_priority used to reach a NOT
    # NULL column and fail the DB write; it now means the default, the way
    # a missing field (or a null vendor_identifier) already did.
    doc = {"devices": [{"id": 1, "name": "D", "objects": [
        {"object_type": "analog-value", "instance_number": 1, "name": "AV-1",
         "bacnet_writable": None, "trend_mode": None, "description": None},
    ]}]}
    (device,) = parse_config(doc)
    row = device["objects"][0]
    assert row["bacnet_writable"] is True
    assert row["trend_mode"] == "interval"
    assert row["description"] is None


# --- Plan digest (preview/apply race) ---------------------------------------


def _plan(**overrides):
    plan = {
        "devices_created": [599003],
        "devices_updated": [599001, 599002],
        "devices_deleted": [],
        "objects": {599001: {"created": ["analog-value:2"], "updated": ["analog-value:1", "binary-value:1"], "deleted": []}},
    }
    plan.update(overrides)
    return plan


def test_plan_digest_is_stable_and_ignores_list_order():
    from bacpack.config_io import plan_digest

    reordered = _plan(
        devices_updated=[599002, 599001],
        objects={599001: {"created": ["analog-value:2"], "updated": ["binary-value:1", "analog-value:1"], "deleted": []}},
    )
    assert plan_digest(_plan()) == plan_digest(_plan())
    assert plan_digest(_plan()) == plan_digest(reordered)
    assert len(plan_digest(_plan())) == 64


def test_plan_digest_changes_when_the_plan_does():
    from bacpack.config_io import plan_digest

    more_deletes = _plan(objects={599001: {"created": ["analog-value:2"], "updated": ["analog-value:1"], "deleted": ["binary-value:1"]}})
    assert plan_digest(_plan()) != plan_digest(more_deletes)
    assert plan_digest(_plan()) != plan_digest(_plan(devices_deleted=[599004]))


# --- Value simulators ------------------------------------------------------


def _sim_doc(*objects):
    return {"version": 1, "devices": [{"id": 1, "name": "D", "objects": list(objects)}]}


def test_parse_config_carries_and_canonicalizes_simulation():
    (device,) = parse_config(
        _sim_doc(
            {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"},
            {
                "object_type": "analog-value",
                "instance_number": 2,
                "name": "AV-2",
                "simulation": {"kind": "Follow", "source": "analog-value:1"},
            },
        )
    )
    assert device["objects"][0]["simulation"] is None
    assert device["objects"][1]["simulation"] == {"kind": "follow", "source": "analog-value:1"}


@pytest.mark.parametrize(
    "simulation,message",
    [
        ({"kind": "cycle", "dwell_s": 5}, "must be one of"),
        ({"kind": "follow", "source": "analog-value:9"}, "does not exist"),
        ({"kind": "follow", "source": "analog-value:2"}, "itself"),
    ],
)
def test_parse_config_refuses_bad_simulation(simulation, message):
    doc = _sim_doc(
        {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"},
        {"object_type": "analog-value", "instance_number": 2, "name": "AV-2", "simulation": simulation},
    )
    with pytest.raises(ConfigImportError, match=message):
        parse_config(doc)


def test_parse_config_checks_simulation_states_against_state_text():
    doc = _sim_doc(
        {
            "object_type": "multi-state-value",
            "instance_number": 1,
            "name": "MSV-1",
            "state_text": ["A", "B"],
            "simulation": {"kind": "cycle", "dwell_s": 5, "states": [1, 3]},
        }
    )
    with pytest.raises(ConfigImportError, match="above this object's 2 states"):
        parse_config(doc)


def test_object_row_rejects_out_of_range_multistate_value_relinquish_default():
    # MSV is commandable, so the shared row validator behind the
    # JSON import, the CSV import and BACnet restore now checks it.
    from bacpack.config_io import validate_object_row

    row = {
        "object_type": "multi-state-value", "name": "MSV-1", "instance_number": 1,
        "state_text": ["Off", "Low", "High"], "relinquish_default": "9",
    }
    with pytest.raises(ConfigImportError, match="relinquish-default"):
        validate_object_row(row, context="object 1")
    validate_object_row({**row, "relinquish_default": "2"}, context="object 1")


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"recipient": {"kind": "address", "address": "not-an-address"}}, "address"),
        ({"from_time": "nope"}, "from_time"),
        ({"recipient": {"kind": "device", "instance_number": "abc"}}, "instance_number"),
        ({"recipient": {"kind": "device", "instance_number": 99999999}}, "instance_number"),
    ],
)
def test_parse_config_refuses_a_recipient_the_device_cant_build(overrides, field):
    # Only the shape was checked, so the device failed to start.
    recipient = {
        "recipient": {"kind": "address", "address": "192.168.1.200"},
        "valid_days": [True] * 7,
        "from_time": "00:00",
        "to_time": "23:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 1,
        **overrides,
    }
    doc = {"devices": [{"id": 1, "name": "Dev-1", "objects": [{
        "object_type": "notification-class", "instance_number": 1, "name": "NC-1",
        "notification_recipient_list": [recipient],
    }]}]}
    with pytest.raises(ConfigImportError, match=field):
        parse_config(doc)


def test_parse_config_refuses_a_local_write_target_with_an_unknown_property():
    schedule = {
        "object_type": "schedule", "instance_number": 1, "name": "Sched-1",
        "schedule_value_type": "analog", "schedule_default": "10.0",
        "schedule_write_targets": [
            {"object_type": "analog-value", "instance_number": 2, "property_identifier": "fooBar"},
        ],
    }
    av = {"object_type": "analog-value", "instance_number": 2, "name": "AV-2"}
    with pytest.raises(ConfigImportError, match="unknown property"):
        parse_config({"devices": [{"id": 1, "name": "D", "objects": [av, schedule]}]})


def test_parse_config_canonicalizes_a_recipient_kind():
    recipient = {
        "recipient": {"kind": "Device", "instance_number": 5},
        "valid_days": [True] * 7,
        "from_time": "00:00",
        "to_time": "23:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 1,
    }
    [device] = parse_config({"devices": [{"id": 1, "name": "Dev-1", "objects": [{
        "object_type": "notification-class", "instance_number": 1, "name": "NC-1",
        "notification_recipient_list": [recipient],
    }]}]})
    assert device["objects"][0]["notification_recipient_list"][0]["recipient"]["kind"] == "device"





@pytest.fixture
def tokyo_zone():
    """Run with the host zone at UTC+9. time.tzset() is Unix-only, so this
    is skipped on Windows and runs on Linux."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() is Unix-only")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


async def test_restore_time_is_stored_as_utc_whatever_the_host_zone(session_factory, tokyo_zone):
    _seed(session_factory)
    _restore(session_factory, 1, _backup_doc(session_factory))
    with session_factory() as session:
        device = session.get(Device, 1)
        stored = device.last_restore_time
        app = build_application(device, list(device.objects))
    assert abs(timeutil.from_db(stored).timestamp() - time.time()) < 5
    # On the wire: device-local time, i.e. Tokyo wall-clock time here.
    stamp = await app.device_object.read_property("lastRestoreTime")
    date, time_of_day = stamp.dateTime.date, stamp.dateTime.time
    wire = dt.datetime(date[0] + 1900, date[1], date[2], time_of_day[0], time_of_day[1], time_of_day[2])
    assert abs((wire - dt.datetime.now()).total_seconds()) < 120


def test_restore_time_follows_the_device_clock(session_factory):
    _seed(session_factory)
    device_clock.set_offset(86400.0)
    try:
        _restore(session_factory, 1, _backup_doc(session_factory))
    finally:
        device_clock.set_offset(0.0)
    with session_factory() as session:
        stored = session.get(Device, 1).last_restore_time
    assert abs(timeutil.from_db(stored).timestamp() - (time.time() + 86400.0)) < 5


# --- Network role and links -------------------------------------------------


def _net_doc(device_id, name, bind=None, **fields):
    return {"id": device_id, "name": name, "bind_address": bind, **fields}


def _import(session_factory, docs, *, dry_run=False):
    with session_factory() as session:
        lock_both_files(session)
        result = sync_config(session, parse_config({"devices": docs}), dry_run=dry_run)
        session.commit()
    return result


def _bbmd_pair():
    return [
        _net_doc(1, "D", "192.168.1.50/24:47810", network_role="bbmd", bbmd_links=[2], bbmd_peers=["10.0.0.9:47808"]),
        _net_doc(2, "E", "192.168.1.50/24:47811"),
    ]


def test_export_carries_the_network_role_and_links_by_instance(session_factory):
    _import(session_factory, _bbmd_pair())
    with session_factory() as session:
        exported = export_config(session.query(Device).order_by(Device.id))["devices"]
    assert exported[0]["network_role"] == "bbmd"
    assert exported[0]["bbmd_links"] == [2]
    assert exported[0]["bbmd_peers"] == ["10.0.0.9:47808"]
    assert exported[0]["foreign_bbmd"] is None and exported[0]["foreign_ttl"] == 60
    assert exported[1]["network_role"] == "normal" and exported[1]["bbmd_links"] == []


def test_a_bbmd_and_its_link_survive_an_export_and_import(session_factory):
    _import(session_factory, _bbmd_pair())
    with session_factory() as session:
        first = export_config(session.query(Device).order_by(Device.id))
    _import(session_factory, first["devices"])
    with session_factory() as session:
        assert export_config(session.query(Device).order_by(Device.id)) == first


def test_a_file_without_the_network_keys_gives_normal_devices():
    (device,) = parse_config({"devices": [{"id": 1, "name": "A"}]})
    assert device["network_role"] == "normal"
    assert device["foreign_bbmd"] is None and device["foreign_ttl"] == 60
    assert device["bbmd_peers"] is None and device["bbmd_links"] == []


def test_null_network_keys_mean_the_defaults():
    (device,) = parse_config(
        {"devices": [{"id": 1, "name": "A", "network_role": None, "foreign_ttl": None, "bbmd_links": None}]}
    )
    assert (device["network_role"], device["foreign_ttl"], device["bbmd_links"]) == ("normal", 60, [])


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"network_role": "router"}, "network_role"),
        ({"network_role": "foreign"}, "BBMD"),
        ({"network_role": "foreign", "foreign_bbmd": "not-a-host"}, "foreign_bbmd"),
        ({"network_role": "bbmd", "bbmd_peers": ["not-a-peer"]}, "bbmd_peers"),
        ({"network_role": "bbmd", "bbmd_peers": ["10.0.0.5/24:47808"]}, "prefix"),
        ({"foreign_ttl": 0}, "foreign_ttl"),
        ({"bbmd_links": ["x"]}, "bbmd_links"),
        ({"bbmd_links": 5}, "bbmd_links"),
    ],
)
def test_parse_refuses_a_bad_network_setting(fields, message):
    with pytest.raises(ConfigImportError, match=message):
        parse_config({"devices": [{"id": 1, "name": "A", **fields}]})


def test_the_role_ignores_case_in_a_file():
    (device,) = parse_config({"devices": [{"id": 1, "name": "A", "network_role": "BBMD"}]})
    assert device["network_role"] == "bbmd"


def test_an_import_links_a_device_not_yet_on_the_server(session_factory):
    _import(session_factory, _bbmd_pair())
    with session_factory() as session:
        assert [l.linked_device_id for l in session.get(Device, 1).bbmd_links] == [2]
        assert session.get(Device, 1).network_role == "bbmd"


def test_an_import_replaces_and_drops_links(session_factory):
    docs = _bbmd_pair() + [_net_doc(3, "F", "192.168.1.50/24:47812")]
    docs[0]["bbmd_links"] = [2, 3]
    _import(session_factory, docs)
    docs[0]["bbmd_links"] = [3]
    _import(session_factory, docs)
    with session_factory() as session:
        assert [l.linked_device_id for l in session.get(Device, 1).bbmd_links] == [3]


def test_an_import_naming_a_missing_instance_is_refused_naming_both_and_writes_nothing(session_factory):
    _import(session_factory, _bbmd_pair())
    docs = _bbmd_pair()
    docs[0]["bbmd_links"] = [2, 777]
    docs[0]["description"] = "changed"
    for dry_run in (True, False):
        with session_factory() as session:
            lock_both_files(session)
            with pytest.raises(ConfigImportError) as raised:
                sync_config(session, parse_config({"devices": docs}), dry_run=dry_run)
            session.rollback()
        assert "D" in str(raised.value) and "777" in str(raised.value)
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.description is None
        assert [l.linked_device_id for l in device.bbmd_links] == [2]


def test_an_import_linking_a_foreign_device_or_one_on_the_same_network_is_refused(session_factory):
    docs = _bbmd_pair()
    docs[1].update(network_role="foreign", foreign_bbmd="10.0.0.5")
    with pytest.raises(ConfigImportError, match="E .*foreign device"):
        _import(session_factory, docs, dry_run=True)
    docs = _bbmd_pair()
    docs[1]["bind_address"] = "192.168.1.51/24:47810"
    with pytest.raises(ConfigImportError, match="already on this device's network"):
        _import(session_factory, docs, dry_run=True)
    docs[0]["bbmd_links"] = [1]
    with pytest.raises(ConfigImportError, match="itself"):
        _import(session_factory, docs, dry_run=True)


def test_a_dry_run_import_writes_no_links(session_factory):
    _import(session_factory, _bbmd_pair(), dry_run=True)
    with session_factory() as session:
        assert session.query(Device).count() == 0


def test_a_restore_keeps_the_devices_own_network_role_and_links(session_factory):
    _import(session_factory, _bbmd_pair())
    doc = _backup_doc(session_factory, 1)
    entry = doc["devices"][0]
    assert entry["network_role"] == "bbmd"
    entry.update(network_role="foreign", foreign_bbmd="10.0.0.77", bbmd_links=[], bbmd_peers=None, foreign_ttl=5)

    _restore(session_factory, 1, doc)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert (device.network_role, device.foreign_bbmd, device.foreign_ttl) == ("bbmd", None, 60)
        assert device.bbmd_peers == ["10.0.0.9:47808"]
        assert [l.linked_device_id for l in device.bbmd_links] == [2]


def test_a_backup_of_a_linked_bbmd_restores_onto_a_device_on_another_server(session_factory):
    _import(session_factory, _bbmd_pair())
    doc = _backup_doc(session_factory, 1)
    with session_factory() as session:
        session.add(Device(id=9, name="Other", bind_address="192.168.1.50/24:47819"))
        session.commit()

    _restore(session_factory, 9, doc)

    with session_factory() as session:
        other = session.get(Device, 9)
        assert other.network_role == "normal" and other.bbmd_links == []


def test_export_import_carries_binary_text():
    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="binary-value", instance_number=1, name="BV-1", active_text="Running", inactive_text="Stopped"
        )
    )
    [parsed] = parse_config(export_config([device]))
    [row] = parsed["objects"]
    assert (row["active_text"], row["inactive_text"]) == ("Running", "Stopped")


def test_a_config_without_binary_text_imports_it_unset():
    doc = export_config([Device(id=1, name="Dev-1", objects=[BacnetObject(object_type="binary-value", instance_number=1, name="BV-1")])])
    del doc["devices"][0]["objects"][0]["active_text"]
    [row] = parse_config(doc)[0]["objects"]
    assert row["active_text"] is None and row["inactive_text"] is None


def test_export_import_carries_start_value():
    device = Device(id=1, name="Dev-1")
    device.objects.append(BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", start_value="21.5"))
    [row] = parse_config(export_config([device]))[0]["objects"]
    assert row["start_value"] == "21.5"


def test_export_import_carries_trend_log_identity():
    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", trended=True,
            trend_log_instance=77, trend_log_name="Zone Temp Log",
        )
    )
    [row] = parse_config(export_config([device]))[0]["objects"]
    assert (row["trend_log_instance"], row["trend_log_name"]) == (77, "Zone Temp Log")


def test_a_sync_that_creates_a_trended_object_on_a_fixed_instance_id_is_refused(session_factory):
    from bacpack.config_schemas import ConfigImportError

    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        device.objects.append(
            BacnetObject(
                object_type="analog-input", instance_number=1, name="AI-1", trended=True, trend_log_instance=2
            )
        )
        session.add(device)
        session.commit()
        rows = parse_config(export_config([device]))[0]["objects"]
        rows.append({**rows[0], "instance_number": 2, "name": "AI-2", "trend_log_instance": None})
        with pytest.raises(ConfigImportError, match="Trend Log instance"):
            sync_objects_for_device(session, device, rows, dry_run=False)


# A dry run plans new objects' ids as the apply's inserts will get them, so it
# refuses a Trend Log instance clash exactly when the apply would.


def _plan_then_apply(session, run) -> list:
    """`run(dry_run)`'s refusal message, or None, for the dry run and then the apply."""
    outcomes = []
    for dry_run in (True, False):
        if not dry_run:
            lock_both_files(session)
        try:
            run(dry_run)
            outcomes.append(None)
        except ConfigImportError as exc:
            outcomes.append(str(exc))
    session.rollback()
    return outcomes


def _ai(instance, **fields):
    return BacnetObject(object_type="analog-input", instance_number=instance, name=f"AI-{instance}", **fields)


def test_a_sync_dry_run_refuses_a_new_trended_object_on_a_fixed_instance_id(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        device.objects.append(_ai(1, trended=True))
        session.add(device)
        session.commit()
        next_id = device.objects[0].id + 1
        device.objects[0].trend_log_instance = next_id
        session.commit()
        rows = parse_config(export_config([device]))[0]["objects"]
        rows.append({**rows[0], "instance_number": 2, "name": "AI-2", "trend_log_instance": None})

        dry, applied = _plan_then_apply(session, lambda dry_run: sync_objects_for_device(session, device, rows, dry_run=dry_run))

    assert dry is not None and "Trend Log instance" in dry
    assert applied == dry


@pytest.mark.parametrize("fixed_offset, refused", [(0, True), (1, False)])
def test_a_sync_dry_run_gives_a_new_object_the_id_a_delete_frees(session_factory, fixed_offset, refused):
    with session_factory() as session:
        device = Device(id=1, name="Dev-1")
        device.objects.extend([_ai(1, trended=True), _ai(2)])
        session.add(device)
        session.commit()
        dropped_id = device.objects[1].id  # the largest id: the new object takes it again
        device.objects[0].trend_log_instance = dropped_id + fixed_offset
        session.commit()
        kept = parse_config(export_config([device]))[0]["objects"][0]
        rows = [kept, {**kept, "instance_number": 3, "name": "AI-3", "trend_log_instance": None}]

        outcomes = _plan_then_apply(session, lambda dry_run: sync_objects_for_device(session, device, rows, dry_run=dry_run))

    assert [outcome is not None for outcome in outcomes] == [refused, refused]


@pytest.mark.parametrize("fixed_offset, refused", [(1, False), (2, True)])
def test_a_config_dry_run_counts_the_ids_earlier_devices_take(session_factory, fixed_offset, refused):
    with session_factory() as session:
        first, second = Device(id=1, name="Dev-1"), Device(id=2, name="Dev-2")
        first.objects.append(_ai(1))
        second.objects.append(_ai(1, trended=True))
        session.add_all([first, second])
        session.commit()
        largest = max(first.objects[0].id, second.objects[0].id)
        second.objects[0].trend_log_instance = largest + fixed_offset
        session.commit()
        docs = export_config([first, second])["devices"]
        docs[0]["objects"].append({**docs[0]["objects"][0], "instance_number": 2, "name": "AI-2"})
        docs[1]["objects"].append(
            {**docs[1]["objects"][0], "instance_number": 2, "name": "AI-2", "trend_log_instance": None}
        )
        parsed = parse_config({**export_config([]), "devices": docs})

        outcomes = _plan_then_apply(session, lambda dry_run: sync_config(session, parsed, dry_run=dry_run))

    assert [outcome is not None for outcome in outcomes] == [refused, refused]


# --- create_devices: add-only ----------------------------------------------------

def _create_doc():
    return {"devices": [
        {"id": 10, "name": "New A", "bind_address": "127.0.0.1/8:52601", "network_role": "bbmd", "bbmd_links": [11],
         "objects": [{"object_type": "analog-value", "instance_number": 1, "name": "AV"}]},
        {"id": 11, "name": "New B", "bind_address": "127.0.0.1/8:52602",
         "objects": [{"object_type": "analog-value", "instance_number": 1, "name": "AV"}]},
    ]}


def test_create_devices_adds_new_devices_and_leaves_the_others_alone(session_factory):
    from bacpack.config_io import create_devices

    with session_factory() as session:
        old = Device(id=1, name="Old")
        session.add(old)
        session.add(BacnetObject(device=old, object_type="analog-value", instance_number=1, name="Keep"))
        session.commit()
    with session_factory() as session:
        lock_both_files(session)
        result = create_devices(session, parse_config(_create_doc()))
        session.commit()
    assert result["devices_created"] == [10, 11]
    with session_factory() as session:
        assert [o.name for o in session.get(Device, 1).objects] == ["Keep"]
        assert [o.name for o in session.get(Device, 10).objects] == ["AV"]
        assert [link.linked_device_id for link in session.get(Device, 10).bbmd_links] == [11]


def test_create_devices_refuses_an_existing_id_and_adds_nothing(session_factory):
    from bacpack.config_io import create_devices

    with session_factory() as session:
        session.add(Device(id=11, name="Taken"))
        session.commit()
    with session_factory() as session:
        lock_both_files(session)
        with pytest.raises(ConfigImportError, match="11"):
            create_devices(session, parse_config(_create_doc()))
        session.rollback()
        assert session.get(Device, 10) is None


def test_create_devices_checks_the_links(session_factory):
    from bacpack.config_io import create_devices

    missing = _create_doc()
    missing["devices"][0]["bbmd_links"] = [99]
    same_socket = _create_doc()
    same_socket["devices"][1]["bind_address"] = "127.0.0.1/8:52601"
    for doc in (missing, same_socket):
        with session_factory() as session:
            lock_both_files(session)
            with pytest.raises(ConfigImportError):
                create_devices(session, parse_config(doc))
            session.rollback()


def test_create_devices_refuses_two_trend_logs_with_one_instance(session_factory):
    from bacpack.config_io import create_devices

    objects = [
        {"object_type": "analog-value", "instance_number": n, "name": f"AV{n}", "trended": True, "trend_log_instance": 5}
        for n in (1, 2)
    ]
    doc = {"devices": [{"id": 10, "name": "New A", "objects": objects}]}
    with session_factory() as session:
        lock_both_files(session)
        with pytest.raises(ConfigImportError):
            create_devices(session, parse_config(doc))
        session.rollback()


def test_parse_config_refuses_a_trended_notification_class():
    import pytest as _pytest

    from bacpack.config_io import parse_config
    from bacpack.config_schemas import ConfigImportError

    nc = {"object_type": "notification-class", "instance_number": 1, "name": "NC-1", "trended": True}
    with _pytest.raises(ConfigImportError, match="Notification Class can't be trended"):
        parse_config({"devices": [{"id": 1, "name": "D", "objects": [nc]}]})
