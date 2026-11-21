"""The login middleware (web/auth_middleware.py): gating by mode, session
cookie and API key resolution, and the cross-site check."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from starlette.requests import Request

from bacpack import login_store, timeutil
from bacpack.web import auth_middleware
from bacpack.web.auth_middleware import SESSION_COOKIE
from tests.web._helpers import LOGIN_PASSWORD, enable_login, make_key, session_cookie, set_login_mode


def test_mode_none_lets_everything_through(web_client):
    assert web_client.get("/api/devices").status_code == 200
    assert web_client.get("/openapi.json").status_code == 200


def test_unset_mode_gives_403_setup_required_except_state_setup_and_version(web_client):
    set_login_mode(web_client, None)
    for path in ("/api/devices", "/api/settings/network", "/openapi.json"):
        r = web_client.get(path)
        assert (r.status_code, r.json()) == (403, {"detail": "setup_required"}), path
    assert web_client.get("/api/version").status_code == 200
    assert web_client.get("/api/auth/state").status_code != 403
    assert web_client.post("/api/auth/setup", json={}).status_code != 403
    assert web_client.get("/").status_code in (200, 503)


def test_login_mode_gives_401_without_a_session_for_api_and_openapi(web_client):
    enable_login(web_client)
    for path in ("/api/devices", "/openapi.json", "/api/settings/login"):
        assert web_client.get(path).status_code == 401, path


def test_login_mode_leaves_state_login_version_and_the_spa_shell_open(web_client):
    enable_login(web_client)
    assert web_client.get("/api/version").status_code == 200
    assert web_client.get("/api/auth/state").status_code != 401
    assert web_client.post("/api/auth/login", json={}).status_code != 401
    assert web_client.get("/some/page").status_code in (200, 503)
    assert web_client.get("/api/does-not-exist").status_code == 401


def test_valid_session_cookie_is_accepted(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    assert web_client.get("/api/devices").status_code == 200


def test_expired_session_is_401(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    with web_client.app.state.session_factory() as session:
        old = timeutil.db_utc_now() - dt.timedelta(hours=9)
        session.execute(text("UPDATE web_sessions SET last_seen = :t"), {"t": old})
        session.commit()
    assert web_client.get("/api/devices").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer nonsense"},
        {"Authorization": "Bearer bpk_" + "x" * 5000},
        {"Authorization": "Basic YWRtaW46cHc="},
    ],
)
def test_malformed_bearer_is_401_not_500(web_client, headers):
    enable_login(web_client)
    assert web_client.get("/api/devices", headers=headers).status_code == 401


@pytest.mark.parametrize("cookie", ["", "x" * 5000, "not-a-token"])
def test_malformed_cookie_is_401_not_500(web_client, cookie):
    enable_login(web_client)
    web_client.cookies.set(SESSION_COOKIE, cookie)
    assert web_client.get("/api/devices").status_code == 401


def test_valid_key_is_accepted_and_ignored_in_mode_none(web_client):
    enable_login(web_client)
    key = make_key(web_client)
    assert web_client.get("/api/devices", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    set_login_mode(web_client, "none")
    assert web_client.get("/api/devices", headers={"Authorization": "Bearer junk"}).status_code == 200


def test_revoked_key_is_401(web_client):
    enable_login(web_client)
    key = make_key(web_client)
    with web_client.app.state.session_factory() as session:
        session.execute(text("UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP"))
        session.commit()
    assert web_client.get("/api/devices", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_bearer_wins_over_a_good_cookie(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    assert web_client.get("/api/devices", headers={"Authorization": "Bearer bpk_bad"}).status_code == 401


def test_cookie_post_without_origin_or_referer_is_refused(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    del web_client.headers["Origin"]
    r = web_client.post("/api/devices", json={"id": 1, "name": "A"})
    assert r.status_code == 403


def test_cookie_post_with_matching_origin_or_referer_is_allowed(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    r = web_client.post("/api/devices", json={"id": 1, "name": "A"})
    assert r.status_code == 201
    del web_client.headers["Origin"]
    r = web_client.post(
        "/api/devices", json={"id": 2, "name": "B"}, headers={"Referer": "http://testserver/devices"}
    )
    assert r.status_code == 201


def test_cookie_post_with_foreign_origin_is_403(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    r = web_client.post(
        "/api/devices", json={"id": 1, "name": "A"}, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403


def test_bearer_post_needs_no_origin(web_client):
    enable_login(web_client)
    key = make_key(web_client)
    r = web_client.post(
        "/api/devices", json={"id": 1, "name": "A"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 201


def test_x_forwarded_host_no_longer_allows_a_foreign_origin(web_client):
    r = web_client.post(
        "/api/devices",
        json={"id": 1, "name": "A"},
        headers={"Origin": "https://bacpack.example.com", "X-Forwarded-Host": "bacpack.example.com"},
    )
    assert r.status_code == 403


def _request(scheme):
    return Request({"type": "http", "scheme": scheme, "method": "GET", "path": "/", "headers": [], "server": ("h", 1)})


def test_secure_flag_only_on_https_requests():
    assert auth_middleware.cookie_kwargs(_request("https"))["secure"] is True
    kwargs = auth_middleware.cookie_kwargs(_request("http"))
    assert kwargs["secure"] is False
    assert kwargs["httponly"] is True and kwargs["samesite"] == "strict" and kwargs["path"] == "/"


def test_epoch_change_clears_the_lockout(web_client):
    web_client.get("/api/version")
    lockout = web_client.app.state.login_lockout
    for _ in range(5):
        lockout.record_failure("9.9.9.9")
    assert lockout.check("9.9.9.9") is not None
    enable_login(web_client)
    web_client.get("/api/version")
    assert lockout.check("9.9.9.9") is None


def test_last_seen_is_written_at_most_once_a_minute(web_client, monkeypatch):
    enable_login(web_client)
    session_cookie(web_client)
    writes = []
    real = login_store.touch_session
    monkeypatch.setattr(login_store, "touch_session", lambda *a: writes.append(a) or real(*a))
    web_client.get("/api/devices")
    web_client.get("/api/devices")
    web_client.get("/api/devices")
    assert len(writes) == 1


def test_cookie_is_secure_over_plain_http_when_the_launcher_marked_https(web_client, monkeypatch):
    monkeypatch.setenv("BACPACK_HTTPS", "1")
    enable_login(web_client)
    r = web_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": LOGIN_PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()
