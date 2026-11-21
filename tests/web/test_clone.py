"""The clone API: a read job queued for the device's own process, with
progress and cancel, then a plan and a create. No device process runs here:
rows are inserted and answered by the tests themselves."""
from __future__ import annotations

import pytest

from bacpack.models import ClientRequest, Device
from tests.web._helpers import _create_device

BASE = "/api/devices/599001/client/clone"
BODY = {"instance": 4001, "timeout_seconds": 5}


def _mark_running(web_client, device_id=599001):
    with web_client.app.state.session_factory() as session:
        session.get(Device, device_id).runtime.process_status = "running"
        session.commit()


@pytest.fixture()
def running_device(web_client):
    _create_device(web_client)
    _mark_running(web_client)
    return web_client


def _job(web_client, job_id):
    with web_client.app.state.session_factory() as session:
        row = session.get(ClientRequest, job_id)
        return None if row is None else {
            "status": row.status, "progress": row.progress, "cancel_requested": row.cancel_requested,
            "error": row.error, "kind": row.kind, "params": row.params,
        }


def _set(web_client, job_id, **fields):
    with web_client.app.state.session_factory() as session:
        row = session.get(ClientRequest, job_id)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def test_start_returns_a_job_without_waiting(running_device):
    r = running_device.post(BASE, json=BODY)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "pending"
    row = _job(running_device, job_id)
    assert row["kind"] == "clone-read" and row["params"]["instance"] == 4001


def test_start_on_a_stopped_device_is_409(web_client):
    _create_device(web_client)
    r = web_client.post(BASE, json=BODY)
    assert r.status_code == 409 and "isn't running" in r.json()["detail"]


def test_start_on_an_unknown_device_is_404(web_client):
    assert web_client.post("/api/devices/42/client/clone", json=BODY).status_code == 404


def test_second_start_while_unfinished_is_409(running_device):
    first = running_device.post(BASE, json=BODY).json()["job_id"]
    r = running_device.post(BASE, json=BODY)
    assert r.status_code == 409
    _set(running_device, first, status="done")
    assert running_device.post(BASE, json=BODY).status_code == 202


def test_get_returns_progress_then_done(running_device):
    job_id = running_device.post(BASE, json={**BODY, "address": "10.0.0.5"}).json()["job_id"]
    body = running_device.get(BASE).json()
    assert body["job_id"] == job_id and body["status"] == "pending"
    assert body["progress"] == {"phase": "object-list", "done": 0, "total": 0}
    assert body["instance"] == 4001 and body["address"] == "10.0.0.5"
    assert "result" not in body

    _set(running_device, job_id, status="running", progress={"phase": "objects", "done": 40, "total": 100})
    body = running_device.get(BASE).json()
    assert (body["status"], body["progress"]) == ("running", {"phase": "objects", "done": 40, "total": 100})

    _set(running_device, job_id, status="done", result={"objects": []})
    body = running_device.get(BASE).json()
    assert body["status"] == "done" and "result" not in body


def test_get_reports_the_newest_job(running_device):
    first = running_device.post(BASE, json=BODY).json()["job_id"]
    _set(running_device, first, status="done")
    second = running_device.post(BASE, json=BODY).json()["job_id"]
    assert running_device.get(BASE).json()["job_id"] == second


def test_get_without_a_job_is_404(running_device):
    assert running_device.get(BASE).status_code == 404


def test_a_running_job_on_a_stopped_device_reads_as_failed(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    _set(running_device, job_id, status="running")
    with running_device.app.state.session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "stopped"
        session.commit()
    body = running_device.get(BASE).json()
    assert body["status"] == "error" and "stopped" in body["error"]
    assert _job(running_device, job_id)["status"] == "error"
    # The device can start a new read once it runs again.
    _mark_running(running_device)
    assert running_device.post(BASE, json=BODY).status_code == 202


def test_cancel_sets_the_flag(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    r = running_device.post(f"{BASE}/{job_id}/cancel")
    assert r.status_code == 200, r.text
    assert _job(running_device, job_id)["cancel_requested"] is True


def test_cancel_of_a_finished_job_leaves_it_alone(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    _set(running_device, job_id, status="done")
    assert running_device.post(f"{BASE}/{job_id}/cancel").status_code == 200
    assert _job(running_device, job_id)["cancel_requested"] is False


def test_cancel_of_an_unknown_job_is_404(running_device):
    assert running_device.post(f"{BASE}/999/cancel").status_code == 404


def test_discard_deletes_the_row(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    assert running_device.delete(f"{BASE}/{job_id}").status_code == 204
    assert _job(running_device, job_id) is None
    assert running_device.delete(f"{BASE}/{job_id}").status_code == 404


def test_another_devices_job_is_not_reachable(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    running_device.post("/api/devices", json={"id": 599002, "name": "Other"})
    assert running_device.post(f"/api/devices/599002/client/clone/{job_id}/cancel").status_code == 404
    assert running_device.delete(f"/api/devices/599002/client/clone/{job_id}").status_code == 404


# --- Plan and create --------------------------------------------------------------

from bacpack import object_config  # noqa: E402
from bacpack.models import BacnetObject, DeviceRuntime, get_or_create_settings  # noqa: E402
from bacpack.object_builder import build_application  # noqa: E402


def _raw_controller():
    def entry(object_type, instance, name, missing=(), **props):
        props = {key.replace("_", "-"): value for key, value in props.items()}
        props["object-name"] = name
        return {"object_type": object_type, "instance": instance, "props": props, "missing": list(missing), "error": None}

    return {
        "device": {
            "instance": 4001, "name": "Boiler Room", "description": "Plant", "location": "Basement",
            "vendor_name": "Acme", "vendor_identifier": 42, "object_list_count": 7,
        },
        "objects": [
            entry("analog-input", 1, "Zone Temp", units="degrees-fahrenheit", present_value=71.5),
            entry("analog-value", 2, "Setpoint", units="percent", relinquish_default=21.5, commandable=True),
            entry("multi-state-value", 3, "Mode", state_text=["Off", "Heat"], relinquish_default=2, commandable=False),
            entry("accumulator", 9, "kWh"),
            entry("trend-log", 20, "Zone Temp Log", logging_type="polled", log_interval=30000, buffer_size=500,
                  trigger=False, log_device_object_property={
                      "objectIdentifier": "analog-input:1", "propertyIdentifier": "present-value",
                      "propertyArrayIndex": None, "deviceIdentifier": "device:4001"}),
        ],
        "read_at": "2026-11-01T12:00:00+00:00",
    }


@pytest.fixture()
def done_job(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    _set(running_device, job_id, status="done", result=_raw_controller())
    return job_id


def _plan_url(job_id):
    return f"{BASE}/{job_id}/plan"


def _create_url(job_id):
    return f"{BASE}/{job_id}/create"


def test_plan_route_returns_counts_skips_and_notes(running_device, done_job):
    r = running_device.post(_plan_url(done_job), json={"id": 777, "name": "Clone"})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["device"]["id"] == 777 and plan["device"]["name"] == "Clone"
    assert plan["vendor_name"] == "Acme" and plan["device"]["description"] == "Plant"
    assert plan["counts"] == {"analog-input": 1, "analog-value": 1, "multi-state-value": 1}
    assert plan["object_count"] == 3
    assert plan["skipped_types"] == {"accumulator": 1}
    assert [s["name"] for s in plan["skipped"]] == ["kWh"]
    assert any("not commandable" in n["note"] for n in plan["notes"])
    assert plan["problem"] is None
    assert "suggested_bind" in r.json()


def test_plan_for_an_unfinished_job_is_409(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    r = running_device.post(_plan_url(job_id), json={"id": 777, "name": "Clone"})
    assert r.status_code == 409 and "still being read" in r.json()["detail"]


def test_plan_for_a_failed_job_is_409_with_its_error(running_device, done_job):
    _set(running_device, done_job, status="error", error="no response within 5 s", result=None)
    r = running_device.post(_plan_url(done_job), json={"id": 777, "name": "Clone"})
    assert r.status_code == 409 and "no response within 5 s" in r.json()["detail"]


def test_plan_rebuilds_with_a_new_id_and_name(running_device, done_job):
    first = running_device.post(_plan_url(done_job), json={"id": 777, "name": "Clone"}).json()["plan"]
    again = running_device.post(_plan_url(done_job), json={"id": 888, "name": "Zone Temp"}).json()["plan"]
    assert (first["device"]["id"], again["device"]["id"]) == (777, 888)
    assert first["counts"]["analog-input"] == 1 and "analog-input" not in again["counts"]
    assert "Zone Temp" in [s["name"] for s in again["skipped"]]


def test_plan_without_an_id_or_name_uses_the_controllers(running_device, done_job):
    r = running_device.post(_plan_url(done_job), json={})
    assert r.status_code == 200, r.text
    device = r.json()["plan"]["device"]
    assert (device["id"], device["name"]) == (4001, "Boiler Room")


def test_plan_with_a_blank_controller_name_falls_back_to_a_device_label(running_device, done_job):
    raw = _raw_controller()
    raw["device"]["name"] = None
    _set(running_device, done_job, result=raw)
    device = running_device.post(_plan_url(done_job), json={}).json()["plan"]["device"]
    assert device["name"] == "Device 4001"


def test_create_still_needs_an_id_and_name(running_device, done_job):
    assert running_device.post(_create_url(done_job), json={}).status_code == 422


def test_plan_names_a_device_id_already_in_use(running_device, done_job):
    r = running_device.post(_plan_url(done_job), json={"id": 599001, "name": "Clone"})
    assert r.status_code == 200
    assert "599001" in r.json()["plan"]["problem"] and "already exists" in r.json()["plan"]["problem"]


def test_plan_suggests_a_bind_that_avoids_the_running_devices_socket(running_device, done_job):
    with running_device.app.state.session_factory() as session:
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()
    suggested = running_device.post(_plan_url(done_job), json={"id": 777, "name": "Clone"}).json()["suggested_bind"]
    assert suggested["port"] is not None and suggested["port"] != 47808
    assert suggested["bind_address"] == f"127.0.0.1/8:{suggested['port']}"


def test_plan_of_an_unknown_job_is_404(running_device):
    assert running_device.post(_plan_url(999), json={"id": 777, "name": "Clone"}).status_code == 404


def test_create_saves_a_stopped_device_with_its_objects(running_device, done_job):
    r = running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"})
    assert r.status_code == 201, r.text
    assert r.json() == {"device_id": 777}

    device = running_device.get("/api/devices/777").json()
    assert (device["name"], device["description"], device["location"]) == ("Clone", "Plant", "Basement")
    assert (device["vendor_name"], device["vendor_identifier"], device["object_count"]) == ("Acme", 42, 3)
    objects = {o["name"]: o for o in running_device.get("/api/devices/777/objects").json()}
    assert objects["Setpoint"]["relinquish_default"] == "21.5" and objects["Setpoint"]["units"] == "percent"
    assert objects["Zone Temp"]["trended"] is True
    assert (objects["Zone Temp"]["trend_log_instance"], objects["Zone Temp"]["trend_log_name"]) == (20, "Zone Temp Log")
    assert objects["Zone Temp"]["start_value"] == "71.5"
    assert objects["Mode"]["bacnet_writable"] is False
    with running_device.app.state.session_factory() as session:
        assert session.get(DeviceRuntime, 777).process_status == "stopped"


def test_create_is_all_or_nothing(running_device, done_job, monkeypatch):
    real = object_config.check_device

    def refuse_once_rows_are_saved(device_name, rows):
        rows = list(rows)
        if any(row.get("id") is not None for row in rows):
            return "forced refusal"
        return real(device_name, rows)

    monkeypatch.setattr(object_config, "check_device", refuse_once_rows_are_saved)
    r = running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"})
    assert r.status_code == 409 and "forced refusal" in r.json()["detail"]
    assert running_device.get("/api/devices/777").status_code == 404
    with running_device.app.state.session_factory() as session:
        assert session.query(BacnetObject).filter_by(device_id=777).count() == 0
        assert session.get(ClientRequest, done_job) is not None  # the read is kept for another try


def test_create_with_an_existing_id_is_409_naming_the_device(running_device, done_job):
    r = running_device.post(_create_url(done_job), json={"id": 599001, "name": "Clone"})
    assert r.status_code == 409
    assert "599001" in r.json()["detail"]
    assert running_device.get("/api/devices/599001").json()["name"] == "Demo Device"
    assert len(running_device.get("/api/devices/599001/objects").json()) == 0


def test_create_twice_is_404_the_second_time(running_device, done_job):
    assert running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"}).status_code == 201
    r = running_device.post(_create_url(done_job), json={"id": 778, "name": "Clone 2"})
    assert r.status_code == 404
    assert running_device.get("/api/devices/778").status_code == 404


def test_create_deletes_the_job(running_device, done_job):
    running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"})
    assert _job(running_device, done_job) is None
    assert running_device.get(BASE).status_code == 404


def test_create_for_an_unfinished_job_is_409_and_keeps_it(running_device):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    r = running_device.post(_create_url(job_id), json={"id": 777, "name": "Clone"})
    assert r.status_code == 409
    assert _job(running_device, job_id) is not None


def test_a_bad_bind_address_is_400(running_device, done_job):
    r = running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone", "bind_address": "not-an-address"})
    assert r.status_code == 400
    assert running_device.get("/api/devices/777").status_code == 404
    assert _job(running_device, done_job) is not None


def test_create_with_a_bind_address_and_port(running_device, done_job):
    r = running_device.post(
        _create_url(done_job), json={"id": 777, "name": "Clone", "bind_address": "127.0.0.1/8", "port": 47850}
    )
    assert r.status_code == 201, r.text
    assert running_device.get("/api/devices/777").json()["bind_address"] == "127.0.0.1/8:47850"


def test_a_blank_name_is_422(running_device, done_job):
    assert running_device.post(_create_url(done_job), json={"id": 777, "name": "  "}).status_code == 422
    assert running_device.post(_plan_url(done_job), json={"id": 777, "name": ""}).status_code == 422


def test_a_reserved_device_name_is_409_on_create(running_device, done_job):
    r = running_device.post(_create_url(done_job), json={"id": 777, "name": "NetworkPort-1"})
    assert r.status_code == 409 and "network port" in r.json()["detail"]


@pytest.mark.asyncio
async def test_the_cloned_device_builds_and_serves_the_cloned_objects(running_device, done_job):
    from bacpypes3.primitivedata import ObjectIdentifier

    running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"})
    session_factory = running_device.app.state.session_factory
    with session_factory() as session:
        device = session.get(Device, 777)
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    names = {str(obj.objectName) for obj in app.objectIdentifier.values()}
    assert {"Zone Temp", "Setpoint", "Mode", "Zone Temp Log"} <= names
    assert str(app.get_object_id(ObjectIdentifier(("trend-log", 20))).objectName) == "Zone Temp Log"


def test_the_other_devices_job_is_not_planned_or_created(running_device, done_job):
    running_device.post("/api/devices", json={"id": 599002, "name": "Other"})
    url = f"/api/devices/599002/client/clone/{done_job}"
    assert running_device.post(f"{url}/plan", json={"id": 777, "name": "Clone"}).status_code == 404
    assert running_device.post(f"{url}/create", json={"id": 777, "name": "Clone"}).status_code == 404


def test_plan_suggests_nothing_while_the_default_bind_is_free_of_other_devices(running_device, done_job):
    with running_device.app.state.session_factory() as session:
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.get(Device, 599001).bind_address = "127.0.0.1/8:47860"
        session.commit()
    suggested = running_device.post(_plan_url(done_job), json={"id": 777, "name": "Clone"}).json()["suggested_bind"]
    assert suggested == {"bind_address": None, "port": None}


def test_create_plans_before_it_takes_the_write_locks(running_device, done_job, monkeypatch):
    from bacpack.web.api import clone

    order = []
    real_plan, real_lock = clone.build_plan, clone.lock_both_files
    monkeypatch.setattr(clone, "build_plan", lambda *a, **k: (order.append("plan"), real_plan(*a, **k))[1])
    monkeypatch.setattr(clone, "lock_both_files", lambda s: (order.append("lock"), real_lock(s))[1])
    assert running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"}).status_code == 201
    assert order == ["plan", "lock"]


def test_a_job_discarded_while_planning_is_404_and_saves_nothing(running_device, done_job, monkeypatch):
    from bacpack.web.api import clone

    real_lock = clone.lock_both_files

    def discard_then_lock(session):
        with running_device.app.state.session_factory() as other:
            other.query(ClientRequest).filter_by(id=done_job).delete()
            other.commit()
        real_lock(session)

    monkeypatch.setattr(clone, "lock_both_files", discard_then_lock)
    assert running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"}).status_code == 404
    assert running_device.get("/api/devices/777").status_code == 404


@pytest.fixture()
def statements(running_device):
    from sqlalchemy import event

    engine = running_device.app.state.session_factory.kw["bind"]
    seen = []

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def _loads_a_result(statements):
    return [s for s in statements if s.lstrip().upper().startswith("SELECT") and "client_requests.result" in s]


def test_a_status_poll_does_not_load_the_result(running_device, done_job, statements):
    statements.clear()
    assert running_device.get(BASE).json()["status"] == "done"
    assert _loads_a_result(statements) == []


def test_a_status_poll_that_fails_a_dead_job_does_not_load_the_result(running_device, statements):
    job_id = running_device.post(BASE, json=BODY).json()["job_id"]
    _set(running_device, job_id, status="running")
    with running_device.app.state.session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "stopped"
        session.commit()
    statements.clear()
    assert running_device.get(BASE).json()["status"] == "error"
    assert _loads_a_result(statements) == []


def test_create_does_not_reload_the_result_under_the_write_locks(running_device, done_job, statements, monkeypatch):
    from bacpack.web.api import clone

    real_lock = clone.lock_both_files

    def mark_then_lock(session):
        real_lock(session)
        statements.append("-- LOCKED")

    monkeypatch.setattr(clone, "lock_both_files", mark_then_lock)
    assert running_device.post(_create_url(done_job), json={"id": 777, "name": "Clone"}).status_code == 201
    after = statements[statements.index("-- LOCKED"):]
    assert _loads_a_result(after) == []
