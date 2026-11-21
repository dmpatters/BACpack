"""BUFFER_READY reporting config through the object API."""
from __future__ import annotations

import json

from tests.web._helpers import _create_device, _create_object
from bacpack.models import get_or_create_history_counters, trend_state_of, TrendState

_FIELDS = (
    "trend_reporting_enabled",
    "trend_notification_threshold",
    "trend_notification_class_instance",
    "trend_event_enable",
    "trend_notify_type",
    "trend_event_detection_enable",
    "trend_last_notify_record",
)


def _setup(client):
    _create_device(client)
    _create_object(client, object_type="notification-class", instance_number=1, name="NC-1", units=None)
    return _create_object(client, trended=True)


def _reporting_patch(**overrides):
    body = {"trend_reporting_enabled": True, "trend_notification_class_instance": 1}
    body.update(overrides)
    return body


def _set_next_sequence(client, object_id, value):
    from bacpack.models import BacnetObject

    with client.app.state.session_factory() as session:
        trend_state_of(session.get(BacnetObject, object_id)).next_sequence = value
        session.commit()


def test_objects_carry_the_reporting_fields_with_defaults(web_client):
    av = _setup(web_client)
    assert {f: av[f] for f in _FIELDS} == {
        "trend_reporting_enabled": False,
        "trend_notification_threshold": 10,
        "trend_notification_class_instance": None,
        "trend_event_enable": None,
        "trend_notify_type": "event",
        "trend_event_detection_enable": True,
        "trend_last_notify_record": 0,
    }


def test_enabling_reporting_round_trips(web_client):
    av = _setup(web_client)
    r = web_client.put(
        f"/api/devices/599001/objects/{av['id']}",
        json=_reporting_patch(trend_notification_threshold=3, trend_notify_type="alarm", trend_event_enable=[True, False, True]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trend_reporting_enabled"] is True
    assert body["trend_notification_threshold"] == 3
    assert body["trend_notify_type"] == "alarm"
    assert body["trend_event_enable"] == [True, False, True]


def test_enabling_seeds_last_notify_record_and_resaving_does_not(web_client):
    av = _setup(web_client)
    _set_next_sequence(web_client, av["id"], 51)
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch())
    assert r.json()["trend_last_notify_record"] == 50

    _set_next_sequence(web_client, av["id"], 61)
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch(trend_notification_threshold=4))
    assert r.json()["trend_last_notify_record"] == 50


def test_last_notify_record_in_an_update_body_is_ignored(web_client):
    av = _setup(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json={"trend_last_notify_record": 999})
    assert r.status_code == 200
    assert r.json()["trend_last_notify_record"] == 0


def test_reporting_requires_trended(web_client):
    _setup(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 2, "name": "AV-2", "units": "percent", **_reporting_patch()},
    )
    assert r.status_code == 400
    assert "trended" in r.json()["detail"]


def test_turning_trended_off_with_reporting_on_is_refused(web_client):
    av = _setup(web_client)
    web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch())
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json={"trended": False})
    assert r.status_code == 400
    assert "trended" in r.json()["detail"]


def test_reporting_requires_an_existing_notification_class(web_client):
    av = _setup(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch(trend_notification_class_instance=None))
    assert r.status_code == 400
    r = web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch(trend_notification_class_instance=9))
    assert r.status_code == 409
    assert "notification-class:9 as its trend log's notification class" in r.json()["detail"]


def test_bad_reporting_values_are_refused(web_client):
    av = _setup(web_client)
    url = f"/api/devices/599001/objects/{av['id']}"
    assert web_client.put(url, json=_reporting_patch(trend_notification_threshold=-1)).status_code == 422
    assert web_client.put(url, json=_reporting_patch(trend_notification_threshold=2**32)).status_code == 422
    assert web_client.put(url, json=_reporting_patch(trend_notify_type="ackNotification")).status_code == 422
    assert web_client.put(url, json=_reporting_patch(trend_event_enable=[True, True])).status_code == 400
    assert web_client.put(url, json={"trend_notification_threshold": None}).status_code == 422


def test_batch_enable_validates_and_seeds(web_client):
    av = _setup(web_client)
    _set_next_sequence(web_client, av["id"], 21)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [av["id"]], "patch": _reporting_patch(trend_notification_class_instance=9)},
    )
    assert r.status_code == 409
    r = web_client.patch("/api/devices/599001/objects/batch", json={"object_ids": [av["id"]], "patch": _reporting_patch()})
    assert r.status_code == 200, r.text
    assert r.json()[0]["trend_last_notify_record"] == 20


def test_deleting_a_notification_class_a_trend_log_reports_to_is_refused(web_client):
    av = _setup(web_client)
    web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch())
    nc_id = next(o["id"] for o in web_client.get("/api/devices/599001/objects").json() if o["name"] == "NC-1")
    r = web_client.delete(f"/api/devices/599001/objects/{nc_id}")
    assert r.status_code == 409
    assert "as its trend log's notification class" in r.json()["detail"]


def test_config_round_trip_keeps_reporting_and_last_notify_record(web_client):
    av = _setup(web_client)
    web_client.put(f"/api/devices/599001/objects/{av['id']}", json=_reporting_patch(trend_notification_threshold=4))
    _set_next_sequence(web_client, av["id"], 31)
    from bacpack.models import BacnetObject

    with web_client.app.state.session_factory() as session:
        trend_state_of(session.get(BacnetObject, av["id"])).last_notify_record = 25
        session.commit()

    doc = web_client.get("/api/config/export").json()
    exported = next(o for o in doc["devices"][0]["objects"] if o["name"] == "AV-1")
    assert exported["trend_reporting_enabled"] is True
    assert exported["trend_notification_threshold"] == 4
    assert "trend_last_notify_record" not in exported

    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text
    after = web_client.get(f"/api/devices/599001/objects/{av['id']}").json()
    assert after["trend_reporting_enabled"] is True
    assert after["trend_notification_threshold"] == 4
    assert after["trend_last_notify_record"] == 25  # already on: not reseeded


def test_config_import_turning_reporting_on_seeds(web_client):
    av = _setup(web_client)
    _set_next_sequence(web_client, av["id"], 41)
    doc = web_client.get("/api/config/export").json()
    exported = next(o for o in doc["devices"][0]["objects"] if o["name"] == "AV-1")
    exported.update(_reporting_patch())
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert web_client.get(f"/api/devices/599001/objects/{av['id']}").json()["trend_last_notify_record"] == 40
