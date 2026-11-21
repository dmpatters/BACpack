"""The device object import with an EDE file: merge and sync into an
existing device, through the CSV import's preview and apply."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from bacpack.models import BacnetObject
from tests.ede_fixtures import WIDE, device_row, ede_bytes, ede_row, ede_text, state_texts_text
from tests.web._helpers import _create_device, _create_object, _create_schedule_object

URL = "/api/devices/599001/objects/import"

PLANT = [
    device_row(1000, "Boiler"),
    ede_row(type=0, instance=1, name="Temp", unit=62),
    ede_row(type=2, instance=1, name="SP", default="7"),
    ede_row(type=3, instance=1, name="Door"),
]


def _file(rows=PLANT, **kw):
    return ("plant.csv", ede_bytes(ede_text(rows, columns=WIDE), **kw), "text/csv")


def _post(client, rows=PLANT, *, mode="merge", dry_run=False, digest=None, ede_device=None, files=None, **kw):
    params = {"mode": mode, "dry_run": str(dry_run).lower()}
    if digest:
        params["plan_digest"] = digest
    if ede_device is not None:
        params["ede_device"] = ede_device
    return client.post(URL, params=params, files={"file": _file(rows, **kw), **(files or {})})


@pytest.fixture
def device(web_client):
    _create_device(web_client)
    return web_client


def _objects(client):
    with client.app.state.session_factory() as session:
        return {o.name: o for o in session.scalars(select(BacnetObject).filter_by(device_id=599001))}


def test_ede_dry_run_merge_reports_created_skipped_and_notes(device):
    _create_object(device, object_type="analog-input", instance_number=1, name="Temp", units="percent")
    r = _post(device, PLANT + [ede_row(type=9, instance=1, name="Loop")], dry_run=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] and body["mode"] == "merge"
    assert body["created"] == ["analog-value:1", "binary-input:1"] and body["updated"] == [] and body["deleted"] == []
    assert {s["name"]: s["code"] for s in body["skipped"]} == {"Temp": "already-present", "Loop": "unsupported-type"}
    # For the tab's request-support link, as on the new-device preview.
    assert body["skipped_types"] == {"event-enrollment (9)": 1}
    assert body["ede"]["read_as"] == "EDE" and body["ede"]["device"] == 1000
    assert body["ede"]["devices"] == [{"instance": 1000, "name": "Boiler", "object_count": 4}]
    assert "min-present-value" in body["skipped_columns"] and body["plan_digest"]
    assert sorted(_objects(device)) == ["Temp"]  # a dry run writes nothing


def test_ede_merge_apply_adds_only_new_points_and_reimport_adds_nothing(device):
    assert _post(device).json() == {"imported": 3}
    objects = _objects(device)
    assert sorted(objects) == ["Door", "SP", "Temp"] and objects["Temp"].units == "degreesCelsius"
    again = _post(device)
    assert again.status_code == 200 and again.json() == {"imported": 0}
    assert len(_objects(device)) == 3


def test_ede_merge_dry_run_digest_applies_and_a_changed_device_is_409(device):
    digest = _post(device, dry_run=True).json()["plan_digest"]
    assert _post(device, digest=digest).status_code == 200
    # The same preview again, after the device changed under it.
    fresh = _post(device, [device_row(1000, "Boiler"), ede_row(type=0, instance=7, name="New")], dry_run=True)
    _create_object(device, object_type="analog-input", instance_number=7, name="New")
    r = _post(device, [device_row(1000, "Boiler"), ede_row(type=0, instance=7, name="New")], digest=fresh.json()["plan_digest"])
    assert r.status_code == 409 and "changed since the preview" in r.json()["detail"]


def test_ede_sync_apply_updates_named_objects_and_deletes_the_rest(device):
    _create_object(device, object_type="analog-input", instance_number=1, name="Temp", units="percent")
    _create_object(device, object_type="analog-value", instance_number=9, name="Other")
    preview = _post(device, mode="sync", dry_run=True).json()
    assert preview["updated"] == ["analog-input:1"] and preview["deleted"] == ["analog-value:9"]
    assert sorted(preview["created"]) == ["analog-value:1", "binary-input:1"]
    r = _post(device, mode="sync", digest=preview["plan_digest"])
    assert r.status_code == 200, r.text
    objects = _objects(device)
    assert sorted(objects) == ["Door", "SP", "Temp"] and objects["Temp"].units == "degreesCelsius"


def test_ede_sync_keeps_settings_the_file_does_not_carry(device):
    _create_object(
        device, object_type="analog-value", instance_number=1, name="SP", units="percent",
        description="keep me", bacnet_writable=False,
    )
    assert _create_schedule_object(device, instance_number=1, name="Sched-1").status_code == 201
    _create_object(
        device, object_type="analog-input", instance_number=1, name="Temp",
        trended=True, trend_mode="interval", trend_interval_seconds=30,
    )
    rows = [
        device_row(1000, "Boiler"),
        ede_row(type=2, instance=1, name="SP", default="7"),
        ede_row(type=17, instance=1, name="Sched-1"),
        ede_row(type=0, instance=1, name="Temp"),
        ede_row(type=20, instance=5, name="Temp.Log"),
    ]
    r = _post(device, rows, mode="sync")
    assert r.status_code == 200, r.text
    objects = _objects(device)
    sp, schedule, temp = objects["SP"], objects["Sched-1"], objects["Temp"]
    assert (sp.units, sp.description, sp.bacnet_writable, sp.relinquish_default) == ("percent", "keep me", False, "7.0")
    assert schedule.weekly_schedule and schedule.schedule_default == "10.0"
    assert (temp.trended, temp.trend_interval_seconds, temp.trend_log_instance) == (True, 30, None)


MULTI = PLANT + [device_row(2000, "Chiller"), ede_row(device=2000, type=0, instance=1, name="CH Temp")]


def test_ede_multi_device_file_dry_run_asks_which(device):
    r = _post(device, MULTI, dry_run=True)
    assert r.status_code == 200
    body = r.json()
    assert body["needs_device"] is True and body["ede"]["device"] is None
    assert body["skipped_types"] == {}
    assert [d["instance"] for d in body["ede"]["devices"]] == [1000, 2000]
    assert body["created"] == [] and "plan_digest" not in body


def test_ede_multi_device_dry_run_with_a_device_plans_it(device):
    body = _post(device, MULTI, dry_run=True, ede_device=2000).json()
    assert body["created"] == ["analog-input:1"] and body["ede"]["device"] == 2000
    assert "needs_device" not in body


def test_ede_multi_device_apply_without_ede_device_is_400(device):
    r = _post(device, MULTI)
    assert r.status_code == 400 and "ede_device" in r.json()["detail"]
    assert _objects(device) == {}


def test_ede_unknown_ede_device_is_400(device):
    r = _post(device, MULTI, ede_device=3000)
    assert r.status_code == 400 and "1000" in r.json()["detail"] and "2000" in r.json()["detail"]


def test_ede_device_row_is_ignored_device_keeps_its_name_and_id(device):
    assert _post(device, mode="sync").status_code == 200
    r = device.get("/api/devices/599001")
    assert r.status_code == 200 and r.json()["name"] == "Demo Device"
    assert device.get("/api/devices/1000").status_code == 404


def test_ede_merge_trend_log_taking_an_existing_logs_default_instance_is_skipped_and_the_point_imports(device):
    existing = _create_object(
        device, object_type="analog-input", instance_number=1, name="Existing",
        trended=True, trend_mode="interval", trend_interval_seconds=30,
    )
    rows = [ede_row(type=2, instance=1, name="SP"), ede_row(type=20, instance=existing["id"], name="SP.Log")]
    dry = _post(device, rows, dry_run=True)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["created"] == ["analog-value:1"]
    assert [(s["name"], s["code"]) for s in body["skipped"]] == [("SP.Log", "refused")]
    assert "Existing" in body["skipped"][0]["reason"]
    assert _post(device, rows, digest=body["plan_digest"]).json() == {"imported": 1}
    assert _objects(device)["SP"].trended is False


def test_ede_merge_trend_log_taking_an_existing_explicit_instance_is_skipped_and_the_point_imports(device):
    _create_object(
        device, object_type="analog-input", instance_number=1, name="Old",
        trended=True, trend_mode="interval", trend_interval_seconds=30, trend_log_instance=5,
    )
    rows = [ede_row(type=2, instance=2, name="New"), ede_row(type=20, instance=5, name="New.Log")]
    body = _post(device, rows, dry_run=True).json()
    assert body["created"] == ["analog-value:2"]
    assert [s["name"] for s in body["skipped"]] == ["New.Log"]
    assert _post(device, rows).json() == {"imported": 1}


def test_ede_merge_point_named_like_an_existing_trend_logs_name_gets_a_suffix(device):
    _create_object(device, object_type="analog-input", instance_number=1, name="Fan", trended=True,
                   trend_mode="interval", trend_interval_seconds=30)
    rows = [ede_row(type=2, instance=2, name="Fan-Trend")]
    body = _post(device, rows, dry_run=True).json()
    assert body["created"] == ["analog-value:2"]
    assert any("Fan-Trend (2)" in n["note"] for n in body["notes"])
    assert _post(device, rows).json() == {"imported": 1}
    assert "Fan-Trend (2)" in _objects(device)


@pytest.mark.parametrize("new_point_first", [False, True])
def test_ede_sync_guessed_log_taking_a_kept_points_default_instance_is_skipped_and_the_point_stays(device, new_point_first):
    kept = _create_object(
        device, object_type="analog-value", instance_number=1, name="A", trended=True,
        trend_mode="interval", trend_interval_seconds=30,
    )
    points = [ede_row(type=2, instance=1, name="A"), ede_row(type=2, instance=2, name="B")]
    if new_point_first:
        points.reverse()
    rows = [device_row(1000, "B"), *points, ede_row(type=20, instance=kept["id"], name="B.Log")]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["created"] == ["analog-value:2"] and body["updated"] == ["analog-value:1"]
    assert [s["name"] for s in body["skipped"]] == ["B.Log"]
    assert _post(device, rows, mode="sync", digest=body["plan_digest"]).status_code == 200
    assert _objects(device)["B"].trended is False


def test_ede_state_texts_file_second_upload(device):
    rows = [ede_row(type=3, instance=1, name="Door", state_ref="1")]
    table = ("t.csv", state_texts_text([["1", "Shut", "Open"]]).encode(), "text/csv")
    assert _post(device, rows, files={"state_texts": table}).status_code == 200
    door = _objects(device)["Door"]
    assert (door.inactive_text, door.active_text) == ("Shut", "Open")


def test_ede_cp1252_file_is_accepted_and_a_cp1252_csv_is_still_refused(device):
    r = _post(device, [ede_row(type=0, instance=1, name="Büro")], encoding="cp1252")
    assert r.status_code == 200 and "Büro" in _objects(device)
    csv_bytes = "object_type,instance_number,name\nanalog-value,1,Bü\n".encode("cp1252")
    r = device.post(URL, files={"file": ("a.csv", csv_bytes, "text/csv")})
    assert r.status_code == 400 and "UTF-8" in r.json()["detail"]


def test_csv_import_unchanged_by_the_extraction(device):
    csv_bytes = b"object_type,instance_number,name\nanalog-value,1,AV-1\nanalog-value,2,AV-2\n"
    assert device.post(URL, files={"file": ("a.csv", csv_bytes, "text/csv")}).json() == {"imported": 2}
    sync = device.post(
        URL, params={"mode": "sync", "dry_run": "true"},
        files={"file": ("a.csv", b"object_type,instance_number,name\nanalog-value,1,AV-1\n", "text/csv")},
    ).json()
    assert sync["deleted"] == ["analog-value:2"] and sync["updated"] == ["analog-value:1"]
    assert sync["skipped_columns"] == [] and "ede" not in sync


# --- EDE sync edge cases -----------------------------------------------------


def test_ede_sync_object_whose_notification_class_the_file_omits_loses_its_alarming_and_imports(device):
    _create_object(device, object_type="notification-class", instance_number=1, name="NC")
    _create_object(
        device, object_type="analog-input", instance_number=1, name="Temp", units="percent",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=50, alarm_low_limit=0,
    )
    rows = [device_row(1000, "B"), ede_row(type=0, instance=1, name="Temp")]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    assert any("notification class 1" in n["note"] for n in dry.json()["notes"])
    r = _post(device, rows, mode="sync", digest=dry.json()["plan_digest"])
    assert r.status_code == 200, r.text
    temp = _objects(device)["Temp"]
    assert (temp.alarming_enabled, temp.notification_class_instance) == (False, None)


def test_ede_sync_schedule_whose_write_target_the_file_omits_loses_the_target_and_imports(device):
    _create_object(device, object_type="analog-value", instance_number=1, name="SP", units="percent")
    target = [{"object_type": "analog-value", "instance_number": 1, "property_identifier": "presentValue"}]
    assert _create_schedule_object(device, instance_number=1, name="Sch", schedule_write_targets=target).status_code == 201
    rows = [device_row(1000, "B"), ede_row(type=17, instance=1, name="Sch")]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    assert any("write target analog-value:1" in n["note"] for n in dry.json()["notes"])
    assert _post(device, rows, mode="sync", digest=dry.json()["plan_digest"]).status_code == 200
    assert not _objects(device)["Sch"].schedule_write_targets


def test_ede_sync_schedule_whose_exception_calendar_the_file_omits_loses_the_exception_and_imports(device):
    _create_object(device, object_type="calendar", instance_number=1, name="Cal", date_list=[
        {"kind": "date", "year": "2030", "month": "1", "day": "1", "day_of_week": "*"}])
    exception = [{
        "period": {"kind": "calendar-reference", "instance_number": 1}, "priority": 8,
        "time_values": [{"time": "08:00", "value": "5.0"}],
    }]
    assert _create_schedule_object(device, instance_number=1, name="Sch", exception_schedule=exception).status_code == 201
    rows = [device_row(1000, "B"), ede_row(type=17, instance=1, name="Sch")]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    assert any("calendar 1" in n["note"] for n in dry.json()["notes"])
    assert _post(device, rows, mode="sync", digest=dry.json()["plan_digest"]).status_code == 200
    assert not _objects(device)["Sch"].exception_schedule


@pytest.mark.parametrize("new_point_first", [True, False])
def test_ede_sync_point_named_like_a_kept_trended_objects_log_gets_a_suffix_in_either_row_order(device, new_point_first):
    _create_object(device, object_type="analog-value", instance_number=1, name="Fan", trended=True,
                   trend_mode="interval", trend_interval_seconds=30)
    points = [ede_row(type=2, instance=2, name="Fan-Trend"), ede_row(type=2, instance=1, name="Fan")]
    if not new_point_first:
        points.reverse()
    rows = [device_row(1000, "B"), *points]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    notes = [n["note"] for n in dry.json()["notes"] if n["name"] == "Fan-Trend (2)"]
    assert len(notes) == 1 and "trend log of 'Fan'" in notes[0] and "file has another" not in notes[0]
    assert _post(device, rows, mode="sync", digest=dry.json()["plan_digest"]).status_code == 200
    objects = _objects(device)
    assert sorted(objects) == ["Fan", "Fan-Trend (2)"] and objects["Fan"].trended is True


def test_ede_sync_refused_objects_name_equal_to_a_renamed_trended_points_log_name_still_imports(device):
    _create_object(device, object_type="multi-state-value", instance_number=1, name="Pump-Trend",
                   state_text=["a", "b", "c", "d"], relinquish_default="4")
    _create_object(device, object_type="analog-value", instance_number=2, name="Old", trended=True,
                   trend_mode="interval", trend_interval_seconds=30)
    rows = [
        device_row(1000, "B"),
        ede_row(type=19, instance=1, name="Pump-Trend", state_text="[x|y|z]"),
        ede_row(type=2, instance=2, name="Pump"),
    ]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    assert _post(device, rows, mode="sync", digest=dry.json()["plan_digest"]).status_code == 200
    assert "Pump-Trend" in _objects(device)


def test_ede_sync_file_naming_a_missing_notification_class_leaves_no_reference_to_it(device):
    _create_object(device, object_type="notification-class", instance_number=1, name="NC")
    _create_object(
        device, object_type="analog-input", instance_number=1, name="Temp", units="percent",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=50, alarm_low_limit=0,
    )
    rows = [device_row(1000, "B"), ede_row(type=0, instance=1, name="Temp", nc="1")]
    dry = _post(device, rows, mode="sync", dry_run=True)
    assert dry.status_code == 200, dry.text
    assert len([n for n in dry.json()["notes"] if "notification class" in n["note"]]) == 1
    assert _post(device, rows, mode="sync", digest=dry.json()["plan_digest"]).status_code == 200
    assert _objects(device)["Temp"].notification_class_instance is None


def test_ede_import_with_an_oversize_state_texts_cell_is_400(device):
    import csv

    texts = state_texts_text([["1", "x" * (csv.field_size_limit() + 1)]]).encode()
    r = _post(device, dry_run=True, files={"state_texts": ("t.csv", texts, "text/csv")})
    assert r.status_code == 400
