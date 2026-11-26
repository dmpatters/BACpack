"""/api/settings/login and /api/settings/api-keys."""
from __future__ import annotations

from tests.web._helpers import LOGIN_PASSWORD, enable_login, session_cookie

NEW_PASSWORD = "a different long password"


def _make_key(web_client, name="a", **kwargs):
    return web_client.post(
        "/api/settings/api-keys", json={"name": name, "current_password": LOGIN_PASSWORD}, **kwargs
    )


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
    web_client.headers["Origin"] = "http://127.0.0.1"
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
    r = _make_key(web_client, "grafana")
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
    assert _make_key(web_client, "a").status_code == 201
    assert _make_key(web_client, "a").status_code == 400
    assert _make_key(web_client, " ").status_code == 400
    assert _make_key(web_client, "x" * 65).status_code == 400


def test_revoking_an_unknown_key_is_404(web_client):
    _signed_in(web_client)
    assert web_client.delete("/api/settings/api-keys/999").status_code == 404


def test_a_key_can_reach_settings_routes_but_needs_the_password_to_mint_a_key(web_client):
    _signed_in(web_client)
    key = _make_key(web_client).json()["key"]
    web_client.cookies.clear()
    headers = {"Authorization": f"Bearer {key}"}
    assert web_client.get("/api/settings/login", headers=headers).status_code == 200
    assert web_client.post("/api/settings/api-keys", json={"name": "b"}, headers=headers).status_code == 401
    assert _make_key(web_client, "b", headers=headers).status_code == 201


def test_creating_a_key_needs_the_current_password(web_client):
    _signed_in(web_client)
    assert web_client.post("/api/settings/api-keys", json={"name": "a"}).status_code == 401
    wrong = {"name": "a", "current_password": "nope nope nope"}
    assert web_client.post("/api/settings/api-keys", json=wrong).status_code == 401
    assert web_client.get("/api/settings/api-keys").json() == []
    assert _make_key(web_client).status_code == 201


def test_a_login_change_between_the_password_check_and_the_save_refuses_the_key(web_client, monkeypatch):
    from bacpack import login_store
    from bacpack.web.api import security_settings

    _signed_in(web_client)
    real = security_settings._require_current_password

    async def check_then_change(request, password, action):
        await real(request, password, action)
        with request.app.state.session_factory() as session:
            stored = login_store.password_hash_of(session)
            login_store.set_login(session, "admin", stored)
            session.commit()

    monkeypatch.setattr(security_settings, "_require_current_password", check_then_change)
    r = _make_key(web_client)
    assert r.status_code == 409
    assert "try again" in r.json()["detail"]
    web_client.cookies.clear()
    session_cookie(web_client)
    assert web_client.get("/api/settings/api-keys").json() == []


def test_a_password_change_that_revokes_leaves_earlier_revokes_alone(web_client):
    from bacpack import history_writer
    from bacpack.models import ApiKey, LogEntry

    _signed_in(web_client)
    first = _make_key(web_client, "first").json()
    _make_key(web_client, "second")
    assert web_client.delete(f"/api/settings/api-keys/{first['id']}").status_code == 204
    with web_client.app.state.session_factory() as session:
        before = session.get(ApiKey, first["id"]).revoked_at
    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        logged = sum("revoked" in e.message for e in session.query(LogEntry))
    body = {"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD, "revoke_api_keys": True}
    assert web_client.put("/api/settings/login", json=body).status_code == 200
    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        assert session.get(ApiKey, first["id"]).revoked_at == before
        assert sum("revoked" in e.message for e in session.query(LogEntry)) == logged + 1


def test_creating_a_key_needs_no_password_without_a_login(web_client):
    r = web_client.post("/api/settings/api-keys", json={"name": "a"})
    assert r.status_code == 201, r.text


def _key_header(web_client):
    key = _make_key(web_client).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def test_password_change_can_revoke_every_key(web_client):
    from bacpack import history_writer
    from bacpack.models import LogEntry

    _signed_in(web_client)
    headers = _key_header(web_client)
    _make_key(web_client, "second")
    body = {"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD, "revoke_api_keys": True}
    assert web_client.put("/api/settings/login", json=body).status_code == 200
    assert web_client.get("/api/settings/api-keys").json() == []
    web_client.cookies.clear()
    assert web_client.get("/api/devices", headers=headers).status_code == 401
    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        revoked = [e.message for e in session.query(LogEntry) if "revoked" in e.message]
    assert len(revoked) == 2


def test_password_change_keeps_keys_unless_asked(web_client):
    _signed_in(web_client)
    headers = _key_header(web_client)
    body = {"new_password": NEW_PASSWORD, "current_password": LOGIN_PASSWORD}
    assert web_client.put("/api/settings/login", json=body).status_code == 200
    assert len(web_client.get("/api/settings/api-keys").json()) == 1
    web_client.cookies.clear()
    assert web_client.get("/api/devices", headers=headers).status_code == 200


def test_revoking_keys_needs_a_new_password(web_client):
    _signed_in(web_client)
    _make_key(web_client)
    r = web_client.put("/api/settings/login", json={"username": "owner", "revoke_api_keys": True})
    assert r.status_code == 400
    assert len(web_client.get("/api/settings/api-keys").json()) == 1


def test_a_wrong_password_revokes_no_keys(web_client):
    _signed_in(web_client)
    _make_key(web_client)
    body = {"new_password": NEW_PASSWORD, "current_password": "nope nope nope", "revoke_api_keys": True}
    assert web_client.put("/api/settings/login", json=body).status_code == 401
    assert len(web_client.get("/api/settings/api-keys").json()) == 1


def test_key_events_are_logged_without_the_key(web_client):
    from bacpack import history_writer
    from bacpack.models import LogEntry

    _signed_in(web_client)
    created = _make_key(web_client, "grafana").json()
    web_client.delete(f"/api/settings/api-keys/{created['id']}")
    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        messages = [e.message for e in session.query(LogEntry)]
    assert any("created" in m for m in messages) and any("revoked" in m for m in messages)
    assert not any(created["key"] in m or "grafana" in m for m in messages)


def _security_messages(web_client):
    from bacpack import history_writer
    from bacpack.models import LogEntry

    history_writer.flush_current()
    with web_client.app.state.session_factory() as session:
        return [e.message for e in session.query(LogEntry) if e.logger_name == "bacpack.security"]


def _wrong_key(web_client, password="nope nope nope"):
    return web_client.post("/api/settings/api-keys", json={"name": "a", "current_password": password})


def test_a_wrong_current_password_is_logged_with_what_it_was_for(web_client):
    _signed_in(web_client)
    _wrong_key(web_client)
    web_client.put("/api/settings/login", json={"new_password": NEW_PASSWORD, "current_password": "nope nope nope"})
    web_client.post("/api/settings/login/disable", json={"current_password": "nope nope nope"})
    messages = _security_messages(web_client)
    assert any("current password wrong" in m and "creating an API key" in m for m in messages)
    assert any("current password wrong" in m and "password change" in m for m in messages)
    assert any("current password wrong" in m and "turning the login off" in m for m in messages)
    assert not any("nope" in m for m in messages)


def test_repeated_wrong_current_passwords_lock_with_429_and_a_log_line(web_client):
    _signed_in(web_client)
    for _ in range(4):
        assert _wrong_key(web_client).status_code == 401
    r = _wrong_key(web_client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert any("locked out" in m for m in _security_messages(web_client))
    assert web_client.get("/api/settings/api-keys").json() == []


def test_the_lock_is_shared_with_the_login_page_and_refuses_the_right_password(web_client):
    _signed_in(web_client)
    for _ in range(5):
        _wrong_key(web_client)
    assert _make_key(web_client).status_code == 429
    r = web_client.post("/api/auth/login", json={"username": "admin", "password": LOGIN_PASSWORD})
    assert r.status_code == 429


def test_login_failures_count_toward_the_current_password_lock(web_client):
    _signed_in(web_client)
    for _ in range(5):
        web_client.post("/api/auth/login", json={"username": "admin", "password": "nope nope nope"})
    assert _make_key(web_client).status_code == 429


def test_a_missing_current_password_is_a_401_and_not_counted(web_client):
    _signed_in(web_client)
    for _ in range(6):
        assert web_client.post("/api/settings/api-keys", json={"name": "a"}).status_code == 401
    assert _make_key(web_client).status_code == 201


def test_an_oversize_current_password_is_counted(web_client):
    _signed_in(web_client)
    for _ in range(4):
        assert _wrong_key(web_client, "x" * 5000).status_code == 401
    assert _wrong_key(web_client, "x" * 5000).status_code == 429


def test_a_right_current_password_clears_the_count(web_client):
    _signed_in(web_client)
    for _ in range(4):
        _wrong_key(web_client)
    assert _make_key(web_client, "ok").status_code == 201
    for _ in range(4):
        assert _wrong_key(web_client).status_code == 401


def _enable_with_keys(web_client, **extra):
    web_client.headers["Origin"] = "http://127.0.0.1"
    first = web_client.post("/api/settings/api-keys", json={"name": "one"}).json()
    web_client.post("/api/settings/api-keys", json={"name": "two"})
    body = {"username": "boss", "password": LOGIN_PASSWORD, **extra}
    return first["key"], web_client.post("/api/settings/login/enable", json=body)


def test_enabling_the_login_can_revoke_the_keys_made_while_it_was_off(web_client):
    key, r = _enable_with_keys(web_client, revoke_api_keys=True)
    assert r.status_code == 200, r.text
    assert web_client.get("/api/settings/api-keys").json() == []
    web_client.cookies.clear()
    assert web_client.get("/api/devices", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    revoked = [m for m in _security_messages(web_client) if "revoked" in m]
    assert len(revoked) == 2


def test_enabling_the_login_keeps_the_keys_unless_asked(web_client):
    key, r = _enable_with_keys(web_client)
    assert r.status_code == 200, r.text
    assert len(web_client.get("/api/settings/api-keys").json()) == 2
    web_client.cookies.clear()
    assert web_client.get("/api/devices", headers={"Authorization": f"Bearer {key}"}).status_code == 200
