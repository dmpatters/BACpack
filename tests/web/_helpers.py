"""Shared setup helpers for the tests/web/ API tests."""
from __future__ import annotations


def _create_device(client, device_id=599001, name="Demo Device"):
    r = client.post("/api/devices", json={"id": device_id, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _create_object(client, device_id=599001, **overrides):
    body = {
        "object_type": "analog-value",
        "instance_number": 1,
        "name": "AV-1",
        "units": "percent",
    }
    body.update(overrides)
    r = client.post(f"/api/devices/{device_id}/objects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _add_log_entry(web_client, *, device_id=None, level="INFO", logger_name="bacpack.x", message="m"):
    from bacpack.models import LogEntry

    session_factory = web_client.app.state.session_factory
    with session_factory() as session:
        session.add(
            LogEntry(device_id=device_id, level=level, logger_name=logger_name, message=message)
        )
        session.commit()


def _create_connector(client, **overrides):
    body = {"name": "Weather", "url": "http://example.invalid/status"}
    body.update(overrides)
    r = client.post("/api/connectors", json=body)
    assert r.status_code == 201, r.text
    return r.json()


_WEEKLY_SCHEDULE = [[{"time": "08:00", "value": "50.0"}] for _ in range(7)]


def _create_schedule_object(client, device_id=599001, **overrides):
    body = {
        "object_type": "schedule",
        "instance_number": 1,
        "name": "Sched-1",
        "schedule_value_type": "analog",
        "schedule_default": "10.0",
        "weekly_schedule": _WEEKLY_SCHEDULE,
    }
    body.update(overrides)
    return client.post(f"/api/devices/{device_id}/objects", json=body)


# --- login mode ---------------------------------------------------------------

LOGIN_PASSWORD = "a long enough password"


def set_login_mode(client, mode):
    """Puts the app's database in "none" or None (not chosen) directly."""
    from sqlalchemy import text

    with client.app.state.session_factory() as session:
        session.execute(
            text("UPDATE server_settings SET login_mode = :m, login_epoch = login_epoch + 1"), {"m": mode}
        )
        session.commit()


def enable_login(client, username="admin", password=LOGIN_PASSWORD):
    from bacpack import auth, login_store
    from bacpack.db import lock_both_files

    password_hash = auth.hash_password(password)
    with client.app.state.session_factory() as session:
        lock_both_files(session)
        login_store.set_login(session, username, password_hash)
        session.commit()


def session_cookie(client):
    """Signs the client in without the login route: a session straight from
    the store, as the cookie, plus the Origin header a browser sends."""
    from bacpack import login_store
    from bacpack.db import lock_both_files
    from bacpack.web.auth_middleware import SESSION_COOKIE

    with client.app.state.session_factory() as session:
        lock_both_files(session)
        token = login_store.create_session(session, "testclient", "pytest")
        session.commit()
    client.cookies.set(SESSION_COOKIE, token)
    client.headers["Origin"] = "http://testserver"
    return token


def sign_in(client, username="admin", password=LOGIN_PASSWORD):
    client.headers["Origin"] = "http://testserver"
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def make_key(client, name="t"):
    from bacpack import login_store
    from bacpack.db import lock_both_files

    with client.app.state.session_factory() as session:
        lock_both_files(session)
        _, raw = login_store.create_api_key(session, name)
        session.commit()
    return raw
