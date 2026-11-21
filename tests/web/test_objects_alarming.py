"""Alarming config and notification-class objects through the object API:
create, update and batch validation."""
from __future__ import annotations

import pytest

from tests.web._helpers import _create_device, _create_object


# --- Alarming config through the JSON API ------------------------
# These tests use the `web_client` fixture from tests/conftest.py.


def test_create_notification_class_object(web_client):
    resp = web_client.post("/api/devices", json={"id": 599001, "name": "D1"})
    device_id = resp.json()["id"]
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["notification_priority"] is None
    assert body["notification_recipient_list"] is None


def test_create_object_with_alarming_enabled_requires_valid_notification_class(web_client):
    resp = web_client.post("/api/devices", json={"id": 599001, "name": "D1"})
    device_id = resp.json()["id"]
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "notification_class_instance": 99,
            "alarm_high_limit": 100.0,
        },
    )
    assert resp.status_code == 409
    assert "does not exist" in resp.json()["detail"]


def test_create_object_with_alarming_enabled_and_valid_notification_class(web_client):
    resp = web_client.post("/api/devices", json={"id": 599001, "name": "D1"})
    device_id = resp.json()["id"]
    web_client.post(
        f"/api/devices/{device_id}/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "notification_class_instance": 1,
            "alarm_high_limit": 100.0,
            "alarm_low_limit": 0.0,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["alarming_enabled"] is True


def test_create_notification_class_rejects_bad_priority_length(web_client):
    resp = web_client.post("/api/devices", json={"id": 599001, "name": "D1"})
    device_id = resp.json()["id"]
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "notification-class",
            "instance_number": 1,
            "name": "NC-1",
            "notification_priority": [100, 100],
        },
    )
    assert resp.status_code == 400


# --- update_object/batch_update_objects notification-class validation -----
# The tests above only exercise create_object's equivalent paths.
# update_object and batch_update_objects need the same notification-class
# shape and existence checks on their edit paths; both run through
# object_config.


def test_update_object_rejects_change_to_invalid_notification_class_instance(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )

    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"notification_class_instance": 99},
    )
    assert r.status_code == 409, r.text
    assert "does not exist" in r.json()["detail"]


def test_update_object_allows_change_to_valid_notification_class_instance(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 2, "name": "NC-2"},
    )
    obj = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )

    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"notification_class_instance": 2},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notification_class_instance"] == 2


def test_update_notification_class_rejects_bad_priority_length(web_client):
    _create_device(web_client)
    nc = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    ).json()

    r = web_client.put(
        f"/api/devices/599001/objects/{nc['id']}",
        json={"notification_priority": [100, 100]},
    )
    assert r.status_code == 400, r.text


def test_update_notification_class_allows_valid_priority_change(web_client):
    _create_device(web_client)
    nc = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    ).json()

    r = web_client.put(
        f"/api/devices/599001/objects/{nc['id']}",
        json={"notification_priority": [50, 50, 50]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notification_priority"] == [50, 50, 50]


def test_batch_update_objects_rejects_invalid_notification_class_instance(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj1 = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        instance_number=1,
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )
    obj2 = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-2",
        instance_number=2,
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [obj1["id"], obj2["id"]],
            "patch": {"notification_class_instance": 99},
        },
    )
    assert r.status_code == 409, r.text
    assert "does not exist" in r.json()["detail"]


def test_batch_update_objects_allows_valid_notification_class_instance(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 2, "name": "NC-2"},
    )
    obj1 = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        instance_number=1,
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )
    obj2 = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-2",
        instance_number=2,
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [obj1["id"], obj2["id"]],
            "patch": {"notification_class_instance": 2},
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert {o["notification_class_instance"] for o in updated} == {2}


def test_batch_update_objects_rejects_bad_notification_class_priority_shape(web_client):
    _create_device(web_client)
    nc1 = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    ).json()
    nc2 = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 2, "name": "NC-2"},
    ).json()

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [nc1["id"], nc2["id"]],
            "patch": {"notification_priority": [100, 100]},
        },
    )
    assert r.status_code == 400, r.text


# --- Alarming
# config validated at save time, not left to crash engine-start ----------


def test_create_object_rejects_binary_alarming_with_no_alarm_value(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "binary-value",
            "instance_number": 1,
            "name": "BV-1",
            "alarming_enabled": True,
            "notification_class_instance": 1,
        },
    )
    assert r.status_code == 400, r.text
    assert "Alarming tab" in r.json()["detail"]


def test_create_object_rejects_analog_alarming_with_no_limits(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "notification_class_instance": 1,
        },
    )
    assert r.status_code == 400, r.text
    assert "Alarming tab" in r.json()["detail"]


def test_create_object_allows_analog_alarming_with_one_sided_limit(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "notification_class_instance": 1,
            "alarm_low_limit": 0.0,
        },
    )
    assert r.status_code == 201, r.text


def test_create_object_rejects_alarming_enabled_with_no_notification_class(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "alarm_high_limit": 100.0,
        },
    )
    assert r.status_code == 400, r.text
    assert "notification_class_instance" in r.json()["detail"]


def test_update_object_rejects_clearing_binary_alarm_value_while_alarming_enabled(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj = _create_object(
        web_client,
        object_type="binary-value",
        name="BV-1",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_value=True,
    )
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"alarm_value": None},
    )
    assert r.status_code == 400, r.text
    assert "Alarming tab" in r.json()["detail"]


def test_update_object_rejects_clearing_both_analog_limits_while_alarming_enabled(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
    )
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"alarm_high_limit": None},
    )
    assert r.status_code == 400, r.text


def test_update_object_rejects_enabling_alarming_with_no_notification_class(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        units="degreesFahrenheit",
        alarm_high_limit=100.0,
    )
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"alarming_enabled": True},
    )
    assert r.status_code == 400, r.text
    assert "notification_class_instance" in r.json()["detail"]


def test_batch_update_objects_rejects_clearing_alarm_value_while_alarming_enabled(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj1 = _create_object(
        web_client,
        object_type="binary-value",
        name="BV-1",
        instance_number=1,
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_value=True,
    )
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [obj1["id"]],
            "patch": {"alarm_value": None},
        },
    )
    assert r.status_code == 400, r.text
    assert "Alarming tab" in r.json()["detail"]


def test_event_detection_enable_round_trips_through_the_api(web_client):
    device_id = web_client.post("/api/devices", json={"id": 599001, "name": "D1"}).json()["id"]
    web_client.post(
        f"/api/devices/{device_id}/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    created = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "units": "degreesFahrenheit",
            "alarming_enabled": True,
            "notification_class_instance": 1,
            "alarm_high_limit": 100.0,
        },
    ).json()
    assert created["alarm_event_detection_enable"] is True

    resp = web_client.put(
        f"/api/devices/{device_id}/objects/{created['id']}",
        json={"alarm_event_detection_enable": False},
    )
    assert resp.status_code == 200
    assert resp.json()["alarm_event_detection_enable"] is False


# --- The rest of the alarm settings -----------------------------------------


def _alarming_ai(web_client, **overrides):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    return _create_object(
        web_client,
        object_type="analog-input",
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        **overrides,
    )


def test_create_rejects_negative_alarm_time_delay(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1",
            "alarming_enabled": True,
            "notification_class_instance": 1,
            "alarm_high_limit": 100.0,
            "alarm_time_delay": -5,
        },
    )
    # The schema's ge=0 refuses it first.
    assert r.status_code == 422, r.text


def test_update_rejects_enabling_a_limit_side_with_no_limit(web_client):
    obj = _alarming_ai(web_client)
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"alarm_limit_enable": [True, True]}
    )
    assert r.status_code == 400, r.text


def test_update_rejects_clearing_a_limit_its_limit_enable_still_uses(web_client):
    obj = _alarming_ai(web_client, alarm_low_limit=10.0, alarm_limit_enable=[True, True])
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"alarm_low_limit": None})
    assert r.status_code == 400, r.text


def test_batch_update_rejects_negative_alarm_time_delay(web_client):
    obj = _alarming_ai(web_client)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"alarm_time_delay": -1}},
    )
    # The schema's ge=0 refuses it first.
    assert r.status_code == 422, r.text


def test_update_rejects_shrinking_state_text_below_an_alarm_value(web_client):
    _create_device(web_client)
    web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    )
    obj = _create_object(
        web_client,
        object_type="multi-state-value",
        name="MSV-1",
        units=None,
        state_text=["a", "b", "c"],
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_values=[3],
    )
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"state_text": ["a", "b"]})
    assert r.status_code == 400, r.text


# --- Recipients the device can't build are refused -------------------------


def _nc_recipient(**overrides):
    entry = {
        "recipient": {"kind": "address", "address": "192.168.1.200"},
        "valid_days": [True] * 7,
        "from_time": "00:00",
        "to_time": "23:59",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": 1,
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "entry",
    [
        _nc_recipient(recipient={"kind": "address", "address": "not-an-address"}),
        _nc_recipient(from_time="nope"),
        _nc_recipient(recipient={"kind": "device", "instance_number": "abc"}),
        _nc_recipient(recipient={"kind": "device", "instance_number": 99999999}),
    ],
)
def test_create_notification_class_refuses_a_recipient_it_cant_build(web_client, entry):
    device_id = web_client.post("/api/devices", json={"id": 599001, "name": "D1"}).json()["id"]
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "notification-class",
            "instance_number": 1,
            "name": "NC-1",
            "notification_recipient_list": [entry],
        },
    )
    assert resp.status_code == 400
    assert web_client.get(f"/api/devices/{device_id}/objects").json() == []


def test_create_notification_class_saves_a_recipient_kind_in_any_case(web_client):
    device_id = web_client.post("/api/devices", json={"id": 599001, "name": "D1"}).json()["id"]
    resp = web_client.post(
        f"/api/devices/{device_id}/objects",
        json={
            "object_type": "notification-class",
            "instance_number": 1,
            "name": "NC-1",
            "notification_recipient_list": [_nc_recipient(recipient={"kind": "Device", "instance_number": 5})],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["notification_recipient_list"][0]["recipient"]["kind"] == "device"
