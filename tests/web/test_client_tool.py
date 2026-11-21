"""The BACnet client tool API: each route queues a ClientRequest for the
device's own process and waits for its answer. No device process runs here: a
thread plays its part by answering the rows (or not, for the timeout test)."""
from __future__ import annotations

import threading
import time

import pytest

from bacpack.models import ClientRequest, Device
from tests.web._helpers import _create_device


def _mark_running(web_client, device_id=599001):
    with web_client.app.state.session_factory() as session:
        session.get(Device, device_id).runtime.process_status = "running"
        session.commit()


@pytest.fixture()
def answerer(web_client):
    """Answers every pending ClientRequest with `reply` (status, result,
    error); records the (kind, params) it saw."""
    state = {"reply": ("done", {"ok": True}, None), "seen": []}
    stop = threading.Event()

    def run():
        session_factory = web_client.app.state.session_factory
        while not stop.is_set():
            with session_factory() as session:
                for row in session.query(ClientRequest).filter_by(status="pending"):
                    state["seen"].append((row.kind, dict(row.params)))
                    row.status, row.result, row.error = state["reply"]
                session.commit()
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield state
    stop.set()
    thread.join(timeout=2)


def test_unknown_device_is_404(web_client):
    assert web_client.post("/api/devices/42/client/who-is", json={}).status_code == 404


def test_stopped_device_is_409(web_client):
    _create_device(web_client)
    r = web_client.post("/api/devices/599001/client/who-is", json={})
    assert r.status_code == 409
    assert "isn't running" in r.json()["detail"]


def test_who_is_returns_the_devices_answer(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    found = [{"instance": 5, "address": "10.0.0.5", "vendor_id": 1, "max_apdu": 1476, "segmentation": "no-segmentation"}]
    answerer["reply"] = ("done", found, None)

    r = web_client.post("/api/devices/599001/client/who-is", json={"low": 1, "high": 10, "window_seconds": 1})

    assert r.status_code == 200, r.text
    assert r.json() == found
    assert answerer["seen"] == [("who-is", {"low": 1, "high": 10, "window_seconds": 1.0})]


@pytest.mark.parametrize(
    "body, sent",
    [
        ({"low": 17}, {"low": 17, "high": 4194303, "window_seconds": 3.0}),
        ({"high": 17}, {"low": 0, "high": 17, "window_seconds": 3.0}),
    ],
)
def test_who_is_with_one_end_fills_the_other(web_client, answerer, body, sent):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("done", [], None)

    r = web_client.post("/api/devices/599001/client/who-is", json=body)

    assert r.status_code == 200, r.text
    assert answerer["seen"] == [("who-is", sent)]


def test_read_params_are_normalized(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("done", {"value": 72.5}, None)

    r = web_client.post(
        "/api/devices/599001/client/read",
        json={"instance": 2, "object_type": "analogInput", "object_instance": 3, "property": "presentValue"},
    )

    assert r.json() == {"value": 72.5}
    kind, params = answerer["seen"][0]
    assert kind == "read"
    assert (params["object_type"], params["property"], params["address"]) == ("analog-input", "present-value", None)


def test_write_and_object_list_are_queued(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)

    r = web_client.post(
        "/api/devices/599001/client/write",
        json={"instance": 2, "address": "10.0.0.2", "object_type": "analog-value", "object_instance": 1, "value": None, "priority": 8},
    )
    assert r.status_code == 200, r.text
    r = web_client.post("/api/devices/599001/client/object-list", json={"instance": 2})
    assert r.status_code == 200, r.text

    assert [kind for kind, _ in answerer["seen"]] == ["write", "object-list"]
    assert answerer["seen"][0][1]["priority"] == 8


def test_bacnet_error_is_502(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("error", None, "object: unknown-object")

    r = web_client.post(
        "/api/devices/599001/client/read",
        json={"instance": 2, "object_type": "analog-input", "object_instance": 99},
    )

    assert (r.status_code, r.json()["detail"]) == (502, "object: unknown-object")


def test_no_answer_is_504_and_marks_the_row(web_client, monkeypatch):
    import bacpack.web.api.client_tool as client_tool

    monkeypatch.setattr(client_tool, "WAIT_MARGIN_SECONDS", 0.0)
    _create_device(web_client)
    _mark_running(web_client)

    r = web_client.post("/api/devices/599001/client/who-is", json={"window_seconds": 0.5})

    assert r.status_code == 504
    with web_client.app.state.session_factory() as session:
        row = session.query(ClientRequest).one()
        assert (row.status, row.error) == ("error", "timed out")


def test_a_write_504_says_it_may_still_land(web_client, monkeypatch):
    import bacpack.web.api.client_tool as client_tool

    monkeypatch.setattr(client_tool, "WAIT_MARGIN_SECONDS", 0.0)
    _create_device(web_client)
    _mark_running(web_client)

    r = web_client.post(
        "/api/devices/599001/client/write",
        json={"instance": 2, "object_type": "analog-value", "object_instance": 1, "value": 1,
              "priority": 8, "timeout_seconds": 1},
    )

    assert r.status_code == 504
    assert "a write already sent may still take effect" in r.json()["detail"]


@pytest.mark.parametrize(
    "path, body",
    [
        ("who-is", {"low": 10, "high": 1}),
        ("read", {"instance": 2, "object_type": "bogus", "object_instance": 1}),
        ("read", {"instance": 2, "address": "somehost", "object_type": "analog-value", "object_instance": 1}),
        ("write", {"instance": 2, "object_type": "analog-value", "object_instance": 1, "value": None}),
        ("write", {"instance": 2, "object_type": "analog-value", "object_instance": 1, "value": 1, "priority": 17}),
    ],
)
def test_bad_requests_are_422(web_client, path, body):
    _create_device(web_client)
    _mark_running(web_client)
    assert web_client.post(f"/api/devices/599001/client/{path}", json=body).status_code == 422


def test_write_refuses_a_non_finite_value(web_client, answerer):
    # A NaN/Infinity would otherwise go out as a Real on the wire.
    _create_device(web_client)
    _mark_running(web_client)
    r = web_client.post(
        "/api/devices/599001/client/write",
        content='{"instance": 2, "object_type": "analog-value", "object_instance": 1, "value": NaN, "priority": 8}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422, r.text
    assert answerer["seen"] == []


def test_identify_is_queued_and_answered(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("done", {"name": "Remote", "vendor_name": "Example"}, None)

    r = web_client.post("/api/devices/599001/client/identify", json={"instance": 5, "address": "10.0.0.5"})

    assert r.status_code == 200, r.text
    assert r.json() == {"name": "Remote", "vendor_name": "Example"}
    kind, params = answerer["seen"][-1]
    assert kind == "identify" and params["instance"] == 5 and params["address"] == "10.0.0.5"


def test_identify_takes_the_wildcard_instance_with_a_static_address(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("done", {"name": "Remote", "vendor_name": "Example"}, None)

    r = web_client.post("/api/devices/599001/client/identify", json={"instance": 4194303, "address": "10.0.0.5"})

    assert r.status_code == 200, r.text
    kind, params = answerer["seen"][-1]
    assert kind == "identify" and params["instance"] == 4194303 and params["address"] == "10.0.0.5"


@pytest.mark.parametrize(
    "route, extra",
    [
        ("identify", {}),
        ("object-list", {}),
        ("read", {"object_type": "device", "object_instance": 4194303}),
    ],
)
def test_the_wildcard_instance_without_an_address_is_422(web_client, answerer, route, extra):
    _create_device(web_client)
    _mark_running(web_client)

    r = web_client.post(f"/api/devices/599001/client/{route}", json={"instance": 4194303, **extra})

    assert r.status_code == 422, r.text
    assert "static address" in r.text
    assert answerer["seen"] == []


def test_an_instance_above_the_wildcard_is_still_422(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)

    r = web_client.post("/api/devices/599001/client/identify", json={"instance": 4194304, "address": "10.0.0.5"})

    assert r.status_code == 422


def test_read_and_object_list_take_the_wildcard_with_a_static_address(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    target = {"instance": 4194303, "address": "10.0.0.5"}

    assert web_client.post("/api/devices/599001/client/object-list", json=target).status_code == 200
    read = {**target, "object_type": "device", "object_instance": 4194303, "property": "object-name"}
    assert web_client.post("/api/devices/599001/client/read", json=read).status_code == 200
    assert [kind for kind, _ in answerer["seen"]] == ["object-list", "read"]


def test_identify_of_a_device_that_does_not_answer_is_502(web_client, answerer):
    _create_device(web_client)
    _mark_running(web_client)
    answerer["reply"] = ("error", None, "no response within 3 s")

    r = web_client.post("/api/devices/599001/client/identify", json={"instance": 5, "timeout_seconds": 3})

    assert r.status_code == 502


def test_identify_waits_long_enough_for_a_device_without_rpm():
    # Bind, the RPM it rejects, then the name and vendor read one at a time.
    import bacpack.web.api.client_tool as client_tool

    assert client_tool._wait_seconds("identify", {"timeout_seconds": 3}) == 3 * 4 + client_tool.WAIT_MARGIN_SECONDS
