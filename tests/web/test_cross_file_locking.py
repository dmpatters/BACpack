"""Saves that write both database files wait for a lock another process holds
instead of failing at once. Each test holds one file's write lock for 0.5 s
from another connection, the way a device process writing at that moment
would."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from contextlib import contextmanager

from bacpack.db import history_db_path
from bacpack.trending import record_sample
from tests.web._helpers import _create_device, _create_object

HOLD_SECONDS = 0.5


@contextmanager
def _held(path):
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(HOLD_SECONDS, holder.rollback)
    timer.start()
    try:
        yield
    finally:
        timer.join()
        holder.close()


def _main(tmp_path):
    return tmp_path / "web-test.db"


def _trended_object_with_samples(web_client, device_id=599001):
    obj = _create_object(web_client, device_id=device_id, trended=True, trend_mode="interval", trend_interval_seconds=30)
    for value in (1.0, 2.0, 3.0):
        record_sample(web_client.app.state.session_factory, obj["id"], value)
    return obj


def test_batch_delete_waits_for_a_held_main_lock(web_client, tmp_path):
    _create_device(web_client)
    obj = _trended_object_with_samples(web_client)
    with _held(_main(tmp_path)):
        r = web_client.request("DELETE", "/api/devices/599001/objects/batch", json={"object_ids": [obj["id"]]})
    assert r.status_code == 200, r.text


def test_object_delete_waits_for_a_held_main_lock(web_client, tmp_path):
    _create_device(web_client)
    obj = _trended_object_with_samples(web_client)
    with _held(_main(tmp_path)):
        r = web_client.delete(f"/api/devices/599001/objects/{obj['id']}")
    assert r.status_code == 204, r.text


def test_device_delete_waits_for_a_held_main_lock(web_client, tmp_path):
    _create_device(web_client)
    _trended_object_with_samples(web_client)
    with _held(_main(tmp_path)):
        r = web_client.delete("/api/devices/599001")
    assert r.status_code == 204, r.text


def test_lowering_a_trend_cap_waits_for_a_held_history_lock(web_client, tmp_path):
    _create_device(web_client)
    obj = _trended_object_with_samples(web_client)
    with _held(history_db_path(_main(tmp_path))):
        r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trend_sample_cap": 1})
    assert r.status_code == 200, r.text


def test_batch_lowering_a_trend_cap_waits_for_a_held_history_lock(web_client, tmp_path):
    _create_device(web_client)
    obj = _trended_object_with_samples(web_client)
    with _held(history_db_path(_main(tmp_path))):
        r = web_client.patch(
            "/api/devices/599001/objects/batch",
            json={"object_ids": [obj["id"]], "patch": {"trend_sample_cap": 1}},
        )
    assert r.status_code == 200, r.text


def test_lowering_the_trend_limit_waits_for_a_held_history_lock(web_client, tmp_path, monkeypatch):
    from bacpack import trending

    # As test_settings.py's prune test: a sample size large enough that a
    # whole-MB limit prunes a handful of samples.
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 300_000)
    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        record_sample(web_client.app.state.session_factory, obj["id"], value)
    with _held(history_db_path(_main(tmp_path))):
        r = web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 1})
    assert r.status_code == 200, r.text
    assert web_client.app.state.trend_pruner.wait_idle(5)
    assert len(web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend").json()) <= 3


# --- Samples deleted before the lock ----------------------------------------
# A delete of millions of samples would otherwise hold main's lock for the
# whole delete. The routes delete the samples in history-only chunks first.


def _sample_count(web_client, object_id=None):
    from sqlalchemy import func, select

    from bacpack.models import TrendSample

    with web_client.app.state.session_factory() as session:
        query = select(func.count()).select_from(TrendSample)
        if object_id is not None:
            query = query.where(TrendSample.object_id == object_id)
        return session.scalar(query)


def _running_total(web_client):
    from bacpack.models import get_or_create_history_counters

    with web_client.app.state.session_factory() as session:
        return get_or_create_history_counters(session).trend_total_sample_count


@pytest.mark.parametrize(
    "module, request_args",
    [
        ("objects", lambda obj: ("DELETE", f"/api/devices/599001/objects/{obj['id']}", {})),
        ("objects_batch", lambda obj: ("DELETE", "/api/devices/599001/objects/batch", {"json": {"object_ids": [obj["id"]]}})),
        ("devices", lambda obj: ("DELETE", "/api/devices/599001", {})),
    ],
)
def test_a_delete_removes_the_samples_before_locking_both_files(web_client, monkeypatch, module, request_args):
    import importlib

    from bacpack import db

    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 2)
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    other = _trended_object_with_samples(web_client, device_id=599002)
    for value in range(5):
        record_sample(web_client.app.state.session_factory, obj["id"], float(value))
    route_module = importlib.import_module(f"bacpack.web.api.{module}")
    real_lock = route_module.lock_both_files
    at_lock = []

    def spy(session):
        at_lock.append((_sample_count(web_client, obj["id"]), _running_total(web_client)))
        real_lock(session)

    monkeypatch.setattr(route_module, "lock_both_files", spy)
    method, url, kwargs = request_args(obj)
    r = web_client.request(method, url, **kwargs)
    assert r.status_code in (200, 204), r.text
    assert at_lock == [(0, 3)]
    assert _sample_count(web_client) == 3 == _sample_count(web_client, other["id"])
    assert _running_total(web_client) == 3


# Sync imports delete objects and devices too.

def _csv_sync(web_client, csv_text):
    return web_client.post(
        "/api/devices/599001/objects/import?mode=sync",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )


def _config_import(web_client, drop_object=False, drop_device=False):
    doc = web_client.get("/api/config/export").json()
    if drop_device:
        doc["devices"] = [d for d in doc["devices"] if d["id"] != 599001]
    if drop_object:
        device = next(d for d in doc["devices"] if d["id"] == 599001)
        device["objects"] = [o for o in device["objects"] if o["name"] != "AV-1"]
    return web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )


def _sync_setup(web_client, monkeypatch):
    from bacpack import db

    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 2)
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    _create_object(web_client, instance_number=2, name="AV-2")
    other = _trended_object_with_samples(web_client, device_id=599002)
    for value in range(5):
        record_sample(web_client.app.state.session_factory, obj["id"], float(value))
    return obj, other


@pytest.mark.parametrize(
    "send",
    [
        lambda client: _csv_sync(client, "object_type,instance_number,name\nanalog-value,2,AV-2\n"),
        lambda client: _config_import(client, drop_object=True),
        lambda client: _config_import(client, drop_device=True),
    ],
    ids=["csv-sync", "config-object", "config-device"],
)
def test_a_sync_import_removes_the_samples_before_locking_both_files(web_client, monkeypatch, send):
    from bacpack.web.api import object_io

    obj, other = _sync_setup(web_client, monkeypatch)
    real_lock = object_io.lock_both_files
    at_lock = []

    def spy(session):
        at_lock.append((_sample_count(web_client, obj["id"]), _running_total(web_client)))
        real_lock(session)

    monkeypatch.setattr(object_io, "lock_both_files", spy)
    r = send(web_client)
    assert r.status_code == 200, r.text
    assert at_lock == [(0, 3)]
    assert _sample_count(web_client) == 3 == _sample_count(web_client, other["id"])
    assert _running_total(web_client) == 3


def test_a_refused_csv_sync_keeps_the_samples(web_client, monkeypatch):
    obj, _ = _sync_setup(web_client, monkeypatch)
    # Only the whole-device check refuses an object named after the device.
    r = _csv_sync(web_client, "object_type,instance_number,name\nanalog-value,2,Demo Device\n")
    assert r.status_code == 409, r.text
    assert _sample_count(web_client, obj["id"]) == 5
    assert _running_total(web_client) == 8


def test_a_csv_sync_with_a_stale_digest_keeps_the_samples(web_client, monkeypatch):
    obj, _ = _sync_setup(web_client, monkeypatch)
    csv_text = "object_type,instance_number,name\nanalog-value,2,AV-2\n"
    preview = web_client.post(
        "/api/devices/599001/objects/import?mode=sync&dry_run=true",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )
    digest = preview.json()["plan_digest"]
    _create_object(web_client, instance_number=9, name="AV-9")

    r = web_client.post(
        f"/api/devices/599001/objects/import?mode=sync&plan_digest={digest}",
        files={"file": ("objects.csv", csv_text, "text/csv")},
    )

    assert r.status_code == 409, r.text
    assert _sample_count(web_client, obj["id"]) == 5
    assert _running_total(web_client) == 8


# A lowered soft limit prunes nowhere under the lock: the save commits
# and kicks the background pruner.


def _soft_limit_setup(web_client, monkeypatch):
    """Eight samples, 1 MB holding three of them."""
    from bacpack import trending

    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 300_000)
    _create_device(web_client)
    _trended_object_with_samples(web_client)
    obj = _create_object(web_client, instance_number=2, name="AV-2", trended=True, trend_mode="interval", trend_interval_seconds=30)
    for value in (10.0, 11.0, 12.0, 13.0, 14.0):
        record_sample(web_client.app.state.session_factory, obj["id"], value)
    return obj


def _spy_kicks(monkeypatch):
    from bacpack.trending import TrendPruner

    kicks = []
    real_kick = TrendPruner.kick

    def kick(self):
        kicks.append(1)
        real_kick(self)

    monkeypatch.setattr(TrendPruner, "kick", kick)
    return kicks


def _settings_save(web_client):
    return web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 1})


def _config_import_with_soft_limit(web_client):
    doc = web_client.get("/api/config/export").json()
    doc["settings"]["trend_soft_limit_mb"] = 1
    return web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(doc), "application/json")},
    )


@pytest.mark.parametrize(
    "module, send",
    [("settings", _settings_save), ("object_io", _config_import_with_soft_limit)],
    ids=["settings", "config-import"],
)
@pytest.mark.usefixtures("quiet_retention")
def test_lowering_the_soft_limit_prunes_nothing_under_the_lock(web_client, monkeypatch, module, send):
    import importlib

    obj = _soft_limit_setup(web_client, monkeypatch)
    route_module = importlib.import_module(f"bacpack.web.api.{module}")
    real_lock = route_module.lock_both_files
    at_lock = []

    def spy(session):
        at_lock.append(_sample_count(web_client))
        real_lock(session)

    monkeypatch.setattr(route_module, "lock_both_files", spy)
    kicks = _spy_kicks(monkeypatch)
    r = send(web_client)
    assert r.status_code == 200, r.text
    assert at_lock == [8]
    assert len(kicks) == 1
    assert web_client.app.state.trend_pruner.wait_idle(5)
    trend = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend").json()
    assert [s["value"] for s in trend] == [12.0, 13.0, 14.0]  # the newest three
    assert _running_total(web_client) == 3


@pytest.mark.parametrize("refused", [{"bind_address": "not-an-address"}], ids=["network"])
@pytest.mark.usefixtures("quiet_retention")
def test_a_refused_settings_save_kicks_nothing(web_client, monkeypatch, refused):
    _soft_limit_setup(web_client, monkeypatch)
    kicks = _spy_kicks(monkeypatch)
    r = web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 1, **refused})
    assert r.status_code == 400, r.text
    assert kicks == []
    assert _sample_count(web_client) == 8 == _running_total(web_client)


def test_starting_a_clone_read_waits_for_a_held_history_lock(web_client, tmp_path):
    from bacpack.models import Device

    _create_device(web_client)
    with web_client.app.state.session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "running"
        session.commit()
    with _held(history_db_path(_main(tmp_path))):
        r = web_client.post("/api/devices/599001/client/clone", json={"instance": 4001, "timeout_seconds": 5})
    assert r.status_code == 202, r.text


def test_a_double_click_on_start_clone_makes_one_job_and_a_409_not_a_500(web_client):
    from concurrent.futures import ThreadPoolExecutor

    from bacpack.models import ClientRequest, Device

    _create_device(web_client)
    with web_client.app.state.session_factory() as session:
        session.get(Device, 599001).runtime.process_status = "running"
        session.commit()

    def start(_):
        return web_client.post("/api/devices/599001/client/clone", json={"instance": 4001, "timeout_seconds": 5}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = sorted(pool.map(start, range(8)))
    assert codes == [202] + [409] * 7
    with web_client.app.state.session_factory() as session:
        assert session.query(ClientRequest).filter_by(kind="clone-read").count() == 1
