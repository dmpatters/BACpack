"""Schedule and Calendar object config through the object API: create,
update, batch, and DB-backed write-target / calendar-reference checks."""
from __future__ import annotations

import pytest

from tests.web._helpers import _WEEKLY_SCHEDULE, _create_device, _create_object, _create_schedule_object


# --- Schedule / Calendar objects --------------------------------------------
# validate_schedule_config/validate_calendar_config are unit-tested directly
# in test_bacnet.py -- these cover the API-layer wiring (create/update/batch,
# DB-backed write-target/calendar-reference existence checks, and the
# connector-mapping rejection), same 4-test skeleton already used above for
# units/relinquish_default.


def test_create_object_accepts_valid_schedule(web_client):
    _create_device(web_client)
    r = _create_schedule_object(web_client)
    assert r.status_code == 201, r.text
    assert r.json()["schedule_priority"] == 14  # default


def test_create_object_accepts_a_schedule_with_a_null_default(web_client):
    _create_device(web_client)
    r = _create_schedule_object(web_client, schedule_default=None)
    assert r.status_code == 201, r.text
    assert r.json()["schedule_default"] is None


def test_a_schedule_default_can_be_cleared_and_set_again(web_client):
    _create_device(web_client)
    created = _create_schedule_object(web_client).json()
    url = f"/api/devices/599001/objects/{created['id']}"
    r = web_client.put(url, json={"schedule_default": None})
    assert r.status_code == 200, r.text
    assert web_client.get(url).json()["schedule_default"] is None
    web_client.put(url, json={"schedule_default": "5.0"})
    assert web_client.get(url).json()["schedule_default"] == "5.0"


def test_a_null_default_survives_the_json_config_and_csv_round_trips(web_client):
    import json

    _create_device(web_client)
    _create_schedule_object(web_client, schedule_default=None)

    doc = web_client.get("/api/config/export").json()
    [sched] = [o for o in doc["devices"][0]["objects"] if o["object_type"] == "schedule"]
    assert sched["schedule_default"] is None
    web_client.delete("/api/devices/599001")
    r = web_client.post(
        "/api/config/import?dry_run=false", files={"file": ("config.json", json.dumps(doc), "application/json")}
    )
    assert r.status_code == 200, r.text
    [back] = [o for o in web_client.get("/api/devices/599001/objects").json() if o["object_type"] == "schedule"]
    assert back["schedule_default"] is None

    csv_text = web_client.get("/api/devices/599001/objects/export.csv").text
    _create_device(web_client, device_id=599002, name="Second Device")
    r = web_client.post("/api/devices/599002/objects/import", files={"file": ("objects.csv", csv_text, "text/csv")})
    assert r.status_code == 200, r.text
    [copy] = web_client.get("/api/devices/599002/objects").json()
    assert copy["schedule_default"] is None


@pytest.mark.parametrize("weekly", [None, [[] for _ in range(7)]])
def test_create_object_accepts_schedule_with_neither_weekly_nor_exception_schedule(web_client, weekly):
    # Always at Schedule_Default.
    _create_device(web_client)
    r = _create_schedule_object(web_client, weekly_schedule=weekly)
    assert r.status_code == 201, r.text


def test_create_object_rejects_schedule_write_target_referencing_nonexistent_object(web_client):
    _create_device(web_client)
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 99}],
    )
    assert r.status_code == 409
    assert "does not exist" in r.json()["detail"]


def test_create_object_accepts_schedule_write_target_referencing_real_object(web_client):
    _create_device(web_client)
    _create_object(web_client, name="Target-AV", instance_number=1)
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1}],
    )
    assert r.status_code == 201, r.text


def test_update_object_rejects_invalid_schedule_config(web_client):
    # A duplicate time-of-day within one day's schedule is a shape Pydantic
    # itself has no reason to reject (it's a structurally valid list) --
    # only validate_schedule_config's own semantic check catches it, so this
    # exercises that second layer specifically (schedule_priority out of
    # 1-16 range, tried first, turned out to be caught by Pydantic's own
    # Field(ge=1, le=16) before ever reaching validate_schedule_config).
    _create_device(web_client)
    obj = _create_schedule_object(web_client).json()
    duplicate_day = [{"time": "08:00", "value": "1.0"}, {"time": "08:00", "value": "2.0"}]
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"weekly_schedule": [duplicate_day] + _WEEKLY_SCHEDULE[1:]},
    )
    assert r.status_code == 400
    assert "duplicate schedule time" in r.json()["detail"]


def test_batch_update_objects_rejects_invalid_schedule_config(web_client):
    _create_device(web_client)
    obj = _create_schedule_object(web_client).json()
    duplicate_day = [{"time": "08:00", "value": "1.0"}, {"time": "08:00", "value": "2.0"}]
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [obj["id"]],
            "patch": {"weekly_schedule": [duplicate_day] + _WEEKLY_SCHEDULE[1:]},
        },
    )
    assert r.status_code == 400
    assert "duplicate schedule time" in r.json()["detail"]


def test_update_object_rejects_schedule_write_target_referencing_nonexistent_calendar(web_client):
    _create_device(web_client)
    obj = _create_schedule_object(web_client).json()
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={
            "exception_schedule": [
                {
                    "period": {"kind": "calendar-reference", "instance_number": 99},
                    "priority": 1,
                    "time_values": [{"time": "08:00", "value": "1.0"}],
                }
            ]
        },
    )
    assert r.status_code == 409
    assert "does not exist" in r.json()["detail"]


def test_create_object_accepts_valid_calendar(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "calendar",
            "instance_number": 1,
            "name": "Cal-1",
            "date_list": [{"kind": "date", "year": "*", "month": 12, "day": 25}],
        },
    )
    assert r.status_code == 201, r.text


def test_create_object_accepts_a_calendar_with_no_dates(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "calendar", "instance_number": 1, "name": "Cal-1"},
    )
    assert r.status_code == 201, r.text
    assert not r.json()["date_list"]


def test_update_object_rejects_invalid_calendar_config(web_client):
    _create_device(web_client)
    obj = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "calendar",
            "instance_number": 1,
            "name": "Cal-1",
            "date_list": [{"kind": "date", "year": "*", "month": 12, "day": 25}],
        },
    ).json()
    # "not-a-real-kind" is rejected by Pydantic's own Literal type (422)
    # before ever reaching validate_calendar_config -- use an
    # out-of-range value component instead, a shape Pydantic has no reason
    # to reject, to actually exercise the second (bacnet.py) layer.
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"date_list": [{"kind": "date", "year": "*", "month": 13, "day": 1}]},
    )
    assert r.status_code == 400
    assert "month" in r.json()["detail"]


# --- External write targets -------------------------------------------------

_REMOTE = {
    "device_instance": 599005,
    "object_type": "analogValue",
    "instance_number": 3,
    "property_identifier": "presentValue",
}


def test_create_schedule_with_remote_target_skips_existence_check_and_normalizes_names(web_client):
    _create_device(web_client)
    r = _create_schedule_object(web_client, schedule_write_targets=[_REMOTE])
    assert r.status_code == 201, r.text
    assert r.json()["schedule_write_targets"] == [
        {
            "device_instance": 599005,
            "object_type": "analog-value",
            "instance_number": 3,
            "property_identifier": "present-value",
        }
    ]


def test_create_schedule_local_target_is_stored_without_device_instance(web_client):
    _create_device(web_client)
    _create_object(web_client, name="Target-AV", instance_number=1)
    r = _create_schedule_object(
        web_client, schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1}]
    )
    assert r.status_code == 201, r.text
    assert r.json()["schedule_write_targets"] == [
        {"object_type": "analog-value", "instance_number": 1, "property_identifier": "presentValue"}
    ]


def test_own_device_instance_target_is_local_and_existence_checked(web_client):
    _create_device(web_client)
    r = _create_schedule_object(
        web_client, schedule_write_targets=[{**_REMOTE, "device_instance": 599001, "object_type": "analog-value"}]
    )
    assert r.status_code == 409
    assert "does not exist" in r.json()["detail"]


def test_own_device_instance_target_is_stored_like_a_local_one(web_client):
    # Same stored form as a target added as local: no device_instance, a
    # camelCase property -- not the kebab-case a remote target is given.
    _create_device(web_client)
    _create_object(web_client, name="Target-AV", instance_number=3)
    r = _create_schedule_object(
        web_client, schedule_write_targets=[{**_REMOTE, "device_instance": 599001, "property_identifier": "present-value"}]
    )
    assert r.status_code == 201, r.text
    assert r.json()["schedule_write_targets"] == [
        {"object_type": "analog-value", "instance_number": 3, "property_identifier": "presentValue"}
    ]


def test_create_schedule_rejects_bad_remote_target(web_client):
    _create_device(web_client)
    r = _create_schedule_object(web_client, schedule_write_targets=[{**_REMOTE, "object_type": "nonsense"}])
    assert r.status_code == 400
    assert "object type" in r.json()["detail"]


def test_update_schedule_to_remote_target(web_client):
    _create_device(web_client)
    sched = _create_schedule_object(web_client).json()
    r = web_client.put(
        f"/api/devices/599001/objects/{sched['id']}", json={"schedule_write_targets": [_REMOTE]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["schedule_write_targets"][0]["device_instance"] == 599005


def test_batch_update_schedule_to_remote_target(web_client):
    _create_device(web_client)
    sched = _create_schedule_object(web_client).json()
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [sched["id"]], "patch": {"schedule_write_targets": [_REMOTE]}},
    )
    assert r.status_code == 200, r.text
    got = web_client.get(f"/api/devices/599001/objects/{sched['id']}").json()
    assert got["schedule_write_targets"][0]["object_type"] == "analog-value"


def test_schedule_write_status_is_returned_and_read_only(web_client):
    _create_device(web_client)
    sched = _create_schedule_object(web_client).json()
    assert sched["schedule_write_status"] is None
    r = web_client.put(
        f"/api/devices/599001/objects/{sched['id']}", json={"schedule_write_status": {"x": {}}}
    )
    got = web_client.get(f"/api/devices/599001/objects/{sched['id']}").json()
    assert got["schedule_write_status"] is None


def test_schedule_mixed_case_choices_stored_canonical(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="binary-value", instance_number=1, name="Fan", units=None)
    r = _create_schedule_object(
        web_client,
        schedule_value_type="Binary",
        schedule_default="Active",
        weekly_schedule=[[{"time": "08:00", "value": "INACTIVE"}] for _ in range(7)],
        schedule_write_targets=[
            {"object_type": "Binary-Value", "instance_number": 1, "property_identifier": "Present_Value"}
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["schedule_value_type"], body["schedule_default"]) == ("binary", "active")
    assert body["weekly_schedule"][0] == [{"time": "08:00", "value": "inactive"}]
    assert body["schedule_write_targets"][0]["object_type"] == "binary-value"
    assert body["schedule_write_targets"][0]["property_identifier"] == "presentValue"


def test_create_object_refuses_a_local_write_target_with_an_unknown_property(web_client):
    # Saved, then the device failed to start ("ValueError: fooBar").
    _create_device(web_client)
    _create_object(web_client, name="Target-AV", instance_number=1)
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1, "property_identifier": "fooBar"}],
    )
    assert r.status_code == 400
    assert "unknown property" in r.json()["detail"]


def test_dangling_write_target_on_create_is_409(web_client):
    _create_device(web_client)
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[
            {"object_type": "analog-value", "instance_number": 42, "property_identifier": "presentValue"}
        ],
    )
    assert r.status_code == 409
    assert "analog-value:42" in r.json()["detail"] and "does not exist" in r.json()["detail"]


def test_editing_one_field_of_a_schedule_with_a_local_target_still_saves(web_client):
    _create_device(web_client)
    _create_object(web_client)  # AV-1, analog-value:1
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[
            {
                "device_instance": 599001,
                "object_type": "analog-value",
                "instance_number": 1,
                "property_identifier": "present-value",
            }
        ],
    )
    assert r.status_code == 201, r.text
    r = web_client.put(f"/api/devices/599001/objects/{r.json()['id']}", json={"description": "lobby"})
    assert r.status_code == 200, r.text


def test_schedule_time_values_saved_in_time_order(web_client):
    _create_device(web_client)
    day = [{"time": "17:00", "value": "20.0"}, {"time": "08:00", "value": "50.0"}]
    r = _create_schedule_object(
        web_client,
        weekly_schedule=[day for _ in range(7)],
        exception_schedule=[{"period": {"kind": "date", "year": 2027, "month": 1, "day": 1}, "priority": 1,
                             "time_values": day}],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [e["time"] for e in body["weekly_schedule"][0]] == ["08:00", "17:00"]
    assert [e["time"] for e in body["exception_schedule"][0]["time_values"]] == ["08:00", "17:00"]
    got = web_client.get(f"/api/devices/599001/objects/{body['id']}").json()
    assert [e["time"] for e in got["weekly_schedule"][0]] == ["08:00", "17:00"]
