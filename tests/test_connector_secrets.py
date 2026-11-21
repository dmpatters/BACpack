"""Connector credentials at rest: the wording and the
API when a saved value can't be read, a key that can't be used, and what never
leaves the install."""
from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select

from bacpack import config_connectors, secrets_store
from bacpack.models import Connector
from tests.test_connectors import _counting_poller, _make_connector
from tests.web._helpers import _create_connector


def _lose_the_key(data_dir):
    (data_dir / secrets_store.KEY_FILE).unlink()
    secrets_store._fernets.clear()
    secrets_store.ensure_key(data_dir)


def _break_the_key(data_dir):
    (data_dir / secrets_store.KEY_FILE).write_bytes(b"junk")
    secrets_store._fernets.clear()


@pytest.mark.asyncio
async def test_poller_reports_unreadable_password_as_missing_and_other_connectors_still_poll(session_factory, tmp_path):
    first, _, _ = _make_connector(session_factory, auth_type="basic", auth_username="u", auth_password="pw")
    with session_factory() as session:
        other = Connector(name="Other", url="http://example.invalid/other")
        session.add(other)
        session.commit()
        other_id = other.id
    _lose_the_key(tmp_path)
    poller, client, requests = _counting_poller(session_factory)

    await poller._poll_one(first)
    assert requests == []
    assert poller.status[first].last_success is False
    assert poller.status[first].last_error == (
        "Missing credentials: password (the saved password can't be read; enter it again)"
    )
    await poller._poll_one(other_id)
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_of_an_unreadable_connector_says_enter_it_again(session_factory, tmp_path):
    connector_id, _, _ = _make_connector(session_factory, auth_type="bearer", auth_token="tk")
    _lose_the_key(tmp_path)
    poller, client, requests = _counting_poller(session_factory)

    result = await poller.preview(connector_id)

    assert result["ok"] is False and "can't be read" in result["error"] and "enter it again" in result["error"]
    assert requests == []
    await client.aclose()


def test_api_lists_unreadable_credentials_and_never_the_secret(web_client):
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                                headers={"X-Api-Key": "hdr-secret"})
    assert created["unreadable_credentials"] == []
    _lose_the_key(web_client.app.state.data_dir)

    body = web_client.get(f"/api/connectors/{created['id']}")

    assert body.json()["unreadable_credentials"] == ["password", "header X-Api-Key"]
    assert body.json()["missing_credentials"] == ["password", "header X-Api-Key"]
    for text in (body.text, web_client.get("/api/connectors").text):
        assert "s3cret-pw" not in text and "hdr-secret" not in text and "enc:v1" not in text


def test_saving_a_new_password_over_an_unreadable_one_clears_the_state(web_client):
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="old")
    _lose_the_key(web_client.app.state.data_dir)

    r = web_client.put(f"/api/connectors/{created['id']}", json={"auth_password": "new"})

    assert r.status_code == 200, r.text
    assert r.json()["unreadable_credentials"] == [] and r.json()["missing_credentials"] == []


def test_import_over_an_unreadable_connector_still_needs_credentials(session_factory, tmp_path):
    with session_factory() as session:
        session.add(Connector(name="C", url="http://x", auth_type="basic", auth_password="pw"))
        session.commit()
    _lose_the_key(tmp_path)
    with session_factory() as session:
        existing = session.scalar(select(Connector))
        missing = config_connectors._missing_credentials({"type": "http-poll", "auth_type": "basic", "headers": {}}, existing)
    assert missing == ["password"]


def test_saving_a_credential_with_an_unusable_key_is_refused_with_409(web_client, caplog):
    data_dir = web_client.app.state.data_dir
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="old-pw-1")
    _break_the_key(data_dir)

    with caplog.at_level(logging.ERROR, logger="bacpack.security"):
        updated = web_client.put(f"/api/connectors/{created['id']}", json={"auth_password": "new-pw-2"})
        made = web_client.post("/api/connectors", json={"name": "N", "url": "http://x", "auth_type": "bearer", "auth_token": "tok-3"})

    for r in (updated, made):
        assert r.status_code == 409, r.text
        assert "secret.key" in r.json()["detail"] and "delete" in r.json()["detail"]
        assert "new-pw-2" not in r.text and "tok-3" not in r.text
    assert "secret.key" in caplog.text and '"' in caplog.text  # named through log_names.quote
    assert "old-pw-1" not in caplog.text and "new-pw-2" not in caplog.text
    # Nothing that needs no encryption is blocked, and nothing was half saved.
    assert web_client.put(f"/api/connectors/{created['id']}", json={"enabled": False}).status_code == 200
    assert web_client.get("/api/connectors").json()[0]["id"] == created["id"]
    assert len(web_client.get("/api/connectors").json()) == 1


def test_config_export_has_no_connector_secrets_or_ciphertext(web_client):
    _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                      headers={"X-Api-Key": "hdr-secret"})
    r = web_client.get("/api/config/export")
    assert r.status_code == 200
    assert "s3cret-pw" not in r.text and "hdr-secret" not in r.text and "enc:v1" not in r.text
    assert "connectors" in json.loads(r.text)


def test_no_log_line_holds_a_secret(web_client, caplog):
    with caplog.at_level(logging.DEBUG):
        created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                                    headers={"X-Api-Key": "hdr-secret"})
        web_client.put(f"/api/connectors/{created['id']}", json={"auth_password": "s3cret-pw-2"})
    assert "s3cret-pw" not in caplog.text and "hdr-secret" not in caplog.text


def test_config_import_keeps_the_stored_secrets(web_client, session_factory):
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                                headers={"X-Api-Key": "hdr-secret"})
    document = web_client.get("/api/config/export").json()
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(document), "application/json")},
    )
    assert r.status_code == 200, r.text
    with web_client.app.state.session_factory() as session:
        connector = session.get(Connector, created["id"])
        assert connector.auth_password == "s3cret-pw"
        assert connector.headers == {"X-Api-Key": "hdr-secret"}


def test_diagnostics_zip_holds_no_secret_or_key(web_client):
    import io
    import zipfile

    _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                      auth_token="tok-secret", headers={"X-Api-Key": "hdr-secret"})
    assert (web_client.app.state.data_dir / secrets_store.KEY_FILE).exists()
    response = web_client.get("/api/diagnostics/download?real_values=true")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            assert "secret.key" not in name and "key.pem" not in name
            body = archive.read(name)
            for secret in (b"s3cret-pw", b"tok-secret", b"hdr-secret", b"enc:v1", b"PRIVATE KEY"):
                assert secret not in body


def test_device_backup_has_no_connector_secrets(session):
    from bacpack.config_io import export_device_backup
    from tests.test_config_connectors import _bacnet_connector, _device

    device = _device(session, 599001)
    connector = _bacnet_connector(session, device, name="Room")
    connector.auth_password = "s3cret-pw"
    connector.headers = {"X-Api-Key": "hdr-secret"}
    session.commit()
    backup = export_device_backup(device)
    assert b"s3cret-pw" not in backup and b"hdr-secret" not in backup and b"enc:v1" not in backup


def _save_key(data_dir):
    return (data_dir / secrets_store.KEY_FILE).read_bytes()


def _restore_key(data_dir, key):
    (data_dir / secrets_store.KEY_FILE).write_bytes(key)
    secrets_store._fernets.clear()


def _stored_headers(web_client, connector_id):
    with web_client.app.state.session_factory() as session:
        return session.get(Connector, connector_id).headers


def test_a_save_while_the_key_is_replaced_keeps_the_old_ciphertext(web_client):
    """A save that rewrites the headers must hand back what it could not read,
    or putting the good key back would bring nothing back."""
    data_dir = web_client.app.state.data_dir
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                                headers={"X-Api-Key": "hdr-secret"})
    good = _save_key(data_dir)
    _lose_the_key(data_dir)

    r = web_client.put(
        f"/api/connectors/{created['id']}",
        json={"name": "Renamed", "headers": {"X-Api-Key": None, "X-New": "n"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["unreadable_credentials"] == ["password", "header X-Api-Key"]

    _restore_key(data_dir, good)
    with web_client.app.state.session_factory() as session:
        connector = session.get(Connector, created["id"])
        assert connector.name == "Renamed"
        assert connector.headers["X-Api-Key"] == "hdr-secret"
        assert connector.auth_password == "s3cret-pw"


def test_a_config_import_while_the_key_is_replaced_keeps_the_old_ciphertext(web_client):
    data_dir = web_client.app.state.data_dir
    created = _create_connector(web_client, auth_type="basic", auth_username="u", auth_password="s3cret-pw",
                                headers={"X-Api-Key": "hdr-secret"})
    document = web_client.get("/api/config/export").json()
    document["connectors"][0]["headers"]["X-New"] = None
    good = _save_key(data_dir)
    _lose_the_key(data_dir)

    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("config.json", json.dumps(document), "application/json")},
    )
    assert r.status_code == 200, r.text

    _restore_key(data_dir, good)
    assert _stored_headers(web_client, created["id"]) == {"X-Api-Key": "hdr-secret", "X-New": ""}
