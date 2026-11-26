"""/api/auth: state, first-use setup, login (with lockout) and logout."""
from __future__ import annotations

import json

import pytest

from bacpack import history_writer
from bacpack.models import LogEntry
from bacpack.web.auth_middleware import SESSION_COOKIE
from tests.web._helpers import LOGIN_PASSWORD, enable_login, make_key, session_cookie, set_login_mode

ORIGIN = {"Origin": "http://127.0.0.1"}


def _security_logs(client):
    history_writer.flush_current()
    with client.app.state.session_factory() as session:
        return [e for e in session.query(LogEntry).order_by(LogEntry.id) if e.logger_name == "bacpack.security"]


def _login(client, username="admin", password=LOGIN_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password}, headers=ORIGIN)


def test_state_reports_unset_none_login_and_signed_in(web_client):
    assert web_client.get("/api/auth/state").json() == {"mode": "none", "signed_in": False, "username": None}
    set_login_mode(web_client, None)
    assert web_client.get("/api/auth/state").json()["mode"] is None
    enable_login(web_client)
    assert web_client.get("/api/auth/state").json() == {"mode": "login", "signed_in": False, "username": None}
    session_cookie(web_client)
    assert web_client.get("/api/auth/state").json() == {"mode": "login", "signed_in": True, "username": "admin"}
    web_client.cookies.clear()
    key = make_key(web_client)
    r = web_client.get("/api/auth/state", headers={"Authorization": f"Bearer {key}"})
    assert r.json()["signed_in"] is True


def test_setup_with_login_creates_it_signs_in_and_logs(web_client):
    set_login_mode(web_client, None)
    r = web_client.post(
        "/api/auth/setup", json={"mode": "login", "username": "owner", "password": LOGIN_PASSWORD}, headers=ORIGIN
    )
    assert r.status_code == 200, r.text
    assert SESSION_COOKIE in web_client.cookies
    state = web_client.get("/api/auth/state").json()
    assert state == {"mode": "login", "signed_in": True, "username": "owner"}
    assert any("owner" in e.message for e in _security_logs(web_client))


def test_setup_none_requires_confirm_true(web_client):
    set_login_mode(web_client, None)
    assert web_client.post("/api/auth/setup", json={"mode": "none"}, headers=ORIGIN).status_code == 400
    assert web_client.get("/api/auth/state").json()["mode"] is None


def test_setup_none_records_the_choice(web_client):
    set_login_mode(web_client, None)
    r = web_client.post("/api/auth/setup", json={"mode": "none", "confirm": True}, headers=ORIGIN)
    assert r.status_code == 200
    assert web_client.get("/api/auth/state").json()["mode"] == "none"
    assert web_client.get("/api/devices").status_code == 200
    assert _security_logs(web_client)


@pytest.mark.parametrize(
    "username, password",
    [("owner", "short"), ("", LOGIN_PASSWORD), ("owner", "owner")],
)
def test_setup_applies_the_password_rules(web_client, username, password):
    set_login_mode(web_client, None)
    r = web_client.post(
        "/api/auth/setup", json={"mode": "login", "username": username, "password": password}, headers=ORIGIN
    )
    assert r.status_code == 400
    assert web_client.get("/api/auth/state").json()["mode"] is None


def test_setup_twice_gives_409_and_keeps_the_first_credentials(web_client):
    set_login_mode(web_client, None)
    first = {"mode": "login", "username": "first", "password": LOGIN_PASSWORD}
    second = {"mode": "login", "username": "second", "password": "another long password"}
    assert web_client.post("/api/auth/setup", json=first, headers=ORIGIN).status_code == 200
    # Signed in from the first call, so the gate lets this reach the route; a
    # request that raced it while the mode was still unset gets the same 409.
    assert web_client.post("/api/auth/setup", json=second, headers=ORIGIN).status_code == 409
    web_client.cookies.clear()
    assert _login(web_client, "second", "another long password").status_code == 401
    assert _login(web_client, "first").status_code == 200


def test_login_ok_sets_cookie_and_logs(web_client):
    enable_login(web_client)
    r = _login(web_client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert web_client.get("/api/devices").status_code == 200
    assert any("signed in" in e.message for e in _security_logs(web_client))


def test_a_login_checked_before_a_password_change_gets_no_session(web_client, monkeypatch):
    # The change ends every session; a login that checked the old password
    # just before it must not add a fresh one just after.
    from bacpack import auth
    from bacpack.models import ServerSettings, WebSession

    enable_login(web_client)
    real = auth.verify_password

    def verify_then_change(password, stored):
        ok = real(password, stored)
        with web_client.app.state.session_factory() as session:
            settings = session.query(ServerSettings).one()
            settings.login_epoch = (settings.login_epoch or 0) + 1
            session.query(WebSession).delete()
            session.commit()
        return ok

    monkeypatch.setattr(auth, "verify_password", verify_then_change)
    r = _login(web_client)

    assert r.status_code == 401, r.text
    assert SESSION_COOKIE not in r.cookies
    with web_client.app.state.session_factory() as session:
        assert session.query(WebSession).count() == 0


def test_wrong_password_is_401_and_logs_without_the_attempted_text(web_client):
    enable_login(web_client)
    assert _login(web_client, "admin", "TYPO-secret-password").status_code == 401
    assert _login(web_client, "TYPO-who-is-this", LOGIN_PASSWORD).status_code == 401
    messages = [e.message for e in _security_logs(web_client)]
    assert any("wrong password" in m for m in messages)
    assert any("unknown username" in m for m in messages)
    joined = "\n".join(messages)
    for secret in ("TYPO-secret-password", "TYPO-who-is-this", LOGIN_PASSWORD):
        assert secret not in joined


def test_wrong_username_and_wrong_password_look_identical_to_the_client(web_client):
    enable_login(web_client)
    a = _login(web_client, "admin", "wrong wrong wrong")
    b = _login(web_client, "nobody", "wrong wrong wrong")
    assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_fifth_failure_locks_with_429_and_retry_after_and_logs(web_client):
    enable_login(web_client)
    for _ in range(4):
        assert _login(web_client, "admin", "wrong wrong wrong").status_code == 401
    r = _login(web_client, "admin", "wrong wrong wrong")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert any("locked out" in e.message for e in _security_logs(web_client))


def test_correct_password_while_locked_is_429(web_client):
    enable_login(web_client)
    for _ in range(5):
        _login(web_client, "admin", "wrong wrong wrong")
    assert _login(web_client).status_code == 429


def test_success_clears_failures(web_client):
    enable_login(web_client)
    for _ in range(4):
        _login(web_client, "admin", "wrong wrong wrong")
    assert _login(web_client).status_code == 200
    web_client.cookies.clear()
    for _ in range(4):
        assert _login(web_client, "admin", "wrong wrong wrong").status_code == 401


def test_logout_deletes_the_session_and_logs(web_client):
    enable_login(web_client)
    _login(web_client)
    token = web_client.cookies.get(SESSION_COOKIE)
    assert web_client.post("/api/auth/logout", headers=ORIGIN).status_code == 204
    web_client.cookies.set(SESSION_COOKIE, token)
    assert web_client.get("/api/devices").status_code == 401
    assert any("logout" in e.message for e in _security_logs(web_client))


@pytest.mark.parametrize("mode", ["none", None])
def test_login_is_refused_when_mode_is_none_or_unset(web_client, mode):
    set_login_mode(web_client, mode)
    assert _login(web_client).status_code == (409 if mode == "none" else 403)


async def test_concurrent_logins_from_one_address_cannot_outrun_the_lockout(web_client, monkeypatch):
    """The lock was only checked before the (slow) password check,
    so a burst all passed the check. Attempts in flight now count."""
    import asyncio
    import time

    import httpx

    from bacpack import auth
    from bacpack.login_lockout import MAX_FAILURES

    enable_login(web_client)
    verified = []
    real = auth.verify_password

    def slow_verify(password, stored):
        verified.append(password)
        time.sleep(0.05)
        return real(password, stored)

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    transport = httpx.ASGITransport(app=web_client.app, client=("9.9.9.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=ORIGIN) as client:
        results = await asyncio.gather(
            *[
                client.post("/api/auth/login", json={"username": "admin", "password": f"wrong password {i}"})
                for i in range(30)
            ]
        )
        assert len(verified) <= MAX_FAILURES
        assert sorted({r.status_code for r in results}) == [401, 429]
        after = await client.post("/api/auth/login", json={"username": "admin", "password": LOGIN_PASSWORD})
        assert after.status_code == 429


def test_oversize_credentials_are_a_plain_failure_without_a_password_check(web_client, monkeypatch):
    from bacpack import auth

    enable_login(web_client)
    checked = []
    monkeypatch.setattr(auth, "verify_password", lambda *a: checked.append(a) or False)
    monkeypatch.setattr(auth, "dummy_verify", lambda *a: checked.append(a))
    r = _login(web_client, "admin", "x" * 5000)
    assert r.status_code == 401
    r = _login(web_client, "u" * 100, "short")
    assert r.status_code == 401
    assert checked == []


def test_oversize_logins_still_count_toward_the_lockout(web_client):
    enable_login(web_client)
    for _ in range(4):
        assert _login(web_client, "admin", "x" * 5000).status_code == 401
    assert _login(web_client, "admin", "x" * 5000).status_code == 429


def test_oversize_current_password_is_refused_without_a_password_check(web_client, monkeypatch):
    from bacpack import auth
    from tests.web._helpers import session_cookie

    enable_login(web_client)
    session_cookie(web_client)
    checked = []
    monkeypatch.setattr(auth, "verify_password", lambda *a: checked.append(a) or False)
    r = web_client.put(
        "/api/settings/login", json={"new_password": "a long enough new password", "current_password": "x" * 5000}
    )
    assert r.status_code == 401
    r = web_client.post("/api/settings/login/disable", json={"current_password": "x" * 5000})
    assert r.status_code == 401
    assert checked == []


def test_logout_works_without_a_live_session_and_clears_the_cookie(web_client):
    enable_login(web_client)
    web_client.cookies.set(SESSION_COOKIE, "a-stale-token")
    r = web_client.post("/api/auth/logout", headers=ORIGIN)
    assert r.status_code == 204
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")
    web_client.cookies.clear()
    assert web_client.post("/api/auth/logout", headers=ORIGIN).status_code == 204


def test_logout_is_still_checked_for_cross_site(web_client):
    enable_login(web_client)
    r = web_client.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("username,password", [("\ud800", "x"), ("admin", "\udfff")])
def test_a_login_with_an_unpaired_surrogate_is_a_counted_failure(web_client, username, password):
    enable_login(web_client)
    body = ('{"username":%s,"password":%s}' % (json.dumps(username), json.dumps(password))).encode()
    for _ in range(4):
        r = web_client.post("/api/auth/login", content=body, headers={**ORIGIN, "content-type": "application/json"})
        assert r.status_code == 401
    r = web_client.post("/api/auth/login", content=body, headers={**ORIGIN, "content-type": "application/json"})
    assert r.status_code == 429
    assert _login(web_client).status_code == 429


def test_setup_refuses_a_surrogate_and_stays_unclaimed(web_client):
    set_login_mode(web_client, None)
    body = rb'{"mode":"login","username":"owner","password":"\ud800 long enough"}'
    r = web_client.post("/api/auth/setup", content=body, headers={**ORIGIN, "content-type": "application/json"})
    assert r.status_code == 422
    assert web_client.get("/api/auth/state").json()["mode"] is None
