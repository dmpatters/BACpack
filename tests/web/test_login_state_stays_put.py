"""Security state stays on the install where it was set: it is not in a config
export, an import can't change it, and the diagnostics file leaves it out."""
from __future__ import annotations

import json

import pytest

from bacpack.config_connectors import SETTINGS_DEFAULTS, ConfigImportError, parse_settings
from bacpack.models import ApiKey, ServerSettings, WebSession
from tests.web._helpers import LOGIN_PASSWORD, enable_login, session_cookie


def test_settings_defaults_hold_no_login_state():
    assert set(SETTINGS_DEFAULTS) == {"trend_retention_days", "log_retention_days", "trend_soft_limit_mb"}


def test_login_state_is_not_in_a_config_export(web_client):
    enable_login(web_client, "PLANT-owner")
    session_cookie(web_client)
    web_client.post("/api/settings/api-keys", json={"name": "PLANT-keyname"})
    exported = web_client.get("/api/config/export").text.lower()
    for needle in ("login", "plant-owner", "plant-keyname", "api_key", "session", "bpk_"):
        assert needle not in exported, needle


@pytest.mark.parametrize("key", ["login_mode", "login_username", "login_password_hash", "api_keys"])
def test_config_import_cannot_carry_login_settings(key):
    with pytest.raises(ConfigImportError):
        parse_settings({key: "none"})


def test_importing_a_config_leaves_login_sessions_and_keys_alone(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    web_client.post("/api/settings/api-keys", json={"name": "k"})
    r = web_client.post(
        "/api/config/import?dry_run=false",
        files={"file": ("c.json", json.dumps({"version": 1, "devices": []}).encode(), "application/json")},
    )
    assert r.status_code == 200, r.text
    with web_client.app.state.session_factory() as session:
        settings = session.get(ServerSettings, 1)
        assert (settings.login_mode, settings.login_username) == ("login", "admin")
        assert session.query(WebSession).count() == 1
        assert session.query(ApiKey).count() == 1


def test_diagnostics_download_leaves_out_credentials_and_keys(web_client):
    import io
    import zipfile

    enable_login(web_client, "PLANT-owner")
    session_cookie(web_client)
    key = web_client.post("/api/settings/api-keys", json={"name": "k"}).json()["key"]
    r = web_client.get("/api/diagnostics/download?real_values=true")
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        text = "\n".join(archive.read(name).decode() for name in archive.namelist())
    assert key not in text and LOGIN_PASSWORD not in text
    assert "scrypt" not in text and "password_hash" not in text
