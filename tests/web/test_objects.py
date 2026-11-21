"""Object CRUD and batch edit/delete: units, relinquish_default, duplicate
name/instance-number, unknown type, multistate State_Text validation."""
from __future__ import annotations

import pytest

from tests.web._helpers import _create_device, _create_object


# --- Objects API -----------------------------------------------------------


def test_object_crud(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    object_id = obj["id"]

    r = web_client.get(f"/api/devices/599001/objects")
    assert len(r.json()) == 1

    r = web_client.put(
        f"/api/devices/599001/objects/{object_id}", json={"description": "updated"}
    )
    assert r.status_code == 200
    assert r.json()["description"] == "updated"

    r = web_client.delete(f"/api/devices/599001/objects/{object_id}")
    assert r.status_code == 204
    assert web_client.get(f"/api/devices/599001/objects").json() == []


@pytest.mark.parametrize("route", ["list", "batch"])
def test_object_lists_are_encoded_in_the_handlers_thread_not_on_the_event_loop(web_client, monkeypatch, route):
    # FastAPI's serialize_response runs jsonable_encoder on the event loop; for
    # 5,000 objects that blocked every other request for ~0.3 s.
    import fastapi.routing

    _create_device(web_client)
    objs = [_create_object(web_client, name=f"AV-{i}", instance_number=i) for i in (1, 2)]
    singles = [web_client.get(f"/api/devices/599001/objects/{o['id']}").json() for o in objs]
    calls = []
    real = fastapi.routing.serialize_response

    async def spy(*args, **kwargs):
        calls.append(kwargs.get("response_content"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "serialize_response", spy)
    if route == "list":
        r = web_client.get("/api/devices/599001/objects")
    else:
        r = web_client.patch(
            "/api/devices/599001/objects/batch",
            json={"object_ids": [o["id"] for o in objs], "patch": {"bacnet_writable": True}},
        )
    assert r.status_code == 200, r.text
    assert calls == []
    assert r.json() == singles


def test_batch_update_objects(web_client):
    _create_device(web_client)
    obj1 = _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={
            "object_ids": [obj1["id"], obj2["id"]],
            "patch": {"description": "batch-set", "bacnet_writable": False},
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert {o["description"] for o in updated} == {"batch-set"}
    assert not any(o["bacnet_writable"] for o in updated)

    # unset fields in the patch are left alone, not nulled out
    assert {o["name"] for o in updated} == {"AV-1", "AV-2"}


def test_batch_with_40k_ids_still_404s_on_the_first_missing_one(web_client):
    # One IN (...) over every id passed SQLite's 32766
    # variable limit and 500ed.
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1", instance_number=1)
    ids = [obj["id"]] + list(range(1_000_000, 1_040_000))
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": ids, "patch": {"description": "x"}}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "no object with id 1000000"


def test_batch_delete_with_40k_ids_succeeds(web_client):
    # The delete's trend-sample count also put every id in
    # one IN (...).
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1", instance_number=1)
    r = web_client.request(
        "DELETE", "/api/devices/599001/objects/batch", json={"object_ids": [obj["id"]] * 40_000}
    )
    assert r.status_code == 200, r.text[:200]
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_batch_with_40k_ids_that_all_exist_succeeds(web_client):
    _create_device(web_client)
    obj1 = _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)
    ids = [obj1["id"], obj2["id"]] * 20_000
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": ids, "patch": {"description": "batched"}}
    )
    assert r.status_code == 200, r.text[:200]
    assert {o["description"] for o in r.json()} == {"batched"}
    assert [o["id"] for o in r.json()[:4]] == [obj1["id"], obj2["id"], obj1["id"], obj2["id"]]


def test_batch_update_objects_query_count_does_not_grow_with_the_batch(web_client):
    # Each object's feeding connector is loaded with the objects in one
    # query, not lazily per object (an N+1). The route is a plain def, so its
    # queries run on a worker thread; the event-loop thread's own queries
    # (the login middleware's settings read, background tasks) are not the
    # route's and come and go with timing.
    import threading

    from sqlalchemy import event

    _create_device(web_client)
    ids = [_create_object(web_client, name=f"AV-{i}", instance_number=i)["id"] for i in range(1, 7)]
    engine = web_client.app.state.session_factory.kw["bind"]
    loop_thread = web_client.portal.call(threading.get_ident)
    selects = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT") and threading.get_ident() != loop_thread:
            selects.append(statement)

    def selects_for(object_ids):
        selects.clear()
        event.listen(engine, "before_cursor_execute", count)
        try:
            r = web_client.patch(
                "/api/devices/599001/objects/batch",
                json={"object_ids": object_ids, "patch": {"description": "x"}},
            )
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert r.status_code == 200, r.text
        assert [o["fed_by_connector_id"] for o in r.json()] == [None] * len(object_ids)
        return len(selects)

    many, few = selects_for(ids), selects_for(ids[:2])
    # Zero would mean the route's queries moved onto the loop thread and none were counted.
    assert few > 0
    assert many == few


def test_batch_update_objects_404s_whole_request_for_foreign_object(web_client):
    _create_device(web_client)
    obj1 = _create_object(web_client, name="AV-1", instance_number=1)
    _create_device(web_client, device_id=599002, name="Other Device")
    other_obj = _create_object(web_client, device_id=599002, name="AV-1", instance_number=1)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj1["id"], other_obj["id"]], "patch": {"description": "x"}},
    )
    assert r.status_code == 404

    # nothing was applied -- all-or-nothing, not a partial update
    assert web_client.get(f"/api/devices/599001/objects/{obj1['id']}").json()["description"] is None


def test_batch_update_objects_rejects_name_conflict(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj2["id"]], "patch": {"name": "AV-1"}},
    )
    assert r.status_code == 409


def test_batch_update_objects_rejects_same_name_on_multiple_objects(web_client):
    # Setting a single literal `name` across more than one object is always
    # a conflict (device_id+name is unique) -- caught by an explicit
    # pre-check now, not just the IntegrityError fallback, so the except
    # around session.commit() could be narrowed from a broad `except
    # Exception` to `except IntegrityError` without missing this case.
    _create_device(web_client)
    obj1 = _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj1["id"], obj2["id"]], "patch": {"name": "Same"}},
    )
    assert r.status_code == 409


# --- Units validation (found live in SPA testing) --------------------------
# A bad units string used to be accepted silently and only crash at
# engine-start time (see point_config.validate_units and test_bacnet.py). The API
# now rejects it immediately, on create, single update, and batch update.


def test_create_object_rejects_invalid_units(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-output", "instance_number": 1, "name": "AO-1", "units": "F"},
    )
    assert r.status_code == 400
    assert "not a valid BACnet engineering unit" in r.json()["detail"]


def test_update_object_rejects_invalid_units(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"units": "F"})
    assert r.status_code == 400
    assert "not a valid BACnet engineering unit" in r.json()["detail"]


def test_update_object_ignores_units_validation_for_binary_objects(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, object_type="binary-value", instance_number=1, name="BV-1")
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"units": "not-a-unit"})
    assert r.status_code == 200


def test_batch_update_objects_rejects_invalid_units(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1", instance_number=1)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"units": "F"}},
    )
    assert r.status_code == 400
    assert "not a valid BACnet engineering unit" in r.json()["detail"]


# --- relinquish_default validation (wired up alongside the "why is vendor ID
# 999" / connector-mapping session) -- see validate_relinquish_default in
# bacnet.py. Same shape as the units-validation section above.


def test_create_object_rejects_invalid_relinquish_default(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-value",
            "instance_number": 1,
            "name": "AV-1",
            "units": "percent",
            "relinquish_default": "not-a-number",
        },
    )
    assert r.status_code == 400
    assert "not a valid relinquish-default" in r.json()["detail"]


def test_create_object_accepts_valid_relinquish_default(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-value",
            "instance_number": 1,
            "name": "AV-1",
            "units": "percent",
            "relinquish_default": "42.5",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["relinquish_default"] == "42.5"


def test_update_object_rejects_invalid_relinquish_default(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"relinquish_default": "not-a-number"}
    )
    assert r.status_code == 400
    assert "not a valid relinquish-default" in r.json()["detail"]


def test_update_object_ignores_relinquish_default_validation_for_non_commandable_type(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, object_type="analog-input", instance_number=1, name="AI-1")
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"relinquish_default": "not-a-number"}
    )
    assert r.status_code == 200


def test_batch_update_objects_rejects_invalid_relinquish_default(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, object_type="analog-value", name="AV-1", instance_number=1)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"relinquish_default": "not-a-number"}},
    )
    assert r.status_code == 400
    assert "not a valid relinquish-default" in r.json()["detail"]


# --- Duplicate instance number (found live in testing) ---------------------
# Creating a second object of the same (object_type, instance_number) on a
# device used to fall through to the DB's unique constraint uncaught -- a
# bare sqlite3.IntegrityError, 500. Now rejected up front as a clean 409.


def test_create_object_rejects_duplicate_instance_number(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="analog-input", instance_number=1, name="AI-1")
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-input",
            "instance_number": 1,
            "name": "AI-1-dup",
            "units": "percent",
        },
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_create_object_allows_same_instance_number_for_different_type(web_client):
    # Different object_type, same instance_number is fine -- BACnet object
    # identifiers are (type, instance) pairs, so this isn't a collision.
    _create_device(web_client)
    _create_object(web_client, object_type="analog-input", instance_number=1, name="AI-1")
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-output",
            "instance_number": 1,
            "name": "AO-1",
            "units": "percent",
        },
    )
    assert r.status_code == 201, r.text


# --- Duplicate name / unknown object_type -
# Same incident class as the instance-number collision above, found by
# deliberately fuzzing the object CRUD routes: the DB's other unique
# constraint (device_id, name) had no pre-check at all, on either create or
# rename, so both used to 500 on a bare sqlite3.IntegrityError.


def test_create_object_rejects_duplicate_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-value",
            "instance_number": 2,
            "name": "AV-1",
            "units": "percent",
        },
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_update_object_rejects_rename_to_existing_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)
    r = web_client.put(f"/api/devices/599001/objects/{obj2['id']}", json={"name": "AV-1"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_update_object_allows_renaming_to_its_own_current_name(web_client):
    # Not a self-collision -- a no-op rename (or a PUT that just repeats the
    # existing name alongside other field changes) must not 409 against
    # itself.
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1", instance_number=1)
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={"name": "AV-1", "description": "still fine"},
    )
    assert r.status_code == 200, r.text


def test_create_object_rejects_unknown_object_type(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "not-a-real-type", "instance_number": 1, "name": "Bogus"},
    )
    assert r.status_code == 400
    assert "not-a-real-type" in r.json()["detail"]


def test_batch_delete_objects(web_client):
    _create_device(web_client)
    obj1 = _create_object(web_client, name="AV-1", instance_number=1)
    obj2 = _create_object(web_client, name="AV-2", instance_number=2)

    r = web_client.request(
        "DELETE",
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj1["id"], obj2["id"]]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 2}
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_create_object_rejects_multistate_without_state_text(web_client):
    # Used to get a clean 201 and then silently block the device from ever
    # starting (build_bacnet_object raises at engine-start time instead) --
    # parse_config/parse_objects_csv already caught this, the manual API
    # route hadn't.
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "multi-state-value", "instance_number": 1, "name": "MSV-1"},
    )
    assert r.status_code == 400
    assert "state_text" in r.json()["detail"]


def test_create_object_accepts_multistate_with_state_text(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "multi-state-value",
            "instance_number": 1,
            "name": "MSV-1",
            "state_text": ["Off", "On"],
        },
    )
    assert r.status_code == 201, r.text


def test_update_object_rejects_clearing_state_text_on_multistate(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client,
        object_type="multi-state-value",
        instance_number=1,
        name="MSV-1",
        state_text=["Off", "On"],
    )
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"state_text": None}
    )
    assert r.status_code == 400
    assert "state_text" in r.json()["detail"]


def test_batch_update_objects_rejects_clearing_state_text_on_multistate(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client,
        object_type="multi-state-value",
        instance_number=1,
        name="MSV-1",
        state_text=["Off", "On"],
    )
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"state_text": None}},
    )
    assert r.status_code == 400
    assert "state_text" in r.json()["detail"]


def test_update_object_rejects_explicit_null_name(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"name": None})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        "bacnet_writable",
        "out_of_service_default",
        "trended",
        "trend_mode",
        "schedule_priority",
        "alarming_enabled",
        "alarm_time_delay",
        "alarm_notify_type",
        "alarm_deadband",
        "alarm_event_detection_enable",
    ],
)
def test_update_object_rejects_explicit_null_for_a_not_null_column(web_client, field):
    # Optional in ObjectUpdate only so the field can be omitted; an explicit
    # null used to reach the NOT NULL column and come back as a 500.
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={field: None})
    assert r.status_code == 422


# --- Trend log name clash ----------------------------------------------------
# A trended object X gets a Trend_Log named "X-Trend"; another object already
# named "X-Trend" made the device fail to start (duplicate objectName).


def test_create_object_rejects_name_of_an_existing_trend_log(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True)

    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 2, "name": "AV-1-Trend", "units": "percent"},
    )
    assert r.status_code == 409
    assert "AV-1-Trend" in r.json()["detail"]


def test_create_trended_object_rejects_clash_with_existing_object_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1-Trend", instance_number=2)

    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent", "trended": True},
    )
    assert r.status_code == 409


def test_update_object_rejects_enabling_trending_onto_a_clash(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1")
    _create_object(web_client, name="AV-1-Trend", instance_number=2)

    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trended": True})
    assert r.status_code == 409
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["trended"] is False


def test_update_object_rejects_rename_onto_a_trend_log_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True)
    other = _create_object(web_client, name="AV-2", instance_number=2)

    r = web_client.put(f"/api/devices/599001/objects/{other['id']}", json={"name": "AV-1-Trend"})
    assert r.status_code == 409


def test_batch_update_rejects_enabling_trending_onto_a_clash(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, name="AV-1")
    _create_object(web_client, name="AV-1-Trend", instance_number=2)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"trended": True}},
    )
    assert r.status_code == 409
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["trended"] is False


def test_api_mixed_case_choices_stored_canonical(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, object_type="Analog-Value", units="Degrees-Fahrenheit",
                         name="Zone Temp", description="Keep My Case", trend_mode="COV")
    assert (obj["object_type"], obj["units"], obj["trend_mode"]) == ("analog-value", "degreesFahrenheit", "cov")
    assert (obj["name"], obj["description"]) == ("Zone Temp", "Keep My Case")

    bv = _create_object(web_client, object_type="binary-value", instance_number=1, name="Fan", units=None)
    r = web_client.put(f"/api/devices/599001/objects/{bv['id']}", json={"relinquish_default": "INACTIVE"})
    assert r.status_code == 200, r.text
    assert r.json()["relinquish_default"] == "inactive"


def test_api_mixed_case_binary_word_on_analog_still_rejected(web_client):
    # Canonicalizing never makes an invalid value valid.
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"relinquish_default": "Active"})
    assert r.status_code == 400


def test_api_batch_patch_canonicalizes(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"trend_mode": "Interval", "units": "PERCENT"}},
    )
    assert r.status_code == 200, r.text
    assert (r.json()[0]["trend_mode"], r.json()[0]["units"]) == ("interval", "percent")


# --- Reserved backup-file name ----------------------------------------------
# "<device name>-config.json" is file,1's name; it used to be refused only
# when the device started, by which point it was saved.


def test_create_object_refuses_the_reserved_backup_file_name(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "Demo Device-config.json"},
    )
    assert r.status_code == 409
    assert "reserved" in r.json()["detail"]


def test_rename_object_refuses_the_reserved_backup_file_name(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"name": "Demo Device-config.json"})
    assert r.status_code == 409
    assert "reserved" in r.json()["detail"]
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"name": "Demo Device-config.json"}},
    )
    assert r.status_code == 409
    assert "reserved" in r.json()["detail"]
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["name"] == "AV-1"


def test_object_name_cannot_be_the_device_name(web_client):
    # Object_Name is unique within a device, the Device included.
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "Demo Device"},
    )
    assert r.status_code == 409
    assert "device's own name" in r.json()["detail"]
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"name": "Demo Device"})
    assert r.status_code == 409
    assert "device's own name" in r.json()["detail"]
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["name"] == "AV-1"


def test_object_name_differing_from_the_device_name_only_in_case_is_accepted(web_client):
    _create_device(web_client)
    _create_object(web_client, name="demo device")


# --- Integer bounds -------------------------------------------------------
# An int past SQLite's 64 bits used to pass the schema and 500 on the DB
# write; one past the BACnet Unsigned range saved but kept the device from
# starting. Both are now a 422 on create, update and batch update.

_OUT_OF_RANGE = [
    ("trend_interval_seconds", 0),
    ("trend_interval_seconds", 42949673),
    ("trend_sample_cap", 0),
    ("trend_sample_cap", 32768),
    ("trend_sample_cap", 2**64),
    ("trend_notification_class_instance", -1),
    ("trend_notification_class_instance", 4194303),
    ("notification_class_instance", -1),
    ("notification_class_instance", 2**64),
    ("alarm_time_delay", -5),
    ("alarm_time_delay", 4294967296),
    ("alarm_time_delay_normal", -1),
    ("alarm_time_delay_normal", 2**64),
]


@pytest.mark.parametrize(("field", "value"), _OUT_OF_RANGE)
def test_object_int_fields_out_of_range_are_a_422(web_client, field, value):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "AV-1", field: value},
    )
    assert r.status_code == 422, r.text
    obj = _create_object(web_client)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={field: value})
    assert r.status_code == 422, r.text
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": [obj["id"]], "patch": {field: value}}
    )
    assert r.status_code == 422, r.text


def test_object_int_fields_accept_their_upper_bounds(web_client):
    _create_device(web_client)
    _create_object(web_client, object_type="notification-class", instance_number=4194302, name="NC-max")
    obj = _create_object(web_client)
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        json={
            "trend_interval_seconds": 42949672,
            "trend_sample_cap": 32767,
            "alarm_time_delay": 4294967295,
            "alarm_time_delay_normal": 4294967295,
            "notification_class_instance": 4194302,
        },
    )
    assert r.status_code == 200, r.text


def test_notification_recipient_process_identifier_out_of_range_is_a_422(web_client):
    _create_device(web_client)
    recipient = {
        "recipient": {"kind": "device", "instance_number": 5},
        "valid_days": [True] * 7,
        "from_time": "00:00:00",
        "to_time": "23:59:59",
        "transitions": [True] * 3,
        "confirmed": False,
        "process_identifier": 4294967296,
    }
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "notification-class",
            "instance_number": 1,
            "name": "NC-1",
            "notification_recipient_list": [recipient],
        },
    )
    assert r.status_code == 422, r.text


# --- Non-finite numbers -----------------------------------------------------
# Python's JSON parser accepts Infinity/NaN and pydantic's float took them,
# after which the object list 500'd (JSON can't encode them).


def _post_raw(web_client, path, text):
    return web_client.post(path, content=text, headers={"content-type": "application/json"})


@pytest.mark.parametrize("field, token", [("alarm_high_limit", "Infinity"), ("alarm_deadband", "NaN"), ("alarm_low_limit", "-Infinity")])
def test_create_object_refuses_non_finite_float(web_client, field, token):
    _create_device(web_client)
    r = _post_raw(
        web_client,
        "/api/devices/599001/objects",
        '{"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "%s": %s}' % (field, token),
    )
    assert r.status_code == 422, r.text
    assert web_client.get("/api/devices/599001/objects").status_code == 200


def test_update_object_refuses_non_finite_float(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}",
        content='{"alarm_high_limit": Infinity}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("text", ["nan", "inf", "1e999", "-Infinity"])
def test_create_object_refuses_non_finite_relinquish_default(web_client, text):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "relinquish_default": text},
    )
    assert r.status_code == 400, r.text
    assert "not a valid relinquish-default" in r.json()["detail"]


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_object_names_are_refused(web_client, name):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": name},
    )
    assert r.status_code == 422
    obj = _create_object(web_client)
    assert web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"name": name}).status_code == 422


# --- the trend log and network port names -----------------------------------


def test_trending_an_object_whose_trend_log_takes_the_device_name_is_refused(web_client):
    _create_device(web_client, name="AV-1-Trend")
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent", "trended": True},
    )
    assert r.status_code == 409
    assert "trend log" in r.json()["detail"]
    obj = _create_object(web_client, name="AV-1")
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trended": True})
    assert r.status_code == 409
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["trended"] is False


def test_object_name_cannot_be_the_network_port_name(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 1, "name": "NetworkPort-1"},
    )
    assert r.status_code == 409
    assert "network port" in r.json()["detail"]
    obj = _create_object(web_client)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"name": "NetworkPort-1"}},
    )
    assert r.status_code == 409
    assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["name"] == "AV-1"


def test_batch_edit_on_mixed_types_names_the_object_that_refuses(web_client):
    _create_device(web_client)
    av = _create_object(web_client)
    bv = _create_object(web_client, object_type="binary-value", instance_number=1, name="BV-1", units=None)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [av["id"], bv["id"]], "patch": {"relinquish_default": "42.5"}},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("BV-1: ")


# --- Names are trimmed on every save path ------------------------------------


def test_object_names_are_trimmed_on_create_update_and_batch(web_client):
    _create_device(web_client)
    created = _create_object(web_client, name="  Supply Fan  ")
    assert created["name"] == "Supply Fan"
    r = web_client.put(f"/api/devices/599001/objects/{created['id']}", json={"name": " Return Fan "})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Return Fan"
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [created["id"]], "patch": {"name": "Exhaust Fan\t"}},
    )
    assert r.status_code == 200, r.text
    assert web_client.get(f"/api/devices/599001/objects/{created['id']}").json()["name"] == "Exhaust Fan"


def test_a_name_differing_only_in_edge_spaces_is_a_duplicate(web_client):
    _create_device(web_client)
    _create_object(web_client, name="Fan")
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 2, "name": "Fan "},
    )
    assert r.status_code == 409, r.text


def test_create_and_update_binary_text_round_trips(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client, object_type="binary-value", name="BV-1", units=None, active_text="Running", inactive_text="Stopped"
    )
    assert (obj["active_text"], obj["inactive_text"]) == ("Running", "Stopped")
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"active_text": "Open"})
    assert r.status_code == 200, r.text
    assert (r.json()["active_text"], r.json()["inactive_text"]) == ("Open", "Stopped")
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"inactive_text": " "})
    assert r.status_code == 400


def test_update_text_to_null_restores_the_defaults(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client, object_type="binary-value", name="BV-1", units=None, active_text="Running", inactive_text="Stopped"
    )
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"active_text": None, "inactive_text": None}
    )
    assert r.status_code == 200, r.text
    assert (r.json()["active_text"], r.json()["inactive_text"]) == (None, None)


def test_start_value_round_trips_and_canonicalises_case(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client, object_type="binary-input", name="BI-1", units=None, start_value="ACTIVE"
    )
    assert obj["start_value"] == "active"
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"start_value": "maybe"})
    assert r.status_code == 400
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"start_value": None})
    assert r.status_code == 200 and r.json()["start_value"] is None


def test_trend_log_identity_round_trips_and_clears(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_log_instance=77, trend_log_name="Zone Temp Log")
    assert (obj["trend_log_instance"], obj["trend_log_name"]) == (77, "Zone Temp Log")
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"name": "Renamed", "trend_log_name": "  Log 2 "})
    assert r.status_code == 200, r.text
    assert (r.json()["trend_log_instance"], r.json()["trend_log_name"]) == (77, "Log 2")
    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"trend_log_instance": None, "trend_log_name": None}
    )
    assert (r.json()["trend_log_instance"], r.json()["trend_log_name"]) == (None, None)
    assert web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trend_log_name": " "}).status_code == 400
    assert web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trend_log_instance": -1}).status_code == 422


def test_creating_a_trended_object_whose_id_is_a_fixed_trend_log_instance_is_refused(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True, trend_log_instance=2)
    body = {"object_type": "analog-value", "instance_number": 2, "name": "AV-2", "units": "percent", "trended": True}
    r = web_client.post("/api/devices/599001/objects", json=body)
    assert r.status_code == 409
    assert "AV-1" in r.json()["detail"] and "AV-2" in r.json()["detail"] and "Trend Log instance" in r.json()["detail"]
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


def test_turning_on_trending_for_an_object_whose_id_is_a_fixed_trend_log_instance_is_refused(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True, trend_log_instance=2)
    second = _create_object(web_client, name="AV-2", instance_number=2)
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": [second["id"]], "patch": {"trended": True}}
    )
    assert r.status_code == 409 and "Trend Log instance" in r.json()["detail"]
    r = web_client.put(f"/api/devices/599001/objects/{second['id']}", json={"trended": True})
    assert r.status_code == 409


def test_a_csv_import_of_a_trended_object_whose_id_is_a_fixed_trend_log_instance_is_refused(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True, trend_log_instance=2)
    csv_text = "object_type,instance_number,name,trended\nanalog-value,2,AV-2,true\n"
    r = web_client.post(
        "/api/devices/599001/objects/import", files={"file": ("points.csv", csv_text, "text/csv")}
    )
    assert r.status_code == 409, r.text
    assert "Trend Log instance" in r.json()["detail"]
    assert len(web_client.get("/api/devices/599001/objects").json()) == 1


@pytest.mark.parametrize("mode", ["merge", "sync"])
def test_a_csv_dry_run_refuses_a_trended_object_whose_new_id_is_a_fixed_trend_log_instance(web_client, mode):
    _create_device(web_client)
    first = _create_object(web_client, name="AV-1", trended=True)
    web_client.put(f"/api/devices/599001/objects/{first['id']}", json={"trend_log_instance": first["id"] + 1})
    csv_text = "object_type,instance_number,name,trended\nanalog-value,2,AV-2,true\n"
    if mode == "sync":
        csv_text += "analog-value,1,AV-1,true\n"
    r = web_client.post(
        f"/api/devices/599001/objects/import?mode={mode}&dry_run=true",
        files={"file": ("points.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 409, r.text
    assert "Trend Log instance" in r.json()["detail"]


def test_a_batch_giving_two_trended_objects_the_same_trend_log_instance_is_refused(web_client):
    _create_device(web_client)
    first = _create_object(web_client, name="AV-1", instance_number=1, trended=True)
    second = _create_object(web_client, name="AV-2", instance_number=2, trended=True)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [first["id"], second["id"]], "patch": {"trend_log_instance": 77}},
    )
    assert r.status_code == 409, r.text
    assert "Trend Log instance" in r.json()["detail"]
    assert web_client.get(f"/api/devices/599001/objects/{first['id']}").json()["trend_log_instance"] is None


def test_a_batch_moving_a_trend_log_onto_another_default_instance_is_refused(web_client):
    _create_device(web_client)
    first = _create_object(web_client, name="AV-1", instance_number=1, trended=True)
    second = _create_object(web_client, name="AV-2", instance_number=2, trended=True)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [first["id"]], "patch": {"trend_log_instance": second["id"]}},
    )
    assert r.status_code == 409 and "Trend Log instance" in r.json()["detail"]


# --- A Notification Class can't be trended ----------------------------------


def test_creating_a_trended_notification_class_is_refused(web_client):
    _create_device(web_client)
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "notification-class", "instance_number": 1, "name": "NC-1", "trended": True},
    )
    assert r.status_code == 400 and "Notification Class can't be trended" in r.json()["detail"]
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_turning_trending_on_for_a_notification_class_is_refused(web_client):
    _create_device(web_client)
    nc = _create_object(web_client, object_type="notification-class", name="NC-1", units=None)
    r = web_client.put(f"/api/devices/599001/objects/{nc['id']}", json={"trended": True})
    assert r.status_code == 400 and "Notification Class can't be trended" in r.json()["detail"]
    assert web_client.get(f"/api/devices/599001/objects/{nc['id']}").json()["trended"] is False


def test_a_batch_trending_a_notification_class_is_refused_and_changes_nothing(web_client):
    _create_device(web_client)
    nc = _create_object(web_client, object_type="notification-class", name="NC-1", units=None)
    av = _create_object(web_client, name="AV-1", instance_number=2)
    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [av["id"], nc["id"]], "patch": {"trended": True}},
    )
    assert r.status_code == 400 and r.json()["detail"].startswith("NC-1: ")
    for obj in (av, nc):
        assert web_client.get(f"/api/devices/599001/objects/{obj['id']}").json()["trended"] is False


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_csv_import_refuses_a_trended_notification_class(web_client, dry_run):
    _create_device(web_client)
    csv_text = "object_type,instance_number,name,trended\nnotification-class,1,NC-1,true\n"
    r = web_client.post(
        f"/api/devices/599001/objects/import?dry_run={'true' if dry_run else 'false'}",
        files={"file": ("points.csv", csv_text, "text/csv")},
    )
    assert r.status_code >= 400, r.text
    assert "row 2" in r.text and "Notification Class can't be trended" in r.text
    assert web_client.get("/api/devices/599001/objects").json() == []
