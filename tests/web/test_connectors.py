"""Pull connector and connector-mapping CRUD, URL-scheme and json_path
guardrails, preview, poller status, and which object types can be a
mapping target."""
from __future__ import annotations

import httpx

from tests.web._helpers import _create_connector, _create_device, _create_object, _create_schedule_object


# --- Pull connectors -------------------------------------------------------


def test_connector_crud(web_client):
    assert web_client.get("/api/connectors").json() == []

    created = _create_connector(web_client)
    assert created["enabled"] is True
    assert created["poll_interval_seconds"] == 30
    assert "auth_password" not in created

    r = web_client.post("/api/connectors", json={"name": "Weather", "url": "http://x"})
    assert r.status_code == 409  # duplicate name

    r = web_client.put(f"/api/connectors/{created['id']}", json={"poll_interval_seconds": 60})
    assert r.status_code == 200
    assert r.json()["poll_interval_seconds"] == 60

    r = web_client.delete(f"/api/connectors/{created['id']}")
    assert r.status_code == 204
    assert web_client.get("/api/connectors").json() == []


# --- Connector URL scheme guardrail ---------------------------------------
# Not real SSRF hardening (private/loopback/metadata addresses are still
# allowed, by design -- see the docstring on schemas._require_http_scheme).
# Just a cheap rejection of obviously-wrong values that used to be accepted
# and only fail later, confusingly, as a poll-time transport error.


def test_create_connector_rejects_non_http_scheme(web_client):
    r = web_client.post("/api/connectors", json={"name": "Bad", "url": "file:///etc/passwd"})
    assert r.status_code == 422


def test_create_connector_rejects_schemeless_url(web_client):
    r = web_client.post("/api/connectors", json={"name": "Bad", "url": "example.invalid/status"})
    assert r.status_code == 422


def test_create_connector_allows_https(web_client):
    r = web_client.post("/api/connectors", json={"name": "Weather", "url": "https://example.invalid"})
    assert r.status_code == 201, r.text


def test_update_connector_rejects_non_http_scheme(web_client):
    created = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{created['id']}", json={"url": "javascript:alert(1)"})
    assert r.status_code == 422


def test_connector_mapping_crud(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)

    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "data.temp", "scale": 2.0},
    )
    assert r.status_code == 201, r.text
    mapping = r.json()
    assert mapping["scale"] == 2.0

    # a second mapping onto the same object is rejected -- unique per object
    other_connector = _create_connector(web_client, name="Other")
    r = web_client.post(
        f"/api/connectors/{other_connector['id']}/mappings",
        json={"object_id": obj["id"]},
    )
    assert r.status_code == 409

    r = web_client.put(
        f"/api/connectors/{connector['id']}/mappings/{mapping['id']}", json={"offset": 5.0}
    )
    assert r.status_code == 200
    assert r.json()["offset"] == 5.0

    r = web_client.delete(f"/api/connectors/{connector['id']}/mappings/{mapping['id']}")
    assert r.status_code == 204
    assert web_client.get(f"/api/connectors/{connector['id']}/mappings").json() == []


def test_connectors_status_endpoint_empty_when_nothing_running(web_client):
    assert web_client.get("/api/connectors/status").json() == {}


# --- Connector preview / JSON path picker ---------------------------


def test_preview_connector_returns_flattened_fields(web_client):
    connector = _create_connector(web_client)

    def handler(request):
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    web_client.app.state.connector_poller._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    r = web_client.post(f"/api/connectors/{connector['id']}/preview")
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "response_format": "json",
        "fields": [{"path": "data.temp", "value": 72.5, "is_array": False}],
    }


def test_preview_connector_404_for_missing_connector(web_client):
    r = web_client.post("/api/connectors/999999/preview")
    assert r.status_code == 404


def test_preview_connector_upstream_failure_is_ok_false_not_500(web_client):
    connector = _create_connector(web_client)

    def handler(request):
        return httpx.Response(500)

    web_client.app.state.connector_poller._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    r = web_client.post(f"/api/connectors/{connector['id']}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


def test_deleting_object_cascades_its_connector_mapping(web_client):
    """Regression test: without BacnetObject.connector_mapping's cascade,
    the mapping row outlives its object_id FK target (no PRAGMA
    foreign_keys enforcement), leaving ConnectorMapping.object resolving to
    None -- previously this crashed the connector detail *page*; that page
    is gone under the SPA, but the underlying cascade still needs to hold
    since the SPA's connector detail view walks the same relationship via
    the JSON API.
    """
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    )

    r = web_client.delete(f"/api/devices/599001/objects/{obj['id']}")
    assert r.status_code == 204

    assert web_client.get(f"/api/connectors/{connector['id']}/mappings").json() == []
    r = web_client.get(f"/api/connectors/{connector['id']}")
    assert r.status_code == 200


def test_connector_update_preserves_secret_when_field_omitted(web_client):
    """The JSON API's own contract for "leave blank to keep the current
    secret": ConnectorUpdate fields are Optional with exclude_unset=True, so
    a PUT body that omits auth_password entirely leaves it untouched -- the
    SPA's edit form relies on this by not sending the key at all when the
    password field is left blank, same behavior the old edit-page route
    implemented server-side by hand.
    """
    connector = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="p")

    r = web_client.put(
        f"/api/connectors/{connector['id']}",
        json={"auth_username": "u"},  # auth_password key omitted, not sent as ""
    )
    assert r.status_code == 200

    with web_client.app.state.session_factory() as session:
        from bacpack.models import Connector

        saved = session.get(Connector, connector["id"])
        assert saved.auth_password == "p"


# --- Unguarded IntegrityErrors -> raw 500s -----------------------------------
# The same class fixed for objects in the prior session (name/instance-number
# collisions -> clean 409s instead of a bare sqlite3.IntegrityError); this
# time for connector rename, and for an explicit `null` on a NOT NULL column
# that both create_object's own checks and Pydantic's Optional typing let
# straight through to setattr().


def test_update_connector_rejects_rename_to_existing_name(web_client):
    _create_connector(web_client, name="Weather")
    other = _create_connector(web_client, name="Other")
    r = web_client.put(f"/api/connectors/{other['id']}", json={"name": "Weather"})
    assert r.status_code == 409


def test_update_connector_allows_renaming_to_its_own_current_name(web_client):
    connector = _create_connector(web_client, name="Weather")
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"name": "Weather"})
    assert r.status_code == 200


def test_update_connector_rejects_explicit_null_name(web_client):
    connector = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"name": None})
    assert r.status_code == 422


def test_update_connector_rejects_explicit_null_url(web_client):
    connector = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"url": None})
    assert r.status_code == 422


def test_update_connector_rejects_blank_url(web_client):
    connector = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"url": ""})
    assert r.status_code == 422


# --- json_path required for a json-format connector -----------------------
# extract_json_path(body, None) raised AttributeError on every poll --
# caught by ConnectorPoller's broad handler and only logged, so a mapping
# created without a json_path was silently dead from the start.


def test_create_mapping_requires_json_path_for_json_connector(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)  # response_format defaults to "json"
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    )
    assert r.status_code == 400
    assert "json_path" in r.json()["detail"]


def test_create_mapping_allows_missing_json_path_for_plain_text_connector(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client, response_format="plain-text")
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    )
    assert r.status_code == 201, r.text


def test_update_mapping_rejects_clearing_json_path_for_json_connector(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "data.temp"},
    ).json()
    r = web_client.put(
        f"/api/connectors/{connector['id']}/mappings/{mapping['id']}", json={"json_path": None}
    )
    assert r.status_code == 400


def test_create_mapping_rejects_schedule_target(web_client):
    _create_device(web_client)
    obj = _create_schedule_object(web_client).json()
    connector = _create_connector(web_client)
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    )
    assert r.status_code == 400
    assert "schedule objects can't be a connector-mapping target" in r.json()["detail"]


def test_create_mapping_rejects_calendar_target(web_client):
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
    connector = _create_connector(web_client)
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    )
    assert r.status_code == 400
    assert "calendar objects can't be a connector-mapping target" in r.json()["detail"]


# --- Explicit nulls ---------------------------------------------------------
# Each of these columns is NOT NULL; an explicit null reached setattr() and
# failed the commit as a 500.


def test_update_connector_rejects_null_for_required_fields(web_client):
    connector = _create_connector(web_client)
    for field in ("enabled", "poll_interval_seconds", "timeout_seconds", "response_format", "auth_type"):
        r = web_client.put(f"/api/connectors/{connector['id']}", json={field: None})
        assert r.status_code == 422, (field, r.text)


def test_update_mapping_rejects_null_scale_or_offset(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "a.b"},
    ).json()
    for field in ("scale", "offset"):
        r = web_client.put(
            f"/api/connectors/{connector['id']}/mappings/{mapping['id']}", json={field: None}
        )
        assert r.status_code == 422, (field, r.text)


# --- BACnet connectors --------------------------------------------


def _bacnet(web_client, device_id=599001, **overrides):
    body = {"name": "Remote", "type": "bacnet", "device_id": device_id, "remote_device_instance": 1234}
    body.update(overrides)
    return web_client.post("/api/connectors", json=body)


def _add_object(web_client, device_id, object_type, instance_number, name):
    from bacpack.models import BacnetObject

    with web_client.app.state.session_factory() as session:
        obj = BacnetObject(device_id=device_id, object_type=object_type, instance_number=instance_number, name=name)
        session.add(obj)
        session.commit()
        return obj.id


def test_create_bacnet_connector(web_client):
    _create_device(web_client)

    r = _bacnet(web_client, remote_address=" 10.0.0.9:47809 ", url="http://ignored")

    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["type"], body["url"], body["device_id"]) == ("bacnet", "", 599001)
    assert (body["remote_device_instance"], body["remote_address"]) == (1234, "10.0.0.9:47809")
    assert (body["rewrite_interval_seconds"], body["write_mapping_count"]) == (60, 0)


def test_bacnet_connector_requires_device_and_remote_instance(web_client):
    _create_device(web_client)
    r = web_client.post("/api/connectors", json={"name": "R", "type": "bacnet", "remote_device_instance": 5})
    assert r.status_code == 422
    r = web_client.post("/api/connectors", json={"name": "R", "type": "bacnet", "device_id": 599001})
    assert r.status_code == 422


def test_bacnet_connector_refuses_unknown_device_itself_and_bad_address(web_client):
    _create_device(web_client)
    assert _bacnet(web_client, device_id=42).status_code == 422
    assert _bacnet(web_client, remote_device_instance=599001).status_code == 422
    assert _bacnet(web_client, remote_address="somehost").status_code == 422
    assert _bacnet(web_client, remote_device_instance=4194303).status_code == 422


def test_http_connector_still_requires_a_url(web_client):
    assert web_client.post("/api/connectors", json={"name": "W"}).status_code == 422


def test_create_http_connector_ignores_the_bacnet_fields(web_client):
    # A device_id would tie the HTTP connector to that
    # device (cascade-deleted with it), as update_connector already prevents.
    _create_device(web_client)
    r = web_client.post(
        "/api/connectors",
        json={
            "name": "W", "url": "http://x", "device_id": 599001, "remote_device_instance": 5,
            "remote_address": "10.0.0.9", "rewrite_interval_seconds": 0,
        },
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["type"], body["device_id"], body["remote_device_instance"], body["remote_address"]) == (
        "http-poll", None, None, None,
    )
    assert body["rewrite_interval_seconds"] == 60
    assert web_client.delete("/api/devices/599001").status_code in (200, 204)
    assert [c["name"] for c in web_client.get("/api/connectors").json()] == ["W"]


def test_update_bacnet_connector(web_client):
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    connector = _bacnet(web_client, remote_address="10.0.0.9").json()

    r = web_client.put(
        f"/api/connectors/{connector['id']}",
        json={"remote_address": "", "rewrite_interval_seconds": 0, "url": "http://x", "type": "http-poll", "device_id": 599002},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["remote_address"], body["rewrite_interval_seconds"], body["url"]) == (None, 0, "")
    assert (body["type"], body["device_id"]) == ("bacnet", 599001)
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"remote_device_instance": 599001})
    assert r.status_code == 422


def test_bacnet_read_mapping_is_normalized(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()

    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "remote_object_type": "analogInput", "remote_object_instance": 3, "json_path": "x"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["remote_object_type"], body["remote_object_instance"]) == ("analog-input", 3)
    assert (body["remote_property"], body["remote_array_index"], body["mode"], body["json_path"]) == (
        "present-value", None, "poll", None,
    )


def test_update_bacnet_read_mapping_ignores_json_path(web_client):
    """A bacnet connector's response_format still defaults to "json", so
    the pre-existing json-format-requires-json_path check (from the
    http-poll path) must not fire for a bacnet read mapping's inert
    json_path field."""
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "remote_object_type": "analog-input", "remote_object_instance": 3},
    ).json()

    r = web_client.put(
        f"/api/connectors/{connector['id']}/mappings/{mapping['id']}",
        json={"json_path": "", "scale": 2.0},
    )

    assert r.status_code == 200, r.text
    assert r.json()["scale"] == 2.0


def test_bacnet_read_mapping_validation(web_client):
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    obj = _create_object(web_client)
    other = _add_object(web_client, 599002, "analog-value", 1, "AV-other")
    connector = _bacnet(web_client).json()
    url = f"/api/connectors/{connector['id']}/mappings"

    assert web_client.post(url, json={"object_id": obj["id"]}).status_code == 422
    assert web_client.post(
        url, json={"object_id": obj["id"], "remote_object_type": "bogus", "remote_object_instance": 1}
    ).status_code == 422
    assert web_client.post(
        url, json={"object_id": other, "remote_object_type": "analog-input", "remote_object_instance": 1}
    ).status_code == 422


def test_write_mapping_crud(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()
    url = f"/api/connectors/{connector['id']}/write-mappings"

    r = web_client.post(url, json={"object_id": obj["id"], "remote_object_type": "binaryOutput", "remote_object_instance": 4})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created == {
        "id": created["id"], "connector_id": connector["id"], "object_id": obj["id"],
        "remote_object_type": "binary-output", "remote_object_instance": 4,
        "remote_property": "present-value", "remote_array_index": None, "priority": 8,
    }
    assert web_client.get(f"/api/connectors/{connector['id']}").json()["write_mapping_count"] == 1

    r = web_client.put(f"{url}/{created['id']}", json={"priority": 12})
    assert (r.status_code, r.json()["priority"]) == (200, 12)
    assert [m["id"] for m in web_client.get(url).json()] == [created["id"]]

    assert web_client.delete(f"{url}/{created['id']}").status_code == 204
    assert web_client.get(url).json() == []


def test_write_mapping_rules(web_client):
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    obj = _create_object(web_client)
    other = _add_object(web_client, 599002, "analog-value", 1, "AV-other")
    nc = _add_object(web_client, 599001, "notification-class", 1, "NC-1")
    connector = _bacnet(web_client).json()
    http = _create_connector(web_client)
    url = f"/api/connectors/{connector['id']}/write-mappings"
    target = {"remote_object_type": "analog-value", "remote_object_instance": 7}

    assert web_client.post(f"/api/connectors/{http['id']}/write-mappings", json={"object_id": obj["id"], **target}).status_code == 400
    assert web_client.post(url, json={"object_id": other, **target}).status_code == 422
    assert web_client.post(url, json={"object_id": nc, **target}).status_code == 400
    assert web_client.post(url, json={"object_id": obj["id"], **target, "priority": 17}).status_code == 422

    first = web_client.post(url, json={"object_id": obj["id"], **target}).json()
    assert web_client.post(url, json={"object_id": obj["id"], **target}).status_code == 409
    second = web_client.post(
        url, json={"object_id": obj["id"], "remote_object_type": "analog-value", "remote_object_instance": 8}
    ).json()
    assert web_client.put(f"{url}/{second['id']}", json={"remote_object_instance": 7}).status_code == 409
    assert web_client.put(f"{url}/{first['id']}", json={"priority": 3}).status_code == 200


def test_status_includes_bacnet_client_status_for_running_devices(web_client):
    from bacpack.models import Connector, Device

    _create_device(web_client)
    connector = _bacnet(web_client).json()
    session_factory = web_client.app.state.session_factory
    with session_factory() as session:
        session.get(Connector, connector["id"]).runtime.client_status = {"last_success": True, "consecutive_failures": 0}
        session.commit()

    assert str(connector["id"]) not in web_client.get("/api/connectors/status").json()

    with session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "running"
        session.commit()

    status = web_client.get("/api/connectors/status").json()
    assert status[str(connector["id"])] == {"last_success": True, "consecutive_failures": 0}


def test_preview_is_refused_for_a_bacnet_connector(web_client):
    _create_device(web_client)
    connector = _bacnet(web_client).json()
    assert web_client.post(f"/api/connectors/{connector['id']}/preview").status_code == 400


def test_deleting_the_device_deletes_its_bacnet_connector(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()
    web_client.post(
        f"/api/connectors/{connector['id']}/write-mappings",
        json={"object_id": obj["id"], "remote_object_type": "analog-value", "remote_object_instance": 7},
    )

    assert web_client.delete("/api/devices/599001").status_code == 204
    assert web_client.get("/api/connectors").json() == []


def test_connector_choice_fields_ignore_case(web_client):
    r = web_client.post(
        "/api/connectors",
        json={"name": "W", "url": "http://example.invalid/x", "type": "HTTP-Poll",
              "response_format": "Plain-Text", "auth_type": "NONE"},
    )
    assert r.status_code == 201, r.text
    assert (r.json()["type"], r.json()["response_format"], r.json()["auth_type"]) == ("http-poll", "plain-text", "none")
    r = web_client.put(f"/api/connectors/{r.json()['id']}", json={"response_format": "JSON"})
    assert r.status_code == 200, r.text
    assert r.json()["response_format"] == "json"


def test_bacnet_mapping_remote_names_ignore_case(web_client):
    _create_device(web_client)
    connector = _bacnet(web_client).json()
    obj = _create_object(web_client)
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "remote_object_type": "Analog-Input", "remote_object_instance": 1,
              "remote_property": "Present-Value"},
    )
    assert r.status_code == 201, r.text
    assert (r.json()["remote_object_type"], r.json()["remote_property"]) == ("analog-input", "present-value")


def test_object_list_says_which_connector_feeds_each_object(web_client):
    _create_device(web_client)
    connector = _bacnet(web_client, name="AHU feed").json()
    fed = _create_object(web_client, instance_number=1, name="AV-1")
    free = _create_object(web_client, instance_number=2, name="AV-2")
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": fed["id"], "remote_object_type": "analog-input", "remote_object_instance": 1},
    ).json()

    by_id = {o["id"]: o for o in web_client.get("/api/devices/599001/objects").json()}
    assert (by_id[fed["id"]]["fed_by_connector_id"], by_id[fed["id"]]["fed_by_connector_name"]) == (connector["id"], "AHU feed")
    assert (by_id[free["id"]]["fed_by_connector_id"], by_id[free["id"]]["fed_by_connector_name"]) == (None, None)

    web_client.delete(f"/api/connectors/{connector['id']}/mappings/{mapping['id']}")
    single = web_client.get(f"/api/devices/599001/objects/{fed['id']}").json()
    assert single["fed_by_connector_id"] is None


def test_create_mapping_rejects_notification_class_target(web_client):
    # No Present_Value to feed -- the mapping failed every poll.
    _create_device(web_client)
    obj = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1"},
    ).json()
    connector = _create_connector(web_client)
    r = web_client.post(f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]})
    assert r.status_code == 400
    assert "notification-class objects can't be a connector-mapping target" in r.json()["detail"]


# --- HTTP connectors let go of their points ----------------------------------
# A pull connector writes at priority 10. Deleting its mapping, or disabling
# or deleting it, left that value in place until the device restarted (BACnet
# connectors already released).

import pytest


def _http_mapping(web_client, *, running=True):
    from bacpack.models import BacnetObject, Device

    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "data.temp"},
    ).json()
    session_factory = web_client.app.state.session_factory
    with session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "running" if running else "stopped"
        row = session.get(BacnetObject, obj["id"])
        row.runtime.pending_feed_value, row.runtime.pending_feed_queued = None, False
        session.commit()
    return connector, mapping, obj


def _pending_feed(web_client, object_id):
    from bacpack.models import BacnetObject

    with web_client.app.state.session_factory() as session:
        row = session.get(BacnetObject, object_id)
        return row.runtime.pending_feed_queued, row.runtime.pending_feed_value


@pytest.mark.parametrize(
    "action",
    [
        lambda c, conn, m: c.delete(f"/api/connectors/{conn['id']}/mappings/{m['id']}"),
        lambda c, conn, m: c.delete(f"/api/connectors/{conn['id']}"),
        lambda c, conn, m: c.put(f"/api/connectors/{conn['id']}", json={"enabled": False}),
    ],
    ids=["delete-mapping", "delete-connector", "disable-connector"],
)
def test_http_connector_relinquishes_its_point(web_client, action):
    connector, mapping, obj = _http_mapping(web_client)
    r = action(web_client, connector, mapping)
    assert r.status_code in (200, 204), r.text
    assert _pending_feed(web_client, obj["id"]) == (True, None)


def test_no_relinquish_queued_for_a_stopped_device(web_client):
    connector, mapping, obj = _http_mapping(web_client, running=False)
    web_client.delete(f"/api/connectors/{connector['id']}/mappings/{mapping['id']}")
    assert _pending_feed(web_client, obj["id"]) == (False, None)


def test_renaming_an_enabled_connector_relinquishes_nothing(web_client):
    connector, mapping, obj = _http_mapping(web_client)
    web_client.put(f"/api/connectors/{connector['id']}", json={"name": "Renamed", "enabled": True})
    assert _pending_feed(web_client, obj["id"]) == (False, None)


def test_get_returns_header_names_not_values(web_client):
    # Header values (API keys) used to come back in full.
    created = _create_connector(web_client, headers={"X-Api-Key": "secret", "Accept": "application/json"})
    for body in (created, web_client.get(f"/api/connectors/{created['id']}").json(),
                 web_client.get("/api/connectors").json()[0]):
        assert "headers" not in body
        assert body["header_names"] == ["X-Api-Key", "Accept"]
    assert "secret" not in web_client.get("/api/connectors").text


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_connector_names_are_refused(web_client, name):
    assert web_client.post("/api/connectors", json={"name": name, "url": "http://x"}).status_code == 422
    created = _create_connector(web_client)
    assert web_client.put(f"/api/connectors/{created['id']}", json={"name": name}).status_code == 422


@pytest.mark.parametrize(
    "field, good, bad",
    [
        ("poll_interval_seconds", 86400, 86401),
        ("timeout_seconds", 300, 301),
        ("rewrite_interval_seconds", 86400, 86401),
    ],
)
def test_connector_numbers_are_bounded(web_client, field, good, bad):
    created = _create_connector(web_client)
    assert web_client.put(f"/api/connectors/{created['id']}", json={field: good}).status_code == 200
    assert web_client.put(f"/api/connectors/{created['id']}", json={field: bad}).status_code == 422
    r = web_client.post("/api/connectors", json={"name": "Other", "url": "http://x", field: bad})
    assert r.status_code == 422


def test_mapping_array_index_is_bounded(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(
        web_client, name="B", type="bacnet", device_id=599001, remote_device_instance=2
    )
    base = {"object_id": obj["id"], "remote_object_type": "analog-value", "remote_object_instance": 1}
    url = f"/api/connectors/{connector['id']}"
    assert web_client.post(f"{url}/mappings", json={**base, "remote_array_index": 2**32}).status_code == 422
    assert web_client.post(f"{url}/write-mappings", json={**base, "remote_array_index": 2**32}).status_code == 422


@pytest.mark.parametrize("value_map", [{"open": "1"}, {"open": None}, {"open": [1]}, {"open": {"a": 1}}])
def test_value_map_values_must_be_numbers_or_booleans(web_client, value_map):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "a", "value_map": value_map},
    )
    assert r.status_code == 422


def test_value_map_takes_numbers_and_booleans(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client)
    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "json_path": "a", "value_map": {"open": 1, "half": 0.5, "closed": False}},
    )
    assert r.status_code == 201, r.text


def _stored_headers(web_client, connector_id):
    from bacpack.models import Connector

    with web_client.app.state.session_factory() as session:
        return session.get(Connector, connector_id).headers


def test_connector_says_which_credentials_are_missing(web_client):
    created = _create_connector(
        web_client, auth_type="basic", auth_username="u", headers={"X-Api-Key": None, "Accept": "application/json"}
    )
    assert created["missing_credentials"] == ["password", "header X-Api-Key"]
    assert _stored_headers(web_client, created["id"]) == {"X-Api-Key": "", "Accept": "application/json"}
    bacnet = _create_connector(web_client, name="B", type="bacnet", device_id=_create_device(web_client)["id"],
                               remote_device_instance=2)
    assert bacnet["missing_credentials"] == []


def test_a_null_header_value_keeps_the_saved_one_and_a_left_out_name_goes(web_client):
    created = _create_connector(web_client, headers={"X-Api-Key": "secret", "Accept": "application/json"})
    r = web_client.put(f"/api/connectors/{created['id']}", json={"headers": {"X-Api-Key": None, "X-New": "n"}})
    assert r.status_code == 200, r.text
    assert _stored_headers(web_client, created["id"]) == {"X-Api-Key": "secret", "X-New": "n"}
    assert r.json()["header_names"] == ["X-Api-Key", "X-New"]
    assert r.json()["missing_credentials"] == []


def test_a_null_value_for_a_new_header_is_not_set(web_client):
    created = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{created['id']}", json={"headers": {"X-Api-Key": None}})
    assert r.json()["missing_credentials"] == ["header X-Api-Key"]


@pytest.mark.parametrize("name", ["", "  "])
def test_a_blank_header_name_is_refused(web_client, name):
    assert web_client.post("/api/connectors", json={"name": "H", "url": "http://x", "headers": {name: "v"}}).status_code == 422
    created = _create_connector(web_client)
    assert web_client.put(f"/api/connectors/{created['id']}", json={"headers": {name: "v"}}).status_code == 422


def test_header_values_must_be_strings(web_client):
    r = web_client.post("/api/connectors", json={"name": "H", "url": "http://x", "headers": {"X-N": 5}})
    assert r.status_code == 422


def test_switch_to_json_is_refused_while_a_mapping_has_no_path(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _create_connector(web_client, response_format="plain-text")
    mapping = web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": obj["id"]}
    ).json()

    r = web_client.put(f"/api/connectors/{connector['id']}", json={"response_format": "json"})

    assert r.status_code == 400
    assert str(mapping["id"]) in r.json()["detail"]
    assert web_client.get(f"/api/connectors/{connector['id']}").json()["response_format"] == "plain-text"


# --- verify_tls --------------------------------------------------------------


def test_connector_verifies_tls_by_default(web_client):
    assert _create_connector(web_client)["verify_tls"] is True


def test_verify_tls_can_be_set_on_create_and_update(web_client):
    connector = _create_connector(web_client, verify_tls=False)
    assert connector["verify_tls"] is False

    r = web_client.put(f"/api/connectors/{connector['id']}", json={"verify_tls": True})
    assert r.status_code == 200, r.text
    assert r.json()["verify_tls"] is True
    assert web_client.get(f"/api/connectors/{connector['id']}").json()["verify_tls"] is True


def test_update_connector_rejects_null_verify_tls(web_client):
    connector = _create_connector(web_client)
    r = web_client.put(f"/api/connectors/{connector['id']}", json={"verify_tls": None})
    assert r.status_code == 422


def test_bacnet_connector_ignores_verify_tls(web_client):
    _create_device(web_client)
    r = _bacnet(web_client, verify_tls=False)
    assert r.status_code == 201, r.text
    assert r.json()["verify_tls"] is True

    r = web_client.put(f"/api/connectors/{r.json()['id']}", json={"verify_tls": False})
    assert r.json()["verify_tls"] is True


# --- Proxy mappings -------------------------------------------------

_PROXY = {"direction": "proxy", "remote_object_type": "analog-value", "remote_object_instance": 5}


def test_proxy_mapping_is_created_and_listed(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()
    url = f"/api/connectors/{connector['id']}/mappings"

    r = web_client.post(url, json={"object_id": obj["id"], **_PROXY, "direction": "Proxy"})

    assert r.status_code == 201, r.text
    assert (r.json()["direction"], r.json()["remote_property"]) == ("proxy", "present-value")
    assert web_client.get(url).json()[0]["direction"] == "proxy"


def test_read_mapping_direction_defaults_to_read(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    connector = _bacnet(web_client).json()

    r = web_client.post(
        f"/api/connectors/{connector['id']}/mappings",
        json={"object_id": obj["id"], "remote_object_type": "analog-input", "remote_object_instance": 3},
    )

    assert r.status_code == 201, r.text
    assert r.json()["direction"] == "read"


def test_proxy_mapping_rules(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    ai = _add_object(web_client, 599001, "analog-input", 1, "AI-1")
    msv = _add_object(web_client, 599001, "multi-state-value", 1, "MSV-1")
    connector = _bacnet(web_client).json()
    http = _create_connector(web_client)
    url = f"/api/connectors/{connector['id']}/mappings"

    def refused(body, target=url):
        r = web_client.post(target, json=body)
        assert r.status_code == 422, r.text
        return r.json()["detail"]

    http_url = f"/api/connectors/{http['id']}/mappings"
    assert "bacnet" in refused({"object_id": obj["id"], **_PROXY, "json_path": "x"}, http_url)
    assert "commandable" in refused({"object_id": ai, **_PROXY})
    r = web_client.post(url, json={"object_id": msv, **_PROXY, "remote_object_type": "multi-state-value"})
    assert r.status_code == 201, r.text  # MSV is commandable
    for extra in ({"scale": 2.0}, {"offset": 1.0}, {"value_map": {"on": 1}}):
        assert "as they are" in refused({"object_id": obj["id"], **_PROXY, **extra})
    assert "present-value" in refused({"object_id": obj["id"], **_PROXY, "remote_property": "high-limit"})
    assert "present-value" in refused({"object_id": obj["id"], **_PROXY, "remote_array_index": 1})
    assert web_client.post(url, json={"object_id": obj["id"], **_PROXY, "direction": "sideways"}).status_code == 422


def test_proxy_and_write_mapping_cant_share_a_remote_point(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    other = _create_object(web_client, instance_number=2, name="AV-2")
    connector = _bacnet(web_client).json()
    base = f"/api/connectors/{connector['id']}"
    write = {"object_id": other["id"], "remote_object_type": "analog-value", "remote_object_instance": 5}

    proxy = web_client.post(f"{base}/mappings", json={"object_id": obj["id"], **_PROXY}).json()
    r = web_client.post(f"{base}/write-mappings", json=write)
    assert r.status_code == 422 and "proxies" in r.json()["detail"], r.text

    moved = web_client.post(f"{base}/write-mappings", json={**write, "remote_object_instance": 6}).json()
    assert web_client.put(f"{base}/write-mappings/{moved['id']}", json={"remote_object_instance": 5}).status_code == 422

    # From the other side: the write mapping holds the point, then a proxy asks for it.
    assert web_client.delete(f"{base}/mappings/{proxy['id']}").status_code == 204
    assert web_client.put(f"{base}/write-mappings/{moved['id']}", json={"remote_object_instance": 5}).status_code == 200
    r = web_client.post(f"{base}/mappings", json={"object_id": obj["id"], **_PROXY})
    assert r.status_code == 422 and "write mapping" in r.json()["detail"], r.text


def test_echo_guard_covers_every_connector_on_the_same_remote(web_client):
    # The guard used to stop at the proxy's own connector.
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Device B")
    obj = _create_object(web_client)
    other = _create_object(web_client, instance_number=2, name="AV-2")
    elsewhere = _add_object(web_client, 599002, "analog-value", 1, "AV-B")
    first = _bacnet(web_client).json()
    second = _bacnet(web_client, name="Remote 2").json()
    on_other_device = _bacnet(web_client, device_id=599002, name="Remote B").json()
    unrelated = _bacnet(web_client, name="Remote 3", remote_device_instance=4321).json()
    write = {"object_id": other["id"], "remote_object_type": "analog-value", "remote_object_instance": 5}

    proxy = web_client.post(f"/api/connectors/{first['id']}/mappings", json={"object_id": obj["id"], **_PROXY}).json()
    r = web_client.post(f"/api/connectors/{second['id']}/write-mappings", json=write)
    assert r.status_code == 422 and "proxies" in r.json()["detail"], r.text
    r = web_client.post(
        f"/api/connectors/{on_other_device['id']}/write-mappings", json={**write, "object_id": elsewhere}
    )
    assert r.status_code == 422, r.text
    moved = web_client.post(
        f"/api/connectors/{second['id']}/write-mappings", json={**write, "remote_object_instance": 6}
    ).json()
    url = f"/api/connectors/{second['id']}/write-mappings/{moved['id']}"
    assert web_client.put(url, json={"remote_object_instance": 5}).status_code == 422

    # Another remote device's point 5 is a different point.
    held = web_client.post(f"/api/connectors/{unrelated['id']}/write-mappings", json=write)
    assert held.status_code == 201, held.text
    # ...until that connector is pointed at the proxied remote.
    r = web_client.put(f"/api/connectors/{unrelated['id']}", json={"remote_device_instance": 1234})
    assert r.status_code == 422 and "proxies" in r.json()["detail"], r.text

    # From the other side: a write mapping on another connector holds the point.
    assert web_client.delete(f"/api/connectors/{first['id']}/mappings/{proxy['id']}").status_code == 204
    assert web_client.put(url, json={"remote_object_instance": 5}).status_code == 200
    r = web_client.post(f"/api/connectors/{first['id']}/mappings", json={"object_id": obj["id"], **_PROXY})
    assert r.status_code == 422 and "write mapping" in r.json()["detail"], r.text
    read = web_client.post(
        f"/api/connectors/{first['id']}/mappings",
        json={**_PROXY, "object_id": obj["id"], "direction": "read"},
    ).json()
    r = web_client.put(f"/api/connectors/{first['id']}/mappings/{read['id']}", json={"direction": "proxy"})
    assert r.status_code == 422, r.text
    # A proxy's connector pointed at a remote where a write mapping holds the point.
    assert web_client.delete(url).status_code == 204
    assert web_client.put(f"/api/connectors/{first['id']}/mappings/{read['id']}", json={"direction": "proxy"}).status_code == 200
    r = web_client.put(f"/api/connectors/{first['id']}", json={"remote_device_instance": 4321})
    assert r.status_code == 422 and "write mapping" in r.json()["detail"], r.text


def test_mapping_switches_between_read_and_proxy(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    ai = _add_object(web_client, 599001, "analog-input", 1, "AI-1")
    connector = _bacnet(web_client).json()
    base = f"/api/connectors/{connector['id']}/mappings"
    mapping = web_client.post(
        base,
        json={"object_id": obj["id"], "remote_object_type": "analog-value", "remote_object_instance": 5, "scale": 2.0},
    ).json()
    url = f"{base}/{mapping['id']}"

    assert web_client.put(url, json={"direction": "proxy"}).status_code == 422  # still scale 2
    r = web_client.put(url, json={"direction": "proxy", "scale": 1.0})
    assert (r.status_code, r.json()["direction"]) == (200, "proxy")
    assert web_client.put(url, json={"offset": 3.0}).status_code == 422
    assert web_client.put(url, json={"direction": None}).status_code == 422
    r = web_client.put(url, json={"direction": "READ", "offset": 3.0})
    assert (r.status_code, r.json()["direction"], r.json()["offset"]) == (200, "read", 3.0)

    on_input = web_client.post(
        base, json={"object_id": ai, "remote_object_type": "analog-input", "remote_object_instance": 1}
    ).json()
    assert web_client.put(f"{base}/{on_input['id']}", json={"direction": "proxy"}).status_code == 422
