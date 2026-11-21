"""Deleting an object another object on the device still references: a notification class used by an alarming object, a calendar used
by a schedule exception, or a schedule's write target. Blocked with a 409
naming the referrer, on single delete, batch delete and CSV sync import."""
from __future__ import annotations

from tests.web._helpers import _create_device, _create_object, _create_schedule_object


def _create_notification_class_and_alarming_object(client):
    nc = _create_object(client, object_type="notification-class", instance_number=5, name="NC-5", units=None)
    ai = _create_object(
        client,
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=5,
        alarm_high_limit=100.0,
    )
    return nc, ai


def _create_calendar_and_referencing_schedule(client):
    cal = _create_object(
        client,
        object_type="calendar",
        instance_number=3,
        name="Cal-3",
        units=None,
        date_list=[{"kind": "date", "year": "*", "month": 12, "day": 25}],
    )
    r = _create_schedule_object(
        client,
        exception_schedule=[
            {
                "period": {"kind": "calendar-reference", "instance_number": 3},
                "priority": 1,
                "time_values": [{"time": "08:00", "value": "1.0"}],
            }
        ],
    )
    assert r.status_code == 201, r.text
    return cal, r.json()


def _object_names(client):
    return {o["name"] for o in client.get("/api/devices/599001/objects").json()}


def test_delete_notification_class_used_by_alarming_object_is_refused(web_client):
    _create_device(web_client)
    nc, _ = _create_notification_class_and_alarming_object(web_client)

    r = web_client.delete(f"/api/devices/599001/objects/{nc['id']}")
    assert r.status_code == 409
    assert "AI-1" in r.json()["detail"]
    assert "NC-5" in _object_names(web_client)


def test_delete_notification_class_allowed_once_alarming_is_off(web_client):
    _create_device(web_client)
    nc, ai = _create_notification_class_and_alarming_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{ai['id']}", json={"alarming_enabled": False})
    assert r.status_code == 200, r.text

    r = web_client.delete(f"/api/devices/599001/objects/{nc['id']}")
    assert r.status_code == 204


def test_delete_calendar_used_by_schedule_exception_is_refused(web_client):
    _create_device(web_client)
    cal, _ = _create_calendar_and_referencing_schedule(web_client)

    r = web_client.delete(f"/api/devices/599001/objects/{cal['id']}")
    assert r.status_code == 409
    assert "Sched-1" in r.json()["detail"]


def test_delete_schedule_write_target_is_refused(web_client):
    _create_device(web_client)
    av = _create_object(web_client, name="AV-1")
    r = _create_schedule_object(
        web_client, schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1}]
    )
    assert r.status_code == 201, r.text

    r = web_client.delete(f"/api/devices/599001/objects/{av['id']}")
    assert r.status_code == 409
    assert "Sched-1" in r.json()["detail"]


def test_delete_the_referencing_object_itself_is_allowed(web_client):
    _create_device(web_client)
    _, ai = _create_notification_class_and_alarming_object(web_client)

    r = web_client.delete(f"/api/devices/599001/objects/{ai['id']}")
    assert r.status_code == 204


def test_batch_delete_refuses_a_referenced_object_left_behind(web_client):
    _create_device(web_client)
    nc, _ = _create_notification_class_and_alarming_object(web_client)
    other = _create_object(web_client, name="AV-9", instance_number=9)

    r = web_client.request(
        "DELETE",
        "/api/devices/599001/objects/batch",
        json={"object_ids": [nc["id"], other["id"]]},
    )
    assert r.status_code == 409
    assert {"NC-5", "AV-9"} <= _object_names(web_client)


def test_batch_delete_allows_referrer_and_target_together(web_client):
    _create_device(web_client)
    nc, ai = _create_notification_class_and_alarming_object(web_client)

    r = web_client.request(
        "DELETE",
        "/api/devices/599001/objects/batch",
        json={"object_ids": [nc["id"], ai["id"]]},
    )
    assert r.status_code == 200, r.text
    assert _object_names(web_client) == set()


def test_csv_sync_that_drops_a_referenced_calendar_is_refused(web_client):
    _create_device(web_client)
    _create_calendar_and_referencing_schedule(web_client)
    schedule_csv = web_client.get("/api/devices/599001/objects/export.csv").text
    # Keep only the header and the schedule row -- the calendar is dropped.
    lines = schedule_csv.splitlines()
    kept = [lines[0]] + [line for line in lines[1:] if line.startswith("schedule,")]

    r = web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", "\n".join(kept) + "\n", "text/csv")},
    )
    assert r.status_code == 409
    assert "Cal-3" in _object_names(web_client)


def test_csv_merge_adding_a_schedule_with_a_missing_calendar_is_refused(web_client):
    _create_device(web_client)
    _create_calendar_and_referencing_schedule(web_client)
    exported = web_client.get("/api/devices/599001/objects/export.csv").text
    schedule_row = next(line for line in exported.splitlines() if line.startswith("schedule,"))
    # Same schedule at a new instance/name, pointing at calendar 3 -> 4 (missing).
    new_row = schedule_row.replace("schedule,1,Sched-1,", "schedule,2,Sched-2,", 1).replace(
        '""instance_number"": 3', '""instance_number"": 4'
    )
    assert new_row != schedule_row and '""instance_number"": 4' in new_row

    r = web_client.post(
        "/api/devices/599001/objects/import",
        files={"file": ("objects.csv", exported.splitlines()[0] + "\n" + new_row + "\n", "text/csv")},
    )
    assert r.status_code == 409
    assert "Sched-2" not in _object_names(web_client)


def test_deleting_a_local_object_is_not_blocked_by_a_remote_target_with_the_same_id(web_client):
    _create_device(web_client)
    av = _create_object(web_client, name="AV-3", instance_number=3)
    r = _create_schedule_object(
        web_client,
        schedule_write_targets=[
            {"device_instance": 599005, "object_type": "analog-value", "instance_number": 3}
        ],
    )
    assert r.status_code == 201, r.text
    assert web_client.delete(f"/api/devices/599001/objects/{av['id']}").status_code in (200, 204)
