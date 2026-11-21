"""/api/settings/login and /api/settings/api-keys."""
from __future__ import annotations

from tests.web._helpers import LOGIN_PASSWORD, enable_login, session_cookie

NEW_PASSWORD = "a different long password"


def _signed_in(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    return web_client


def test_get_login_settings(web_client):
    _signed_in(web_client)
    body = web_client.get("/api/settings/login").json()
    assert body == {"mode": "login", "username": "admin"}


def test_password_change_needs_the_current_password(web_client):
    _signed_in(web_client)
    r = web_client.put("/api/settings/login", json={"new_password": NEW_PASSWORD, "current_password": "nope nope nope"})
    assert r.status_code == 401
    r = web_client.put("/api/settings/login", json={"new_password": NEW_PASSWORD})
    assert r.status_code == 401
    r = web_client.put(
        "/api/settings/login", json={"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD}
    )
    assert r.status_code == 200, r.text


def test_password_change_ends_other_sessions_but_keeps_the_callers_new_one(web_client):
    _signed_in(web_client)
    other_token = web_client.cookies.get("bacpack_session")
    web_client.cookies.clear()
    from tests.web._helpers import sign_in

    sign_in(web_client)
    r = web_client.put(
        "/api/settings/login", json={"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD}
    )
    assert r.status_code == 200
    assert web_client.get("/api/devices").status_code == 200
    web_client.cookies.set("bacpack_session", other_token)
    assert web_client.get("/api/devices").status_code == 401


def test_the_new_password_works_and_the_old_one_does_not(web_client):
    _signed_in(web_client)
    web_client.put("/api/settings/login", json={"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD})
    web_client.cookies.clear()
    assert web_client.post("/api/auth/login", json={"username": "admin", "password": LOGIN_PASSWORD}).status_code == 401
    assert web_client.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD}).status_code == 200


def test_username_change_alone_needs_no_current_password(web_client):
    _signed_in(web_client)
    r = web_client.put("/api/settings/login", json={"username": "owner"})
    assert r.status_code == 200
    assert web_client.get("/api/settings/login").json()["username"] == "owner"
    assert web_client.get("/api/devices").status_code == 200


def test_new_password_follows_the_rules(web_client):
    _signed_in(web_client)
    r = web_client.put("/api/settings/login", json={"new_password": "short", "current_password": LOGIN_PASSWORD})
    assert r.status_code == 400
    r = web_client.put("/api/settings/login", json={"username": "  "})
    assert r.status_code == 400


def test_disable_needs_the_password_and_ends_sessions(web_client):
    _signed_in(web_client)
    assert web_client.post("/api/settings/login/disable", json={"current_password": "wrong wrong wrong"}).status_code == 401
    r = web_client.post("/api/settings/login/disable", json={"current_password": LOGIN_PASSWORD})
    assert r.status_code == 200
    assert web_client.get("/api/auth/state").json()["mode"] == "none"
    assert web_client.get("/api/devices").status_code == 200
    with web_client.app.state.session_factory() as session:
        from bacpack.models import WebSession

        assert session.query(WebSession).count() == 0


def test_enable_from_none_creates_login_and_signs_in_and_409_when_already_login(web_client):
    web_client.headers["Origin"] = "http://testserver"
    r = web_client.post("/api/settings/login/enable", json={"username": "boss", "password": LOGIN_PASSWORD})
    assert r.status_code == 200, r.text
    assert web_client.get("/api/auth/state").json() == {"mode": "login", "signed_in": True, "username": "boss"}
    r = web_client.post("/api/settings/login/enable", json={"username": "x", "password": NEW_PASSWORD})
    assert r.status_code == 409


def test_enable_applies_the_rules(web_client):
    r = web_client.post("/api/settings/login/enable", json={"username": "boss", "password": "short"})
    assert r.status_code == 400
    assert web_client.get("/api/auth/state").json()["mode"] == "none"


def test_api_key_create_list_revoke_roundtrip(web_client):
    _signed_in(web_client)
    r = web_client.post("/api/settings/api-keys", json={"name": "grafana"})
    assert r.status_code == 201
    created = r.json()
    assert created["key"].startswith("bpk_") and created["prefix"] == created["key"][:12]
    listed = web_client.get("/api/settings/api-keys").json()
    assert [k["name"] for k in listed] == ["grafana"]
    assert "key" not in listed[0] and listed[0]["last_used"] is None

    web_client.cookies.clear()
    headers = {"Authorization": f"Bearer {created['key']}"}
    assert web_client.get("/api/devices", headers=headers).status_code == 200
    assert web_client.delete(f"/api/settings/api-keys/{created['id']}", headers=headers).status_code == 204
    assert web_client.get("/api/devices", headers=headers).status_code == 401


def test_api_key_duplicate_name_is_400(web_client):
    _signed_in(web_client)
    assert web_client.post("/api/settings/api-keys", json={"name": "a"}).status_code == 201
    assert web_client.post("/api/settings/api-keys", json={"name": "a"}).status_code == 400
    assert web_client.post("/api/settings/api-keys", json={"name": " "}).status_code == 400
    assert web_client.post("/api/settings/api-keys", json={"name": "x" * 65}).status_code == 400


def test_revoking_an_unknown_key_is_404(web_client):
    _signed_in(web_client)
    assert web_client.delete("/api/settings/api-keys/999").status_code == 404


def test_a_key_can_reach_settings_routes(web_client):
    _signed_in(web_client)
    key = web_client.post("/api/settings/api-keys", json={"name": "a"}).json()["key"]
    web_client.cookies.clear()
    headers = {"Authorization": f"Bearer {key}"}
    assert web_client.get("/api/settings/login", headers=headers).status_code == 200
    assert web_client.post("/api/settings/api-keys", json={"name": "b"}, headers=headers).status_code == 201


def test_key_events_are_logged_without_the_key(web_client):
    from bacpack import history_writer
    from bacpack.models import LogEntry

    _signed_in(web_client)
    created = web_client.post("/api/settings/api-keys", json={"name": "grafana"}).json()
    web_client.delete(f"/api/settings/api-keys/{created['id']}")
    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        messages = [e.message for e in session.query(LogEntry)]
    assert any("created" in m for m in messages) and any("revoked" in m for m in messages)
    assert not any(created["key"] in m or "grafana" in m for m in messages)
