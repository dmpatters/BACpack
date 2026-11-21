"""The EDE import API for new devices: read, plan, create."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from bacpack.clone_plan import ClonePlan
from bacpack.models import BacnetObject, ClientRequest, Device
from bacpack.web.api import _uploads, ede
from tests.ede_fixtures import WIDE, device_row, ede_bytes, ede_row, ede_text, state_texts_text
from tests.web._helpers import _create_device

TWO_DEVICES = [
    device_row(1000, "Boiler", description="Plant room"),
    ede_row(device=1000, type=0, instance=1, name="Temp", unit=62, keyname="B.T"),
    ede_row(device=1000, type=3, instance=1, name="Door", state_ref="1", keyname="B.D"),
    ede_row(device=1000, type=20, instance=9, name="Temp.Log", keyname="B.T.L"),
    ede_row(device=1000, type=9, instance=1, name="Loop"),
    device_row(2000, "Chiller"),
    ede_row(device=2000, type=2, instance=1, name="SP", default="7"),
]


def _file(rows=TWO_DEVICES):
    return ("plant.csv", ede_bytes(ede_text(rows, columns=WIDE)), "text/csv")


def _choices(*pairs):
    return json.dumps([{"instance": i, "id": d, "name": n} for i, d, n in pairs])


def _both():
    return _choices((1000, 1000, "Boiler"), (2000, 2000, "Chiller"))


def _read(client, **files):
    return client.post("/api/ede/read", files={"file": _file(), **files})


def _plan(client, devices, rows=TWO_DEVICES, **files):
    return client.post("/api/ede/plan", files={"file": _file(rows), **files}, data={"devices": devices})


def _create(client, devices, rows=TWO_DEVICES, digest=None, **files):
    data = {"devices": devices}
    if digest is not None:
        data["plan_digest"] = digest
    return client.post("/api/ede/create", files={"file": _file(rows), **files}, data=data)


def _device_ids(client):
    with client.app.state.session_factory() as session:
        return sorted(session.scalars(select(Device.id)))


# --- read -------------------------------------------------------------------------


def test_read_lists_the_devices_with_counts(web_client):
    r = _read(web_client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_name"] == "Test project" and body["version_of_layout"] == "2.1"
    assert body["devices"] == [
        {"instance": 1000, "name": "Boiler", "object_count": 4, "existing_device": None},
        {"instance": 2000, "name": "Chiller", "object_count": 1, "existing_device": None},
    ]
    assert "columns_not_used" in body and "file_notes" in body


def test_read_marks_a_device_that_already_exists(web_client):
    _create_device(web_client, 1000, "Mine")
    first = _read(web_client).json()["devices"][0]
    assert first["existing_device"] == {"id": 1000, "name": "Mine"}


def test_read_refuses_a_non_ede_file_and_missing_columns_with_400(web_client):
    r = web_client.post("/api/ede/read", files={"file": ("a.csv", b"object_type,name\nx,y\n", "text/csv")})
    assert r.status_code == 400 and "not an EDE file" in r.json()["detail"]
    text = "#keyname;object-name;object-type\nk;A;0\n"
    r = web_client.post("/api/ede/read", files={"file": ("a.csv", text.encode(), "text/csv")})
    assert r.status_code == 400 and "object-instance" in r.json()["detail"]


def test_read_oversized_upload_is_413(web_client, monkeypatch):
    monkeypatch.setattr(_uploads, "MAX_UPLOAD_BYTES", 1024)
    r = web_client.post("/api/ede/read", files={"file": ("big.csv", b"a" * 2048, "text/csv")})
    assert r.status_code == 413


def test_state_texts_file_is_used(web_client):
    table = ("t.csv", state_texts_text([["1", "Shut", "Open"]]).encode(), "text/csv")
    r = _create(web_client, _choices((1000, 1000, "Boiler")), state_texts=table)
    assert r.status_code == 201, r.text
    with web_client.app.state.session_factory() as session:
        door = session.scalars(select(BacnetObject).filter_by(device_id=1000, name="Door")).one()
        assert (door.inactive_text, door.active_text) == ("Shut", "Open")


# --- plan ---------------------------------------------------------------------------


def test_plan_returns_counts_skipped_and_notes_per_device(web_client):
    r = _plan(web_client, _both())
    assert r.status_code == 200, r.text
    body = r.json()
    boiler, chiller = body["plans"]
    assert boiler["instance"] == 1000 and boiler["object_count"] == 2
    assert boiler["counts"] == {"analog-input": 1, "binary-input": 1}
    assert boiler["skipped_types"] == {"event-enrollment (9)": 1} and boiler["skipped"][0]["name"] == "Loop"
    assert any("trending guessed" in n["note"] for n in boiler["notes"])
    assert boiler["device"]["name"] == "Boiler" and boiler["device"]["description"] == "Plant room"
    assert boiler["problem"] is None and chiller["object_count"] == 1
    assert body["plan_digest"] and body["vendor_name"] is None


def test_plan_problem_for_existing_id_duplicate_id_and_name_clash(web_client):
    _create_device(web_client, 1000, "Taken")
    plans = _plan(web_client, _both()).json()["plans"]
    assert "1000 already exists" in plans[0]["problem"] and plans[1]["problem"] is None

    same_id = _plan(web_client, _choices((1000, 5, "A"), (2000, 5, "B"))).json()["plans"]
    assert "5" in same_id[1]["problem"] and "also" in same_id[1]["problem"]

    same_name = _plan(web_client, _choices((1000, 7, "Same"), (2000, 8, "Same"))).json()["plans"]
    assert "Same" in same_name[1]["problem"]

    used = _plan(web_client, _choices((1000, 7, "Taken"))).json()["plans"]
    assert "Taken" in used[0]["problem"] and "already" in used[0]["problem"]


def test_plan_names_a_device_the_file_lacks_with_400(web_client):
    r = _plan(web_client, _choices((3000, 3000, "Nope")))
    assert r.status_code == 400 and "3000" in r.json()["detail"]


def test_plan_bad_devices_field_is_422(web_client):
    assert _plan(web_client, "not json").status_code == 422
    assert _plan(web_client, json.dumps([{"instance": 1000, "id": -1, "name": "x"}])).status_code == 422


# --- create -----------------------------------------------------------------------------


def test_create_saves_stopped_devices_with_the_files_instances_in_one_transaction(web_client):
    r = _create(web_client, _both())
    assert r.status_code == 201, r.text
    assert r.json() == {"device_ids": [1000, 2000]}
    with web_client.app.state.session_factory() as session:
        boiler, chiller = session.get(Device, 1000), session.get(Device, 2000)
        assert (boiler.name, boiler.description, boiler.bind_address) == ("Boiler", "Plant room", None)
        assert boiler.runtime.process_status != "running" and chiller.name == "Chiller"
        assert sorted(o.name for o in boiler.objects) == ["Door", "Temp"]
        temp = next(o for o in boiler.objects if o.name == "Temp")
        assert (temp.units, temp.trended, temp.trend_log_instance, temp.trend_log_name) == (
            "degreesCelsius", True, 9, "Temp.Log",
        )
        assert [o.name for o in chiller.objects] == ["SP"]


def test_create_with_edited_id_and_name(web_client):
    r = _create(web_client, _choices((1000, 599010, "Renamed")))
    assert r.status_code == 201 and r.json() == {"device_ids": [599010]}
    with web_client.app.state.session_factory() as session:
        assert session.get(Device, 599010).name == "Renamed"


def test_unticked_devices_are_not_created(web_client):
    _create(web_client, _choices((2000, 2000, "Chiller")))
    assert _device_ids(web_client) == [2000]


def test_create_refuses_when_a_ticked_device_id_exists_and_saves_nothing(web_client):
    _create_device(web_client, 2000, "Taken")
    r = _create(web_client, _both())
    assert r.status_code == 409 and "2000" in r.json()["detail"]
    assert _device_ids(web_client) == [2000]
    with web_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BacnetObject)) == 0


def test_create_twice_is_409_the_second_time(web_client):
    assert _create(web_client, _both()).status_code == 201
    r = _create(web_client, _both())
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert _device_ids(web_client) == [1000, 2000]


def test_create_refuses_a_plan_with_a_problem_naming_the_device(web_client):
    r = _create(web_client, _choices((1000, 1000, "NetworkPort-1")))
    assert r.status_code == 409 and "1000" in r.json()["detail"]
    assert _device_ids(web_client) == []


def test_create_with_the_previews_digest_applies(web_client):
    digest = _plan(web_client, _both()).json()["plan_digest"]
    assert _create(web_client, _both(), digest=digest).status_code == 201


def test_create_digest_mismatch_is_409(web_client):
    r = _create(web_client, _both(), digest="0" * 64)
    assert r.status_code == 409 and "changed since the preview" in r.json()["detail"]
    assert _device_ids(web_client) == []


def test_digest_changes_with_the_id_the_name_and_the_objects(web_client):
    base = _plan(web_client, _both()).json()["plan_digest"]
    assert _plan(web_client, _choices((1000, 1001, "Boiler"), (2000, 2000, "Chiller"))).json()["plan_digest"] != base
    assert _plan(web_client, _choices((1000, 1000, "Other"), (2000, 2000, "Chiller"))).json()["plan_digest"] != base
    fewer = TWO_DEVICES[:-1]
    assert _plan(web_client, _both(), rows=fewer).json()["plan_digest"] != base


def test_create_checks_trend_log_identity_and_saves_nothing(web_client, monkeypatch):
    # A defaulted trend log instance is the object's row id, which only the
    # flush gives: here the first trended object's (1) is the second's explicit one.
    def row(instance, name, **fields):
        from bacpack.config_io import OBJECT_FIELD_DEFAULTS

        return {
            **OBJECT_FIELD_DEFAULTS, "object_type": "analog-input", "instance_number": instance, "name": name,
            "trended": True, **fields,
        }

    plan = ClonePlan(
        device={"id": 77, "name": "Clash", "description": None, "location": None, "vendor_name": None, "vendor_identifier": None},
        objects=[row(1, "A"), row(2, "B", trend_log_instance=1)],
    )
    monkeypatch.setattr(ede, "plan_device", lambda *a, **k: plan)
    r = _create(web_client, _choices((1000, 77, "Clash")))
    assert r.status_code == 409, r.text
    assert _device_ids(web_client) == []


def test_files_are_not_stored(web_client):
    _create(web_client, _both())
    with web_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ClientRequest)) == 0


# --- routing ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["read", "plan", "create"])
def test_ede_routes_do_not_shadow_other_routes(web_client, path):
    assert web_client.request("PATCH", f"/api/ede/{path}").status_code == 405
    assert web_client.get("/api/devices").status_code == 200
    assert web_client.post(f"/api/ede/{path}").status_code == 422  # a real route: the body is what is missing


def test_read_refuses_a_cell_over_the_csv_limit_in_either_file_with_400(web_client):
    import csv

    big = "x" * (csv.field_size_limit() + 1)
    rows = [device_row(1000, "Boiler"), ede_row(device=1000, type=0, instance=1, name="Temp", description=big)]
    r = web_client.post("/api/ede/read", files={"file": _file(rows)})
    assert r.status_code == 400
    texts = state_texts_text([["1", big]]).encode()
    r = _read(web_client, state_texts=("t.csv", texts, "text/csv"))
    assert r.status_code == 400
    assert "state-texts" in r.json()["detail"]


def test_a_device_choice_with_a_surrogate_in_its_name_is_422(web_client):
    choices = json.dumps([{"instance": 1000, "id": 1000, "name": "\ud800"}])
    r = _plan(web_client, choices)
    assert r.status_code == 422  # pydantic's JSON parser refuses a lone surrogate itself
