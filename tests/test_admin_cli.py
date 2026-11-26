"""bacpack-admin (src/bacpack/admin/)."""
from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from bacpack import login_store
from bacpack.admin.cli import main
from bacpack.db import lock_both_files, make_engine, make_session_factory
from bacpack.models import LogEntry, WebSession
from tests.conftest import _copy_template

PASSWORD = "a long enough password"


@pytest.fixture()
def db_path(tmp_path, _template_db):
    return _copy_template(_template_db, tmp_path / "admin.db")


def _run(db_path, argv, answers=(), environ=None):
    answers = list(answers)
    out = io.StringIO()
    code = main(
        argv,
        prompt=lambda _text: answers.pop(0),
        environ={"BACPACK_DB_PATH": str(db_path), **(environ or {})},
        out=out,
    )
    return code, out.getvalue()


def _state(db_path):
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            return login_store.get_login_state(session), session.query(WebSession).count()
    finally:
        engine.dispose()


def _add_session(db_path):
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            lock_both_files(session)
            login_store.create_session(session, "1.1.1.1", None)
            session.commit()
    finally:
        engine.dispose()


def test_admin_prints_the_message_and_returns_2_for_a_database_from_a_newer_release(db_path):
    import sqlite3
    from contextlib import closing

    from bacpack import migrations

    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"PRAGMA user_version = {len(migrations.MIGRATIONS) + 1}")
        connection.commit()
    code, output = _run(db_path, ["status"])
    assert code == 2
    assert "newer BACpack" in output
    assert "Traceback" not in output


def test_reset_login_sets_login_ends_sessions_and_logs(db_path):
    _add_session(db_path)
    code, output = _run(db_path, ["reset-login", "--username", "owner"], [PASSWORD, PASSWORD])
    assert code == 0, output
    state, sessions = _state(db_path)
    assert (state.mode, state.username, sessions) == ("login", "owner", 0)
    assert PASSWORD not in output
    engine = make_engine(db_path)
    with make_session_factory(engine)() as session:
        assert any("reset" in e.message for e in session.query(LogEntry))
    engine.dispose()


def test_reset_login_refuses_a_short_password_and_changes_nothing(db_path):
    before, _ = _state(db_path)
    code, output = _run(db_path, ["reset-login", "--username", "owner"], ["short", "short"])
    assert code == 1 and "at least" in output
    assert _state(db_path)[0] == before


def test_reset_login_prompts_twice_and_refuses_a_mismatch(db_path):
    code, output = _run(db_path, ["reset-login", "--username", "owner"], [PASSWORD, PASSWORD + "x"])
    assert code == 1 and "differ" in output
    assert _state(db_path)[0].mode == "none"


def test_reset_login_keeps_the_username_unless_given(db_path):
    assert _run(db_path, ["reset-login", "--username", "owner"], [PASSWORD, PASSWORD])[0] == 0
    assert _run(db_path, ["reset-login"], [PASSWORD + "2", PASSWORD + "2"])[0] == 0
    assert _state(db_path)[0].username == "owner"


def test_reset_login_without_any_username_asks_for_one(db_path):
    code, output = _run(db_path, ["reset-login"], [])
    assert code == 1 and "--username" in output


def test_set_login_reads_the_environment_password_and_never_prompts(db_path):
    code, output = _run(
        db_path, ["set-login", "--username", "svc"], [], environ={"BACPACK_LOGIN_PASSWORD": PASSWORD}
    )
    assert code == 0, output
    assert _state(db_path)[0].username == "svc"


def test_set_login_prompts_when_the_variable_is_unset(db_path):
    assert _run(db_path, ["set-login", "--username", "svc"], [PASSWORD, PASSWORD])[0] == 0
    assert _state(db_path)[0].mode == "login"


def test_no_login_records_none(db_path):
    assert _run(db_path, ["set-login", "--username", "svc"], [PASSWORD, PASSWORD])[0] == 0
    assert _run(db_path, ["no-login"])[0] == 0
    assert _state(db_path)[0].mode == "none"


def test_status_prints_mode_username_and_counts_and_never_a_hash(db_path):
    _run(db_path, ["set-login", "--username", "svc"], [PASSWORD, PASSWORD])
    code, output = _run(db_path, ["status"])
    assert code == 0
    assert "login required" in output and "svc" in output and "Sessions: 0" in output and "API keys: 0" in output
    assert "$" not in output


def test_reset_while_the_web_app_runs_unlocks_a_locked_ip_and_kills_old_sessions(web_client):
    from tests.web._helpers import enable_login, session_cookie

    enable_login(web_client)
    session_cookie(web_client)
    old_token = web_client.cookies.get("bacpack_session")
    web_client.cookies.clear()
    origin = {"Origin": "http://127.0.0.1"}
    for _ in range(5):
        web_client.post("/api/auth/login", json={"username": "admin", "password": "wrong wrong wrong"}, headers=origin)
    locked = web_client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=origin)
    assert locked.status_code == 429

    db_path = web_client.app.state.data_dir / "web-test.db"
    code, output = _run(db_path, ["reset-login"], ["brand new password", "brand new password"])
    assert code == 0, output

    ok = web_client.post("/api/auth/login", json={"username": "admin", "password": "brand new password"}, headers=origin)
    assert ok.status_code == 200
    web_client.cookies.set("bacpack_session", old_token)
    assert web_client.get("/api/devices").status_code == 401


def test_admin_runs_on_a_fresh_empty_path(tmp_path):
    path = tmp_path / "brand-new.db"
    code, output = _run(path, ["status"])
    assert code == 0 and "not chosen yet" in output
    assert _run(path, ["set-login", "--username", "svc"], [PASSWORD, PASSWORD])[0] == 0
    assert _state(path)[0].mode == "login"


def test_python_dash_m_bacpack_admin_runs(tmp_path):
    env = {**os.environ, "BACPACK_DB_PATH": str(tmp_path / "m.db")}
    result = subprocess.run(
        [sys.executable, "-m", "bacpack.admin", "status"], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "Login:" in result.stdout


def test_a_bad_command_is_a_usage_error(db_path):
    assert _run(db_path, ["frobnicate"])[0] == 2



# --- web and status (delivery 2) ---------------------------------------------


def _tls_files(db_path):
    from bacpack import tls_certs

    directory = tls_certs.tls_dir(db_path.parent)
    return directory / tls_certs.CERT_FILE, directory / tls_certs.KEY_FILE


def _security_logs(db_path):
    from bacpack import history_writer

    history_writer.flush_current()
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            return [e.message for e in session.query(LogEntry).filter(LogEntry.logger_name == "bacpack.security")]
    finally:
        engine.dispose()


def test_web_self_signed_creates_logs_and_says_restart(db_path):
    from bacpack import tls_certs

    code, output = _run(db_path, ["web", "--self-signed"])
    assert code == 0
    cert_file, key_file = _tls_files(db_path)
    info = tls_certs.describe_certificate(cert_file.read_bytes())
    assert info.fingerprint_sha256 in output
    assert "restart" in output.lower() and "nothing was restarted" in output.lower()
    assert "PRIVATE KEY" not in output
    assert len([m for m in _security_logs(db_path) if "certificate" in m]) == 1


def test_web_self_signed_replaces_an_uploaded_certificate(db_path):
    from bacpack import timeutil, tls_certs
    from tests.test_tls_certs import _foreign_cert

    cert_pem, key_pem = _foreign_cert(timeutil.utc_now(), days_before=1, days_after=100)
    tls_certs.write_pair(db_path.parent, cert_pem, key_pem)
    assert _run(db_path, ["web", "--self-signed"])[0] == 0
    cert_file, _ = _tls_files(db_path)
    assert tls_certs.describe_certificate(cert_file.read_bytes()).generated_by_bacpack


def test_web_address_sets_the_column_and_says_restart(db_path):
    code, output = _run(db_path, ["web", "--address", "127.0.0.1"])
    assert code == 0 and "127.0.0.1" in output and "restart" in output.lower()
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            from bacpack import web_server

            assert web_server.get_listen_address(session) == "127.0.0.1"
    finally:
        engine.dispose()


def test_web_address_refuses_a_bad_one_and_changes_nothing(db_path):
    code, output = _run(db_path, ["web", "--address", "::", "--self-signed"])
    assert code == 1 and "IPv4" in output
    assert not _tls_files(db_path)[0].exists()
    assert "0.0.0.0" in _run(db_path, ["status"])[1]


def test_web_without_options_is_usage(db_path):
    code, output = _run(db_path, ["web"])
    assert code == 2 and "--self-signed" in output and "--address" in output


def test_web_runs_on_a_fresh_empty_path(tmp_path):
    path = tmp_path / "brand-new.db"
    assert _run(path, ["web", "--self-signed"])[0] == 0
    assert _tls_files(path)[0].exists()


def test_status_shows_login_listen_address_port_and_certificate(db_path):
    _run(db_path, ["web", "--self-signed"])
    code, output = _run(db_path, ["status"], environ={"BACPACK_PORT": "443"})
    assert code == 0
    assert "Login:" in output
    assert "listens on 0.0.0.0, port 443" in output
    assert "self-signed, SHA-256" in output and "expires" in output
    assert "localhost" in output
    assert "unknown (8000 by default)" in _run(db_path, ["status"])[1]


def test_status_with_no_certificate_or_a_lone_key_does_not_crash(db_path):
    _, output = _run(db_path, ["status"])
    assert "none yet" in output
    cert_file, key_file = _tls_files(db_path)
    key_file.parent.mkdir()
    key_file.write_bytes(b"x")
    code, output = _run(db_path, ["status"])
    assert code == 0 and "cert.pem" in output


def test_status_login_lines_are_unchanged(db_path):
    _run(db_path, ["set-login", "--username", "svc"], [PASSWORD, PASSWORD])
    lines = _run(db_path, ["status"])[1].splitlines()
    assert lines[:4] == ["Login:    login required", "Username: svc", "Sessions: 0", "API keys: 0"]


def test_web_commands_never_print_a_private_key(db_path):
    outputs = [
        _run(db_path, ["web", "--self-signed"])[1],
        _run(db_path, ["web", "--address", "127.0.0.1"])[1],
        _run(db_path, ["status"])[1],
    ]
    key = _tls_files(db_path)[1].read_text().splitlines()[1]
    assert all("PRIVATE KEY" not in o and key not in o for o in outputs)
