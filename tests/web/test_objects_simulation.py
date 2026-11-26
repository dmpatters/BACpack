"""Value simulator settings through the object API."""
from __future__ import annotations

import json

import pytest

from tests.web._helpers import _create_connector, _create_device, _create_object

_SINE = {"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600}


def _url(object_id):
    return f"/api/devices/599001/objects/{object_id}"


def _feed(web_client, object_id):
    connector = _create_connector(web_client)
    return web_client.post(
        f"/api/connectors/{connector['id']}/mappings", json={"object_id": object_id, "json_path": "temp"}
    )


def test_create_stores_normalized_settings(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, simulation={**_SINE, "kind": "SINE"})
    assert obj["simulation"] == {
        "kind": "sine",
        "center": 72.0,
        "amplitude": 3.0,
        "period_s": 600.0,
        "phase_s": 0.0,
        "interval_s": 5.0,
        "decimals": 2,
    }


def test_update_sets_and_clears_simulation(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.put(_url(obj["id"]), json={"simulation": _SINE})
    assert r.status_code == 200, r.text
    assert r.json()["simulation"]["kind"] == "sine"
    r = web_client.put(_url(obj["id"]), json={"simulation": None})
    assert r.status_code == 200, r.text
    assert r.json()["simulation"] is None


@pytest.mark.parametrize(
    "object_type,simulation,reason",
    [
        ("analog-value", {"kind": "cycle", "dwell_s": 5}, "must be one of sine"),
        ("binary-value", {"kind": "follow", "source": "analog-value:1"}, "must be one of square, random"),
        ("analog-value", {"kind": "sine", "center": 1, "amplitude": 1, "period_s": 0}, "period_s: Input should be greater"),
        ("analog-value", {"kind": "triangle", "low": 5, "high": 5, "period_s": 10}, "low must be below high"),
        (
            "analog-value",
            {"kind": "sine", "center": 1, "amplitude": 1, "period_s": 10, "bogus": 1},
            "bogus: Extra inputs",
        ),
        ("calendar", {"kind": "sine", "center": 1, "amplitude": 1, "period_s": 10}, "can't be simulated"),
    ],
)
def test_bad_settings_are_400(web_client, object_type, simulation, reason):
    _create_device(web_client)
    body = {"object_type": object_type, "instance_number": 5, "name": "X", "simulation": simulation}
    if object_type == "calendar":  # its own check runs first and needs an entry
        body["date_list"] = [{"kind": "date", "year": "*", "month": 12, "day": 25}]
    r = web_client.post("/api/devices/599001/objects", json=body)
    assert r.status_code == 400, r.text
    assert "simulation" in r.json()["detail"]
    assert reason in r.json()["detail"]


def test_nan_setting_is_refused(web_client):
    _create_device(web_client)
    body = {
        "object_type": "analog-value",
        "instance_number": 5,
        "name": "X",
        "simulation": {"kind": "noise", "center": float("nan"), "amplitude": 1},
    }
    r = web_client.post(
        "/api/devices/599001/objects", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400, r.text
    assert web_client.get("/api/devices/599001/objects").json() == []


def test_states_above_the_state_count_are_refused_both_ways(web_client):
    _create_device(web_client)
    obj = _create_object(
        web_client, object_type="multi-state-value", name="MSV-1", units=None, state_text=["A", "B", "C"]
    )
    r = web_client.put(_url(obj["id"]), json={"simulation": {"kind": "cycle", "dwell_s": 10, "states": [1, 4]}})
    assert r.status_code == 400, r.text
    r = web_client.put(_url(obj["id"]), json={"simulation": {"kind": "cycle", "dwell_s": 10, "states": [1, 3]}})
    assert r.status_code == 200, r.text
    r = web_client.put(_url(obj["id"]), json={"state_text": ["A", "B"]})
    assert r.status_code == 400, r.text
    assert web_client.get(_url(obj["id"])).json()["state_text"] == ["A", "B", "C"]


def test_simulator_refused_on_a_fed_object(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    assert _feed(web_client, obj["id"]).status_code == 201
    r = web_client.put(_url(obj["id"]), json={"simulation": _SINE})
    assert r.status_code == 409, r.text
    assert "connector" in r.json()["detail"]


def test_mapping_refused_on_a_simulated_object(web_client):
    # The check sits before the read/proxy branch in create_mapping, so it
    # covers HTTP, BACnet read and proxy mappings alike.
    _create_device(web_client)
    obj = _create_object(web_client, simulation=_SINE)
    r = _feed(web_client, obj["id"])
    assert r.status_code == 409, r.text
    assert "simulat" in r.json()["detail"]


def test_follow_needs_an_existing_analog_source_and_not_itself(web_client):
    _create_device(web_client)
    source = _create_object(web_client)  # analog-value:1
    follow = {"kind": "follow", "source": "analog-value:9"}
    r = web_client.post(
        "/api/devices/599001/objects",
        json={"object_type": "analog-value", "instance_number": 2, "name": "AV-2", "simulation": follow},
    )
    assert r.status_code == 409, r.text
    assert "does not exist" in r.json()["detail"]
    r = web_client.post(
        "/api/devices/599001/objects",
        json={
            "object_type": "analog-value",
            "instance_number": 2,
            "name": "AV-2",
            "simulation": {"kind": "follow", "source": "analog-value:2"},
        },
    )
    assert r.status_code == 400, r.text
    assert "itself" in r.json()["detail"]
    created = _create_object(
        web_client, instance_number=2, name="AV-2", simulation={"kind": "follow", "source": "Analog-Value:1"}
    )
    assert created["simulation"]["source"] == "analog-value:1"
    r = web_client.delete(_url(source["id"]))
    assert r.status_code == 409, r.text
    assert "simulation source" in r.json()["detail"]


def test_config_import_refuses_simulating_a_fed_object(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    assert _feed(web_client, obj["id"]).status_code == 201
    doc = web_client.get("/api/config/export").json()
    doc["devices"][0]["objects"][0]["simulation"] = _SINE
    for dry_run in ("true", "false"):
        r = web_client.post(
            f"/api/config/import?dry_run={dry_run}",
            files={"file": ("c.json", json.dumps(doc), "application/json")},
        )
        assert r.status_code == 409, (dry_run, r.text)
        assert "connector" in r.json()["detail"]
    assert web_client.get(_url(obj["id"])).json()["simulation"] is None


def test_csv_sync_refuses_simulating_a_fed_object(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    assert _feed(web_client, obj["id"]).status_code == 201
    cell = json.dumps({"kind": "noise", "center": 1, "amplitude": 1}).replace('"', '""')
    csv_text = f'object_type,instance_number,name,simulation\nanalog-value,1,AV-1,"{cell}"\n'
    r = web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 409, r.text
    assert web_client.get(_url(obj["id"])).json()["simulation"] is None


# --- Bulk apply -----------------------------------------------------------


def _batch(web_client, ids, patch, **extra):
    return web_client.patch("/api/devices/599001/objects/batch", json={"object_ids": ids, "patch": patch, **extra})


def _analog_ids(web_client, count):
    return [_create_object(web_client, instance_number=i, name=f"AV-{i}")["id"] for i in range(1, count + 1)]


def test_batch_random_phase_gives_each_object_its_own_phase(web_client):
    _create_device(web_client)
    ids = _analog_ids(web_client, 5)
    r = _batch(web_client, ids, {"simulation": _SINE}, random_phase=True)
    assert r.status_code == 200, r.text
    phases = [o["simulation"]["phase_s"] for o in r.json()]
    assert all(0 <= p < 600 for p in phases)
    assert len(set(phases)) == 5


def test_batch_without_random_phase_keeps_the_given_phase(web_client):
    _create_device(web_client)
    ids = _analog_ids(web_client, 3)
    r = _batch(web_client, ids, {"simulation": {**_SINE, "phase_s": 30}})
    assert r.status_code == 200, r.text
    assert [o["simulation"]["phase_s"] for o in r.json()] == [30.0, 30.0, 30.0]


def test_batch_cycle_phase_stays_within_each_objects_own_cycle(web_client, monkeypatch):
    import bacpack.simulation as simulation

    monkeypatch.setattr(simulation.random, "uniform", lambda low, high: high - 0.001)
    _create_device(web_client)
    two = _create_object(web_client, object_type="multi-state-value", name="M2", units=None, state_text=["A", "B"])
    four = _create_object(
        web_client,
        object_type="multi-state-value",
        instance_number=2,
        name="M4",
        units=None,
        state_text=["A", "B", "C", "D"],
    )
    r = _batch(web_client, [two["id"], four["id"]], {"simulation": {"kind": "cycle", "dwell_s": 10}}, random_phase=True)
    assert r.status_code == 200, r.text
    phases = {o["name"]: o["simulation"]["phase_s"] for o in r.json()}
    assert phases == {"M2": pytest.approx(19.999), "M4": pytest.approx(39.999)}


def test_batch_is_all_or_nothing(web_client):
    _create_device(web_client)
    av = _create_object(web_client)
    bv = _create_object(web_client, object_type="binary-value", name="BV-1", units=None)
    r = _batch(web_client, [av["id"], bv["id"]], {"simulation": _SINE})
    assert r.status_code == 400, r.text
    assert "BV-1" in r.json()["detail"]
    assert all(o["simulation"] is None for o in web_client.get("/api/devices/599001/objects").json())


def test_batch_refuses_a_fed_object(web_client):
    _create_device(web_client)
    ids = _analog_ids(web_client, 2)
    assert _feed(web_client, ids[1]).status_code == 201
    r = _batch(web_client, ids, {"simulation": _SINE}, random_phase=True)
    assert r.status_code == 409, r.text
    assert "AV-2" in r.json()["detail"]
    assert all(o["simulation"] is None for o in web_client.get("/api/devices/599001/objects").json())


def test_batch_stop_simulating(web_client):
    _create_device(web_client)
    ids = _analog_ids(web_client, 2)
    assert _batch(web_client, ids, {"simulation": _SINE}).status_code == 200
    r = _batch(web_client, ids, {"simulation": None})
    assert r.status_code == 200, r.text
    assert [o["simulation"] for o in r.json()] == [None, None]
