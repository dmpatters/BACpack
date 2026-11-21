"""GET /api/sample and POST /api/sample/load.
Probes bind real UDP ports and started devices serve on them, so this file is
in UDP_TEST_FILES."""
from __future__ import annotations

import pytest

from bacpack import free_port, sample_building
from bacpack.engine import EngineError
from bacpack.models import Device, get_or_create_settings
from tests.web._helpers import _create_device, _create_object

IDS = [4190001, 4190101, 4190102, 4190103]


@pytest.fixture(autouse=True)
def _range(monkeypatch):
    monkeypatch.setattr(free_port, "FIRST_PORT", 52610)
    monkeypatch.setattr(free_port, "LAST_PORT", 52619)
    monkeypatch.setattr(sample_building, "BACNET_PORT", 52605)


def _default_bind(client, bind="127.0.0.1/8:52605"):
    with client.app.state.session_factory() as session:
        get_or_create_settings(session).bind_address = bind
        session.commit()


def _device_ids(client):
    return sorted(d["id"] for d in client.get("/api/devices").json())


def test_get_shows_the_plan_and_writes_nothing(web_client):
    _default_bind(web_client)
    body = web_client.get("/api/sample").json()
    assert body["refusal"] is None
    assert [d["id"] for d in body["devices"]] == IDS
    assert [d["port"] for d in body["devices"]] == [52605, 52610, 52611, 52612]
    assert body["devices"][0] == {
        "id": 4190001, "name": "Sample AHU-1", "role": "bbmd", "object_count": 13,
        "bind_address": "127.0.0.1/8:52605", "port": 52605,
    }
    assert {"text", "guide"} == set(body["notes"][0])
    assert _device_ids(web_client) == []


def test_get_without_a_default_bind_says_why(web_client):
    r = web_client.get("/api/sample")
    assert r.status_code == 200
    assert "default bind" in r.json()["refusal"]


def test_load_without_starting_creates_stopped_devices(web_client):
    _default_bind(web_client)
    r = web_client.post("/api/sample/load", json={"start": False})
    assert r.status_code == 201, r.text
    devices = r.json()["devices"]
    assert [d["id"] for d in devices] == IDS
    assert all(d["started"] is None and d["error"] is None for d in devices)
    assert _device_ids(web_client) == IDS
    status = web_client.get("/api/engine/status").json()
    assert not any(s["process_status"] == "running" for s in status)


def test_load_with_the_default_starts_every_device(web_client):
    _default_bind(web_client)
    r = web_client.post("/api/sample/load", json={})
    assert r.status_code == 201, r.text
    assert [d["started"] for d in r.json()["devices"]] == [True] * 4
    status = {s["device_id"]: s["process_status"] for s in web_client.get("/api/engine/status").json()}
    assert [status[i] for i in IDS] == ["running"] * 4
    for i in IDS:
        web_client.post("/api/engine/stop", json={"device_id": i})


def test_a_failed_start_does_not_stop_the_rest(web_client):
    _default_bind(web_client)
    engine = web_client.app.state.engine
    real_start = engine.start

    async def start(device_id):
        if device_id == 4190102:
            raise EngineError("port in use")
        await real_start(device_id)

    engine.start = start
    r = web_client.post("/api/sample/load", json={"start": True})
    engine.start = real_start
    assert r.status_code == 201, r.text
    by_id = {d["id"]: d for d in r.json()["devices"]}
    assert by_id[4190102]["started"] is False and "port in use" in by_id[4190102]["error"]
    assert [by_id[i]["started"] for i in (4190001, 4190101, 4190103)] == [True] * 3
    for i in (4190001, 4190101, 4190103):
        web_client.post("/api/engine/stop", json={"device_id": i})


def test_loading_twice_is_a_409_and_adds_nothing(web_client):
    _default_bind(web_client)
    assert web_client.post("/api/sample/load", json={"start": False}).status_code == 201
    r = web_client.post("/api/sample/load", json={"start": False})
    assert r.status_code == 409
    assert "4190001" in r.json()["detail"]
    assert _device_ids(web_client) == IDS


def test_a_name_clash_is_a_409(web_client):
    _default_bind(web_client)
    _create_device(web_client, 1, "Sample VAV-3")
    r = web_client.post("/api/sample/load", json={"start": False})
    assert r.status_code == 409
    assert "Sample VAV-3" in r.json()["detail"]
    assert _device_ids(web_client) == [1]


def test_load_without_a_default_bind_is_a_409(web_client):
    r = web_client.post("/api/sample/load", json={"start": False})
    assert r.status_code == 409
    assert "default bind" in r.json()["detail"]
    assert _device_ids(web_client) == []


def test_the_load_leaves_other_devices_untouched(web_client):
    _default_bind(web_client)
    _create_device(web_client, 599001, "Mine")
    _create_object(web_client, 599001)
    assert web_client.post("/api/sample/load", json={"start": False}).status_code == 201
    objects = web_client.get("/api/devices/599001/objects").json()
    assert [o["name"] for o in objects] == ["AV-1"]
    with web_client.app.state.session_factory() as session:
        assert session.get(Device, 599001).name == "Mine"
