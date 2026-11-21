"""The optional DCC/ReinitializeDevice password on a device: the API, and that
it never travels."""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from bacpack import auth, migrations
from bacpack.config_io import apply_device_restore, export_device_backup
from bacpack.db import make_engine
from bacpack.models import Device, DeviceRuntime
from tests.test_config_connectors import _device
from tests.web._helpers import _create_device


def _hash(web_client, device_id=599001):
    with web_client.app.state.session_factory() as session:
        return session.get(Device, device_id).bacnet_password_hash


def test_new_device_has_no_bacnet_password(web_client):
    assert _create_device(web_client)["bacnet_password_set"] is False


def test_put_sets_then_clears_the_bacnet_password(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"bacnet_password": "opensesame"})
    assert r.status_code == 200, r.text
    assert r.json()["bacnet_password_set"] is True
    assert auth.verify_password("opensesame", _hash(web_client))
    stored = _hash(web_client)
    for text in (r.text, web_client.get("/api/devices").text, web_client.get("/api/devices/599001").text):
        assert "opensesame" not in text and stored not in text
    cleared = web_client.put("/api/devices/599001", json={"bacnet_password": None})
    assert cleared.json()["bacnet_password_set"] is False
    assert _hash(web_client) is None


def test_put_accepts_20_characters_and_refuses_21_or_empty(web_client):
    _create_device(web_client)
    assert web_client.put("/api/devices/599001", json={"bacnet_password": "x" * 20}).status_code == 200
    assert web_client.put("/api/devices/599001", json={"bacnet_password": "x" * 21}).status_code == 422
    assert web_client.put("/api/devices/599001", json={"bacnet_password": ""}).status_code == 422


def test_updating_other_fields_keeps_the_password(web_client):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bacnet_password": "pw"})
    before = _hash(web_client)
    assert web_client.put("/api/devices/599001", json={"name": "Renamed"}).json()["bacnet_password_set"] is True
    assert _hash(web_client) == before


def test_setting_the_password_does_not_bump_the_change_counter(web_client):
    _create_device(web_client)

    def seq():
        with web_client.app.state.session_factory() as session:
            return session.get(DeviceRuntime, 599001).change_seq

    before = seq()
    web_client.put("/api/devices/599001", json={"bacnet_password": "pw"})
    assert seq() == before


def test_add_missing_columns_adds_bacnet_password_hash(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE devices (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"))
            connection.execute(sa.text("INSERT INTO devices (id, name) VALUES (1, 'old')"))
        metadata = sa.MetaData()
        sa.Table("devices", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String, nullable=False),
                 sa.Column("bacnet_password_hash", sa.String, nullable=True))
        migrations.add_missing_columns(engine, metadata)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT bacnet_password_hash FROM devices")).scalar_one() is None
    finally:
        engine.dispose()


def test_config_export_leaves_the_password_out(web_client):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bacnet_password": "pw"})
    text = web_client.get("/api/config/export").text
    assert "bacnet_password" not in text and _hash(web_client) not in text


def test_config_import_keeps_an_existing_devices_password(web_client):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bacnet_password": "pw"})
    before = _hash(web_client)
    document = web_client.get("/api/config/export").json()
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(document), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert _hash(web_client) == before


def test_device_backup_and_restore_leave_the_password_alone(session):
    device = _device(session, 599001)
    device.bacnet_password_hash = auth.hash_password("pw")
    session.commit()
    before = device.bacnet_password_hash

    backup = export_device_backup(device)

    assert b"bacnet_password" not in backup and before.encode() not in backup
    apply_device_restore(session, device, backup)
    session.commit()
    session.refresh(device)
    assert device.bacnet_password_hash == before


@pytest.mark.parametrize("field", ["bacnet_password_hash"])
def test_clone_plan_and_sample_have_no_such_field(field):
    from bacpack import clone_plan, sample_building

    for module in (clone_plan, sample_building):
        assert field not in open(module.__file__, encoding="utf-8").read()
