"""The live-state API: each call queues a "live" ClientRequest and waits for
the device's answer. No device process runs here: a thread answers the rows, as
in test_client_tool.py."""
from __future__ import annotations

import threading
import time

import pytest

from bacpack.models import ClientRequest, Device
from bacpack.web.api import live
from tests.web._helpers import _create_device, _create_object

ENTRY = {
    "object_id": 1,
    "object_type": "analog-value",
    "instance": 1,
    "present_value": 72.5,
    "priority_array": [None] * 7 + [72.5] + [None] * 8,
    "controlling_priority": 8,
    "relinquish_default": 70.0,
    "status_flags": [],
    "event_state": "normal",
    "reliability": "no-fault-detected",
    "out_of_service": False,
}


def _mark_running(web_client, device_id=599001):
    with web_client.app.state.session_factory() as session:
        session.get(Device, device_id).runtime.process_status = "running"
        session.commit()


@pytest.fixture()
def answerer(web_client):
    """Answers every pending request with `reply(params)` -> (status, result, error)."""
    state = {
        "reply": lambda params: ("done", {"read_at": "2026-10-16T10:48:02+00:00", "objects": []}, None),
        "seen": [],
    }
    stop = threading.Event()

    def run():
        session_factory = web_client.app.state.session_factory
        while not stop.is_set():
            with session_factory() as session:
                for row in session.query(ClientRequest).filter_by(status="pending"):
                    state["seen"].append((row.kind, dict(row.params)))
                    row.status, row.result, row.error = state["reply"](row.params)
                session.commit()
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield state
    stop.set()
    thread.join(timeout=2)


def test_unknown_device_is_404(web_client):
    assert web_client.get("/api/devices/42/live").status_code == 404


def test_stopped_device_is_409(web_client):
    _create_device(web_client)
    r = web_client.get("/api/devices/599001/live")
    assert r.status_code == 409
    assert "isn't running" in r.json()["detail"]


def test_device_live_returns_every_object_and_deletes_the_row(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = lambda params: ("done", {"read_at": "2026-10-16T10:48:02+00:00", "objects": [ENTRY]}, None)

    r = web_client.get("/api/devices/599001/live")

    assert r.status_code == 200, r.text
    assert r.json() == {"read_at": "2026-10-16T10:48:02+00:00", "objects": [ENTRY]}
    assert answerer["seen"] == [("live", {"object_ids": None})]
    with web_client.app.state.session_factory() as session:
        assert session.query(ClientRequest).count() == 0


def test_object_live_asks_for_that_object_only(web_client, answerer):
    _create_device(web_client)
    obj = _create_object(web_client)
    _mark_running(web_client)
    answerer["reply"] = lambda params: (
        "done",
        {"read_at": "2026-10-16T10:48:02+00:00", "objects": [{**ENTRY, "object_id": params["object_ids"][0]}]},
        None,
    )

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/live")

    assert r.status_code == 200, r.text
    assert r.json() == {**ENTRY, "object_id": obj["id"], "read_at": "2026-10-16T10:48:02+00:00"}
    assert answerer["seen"] == [("live", {"object_ids": [obj["id"]]})]


def test_unknown_object_is_404_without_asking_the_device(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    assert web_client.get("/api/devices/599001/objects/9999/live").status_code == 404
    assert answerer["seen"] == []


def test_an_object_the_device_has_not_built_yet_is_404_not_live_yet(web_client, answerer):
    _create_device(web_client)
    obj = _create_object(web_client)
    _mark_running(web_client)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/live")

    assert r.status_code == 404
    assert "isn't live yet" in r.json()["detail"]


def test_an_older_device_process_is_409_restart(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = lambda params: ("error", None, "internal error: 'instance'")

    r = web_client.get("/api/devices/599001/live")

    assert r.status_code == 409
    assert "restart the device to use live values" in r.json()["detail"]


def test_a_device_error_is_502(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = lambda params: ("error", None, "internal error: boom")

    r = web_client.get("/api/devices/599001/live")

    assert r.status_code == 502
    assert r.json()["detail"] == "internal error: boom"


def test_no_answer_is_504(web_client, monkeypatch):
    _create_device(web_client)
    _mark_running(web_client)
    monkeypatch.setattr(live, "LIVE_WAIT_SECONDS", 0.0)

    r = web_client.get("/api/devices/599001/live")

    assert r.status_code == 504
    assert "didn't answer in time" in r.json()["detail"]


def test_the_routes_are_in_the_openapi_schema(web_client):
    paths = web_client.get("/openapi.json").json()["paths"]
    assert "/api/devices/{device_id}/live" in paths
    assert "/api/devices/{device_id}/objects/{object_id}/live" in paths
