"""Per-device CSV object import/export and whole-server JSON config
import/export."""
from __future__ import annotations

import json

import pytest

from tests.web._helpers import _create_connector, _create_device, _create_object, _create_schedule_object


def test_csv_export_import_round_trip(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    _create_object(web_client, object_type="analog-input", instance_number=1, name="AI-1")

    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text

    _create_device(web_client, device_id=599002, name="Second Device")
    r = web_client.post(
        "/api/devices/599002/objects/import",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    assert len(web_client.get("/api/devices/599002/objects").json()) == 2


def test_csv_import_rejects_non_utf8_file(web_client):
    """Found live (via a real binary-garbage upload): a non-UTF-8 file
    used to raise UnicodeDecodeError straight out of the route uncaught,
    500ing instead of failing cleanly.
    """
    _create_device(web_client)
    garbage = bytes([0x90, 0xFF, 0xFE, 0x00, 0x01, 0x80])  # not valid UTF-8
    r = web_client.post(
        "/api/devices/599001/objects/import",
        files={"file": ("garbage.bin", garbage, "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "utf-8" in r.json()["detail"].lower()


def test_json_config_import_rejects_non_utf8_file(web_client):
    # json.loads accepts bytes directly and UnicodeDecodeError is itself a
    # ValueError subclass, so import_config_route's existing `except
    # ValueError` already covers this -- verified rather than assumed.
    garbage = bytes([0x90, 0xFF, 0xFE, 0x00, 0x01, 0x80])
    r = web_client.post(
        "/api/config/import",
        files={"file": ("garbage.bin", garbage, "application/octet-stream")},
    )
    assert r.status_code == 400


def test_csv_import_rejects_duplicate_against_existing(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text

    r = web_client.post(
        "/api/devices/599001/objects/import",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 409


def test_csv_import_sync_mode_deletes_objects_missing_from_file(web_client):
    # mode=sync (added for file-based config) makes the file the device's
    # complete object list -- unlike the default merge mode above, an
    # object missing from the file gets deleted, not left alone.
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    _create_object(web_client, name="AV-2", instance_number=2)

    # A file containing only AV-1, renamed.
    csv_text = (
        "object_type,instance_number,name,description,units,bacnet_writable,"
        "out_of_service_default,relinquish_default,state_text,"
        "schedule_value_type,schedule_default,weekly_schedule,exception_schedule,"
        "schedule_effective_start,schedule_effective_end,schedule_priority,"
        "schedule_write_targets,date_list\n"
        "analog-value,1,AV-1-renamed,,,false,false,,,,,,,,,,,\n"
    )
    r = web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"created": [], "updated": ["analog-value:1"], "deleted": ["analog-value:2"]}

    remaining = web_client.get("/api/devices/599001/objects").json()
    assert [o["name"] for o in remaining] == ["AV-1-renamed"]


# --- Whole-server JSON config (file-based config) --------------------------


def test_config_export_import_round_trip(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")

    doc = web_client.get("/api/config/export").json()
    assert doc["version"] == 1
    assert len(doc["devices"]) == 1
    assert doc["devices"][0]["id"] == 599001
    assert len(doc["devices"][0]["objects"]) == 1


def test_config_export_carries_settings_and_connectors(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1")
    connector = _create_connector(web_client, name="Weather", headers={"X-Api-Key": "secret"}, auth_password="pw")
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"], "json_path": "a.b"}
    )
    assert r.status_code == 201, r.text

    doc = web_client.get("/api/config/export").json()

    assert doc["settings"] == {"trend_retention_days": 90, "log_retention_days": 14, "trend_soft_limit_mb": None}
    (exported,) = doc["connectors"]
    assert exported["name"] == "Weather"
    assert exported["headers"] == {"X-Api-Key": None}
    assert exported["mappings"] == [
        {"device": 599001, "object": "analog-value:1", "json_path": "a.b", "scale": 1.0, "offset": 0.0, "value_map": None}
    ]
    assert "secret" not in json.dumps(doc) and "pw" not in json.dumps(doc["connectors"])


def test_config_export_import_round_trip_preserves_bind_address(web_client):
    # A per-device bind_address used to be silently dropped by config export/
    # import (config_io.py didn't know the column either).
    web_client.post(
        "/api/devices",
        json={"id": 599001, "name": "Demo Device", "bind_address": "192.168.1.50/24"},
    )

    doc = web_client.get("/api/config/export").json()
    assert doc["devices"][0]["bind_address"] == "192.168.1.50/24"

    r = web_client.delete("/api/devices/599001")
    assert r.status_code == 204

    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text

    r = web_client.get("/api/devices/599001")
    assert r.json()["bind_address"] == "192.168.1.50/24"


def test_config_import_dry_run_by_default_does_not_write(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")

    doc = {"version": 1, "devices": [{"id": 599001, "name": "Renamed", "objects": []}]}
    r = web_client.post(
        "/api/config/import",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["devices_updated"] == [599001]
    assert body["objects"]["599001"]["deleted"] == ["analog-value:1"]

    # Nothing actually changed -- still the original name and object.
    device = web_client.get("/api/devices/599001").json()
    assert device["name"] == "Demo Device"
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


def test_config_import_commit_creates_updates_and_deletes(web_client):
    _create_device(web_client, device_id=599001, name="Keep")
    _create_object(web_client, device_id=599001, name="AV-1", instance_number=1)
    _create_device(web_client, device_id=599002, name="Drop")

    doc = {
        "version": 1,
        "devices": [
            {"id": 599001, "name": "Keep-Renamed", "objects": []},
            {"id": 599003, "name": "New", "objects": []},
        ],
    }
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert body["devices_created"] == [599003]
    assert body["devices_updated"] == [599001]
    assert body["devices_deleted"] == [599002]

    remaining_ids = {d["id"] for d in web_client.get("/api/devices").json()}
    assert remaining_ids == {599001, 599003}
    assert web_client.get("/api/devices/599001").json()["name"] == "Keep-Renamed"
    # AV-1 wasn't in the file's object list for 599001 -- deleted.
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_config_import_rejects_invalid_config_without_writing(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")

    doc = {
        "version": 1,
        "devices": [
            {
                "id": 599001,
                "name": "Demo Device",
                "objects": [{"object_type": "not-a-type", "instance_number": 1, "name": "X"}],
            }
        ],
    }
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 400
    # Untouched -- the whole file was rejected before anything was written.
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


def test_config_import_stops_engine_before_deleting_active_device(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    web_client.post("/api/engine/start", json={"device_id": 599001})

    doc = {"version": 1, "devices": []}  # deletes the only (and active) device
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text

    # The config import above deleted device 599001 outright (its "devices"
    # list was empty), so there's no row left to inspect for a "stopped"
    # process_status -- the meaningful assertion here is that it's gone
    # entirely.
    status = web_client.get("/api/engine/status").json()
    assert all(row["device_id"] != 599001 for row in status)
    # The DB-row check above doesn't actually depend on stop() having run --
    # sync_config's session.delete(device) succeeds either way. The signal
    # that DOES depend on it is the manager's own in-memory tracking: this
    # fails if the stop-before-delete loop in import_config_route were ever
    # removed.
    assert not web_client.app.state.engine.is_running(599001)


def test_csv_import_rejects_object_named_like_an_existing_trend_log(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True)
    csv_text = (
        "object_type,instance_number,name,description,units,bacnet_writable,"
        "out_of_service_default,relinquish_default,state_text,"
        "schedule_value_type,schedule_default,weekly_schedule,exception_schedule,"
        "schedule_effective_start,schedule_effective_end,schedule_priority,"
        "schedule_write_targets,date_list\n"
        "analog-value,2,AV-1-Trend,,,false,false,,,,,,,,,,,\n"
    )

    r = web_client.post(
        "/api/devices/599001/objects/import",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 409
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


# --- Reserved backup-file name, and JSON import type checks ------------------
# "<device name>-config.json" is file,1's name: an object holding it can't
# start (object_builder refuses), so every save path refuses it up front.


def _config_doc(device=None, obj=None):
    row = {"object_type": "analog-value", "instance_number": 1, "name": "AV-1"}
    row.update(obj or {})
    entry = {"id": 599001, "name": "Demo Device", "objects": [row]}
    entry.update(device or {})
    return {"version": 1, "devices": [entry]}


def _import_config(web_client, doc, dry_run="false"):
    return web_client.post(
        f"/api/config/import?dry_run={dry_run}",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )


def test_csv_import_refuses_the_reserved_backup_file_name(web_client):
    _create_device(web_client)
    created = _create_object(web_client, name="AV-1")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text.replace("AV-1", "Demo Device-config.json")
    web_client.delete(f"/api/devices/599001/objects/{created['id']}")
    for mode in ("merge", "sync"):
        r = web_client.post(
            f"/api/devices/599001/objects/import?mode={mode}",
            files={"file": ("objects.csv", csv_text, "text/csv")},
        )
        assert r.status_code == 409, (mode, r.text)
        assert "reserved" in r.json()["detail"]


def test_json_config_import_refuses_the_reserved_backup_file_name(web_client):
    r = _import_config(web_client, _config_doc(obj={"name": "Demo Device-config.json"}), dry_run="true")
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]


def test_csv_import_refuses_an_object_named_like_the_device(web_client):
    _create_device(web_client)
    created = _create_object(web_client, name="AV-1")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text.replace("AV-1", "Demo Device")
    web_client.delete(f"/api/devices/599001/objects/{created['id']}")
    for mode in ("merge", "sync"):
        r = web_client.post(
            f"/api/devices/599001/objects/import?mode={mode}",
            files={"file": ("objects.csv", csv_text, "text/csv")},
        )
        assert r.status_code == 409, (mode, r.text)
        assert "device's own name" in r.json()["detail"]


def test_json_config_import_refuses_an_object_named_like_the_device(web_client):
    r = _import_config(web_client, _config_doc(obj={"name": "Demo Device"}), dry_run="true")
    assert r.status_code == 400
    assert "device's own name" in r.json()["detail"]


def test_json_config_import_refuses_a_bad_bind_address(web_client):
    r = _import_config(web_client, _config_doc(device={"bind_address": "garbage"}))
    assert r.status_code == 400
    assert "bind_address" in r.json()["detail"]
    assert web_client.get("/api/devices").json() == []


def test_json_config_import_accepts_a_good_bind_address(web_client):
    r = _import_config(web_client, _config_doc(device={"bind_address": "127.0.0.1/8"}))
    assert r.status_code == 200, r.text


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "device, obj, field",
    [
        ({"name": 5}, None, "name"),
        ({"description": ["x"]}, None, "description"),
        ({"vendor_name": {"a": 1}}, None, "vendor_name"),
        (None, {"name": 7}, "name"),
        (None, {"trended": "no"}, "trended"),
        (None, {"bacnet_writable": "no"}, "bacnet_writable"),
        (None, {"description": {"a": 1}}, "description"),
        (None, {"alarm_deadband": "x"}, "alarm_deadband"),
        (None, {"trended": True, "trend_interval_seconds": 0}, "trend_interval_seconds"),
        (None, {"instance_number": True}, "instance_number"),
    ],
)
def test_json_config_import_refuses_wrongly_typed_fields(web_client, device, obj, field):
    # Each of these used to be accepted by parse_config and then either 500
    # on the DB write or store a value the device couldn't start with.
    r = _import_config(web_client, _config_doc(device, obj))
    assert r.status_code == 400, r.text
    assert field in r.json()["detail"]
    assert web_client.get("/api/devices").json() == []


def test_json_config_export_still_imports(web_client):
    # The strict type check must accept everything the export itself writes.
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    _create_object(web_client, object_type="multi-state-value", instance_number=1, name="MSV-1",
                   state_text=["a", "b"], relinquish_default="2")
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "calendar", "instance_number": 1, "name": "Cal-1",
              "date_list": [{"kind": "date", "month": 12, "day": 25}]},
    )
    _create_object(web_client, instance_number=2, name="AV-2")
    r = _create_schedule_object(
        web_client, schedule_write_targets=[{"object_type": "analog-value", "instance_number": 2}],
        exception_schedule=[{"period": {"kind": "date", "month": 1, "day": 1}, "priority": 8,
                             "time_values": [{"time": "00:00", "value": "1.0"}]}],
    )
    assert r.status_code == 201, r.text
    doc = web_client.get("/api/config/export").json()
    r = _import_config(web_client, doc)
    assert r.status_code == 200, r.text


# --- CSV carries every JSON config field ---------------------------

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


def _import_csv(web_client, csv_text, *, device_id=599001, mode="merge"):
    return web_client.post(
        f"/api/devices/{device_id}/objects/import?mode={mode}",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )


def _create_every_feature(web_client):
    _create_object(
        web_client,
        object_type="notification-class",
        instance_number=1,
        name="NC-1",
        units=None,
        notification_priority=[50, 60, 70],
        notification_ack_required=[True, False, True],
        notification_recipient_list=_RECIPIENTS,
    )
    _create_object(
        web_client,
        name="AV-1",
        description='Zone "A", north; east',
        trended=True,
        trend_interval_seconds=60,
        trend_sample_cap=500,
        trend_reporting_enabled=True,
        trend_notification_class_instance=1,
        trend_notification_threshold=5,
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=80.5,
        alarm_low_limit=10.0,
        alarm_deadband=1.5,
        alarm_limit_enable=[True, True],
        alarm_event_enable=[True, False, True],
        alarm_time_delay=5,
    )
    _create_object(
        web_client,
        object_type="binary-value",
        name="BV-1",
        units=None,
        relinquish_default="active",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_value=True,
        simulation={"kind": "square", "period_s": 60},
    )
    _create_object(
        web_client,
        object_type="multi-state-value",
        name="MSV-1",
        units=None,
        state_text=["Off", "Low", "High"],
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_values=[2, 3],
    )
    assert _create_schedule_object(web_client, schedule_priority=10).status_code == 201
    _create_object(
        web_client,
        object_type="calendar",
        name="Cal-1",
        units=None,
        date_list=[{"kind": "date", "year": "*", "month": 12, "day": 25}],
    )
    _create_object(
        web_client,
        instance_number=2,
        name="AV-2",
        simulation={"kind": "follow", "source": "analog-value:1", "scale": 2, "min": 0},
    )


def _objects_by_identifier(doc, device_id):
    (device,) = [d for d in doc["devices"] if d["id"] == device_id]
    return {(o["object_type"], o["instance_number"]): o for o in device["objects"]}


def test_csv_round_trip_matches_the_json_config(web_client):
    _create_device(web_client)
    _create_every_feature(web_client)
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text

    _create_device(web_client, device_id=599002, name="Second Device")
    r = _import_csv(web_client, csv_text, device_id=599002, mode="sync")
    assert r.status_code == 200, r.text

    doc = web_client.get("/api/config/export").json()
    assert _objects_by_identifier(doc, 599002) == _objects_by_identifier(doc, 599001)


def test_csv_merge_creates_alarming_objects_from_csv(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="notification-class", instance_number=1, name="NC-1", units=None)
    csv_text = (
        "object_type,instance_number,name,units,alarming_enabled,notification_class_instance,alarm_high_limit\n"
        "analog-value,1,AV-1,percent,true,1,80\n"
    )
    r = _import_csv(web_client, csv_text)
    assert r.status_code == 200, r.text
    (av,) = [o for o in web_client.get("/api/devices/599001/objects").json() if o["name"] == "AV-1"]
    assert (av["alarming_enabled"], av["alarm_high_limit"]) == (True, 80.0)


def test_csv_import_refuses_what_json_import_refuses(web_client):
    _create_device(web_client)
    csv_text = (
        "object_type,instance_number,name,alarming_enabled,notification_class_instance,alarm_limit_enable,alarm_low_limit\n"
        "analog-value,1,AV-1,true,1,true;true,10\n"
    )
    for mode in ("merge", "sync"):
        r = _import_csv(web_client, csv_text, mode=mode)
        assert r.status_code == 400, (mode, r.text)
        assert r.json()["detail"].startswith("row 2: A limit is enabled")
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_csv_sync_with_a_partial_sheet_keeps_the_other_settings(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="notification-class", instance_number=1, name="NC-1", units=None)
    _create_object(
        web_client,
        name="AV-1",
        trended=True,
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=80.0,
        alarm_deadband=2.0,
    )
    csv_text = (
        "object_type,instance_number,name,alarm_high_limit\n"
        "notification-class,1,NC-1,\n"
        "analog-value,1,AV-1,90\n"
    )
    r = _import_csv(web_client, csv_text, mode="sync")
    assert r.status_code == 200, r.text
    (av,) = [o for o in web_client.get("/api/devices/599001/objects").json() if o["name"] == "AV-1"]
    assert (av["alarm_high_limit"], av["alarm_deadband"], av["alarming_enabled"], av["trended"]) == (90.0, 2.0, True, True)


def test_csv_sync_partial_row_checked_against_the_existing_object(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="notification-class", instance_number=1, name="NC-1", units=None)
    _create_object(web_client, name="AV-1", alarming_enabled=True, notification_class_instance=1, alarm_high_limit=80.0)
    csv_text = (
        "object_type,instance_number,name,alarm_high_limit\n"
        "notification-class,1,NC-1,\n"
        "analog-value,1,AV-1,\n"
    )
    r = _import_csv(web_client, csv_text, mode="sync")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("row 3: Set a high limit or a low limit")


def test_csv_sync_without_write_targets_column_keeps_them(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    targets = [{"object_type": "analog-value", "instance_number": 1, "property_identifier": "presentValue"}]
    r = _create_schedule_object(web_client, schedule_write_targets=targets)
    assert r.status_code == 201, r.text
    stored = r.json()["schedule_write_targets"]
    assert stored
    csv_text = "object_type,instance_number,name\nanalog-value,1,AV-1\nschedule,1,Sched-1\n"
    r = _import_csv(web_client, csv_text, mode="sync")
    assert r.status_code == 200, r.text
    (schedule,) = [o for o in web_client.get("/api/devices/599001/objects").json() if o["name"] == "Sched-1"]
    assert schedule["schedule_write_targets"] == stored


def test_csv_with_old_0_7_0_columns_still_imports(web_client):
    _create_device(web_client)
    csv_text = (
        "object_type,instance_number,name,description,units,bacnet_writable,"
        "out_of_service_default,relinquish_default,state_text,"
        "schedule_value_type,schedule_default,weekly_schedule,exception_schedule,"
        "schedule_effective_start,schedule_effective_end,schedule_priority,"
        "schedule_write_targets,date_list\n"
        "analog-value,1,AV-1,,percent,true,false,,,,,,,,,,,\n"
    )
    r = _import_csv(web_client, csv_text)
    assert r.status_code == 200, r.text
    (av,) = web_client.get("/api/devices/599001/objects").json()
    assert (av["units"], av["trended"], av["alarming_enabled"]) == ("percent", False, False)


def test_csv_creates_a_multistate_point_with_the_default_states(web_client):
    _create_device(web_client)
    r = _import_csv(web_client, "object_type,instance_number,name\nMSV,1,Occ Mode\n")
    assert r.status_code == 200, r.text
    (obj,) = web_client.get("/api/devices/599001/objects").json()
    assert obj["state_text"] == ["State 1", "State 2"]


def test_csv_sync_blank_states_on_an_existing_multistate_is_still_refused(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="multi-state-value", name="Occ Mode", state_text=["Occ", "Unocc"])
    r = _import_csv(web_client, "object_type,instance_number,name,state_text\nMSV,1,Occ Mode,\n", mode="sync")
    assert r.status_code == 400
    assert "requires state_text" in r.json()["detail"]
    (obj,) = web_client.get("/api/devices/599001/objects").json()
    assert obj["state_text"] == ["Occ", "Unocc"]


_POINTS_LIST ="Panel,object_type,instance_number,name,Notes\nP-1,AI,1,Zone Temp,spare\n"


@pytest.mark.parametrize("mode", ["merge", "sync"])
def test_csv_preview_names_the_skipped_columns(web_client, mode):
    _create_device(web_client)
    r = web_client.post(
        f"/api/devices/599001/objects/import?mode={mode}&dry_run=true",
        files={"file": ("objects.csv", _POINTS_LIST, "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["skipped_columns"] == ["Panel", "Notes"]
    assert r.json()["created"] == ["analog-input:1"]

    digest = r.json()["plan_digest"]
    r = web_client.post(
        f"/api/devices/599001/objects/import?mode={mode}&plan_digest={digest}",
        files={"file": ("objects.csv", _POINTS_LIST, "text/csv")},
    )
    assert r.status_code == 200, r.text
    (obj,) = web_client.get("/api/devices/599001/objects").json()
    assert (obj["object_type"], obj["name"]) == ("analog-input", "Zone Temp")


# --- Integer bounds -------------------------------------------------------


def test_config_import_refuses_an_int_past_64_bits(web_client):
    _create_device(web_client)
    doc = {"version": 1, "devices": [{"id": 599001, "name": "Demo Device", "objects": [
        {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "trend_sample_cap": 2**64},
    ]}]}
    r = _import_config(web_client, doc)
    assert r.status_code == 400, r.text
    assert "trend_sample_cap" in r.json()["detail"]


def test_csv_import_refuses_an_int_past_64_bits(web_client):
    _create_device(web_client)
    r = _import_csv(
        web_client,
        f"object_type,instance_number,name,alarm_time_delay\nanalog-value,1,AV-1,{2**64}\n",
    )
    assert r.status_code == 400, r.text
    assert "alarm_time_delay" in r.json()["detail"]
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_csv_import_refuses_a_negative_time_delay(web_client):
    _create_device(web_client)
    r = _import_csv(web_client, "object_type,instance_number,name,alarm_time_delay\nanalog-value,1,AV-1,-5\n")
    assert r.status_code == 400, r.text
    assert "alarm_time_delay" in r.json()["detail"]


def test_imports_refuse_a_spaces_only_name(web_client):
    _create_device(web_client)
    doc = {"version": 1, "devices": [{"id": 599002, "name": "   ", "objects": []}]}
    r = web_client.post(
        "/api/config/import?dry_run=true",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 400

    _create_object(web_client, name="AV-1")
    header, row = web_client.get("/api/devices/599001/objects/export.csv").text.splitlines()[:2]
    assert "AV-1" in row
    # Into a fresh device, so only the blank name can be what's refused.
    _create_device(web_client, device_id=599003, name="Empty Device")
    r = web_client.post(
        "/api/devices/599003/objects/import",
        files={"file": ("objects.csv", f"{header}\n{row.replace('AV-1', '   ', 1)}\n", "text/csv")},
    )
    assert r.status_code == 400
    assert web_client.get("/api/devices/599003/objects").json() == []


# --- upload cap, event loop, deep JSON --------------------------------------

import threading

import pytest

from bacpack.web.api import _uploads, object_io

_MIB = 1024 * 1024


@pytest.fixture
def one_mib_limit(monkeypatch):
    """The upload limit lowered to 1 MiB, so the boundary tests needn't send
    a full-size body."""
    monkeypatch.setattr(_uploads, "MAX_UPLOAD_BYTES", _MIB)


def test_an_oversized_csv_upload_gets_413(web_client, one_mib_limit):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects/import", files={"file": ("big.csv", b"a" * (_MIB + 1), "text/csv")}
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "file larger than 1 MiB"


def test_an_oversized_config_upload_gets_413(web_client, one_mib_limit):
    r = web_client.post(
        "/api/config/import", files={"file": ("big.json", b"a" * (_MIB + 1), "application/json")}
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "file larger than 1 MiB"


def test_a_big_installs_config_upload_is_not_refused_for_size(web_client):
    # A 50-device install with 9,900 objects exports about 17 MB.
    big = b"a" * (20 * _MIB)
    r = web_client.post("/api/config/import", files={"file": ("big.json", big, "application/json")})
    assert r.status_code == 400  # refused as bad JSON, not for its size


def test_an_upload_at_the_cap_is_not_refused_for_size(web_client, one_mib_limit):
    _create_device(web_client)
    # Fields under the csv module's 128 KiB field limit, so the file is
    # refused as a bad CSV rather than tripping that limit.
    at_cap = ((b"a" * 100_000 + b",") * 11)[: _uploads.MAX_UPLOAD_BYTES]
    assert len(at_cap) == _uploads.MAX_UPLOAD_BYTES
    r = web_client.post(
        "/api/devices/599001/objects/import", files={"file": ("cap.csv", at_cap, "text/csv")}
    )
    assert r.status_code == 400  # refused as a bad CSV, not for its size


def test_deeply_nested_config_json_gets_400(web_client):
    deep = "[" * 100_000 + "]" * 100_000
    r = web_client.post("/api/config/import", files={"file": ("deep.json", deep, "application/json")})
    assert r.status_code == 400
    # Whether json raises RecursionError at this depth varies by Python build;
    # when it decodes, the shape check refuses the file instead.
    assert r.json()["detail"] in (
        "not valid JSON: nested too deeply",
        "config file must be an object with a top-level 'devices' list",
    )


def _blocking(monkeypatch, name):
    """Patch object_io.<name> to block until released, so a test can check
    whether another request is served meanwhile."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(object_io, name)

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(object_io, name, blocked)
    return entered, release


@pytest.mark.parametrize(
    "patched, url, upload",
    [
        ("read_objects_csv", "/api/devices/599001/objects/import", ("o.csv", b"object_type\n", "text/csv")),
        ("parse_config_document", "/api/config/import", ("c.json", b'{"devices": []}', "application/json")),
    ],
    ids=["csv", "config"],
)
def test_a_slow_import_does_not_hold_up_other_requests(web_client, monkeypatch, patched, url, upload):
    _create_device(web_client)
    entered, release = _blocking(monkeypatch, patched)
    importer = threading.Thread(target=lambda: web_client.post(url, files={"file": upload}))
    importer.start()
    try:
        assert entered.wait(5)
        answered = {}
        reader = threading.Thread(target=lambda: answered.update(r=web_client.get("/api/version")))
        reader.start()
        reader.join(3)
        assert "r" in answered and answered["r"].status_code == 200
    finally:
        release.set()
        importer.join(10)


def test_deeply_nested_json_in_a_csv_cell_gets_400(web_client):
    # A CSV's JSON cells (date_list etc.) call json.loads too.
    _create_device(web_client)
    deep = "[" * 50_000 + "]" * 50_000  # under csv's 128 KiB field limit
    csv_text = f"object_type,instance_number,name,date_list\ncalendar,1,CAL-1,{deep}\n"
    r = web_client.post(
        "/api/devices/599001/objects/import", files={"file": ("deep.csv", csv_text, "text/csv")}
    )
    # 400 either way: Python 3.13's json raises RecursionError here ("nested
    # too deeply"); 3.14's decodes it and the row fails validation instead.
    assert r.status_code == 400


# --- Plan digest closes the preview/apply race ------------------------------
# A dry run returns plan_digest; Apply sends it back and the server refuses
# (409, nothing changed) if its own plan no longer matches the preview.


def _preview_digest(web_client, doc):
    r = _import_config(web_client, doc, dry_run="true")
    assert r.status_code == 200, r.text
    return r.json()["plan_digest"]


def _apply_with_digest(web_client, doc, digest):
    return web_client.post(
        "/api/config/import",
        params={"dry_run": "false", "plan_digest": digest},
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )


def _doc_with_connector(connector=None, **extra):
    http = {
        "name": "Weather",
        "type": "http-poll",
        "url": "https://example.invalid/forecast",
        "auth_type": "bearer",
        "headers": {"X-Api-Key": None},
        "mappings": [{"device": 599001, "object": "analog-value:1", "json_path": "a.b"}],
    }
    doc = _config_doc()
    doc["connectors"] = [{**http, **(connector or {})}]
    doc.update(extra)
    return doc


def test_config_import_preview_lists_connectors_and_credentials(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")

    r = _import_config(web_client, _doc_with_connector(settings={"trend_retention_days": 30}), dry_run="true")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connectors_created"] == ["Weather"]
    assert body["connectors_updated"] == []
    assert body["connector_mappings"] == {
        "Weather": {"created": ["599001 analog-value:1"], "updated": [], "deleted": []}
    }
    assert body["needs_credentials"] == {"Weather": ["token", "header X-Api-Key"]}
    assert body["settings_changed"] is True
    assert web_client.get("/api/connectors").json() == []
    assert web_client.get("/api/settings/network").json()["trend_retention_days"] == 90


def test_config_import_apply_creates_connectors(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")

    r = _import_config(web_client, _doc_with_connector(settings={"trend_retention_days": 30}))

    assert r.status_code == 200, r.text
    (connector,) = web_client.get("/api/connectors").json()
    assert (connector["name"], connector["mapping_count"], connector["header_names"]) == ("Weather", 1, ["X-Api-Key"])
    assert web_client.get("/api/settings/network").json()["trend_retention_days"] == 30
    # The export now carries what was imported.
    exported = web_client.get("/api/config/export").json()
    assert exported["connectors"][0]["mappings"][0]["object"] == "analog-value:1"


def test_config_import_refuses_two_sources_with_409(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1")
    other = _create_connector(web_client, name="Other")
    web_client.post(f"/api/connectors/{other['id']}/mappings", json={"object_id": obj["id"], "json_path": "x"})

    for dry_run in ("true", "false"):
        r = _import_config(web_client, _doc_with_connector(), dry_run=dry_run)
        assert r.status_code == 409, r.text
        assert "599001 analog-value:1 would be fed by both 'Weather' and 'Other'" in r.json()["detail"]
    assert [c["name"] for c in web_client.get("/api/connectors").json()] == ["Other"]


def test_config_import_parse_refusal_is_400(web_client):
    _create_device(web_client)
    r = _import_config(web_client, _doc_with_connector({"auth_password": "x"}))
    assert r.status_code == 400
    assert "connectors[0] ('Weather'): unknown field 'auth_password'" in r.json()["detail"]
    r = _import_config(web_client, _doc_with_connector({"headers": {"X-Api-Key": "secret"}}))
    assert r.status_code == 400
    assert "header values aren't carried in the file" in r.json()["detail"]


def test_config_import_digest_mismatch_on_connector_added_between_preview_and_apply(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1")
    doc = _doc_with_connector()
    digest = _preview_digest(web_client, doc)

    _create_connector(web_client, name="Weather")

    r = _apply_with_digest(web_client, doc, digest)
    assert r.status_code == 409
    assert "preview" in r.json()["detail"]
    assert web_client.get("/api/connectors").json()[0]["mapping_count"] == 0


def test_config_dry_run_digest_is_stable_for_the_same_file(web_client):
    _create_device(web_client)
    doc = _config_doc()
    assert _preview_digest(web_client, doc) == _preview_digest(web_client, doc)


def test_config_apply_with_matching_digest_applies(web_client):
    _create_device(web_client)
    doc = _config_doc(device={"name": "Renamed"})
    r = _apply_with_digest(web_client, doc, _preview_digest(web_client, doc))
    assert r.status_code == 200, r.text
    assert web_client.get("/api/devices/599001").json()["name"] == "Renamed"


def test_config_apply_refuses_when_the_plan_changed_since_preview(web_client):
    _create_device(web_client)
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    _create_device(web_client, device_id=599002, name="Other")
    doc = _config_doc(device={"name": "Renamed"})  # deletes 599002
    digest = _preview_digest(web_client, doc)

    # Between preview and apply, someone adds an object the preview didn't
    # list -- the new plan would delete it -- and starts 599002.
    _create_object(web_client, name="AV-9", instance_number=9)
    web_client.post("/api/engine/start", json={"device_id": 599002})

    r = _apply_with_digest(web_client, doc, digest)
    assert r.status_code == 409
    assert "preview" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"
    assert [o["name"] for o in web_client.get("/api/devices/599001/objects").json()] == ["AV-9"]
    # Refused before any device was stopped.
    assert web_client.app.state.engine.is_running(599002)


@pytest.mark.parametrize("bad", ["", "not-a-digest", "0" * 64])
def test_config_apply_treats_a_malformed_digest_as_a_mismatch(web_client, bad):
    _create_device(web_client)
    r = _apply_with_digest(web_client, _config_doc(device={"name": "Renamed"}), bad)
    assert r.status_code == 409
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"


def test_config_apply_rolls_back_when_the_real_apply_differs_from_preview(web_client, monkeypatch):
    # A change landing while devices are being stopped slips past the first
    # check; the second one compares sync_config's real result and rolls back.
    from bacpack.web.api import object_io

    _create_device(web_client)
    doc = _config_doc(device={"name": "Renamed"})
    r = _import_config(web_client, doc, dry_run="true")
    previewed = {k: v for k, v in r.json().items() if k not in ("dry_run", "plan_digest")}
    previewed["objects"] = {int(k): v for k, v in previewed["objects"].items()}
    digest = r.json()["plan_digest"]

    _create_object(web_client, name="AV-9", instance_number=9)

    real_sync = object_io.sync_config

    def stale_first_plan(session, parsed, *, dry_run, **kwargs):
        return previewed if dry_run else real_sync(session, parsed, dry_run=False, **kwargs)

    monkeypatch.setattr(object_io, "sync_config", stale_first_plan)
    r = _apply_with_digest(web_client, doc, digest)
    assert r.status_code == 409
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"
    assert [o["name"] for o in web_client.get("/api/devices/599001/objects").json()] == ["AV-9"]


def test_config_apply_without_digest_keeps_todays_behaviour(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-9", instance_number=9)
    r = _import_config(web_client, _config_doc(device={"name": "Renamed"}))
    assert r.status_code == 200, r.text
    assert "plan_digest" not in r.json()
    assert [o["name"] for o in web_client.get("/api/devices/599001/objects").json()] == ["AV-1"]


def test_csv_import_refuses_the_network_port_name(web_client):
    _create_device(web_client)
    created = _create_object(web_client, name="AV-1")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text.replace("AV-1", "NetworkPort-1")
    web_client.delete(f"/api/devices/599001/objects/{created['id']}")
    for mode in ("merge", "sync"):
        r = web_client.post(
            f"/api/devices/599001/objects/import?mode={mode}",
            files={"file": ("objects.csv", csv_text, "text/csv")},
        )
        assert r.status_code == 409, (mode, r.text)
        assert "network port" in r.json()["detail"]


@pytest.mark.parametrize(
    "device, obj, message",
    [
        (None, {"name": "NetworkPort-1"}, "network port"),
        ({"name": "AV-1-Trend"}, {"trended": True}, "trend log"),
        ({"name": "NetworkPort-1"}, None, "network port"),
    ],
)
def test_json_config_import_refuses_a_name_the_device_needs(web_client, device, obj, message):
    r = _import_config(web_client, _config_doc(device=device, obj=obj), dry_run="true")
    assert r.status_code == 400
    assert message in r.json()["detail"]


def test_csv_sync_can_swap_two_names(web_client):
    _create_device(web_client)
    _create_object(web_client, instance_number=1, name="A")
    _create_object(web_client, instance_number=2, name="B")
    r = _import_csv(
        web_client,
        "object_type,instance_number,name,units\nanalog-value,1,B,percent\nanalog-value,2,A,percent\n",
        mode="sync",
    )
    assert r.status_code == 200, r.text


# --- names trimmed, other text cells kept as written --------------------------


def test_csv_import_trims_names_and_keeps_descriptions_as_written(web_client):
    _create_device(web_client)
    created = _create_object(web_client, name="AV-1", description=" two  spaces ")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text.replace("AV-1", " Supply Fan ")
    web_client.delete(f"/api/devices/599001/objects/{created['id']}")
    r = web_client.post(
        "/api/devices/599001/objects/import?mode=merge",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 200, r.text
    [obj] = web_client.get("/api/devices/599001/objects").json()
    assert obj["name"] == "Supply Fan"
    assert obj["description"] == " two  spaces "


def test_csv_export_of_a_description_with_edge_spaces_round_trips(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", description=" padded ")
    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text
    r = web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 200, r.text
    [obj] = web_client.get("/api/devices/599001/objects").json()
    assert obj["description"] == " padded "


def test_json_config_import_trims_device_and_object_names(web_client):
    r = _import_config(web_client, _config_doc(device={"name": " Demo Device "}, obj={"name": "AV-1 "}))
    assert r.status_code == 200, r.text
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"
    [obj] = web_client.get("/api/devices/599001/objects").json()
    assert obj["name"] == "AV-1"


_SYNC_HEADER = (
    "object_type,instance_number,name,description,units,bacnet_writable,"
    "out_of_service_default,relinquish_default,state_text,"
    "schedule_value_type,schedule_default,weekly_schedule,exception_schedule,"
    "schedule_effective_start,schedule_effective_end,schedule_priority,"
    "schedule_write_targets,date_list\n"
)


def _post_csv(web_client, csv_text, query):
    return web_client.post(
        f"/api/devices/599001/objects/import?{query}",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )


def test_csv_sync_dry_run_returns_the_plan_and_writes_nothing(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    _create_object(web_client, name="AV-2", instance_number=2)
    csv_text = _SYNC_HEADER + "analog-value,1,AV-1-renamed,,,false,false,,,,,,,,,,,\nanalog-value,3,AV-3,,,false,false,,,,,,,,,,,\n"

    r = _post_csv(web_client, csv_text, "mode=sync&dry_run=true")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["mode"] == "sync"
    assert (body["created"], body["updated"], body["deleted"]) == (["analog-value:3"], ["analog-value:1"], ["analog-value:2"])
    assert len(body["plan_digest"]) == 64
    names = [o["name"] for o in web_client.get("/api/devices/599001/objects").json()]
    assert names == ["AV-1", "AV-2"]


def test_csv_merge_dry_run_lists_what_it_would_add(web_client):
    _create_device(web_client)
    csv_text = "object_type,instance_number,name\nanalog-value,1,AV-1\nanalog-input,2,AI-2\n"

    r = _post_csv(web_client, csv_text, "mode=merge&dry_run=true")

    assert r.status_code == 200, r.text
    assert r.json()["created"] == ["analog-value:1", "analog-input:2"]
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_csv_dry_run_refuses_what_the_apply_would_refuse(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    # Only the whole-device check refuses an object named after the device.
    csv_text = "object_type,instance_number,name\nanalog-value,1,AV-1\nanalog-value,2,Demo Device\n"

    r = _post_csv(web_client, csv_text, "mode=sync&dry_run=true")

    assert r.status_code == 409, r.text
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


def test_csv_sync_apply_with_the_previews_digest_goes_through(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    _create_object(web_client, name="AV-2", instance_number=2)
    csv_text = _SYNC_HEADER + "analog-value,1,AV-1,,,false,false,,,,,,,,,,,\n"
    digest = _post_csv(web_client, csv_text, "mode=sync&dry_run=true").json()["plan_digest"]

    r = _post_csv(web_client, csv_text, f"mode=sync&plan_digest={digest}")

    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == ["analog-value:2"]


def test_csv_sync_apply_with_a_stale_digest_is_refused_and_deletes_nothing(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    _create_object(web_client, name="AV-2", instance_number=2)
    csv_text = _SYNC_HEADER + "analog-value,1,AV-1,,,false,false,,,,,,,,,,,\n"
    digest = _post_csv(web_client, csv_text, "mode=sync&dry_run=true").json()["plan_digest"]
    _create_object(web_client, name="AV-9", instance_number=9)  # the device changed since the preview

    r = _post_csv(web_client, csv_text, f"mode=sync&plan_digest={digest}")

    assert r.status_code == 409
    assert "changed since the preview" in r.json()["detail"]
    assert len(web_client.get("/api/devices/599001/objects").json()) == 3


def test_csv_sync_refuses_a_device_edit_landing_between_the_digest_check_and_the_lock(web_client, monkeypatch):
    """The digest is checked again on the sync done under the lock."""
    from bacpack.models import BacnetObject
    from bacpack.web.api import object_io

    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    _create_object(web_client, name="AV-2", instance_number=2)
    csv_text = _SYNC_HEADER + "analog-value,1,AV-1,,,false,false,,,,,,,,,,,\n"
    digest = _post_csv(web_client, csv_text, "mode=sync&dry_run=true").json()["plan_digest"]
    real_lock = object_io.lock_both_files

    def edit_then_lock(session):
        with web_client.app.state.session_factory() as other:
            other.add(BacnetObject(device_id=599001, object_type="analog-value", instance_number=9, name="AV-9"))
            other.commit()
        real_lock(session)

    monkeypatch.setattr(object_io, "lock_both_files", edit_then_lock)
    r = _post_csv(web_client, csv_text, f"mode=sync&plan_digest={digest}")

    assert r.status_code == 409, r.text
    assert "changed since the preview" in r.json()["detail"]
    names = sorted(o["name"] for o in web_client.get("/api/devices/599001/objects").json())
    assert names == ["AV-1", "AV-2", "AV-9"]


def test_csv_import_without_dry_run_still_applies_at_once(web_client):
    """Scripts post with no dry_run: the default stays an apply."""
    _create_device(web_client)
    r = _post_csv(web_client, "object_type,instance_number,name\nanalog-value,1,AV-1\n", "mode=merge")
    assert r.status_code == 200
    assert r.json() == {"imported": 1}


def test_both_plan_changed_details_carry_the_phrase_the_spa_keys_on():
    # frontend/src/api/endpoints.ts postImport tells a plan change from any other 409 by it.
    from bacpack.web.api import object_io

    assert "changed since the preview" in object_io._CSV_PLAN_CHANGED
    assert "changed since the preview" in object_io._PLAN_CHANGED


# A sync that lowers a trend's sample cap trims what it stored.

def _object_with_samples(web_client, count):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    for value in range(count):
        record_sample(web_client.app.state.session_factory, obj["id"], float(value))
    return obj


def _stored(web_client, object_id):
    from bacpack.models import TrendSample, get_or_create_history_counters

    with web_client.app.state.session_factory() as session:
        values = [
            s.value for s in session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id)
        ]
        return values, get_or_create_history_counters(session).trend_total_sample_count


def test_a_csv_sync_that_lowers_a_sample_cap_trims_the_samples(web_client):
    obj = _object_with_samples(web_client, 5)
    r = web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", "object_type,instance_number,name,trend_sample_cap\nanalog-value,1,AV-1,2\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert _stored(web_client, obj["id"]) == ([3.0, 4.0], 2)


def test_a_config_import_that_lowers_a_sample_cap_trims_the_samples(web_client):
    obj = _object_with_samples(web_client, 5)
    doc = web_client.get("/api/config/export").json()
    doc["devices"][0]["objects"][0]["trend_sample_cap"] = 2
    doc["devices"][0]["objects"][0]["trended"] = False
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert _stored(web_client, obj["id"]) == ([3.0, 4.0], 2)


def test_config_export_import_round_trip_preserves_network_roles_and_links(web_client):
    for device_id, name, bind in ((599001, "D", "192.168.1.50/24:47810"), (599002, "E", "192.168.1.50/24:47811")):
        web_client.post("/api/devices", json={"id": device_id, "name": name, "bind_address": bind})
    web_client.put(
        "/api/devices/599001",
        json={"network_role": "bbmd", "bbmd_links": [599002], "bbmd_peers": ["10.0.0.9:47808"]},
    )
    doc = web_client.get("/api/config/export").json()
    assert doc["devices"][0]["bbmd_links"] == [599002]

    for device_id in (599001, 599002):
        assert web_client.delete(f"/api/devices/{device_id}").status_code == 204
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text

    assert web_client.get("/api/config/export").json()["devices"] == doc["devices"]


def test_config_import_refuses_a_link_to_a_device_that_is_nowhere(web_client):
    web_client.post("/api/devices", json={"id": 599001, "name": "D", "bind_address": "192.168.1.50/24:47810"})
    doc = web_client.get("/api/config/export").json()
    doc["devices"][0].update(network_role="bbmd", bbmd_links=[599777])
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 409
    assert "D (599001)" in r.json()["detail"] and "599777" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["network_role"] == "normal"


def test_csv_import_of_a_cell_over_the_csv_limit_is_400_with_its_row(web_client):
    import csv

    _create_device(web_client)
    content = "object_type,instance_number,name,description\nanalog-value,1,A," + "x" * (csv.field_size_limit() + 1)
    r = web_client.post("/api/devices/599001/objects/import", files={"file": ("o.csv", content, "text/csv")})
    assert r.status_code == 400
    assert "row 2" in r.json()["detail"]


def test_config_import_of_a_file_with_a_surrogate_is_400(web_client):
    import json as _json

    doc = '{"devices": [{"id": 599001, "name": %s, "objects": []}]}' % _json.dumps("\ud800")
    r = web_client.post("/api/config/import?dry_run=true", files={"file": ("c.json", doc, "application/json")})
    assert r.status_code == 400
    assert "surrogate" in r.json()["detail"]
