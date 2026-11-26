"""Every saved-by-a-release set under tests/upgrade_fixtures/ opens, migrates and
imports with this code."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from packaging.version import Version
from sqlalchemy import select
from starlette.testclient import TestClient

import sys

from bacpack import config_io, lifecycle, migrations, serve
from bacpack.config_io import CONFIG_VERSION
from bacpack.db import init_db, lock_both_files, make_engine, make_session_factory
from bacpack.auth import verify_password
from bacpack.models import BacnetObject, Connector, Device, LogEntry, TrafficLogEntry, TrendSample
from bacpack.object_builder import build_application
from bacpack.object_config import check_device, row_from_object
from bacpack.secrets_store import Unreadable
from bacpack.web.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from make_upgrade_fixtures import schema_fingerprint  # noqa: E402

SETS_DIR = Path(__file__).resolve().parent / "upgrade_fixtures"


def fixture_sets() -> list[Path]:
    return sorted((p for p in SETS_DIR.iterdir() if p.is_dir()), key=lambda p: Version(p.name))


def _manifest(set_dir: Path) -> dict:
    return json.loads((set_dir / "manifest.json").read_text(encoding="utf-8"))


SETS = fixture_sets()
by_set = pytest.mark.parametrize("set_dir", SETS, ids=[p.name for p in SETS])


class Migrated:
    def __init__(self, set_dir: Path, data: Path):
        self.set_dir = set_dir
        self.data = data
        self.db_path = data / "bacpack.db"
        self.manifest = _manifest(set_dir)
        engine = make_engine(self.db_path)
        init_db(engine)
        self.engine = engine
        self.session_factory = make_session_factory(engine)

    def file(self, name: str) -> bytes:
        return (self.set_dir / name).read_bytes()


def _copy_set(set_dir: Path, tmp_path: Path) -> Path:
    # A missing key would only show up later as unreadable credentials.
    assert (set_dir / "secret.key").exists(), f"{set_dir.name} has no secret.key"
    data = tmp_path / "data"
    shutil.copytree(set_dir, data)
    return data


@pytest.fixture()
def migrated(set_dir, tmp_path):
    result = Migrated(set_dir, _copy_set(set_dir, tmp_path))
    yield result
    result.engine.dispose()


def _client(migrated: Migrated):
    from tests.fakes import FakeDeviceProcess

    def factory(db_path, device_id):
        return FakeDeviceProcess(make_session_factory(make_engine(db_path)), device_id)

    app = create_app(db_path=str(migrated.db_path), process_factory=factory)
    return TestClient(app, base_url="http://127.0.0.1", headers={"Origin": "http://127.0.0.1"})


@by_set
def test_the_set_holds_every_file_it_lists(set_dir):
    # A .db ignored by git would leave a clone with an empty database that
    # every later test passes on vacuously.
    manifest = _manifest(set_dir)
    for name in manifest["sha256"]:
        assert (set_dir / name).is_file(), f"{set_dir.name}/{name} is listed in the manifest but missing"
    assert (set_dir / "bacpack.db").stat().st_size > 0
    assert (set_dir / "bacpack-history.db").stat().st_size > 0


@by_set
async def test_each_device_passes_check_device_and_builds(migrated):
    with migrated.session_factory() as session:
        devices = list(session.scalars(select(Device)))
        assert sorted(d.id for d in devices) == sorted(d["id"] for d in migrated.manifest["devices"])
        for device in devices:
            rows = [{**row_from_object(o), "trended": bool(o.trended)} for o in device.objects]
            assert check_device(device.name, rows) is None
            app = build_application(device, list(device.objects), trend_session_factory=migrated.session_factory)
            assert app.device_object is not None


@by_set
def test_network_settings_and_counts_match_the_manifest(migrated):
    manifest = migrated.manifest
    with migrated.session_factory() as session:
        for expected in manifest["devices"]:
            device = session.get(Device, expected["id"])
            assert device is not None
            actual = {
                "id": device.id,
                "name": device.name,
                "bind_address": device.bind_address,
                "network_role": device.network_role,
                "foreign_bbmd": device.foreign_bbmd,
                "foreign_ttl": device.foreign_ttl,
                "bbmd_peers": device.bbmd_peers,
                "bbmd_links": sorted(link.linked_device_id for link in device.bbmd_links),
                "object_count": len(device.objects),
            }
            assert actual == expected
        counts: dict = {}
        for obj in session.scalars(select(BacnetObject)):
            counts[obj.object_type] = counts.get(obj.object_type, 0) + 1
        assert counts == manifest["object_types"]
        connectors = [
            {"name": c.name, "type": c.type} for c in session.scalars(select(Connector).order_by(Connector.id))
        ]
        assert connectors == manifest["connectors"]


@by_set
def test_connector_credentials_decrypt_with_the_sets_key(migrated):
    with migrated.session_factory() as session:
        for name, expected in migrated.manifest["connector_secrets"].items():
            connector = session.scalars(select(Connector).where(Connector.name == name)).one()
            for field, value in expected.items():
                stored = getattr(connector, field)
                assert not isinstance(stored, Unreadable)
                assert stored == value
        for connector in session.scalars(select(Connector)):
            assert not isinstance(connector.auth_password, Unreadable)
            assert not isinstance(connector.auth_token, Unreadable)
            assert not any(isinstance(v, Unreadable) for v in (connector.headers or {}).values())


@by_set
def test_trend_samples_read_back_in_order(migrated):
    with migrated.session_factory() as session:
        for log in migrated.manifest["trend_logs"]:
            rows = session.scalars(
                select(TrendSample).where(TrendSample.object_id == log["object_id"]).order_by(TrendSample.id)
            ).all()
            assert len(rows) == log["samples"]
            stamps = [r.timestamp for r in rows]
            assert stamps == sorted(stamps)
            assert [r.sequence for r in rows] == sorted(r.sequence for r in rows)


@by_set
def test_traffic_and_log_rows_and_device_passwords_survive(migrated):
    expected = migrated.manifest["history_rows"]
    with migrated.session_factory() as session:
        assert session.query(TrafficLogEntry).count() == expected["traffic"]
        assert session.query(LogEntry).filter_by(logger_name="bacpack.fixture").count() == expected["logs"]
        for device_id, password in migrated.manifest["bacnet_passwords"].items():
            stored = session.get(Device, int(device_id)).bacnet_password_hash
            assert stored is not None and verify_password(password, stored)
            assert not verify_password(password + "x", stored)


@by_set
def test_logins_and_write_api_work_on_the_migrated_copy(migrated):
    manifest = migrated.manifest
    target = manifest["feed_object"]
    with _client(migrated) as client:
        assert client.post("/api/auth/login", json=manifest["login"]).status_code == 200
        client.cookies.clear()
        assert client.get("/api/devices").status_code in (401, 403)
        client.headers["Authorization"] = f"Bearer {manifest['api_key']}"
        assert client.get("/api/devices").status_code == 200
        with client.app.state.session_factory() as session:
            lock_both_files(session)
            session.get(Device, target["device_id"]).runtime.process_status = "running"
            session.commit()
        path = f"{target['device_id']}/{target['object_type']}/{target['instance_number']}"
        for route in ("feed", "manual"):
            r = client.post(f"/api/{route}/{path}", json={"value": 70})
            assert r.status_code == 200, r.text
            assert r.json()["queued"] is True


def _import_config(client, content: bytes):
    files = {"file": ("config.json", content, "application/json")}
    dry = client.post("/api/config/import?dry_run=true", files=files)
    assert dry.status_code == 200, dry.text
    applied = client.post(f"/api/config/import?dry_run=false&plan_digest={dry.json()['plan_digest']}", files=files)
    assert applied.status_code == 200, applied.text
    return applied.json()


@by_set
def test_config_export_imports_into_a_fresh_database(set_dir, web_client):
    _import_config(web_client, (set_dir / "config-export.json").read_bytes())
    ids = {d["id"] for d in web_client.get("/api/devices").json()}
    assert ids == {d["id"] for d in _manifest(set_dir)["devices"]}


@by_set
def test_config_export_imports_onto_the_migrated_copy(migrated):
    info = migrated.manifest["backup_device"]
    with _client(migrated) as client:
        client.headers["Authorization"] = f"Bearer {migrated.manifest['api_key']}"
        objects = client.get(f"/api/devices/{info['id']}/objects").json()
        victim = next(o for o in objects if o["name"] == "Damper")  # nothing refers to it
        assert client.delete(f"/api/devices/{info['id']}/objects/{victim['id']}").status_code == 204
        result = _import_config(client, migrated.file("config-export.json"))
        assert result["dry_run"] is False
        names = {o["name"] for o in client.get(f"/api/devices/{info['id']}/objects").json()}
        assert victim["name"] in names and len(names) == len(objects)


@by_set
def test_backup_restores_onto_its_own_device(migrated):
    info = migrated.manifest["backup_device"]
    with migrated.session_factory() as session:
        lock_both_files(session)
        device = session.get(Device, info["id"])
        before = {o.name for o in device.objects}
        by_name = {o.name: o for o in device.objects}
        victim, renamed = by_name["Damper"], by_name["Mode"]
        removed_name, kept_name = victim.name, renamed.name
        session.delete(victim)
        renamed.name = "Renamed by a user"
        session.flush()
        session.expire(device)
        assert removed_name not in {o.name for o in session.get(Device, info["id"]).objects}
        config_io.apply_device_restore(session, device, migrated.file(info["file"]))
        session.commit()
        session.expire_all()
        assert {o.name for o in session.get(Device, info["id"]).objects} == before
        assert removed_name in before and kept_name in before


@by_set
def test_points_csv_imports(migrated):
    info = migrated.manifest["backup_device"]
    csv = migrated.file("points.csv")
    files = {"file": ("points.csv", csv, "text/csv")}
    with _client(migrated) as client:
        client.headers["Authorization"] = f"Bearer {migrated.manifest['api_key']}"
        before = len(client.get(f"/api/devices/{info['id']}/objects").json())
        r = client.post(f"/api/devices/{info['id']}/objects/import?mode=sync", files=files)
        assert r.status_code == 200, r.text
        assert len(client.get(f"/api/devices/{info['id']}/objects").json()) == before
        assert client.post("/api/devices", json={"id": 599900, "name": "Empty"}).status_code == 201
        r = client.post("/api/devices/599900/objects/import?mode=merge", files=files)
        assert r.status_code == 200, r.text
        assert len(client.get("/api/devices/599900/objects").json()) == before


@by_set
def test_frozen_sets_match_their_hashes(set_dir):
    manifest = _manifest(set_dir)
    if manifest["provisional"]:
        pytest.skip("a provisional set is regenerated, not hashed")
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((set_dir / name).read_bytes()).hexdigest() == digest, name


def _contents(data: Path) -> dict:
    """What the files hold, not their journal-mode header byte, which opening
    a file through make_engine switches."""
    result = {}
    for path in data.iterdir():
        if path.suffix == ".db":
            with closing(sqlite3.connect(path)) as connection:
                result[path.name] = (list(connection.iterdump()), connection.execute("PRAGMA user_version").fetchone())
        else:
            result[path.name] = path.read_bytes()
    return result


@by_set
@pytest.mark.parametrize("file_name,schema", [("bacpack.db", "main"), ("bacpack-history.db", "history")])
def test_a_set_one_version_newer_than_this_code_knows_is_refused(set_dir, tmp_path, file_name, schema):
    data = _copy_set(set_dir, tmp_path)
    with closing(sqlite3.connect(data / file_name)) as connection:
        connection.execute(f"PRAGMA user_version = {len(migrations.migrations_for(schema)) + 1}")
        connection.commit()
    before = _contents(data)
    engine = make_engine(data / "bacpack.db")
    with pytest.raises(migrations.DatabaseTooNew) as raised:
        init_db(engine)
    engine.dispose()
    assert raised.value.schema == schema
    assert _contents(data) == before
    calls = []
    code = serve.main(
        ["serve"], run=lambda *a, **k: calls.append(1), environ={"BACPACK_DB_PATH": str(data / "bacpack.db")}
    )
    assert code == lifecycle.DATABASE_TOO_NEW_EXIT_CODE == 81
    assert calls == []


def test_newest_set_records_the_current_formats():
    newest = SETS[-1]
    manifest = _manifest(newest)
    stale = (
        manifest["main_schema_version"] < len(migrations.MIGRATIONS)
        or manifest["history_schema_version"] < len(migrations.HISTORY_MIGRATIONS)
        or manifest["config_version"] < CONFIG_VERSION
    )
    assert not stale and manifest["schema_fingerprint"] == schema_fingerprint(), (
        f"the database schema, its versions or CONFIG_VERSION changed after the newest fixture set ({newest.name}): run scripts/make_upgrade_fixtures.py "
        "into tests/upgrade_fixtures/<new release> (a provisional set is regenerated in place with --replace; "
        "a frozen one gets a new directory). A SQLAlchemy upgrade that changes compiled column types, or a changed "
        "Python-side default, trips this too: regenerate in place while the set is provisional, else freeze a new set."
    )


def provisional_sets_are_newest(flags: list[bool]) -> bool:
    """`flags` are the sets' provisional flags, oldest first."""
    return flags == sorted(flags)


@pytest.mark.parametrize("flags,ok", [([False, True], True), ([False, False], True), ([True], True), ([True, False], False)])
def test_provisional_ordering_rule(flags, ok):
    assert provisional_sets_are_newest(flags) is ok


def test_no_frozen_set_sorts_after_a_provisional_one():
    assert provisional_sets_are_newest([_manifest(p)["provisional"] for p in SETS]), (
        "a provisional set must be the newest"
    )
