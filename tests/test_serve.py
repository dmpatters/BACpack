"""The HTTPS launcher (src/bacpack/serve.py)."""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from bacpack import lifecycle, serve, timeutil, tls_certs, web_server, windows_service
from bacpack.db import make_engine, make_session_factory
from bacpack.models import LogEntry, get_or_create_settings
from tests.conftest import _copy_template
from tests.sqlite_compat import drop_column
from tests.test_tls_certs import _foreign_cert

MARKERS = (
    lifecycle.HTTPS_ENV,
    lifecycle.RUNNING_ADDRESS_ENV,
    lifecycle.RUNNING_PORT_ENV,
    lifecycle.RUNNING_FINGERPRINT_ENV,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in MARKERS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    yield
    logger = logging.getLogger("bacpack")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


@pytest.fixture
def db_path(tmp_path, _template_db):
    path = tmp_path / "data" / "serve.db"
    path.parent.mkdir()
    return _copy_template(_template_db, path)


def _log_messages(db_path):
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            return [(e.level, e.message) for e in session.query(LogEntry)]
    finally:
        engine.dispose()


def _set_address(db_path, address):
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            from bacpack.db import lock_both_files

            lock_both_files(session)
            get_or_create_settings(session).web_listen_address = address
            session.commit()
    finally:
        engine.dispose()


def test_prepare_makes_the_certificate_on_first_start_and_reuses_it(db_path):
    first = serve.prepare(str(db_path), 8000)
    second = serve.prepare(str(db_path), 8000)
    assert first.fingerprint == second.fingerprint
    assert first.certfile.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert first.keyfile.exists()
    created = [m for _, m in _log_messages(db_path) if "generated at first start" in m]
    assert len(created) == 1


def test_prepare_reads_the_listen_address_from_settings(db_path):
    assert serve.prepare(str(db_path), 8443).host == "0.0.0.0"
    _set_address(db_path, "127.0.0.1")
    plan = serve.prepare(str(db_path), 8443)
    assert (plan.host, plan.port) == ("127.0.0.1", 8443)


class Clock:
    """A fake monotonic clock whose sleep advances it."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_prepare_waits_for_a_missing_address_then_refuses_with_a_retryable_code(db_path, monkeypatch):
    _set_address(db_path, "10.99.99.99")
    monkeypatch.setattr(web_server, "local_ipv4_addresses", lambda: ["127.0.0.1"])
    clock = Clock()
    with pytest.raises(serve.AddressMissing) as raised:
        serve.prepare(str(db_path), 8000, sleep=clock.sleep, monotonic=clock.monotonic)
    message = str(raised.value)
    assert "10.99.99.99" in message and "bacpack-admin web --address 0.0.0.0" in message
    assert raised.value.exit_code == lifecycle.ADDRESS_MISSING_EXIT_CODE
    assert lifecycle.ADDRESS_MISSING_EXIT_CODE != lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE
    assert clock.now >= serve.ADDRESS_WAIT_SECONDS and set(clock.sleeps) == {serve.ADDRESS_POLL_SECONDS}
    messages = _log_messages(db_path)
    assert len([m for level, m in messages if "waiting" in m.lower()]) == 1
    assert any(level == "CRITICAL" and "10.99.99.99" in m for level, m in messages)


def test_prepare_goes_on_when_the_address_turns_up_while_waiting(db_path, monkeypatch):
    _set_address(db_path, "10.99.99.99")
    here = ["127.0.0.1"]
    monkeypatch.setattr(web_server, "local_ipv4_addresses", lambda: list(here))
    clock = Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.now >= 6:
            here.append("10.99.99.99")

    plan = serve.prepare(str(db_path), 8000, sleep=sleep, monotonic=clock.monotonic)
    assert plan.host == "10.99.99.99"
    assert clock.now < serve.ADDRESS_WAIT_SECONDS


def test_main_returns_the_retryable_code_for_a_missing_address(db_path, monkeypatch):
    _set_address(db_path, "10.99.99.99")
    monkeypatch.setattr(web_server, "local_ipv4_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(serve, "ADDRESS_WAIT_SECONDS", 0)
    calls = []
    code = serve.main(["serve"], run=lambda *a, **k: calls.append(1), environ={"BACPACK_DB_PATH": str(db_path)})
    assert code == lifecycle.ADDRESS_MISSING_EXIT_CODE and calls == []


@pytest.mark.parametrize("address", ["::", "localhost", "10.0.0", "not-an-address"])
def test_prepare_refuses_a_stored_address_that_is_not_ipv4_at_once(db_path, address):
    _set_address(db_path, address)
    clock = Clock()
    with pytest.raises(serve.ServeRefused) as raised:
        serve.prepare(str(db_path), 8000, sleep=clock.sleep, monotonic=clock.monotonic)
    assert not isinstance(raised.value, serve.AddressMissing)
    assert raised.value.exit_code == lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE
    assert "bacpack-admin web --address 0.0.0.0" in str(raised.value)
    assert clock.sleeps == []


@pytest.mark.parametrize("port", ["0", "65536", "70000"])
def test_a_port_outside_the_range_is_usage_from_the_flag_or_the_environment(port, capsys):
    run = lambda *a, **k: pytest.fail("ran")  # noqa: E731
    assert serve.main(["serve", "--port", port], run=run, environ={}) == 2
    assert serve.main(["serve"], run=run, environ={"BACPACK_PORT": port}) == 2
    assert "usage" in capsys.readouterr().err


def test_prepare_refuses_a_lone_key_and_never_overwrites_it(db_path):
    directory = tls_certs.tls_dir(db_path.parent)
    directory.mkdir()
    (directory / tls_certs.KEY_FILE).write_bytes(b"precious")
    with pytest.raises(serve.ServeRefused) as raised:
        serve.prepare(str(db_path), 8000)
    assert "bacpack-admin web --self-signed" in str(raised.value)
    assert (directory / tls_certs.KEY_FILE).read_bytes() == b"precious"
    assert not (directory / tls_certs.CERT_FILE).exists()


def test_prepare_runs_the_pending_migration_first(db_path):
    engine = make_engine(db_path)
    with engine.begin() as connection:
        drop_column(connection, "server_settings", "web_listen_address")
    engine.dispose()
    assert serve.prepare(str(db_path), 8000).host == "0.0.0.0"


def test_expired_own_certificate_still_starts_with_a_warning(db_path):
    cert_pem, key_pem = _foreign_cert(timeutil.utc_now(), days_before=400, days_after=-10)
    tls_certs.write_pair(db_path.parent, cert_pem, key_pem)
    plan = serve.prepare(str(db_path), 8000)
    assert plan.certfile.read_bytes() == cert_pem
    assert any(level == "WARNING" and "expired" in m for level, m in _log_messages(db_path))


def test_main_starts_uvicorn_with_tls_and_no_proxy_headers(db_path):
    calls = []
    code = serve.main(
        ["serve", "--port", "8443"],
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
        environ={"BACPACK_DB_PATH": str(db_path)},
    )
    assert code == 0
    (args, kwargs), = calls
    assert args == ("bacpack.web.app:app",)
    directory = tls_certs.tls_dir(db_path.parent)
    assert kwargs == {
        "host": "0.0.0.0",
        "port": 8443,
        "ssl_certfile": str(directory / tls_certs.CERT_FILE),
        "ssl_keyfile": str(directory / tls_certs.KEY_FILE),
        "proxy_headers": False,
    }
    running = lifecycle.running_web()
    assert running is not None and (running.address, running.port) == ("0.0.0.0", 8443)
    assert lifecycle.https_only()


def test_main_returns_79_when_prepare_refuses_and_never_calls_run(db_path, capsys):
    directory = tls_certs.tls_dir(db_path.parent)
    directory.mkdir()
    (directory / tls_certs.CERT_FILE).write_bytes(b"lonely")
    calls = []
    code = serve.main(
        ["serve"], run=lambda *a, **k: calls.append(1), environ={"BACPACK_DB_PATH": str(db_path)}
    )
    assert code == lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE == 79
    assert calls == []
    assert "bacpack-admin web --self-signed" in capsys.readouterr().err
    assert lifecycle.running_web() is None


def _bump_main_version(db_path):
    import sqlite3
    from contextlib import closing

    from bacpack import migrations

    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"PRAGMA user_version = {len(migrations.MIGRATIONS) + 1}")
        connection.commit()


def test_prepare_refuses_a_database_from_a_newer_release_with_81(db_path, caplog):
    import hashlib

    _bump_main_version(db_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with caplog.at_level(logging.CRITICAL, logger="bacpack.serve"):
        with pytest.raises(serve.DatabaseTooNewRefused) as raised:
            serve.prepare(str(db_path), 8000)
    assert raised.value.exit_code == lifecycle.DATABASE_TOO_NEW_EXIT_CODE == 81
    assert any(r.levelno == logging.CRITICAL and "newer BACpack" in r.getMessage() for r in caplog.records)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_main_returns_81_and_never_calls_run(db_path):
    _bump_main_version(db_path)
    calls = []
    code = serve.main(["serve"], run=lambda *a, **k: calls.append(1), environ={"BACPACK_DB_PATH": str(db_path)})
    assert code == lifecycle.DATABASE_TOO_NEW_EXIT_CODE
    assert calls == []
    assert lifecycle.running_web() is None


def test_main_returns_78_for_an_old_sqlite(db_path, monkeypatch):
    monkeypatch.setattr(serve, "sqlite_version_problem", lambda: "too old")
    calls = []
    code = serve.main(["serve"], run=lambda *a, **k: calls.append(1), environ={"BACPACK_DB_PATH": str(db_path)})
    assert code == lifecycle.SQLITE_TOO_OLD_EXIT_CODE
    assert calls == []


def test_port_comes_from_the_argument_then_bacpack_port_then_8000(db_path):
    def port_for(argv, extra):
        seen = []
        serve.main(
            argv, run=lambda *a, **k: seen.append(k["port"]), environ={"BACPACK_DB_PATH": str(db_path), **extra}
        )
        return seen[0]

    assert port_for(["serve", "--port", "9001"], {"BACPACK_PORT": "9002"}) == 9001
    assert port_for(["serve"], {"BACPACK_PORT": "9002"}) == 9002
    assert port_for(["serve"], {}) == 8000
    assert port_for(["serve"], {"BACPACK_PORT": "junk"}) == 8000


@pytest.mark.parametrize("argv", [[], ["bogus"], ["serve", "--port"], ["serve", "--port", "x"], ["serve", "extra"]])
def test_unknown_subcommand_is_usage(argv, capsys):
    assert serve.main(argv, run=lambda *a, **k: pytest.fail("ran"), environ={}) == 2
    assert "usage" in capsys.readouterr().err


def _refuse_with(kind, db_path, monkeypatch):
    if kind == lifecycle.SQLITE_TOO_OLD_EXIT_CODE:
        monkeypatch.setattr(serve, "sqlite_version_problem", lambda: "too old")
    elif kind == lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE:
        directory = tls_certs.tls_dir(db_path.parent)
        directory.mkdir()
        (directory / tls_certs.CERT_FILE).write_bytes(b"lonely")
    elif kind == lifecycle.DATABASE_TOO_NEW_EXIT_CODE:
        _bump_main_version(db_path)
    else:
        _set_address(db_path, "10.99.99.99")
        monkeypatch.setattr(web_server, "local_ipv4_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(serve, "ADDRESS_WAIT_SECONDS", 0)


class FakeStop:
    """windows_service.stop_own_service and serve.wait_for_stop, recorded."""

    def __init__(self, monkeypatch, refused=None, interrupt_in_stop=False):
        self.names = []
        self.waits = []
        self.refused = refused
        self.interrupt_in_stop = interrupt_in_stop
        monkeypatch.setattr(windows_service, "stop_own_service", self.stop)
        monkeypatch.setattr(serve, "wait_for_stop", self.wait)

    def stop(self, name):
        self.names.append(name)
        if self.interrupt_in_stop:
            raise KeyboardInterrupt  # WinSW's Ctrl+C, before the wait began
        return self.refused

    def wait(self, seconds):
        self.waits.append(seconds)
        raise KeyboardInterrupt  # WinSW's Ctrl+C


def _service_environ(db_path, **extra):
    return {"BACPACK_DB_PATH": str(db_path), lifecycle.SERVICE_ENV: "1", windows_service.SERVICE_ID_ENV: "BACpack", **extra}


@pytest.mark.parametrize("code", lifecycle.NOT_RETRIED_EXIT_CODES)
def test_the_windows_service_stops_itself_for_what_a_restart_cannot_fix(code, db_path, monkeypatch, capsys):
    """WinSW 2.12 restarts on every non-zero exit and hangs on an exit of 0."""
    _refuse_with(code, db_path, monkeypatch)
    fake = FakeStop(monkeypatch)
    calls = []
    environ = _service_environ(db_path)
    assert serve.main(["serve"], run=lambda *a, **k: calls.append(1), environ=environ, platform="win32") == code
    assert calls == []
    assert fake.names == ["BACpack"]
    assert fake.waits == [serve.STOP_WAIT_SECONDS]
    err = capsys.readouterr().err
    assert f"is stopping the Windows service BACpack (exit code {code})" in err


def test_a_ctrl_c_before_the_wait_still_exits_with_the_real_code(db_path, monkeypatch):
    _refuse_with(lifecycle.DATABASE_TOO_NEW_EXIT_CODE, db_path, monkeypatch)
    fake = FakeStop(monkeypatch, interrupt_in_stop=True)
    code = serve.main(["serve"], run=lambda *a, **k: None, environ=_service_environ(db_path), platform="win32")
    assert code == lifecycle.DATABASE_TOO_NEW_EXIT_CODE
    assert fake.names == ["BACpack"] and fake.waits == []


def test_a_refused_stop_leaves_the_restarts_and_says_why(db_path, monkeypatch, capsys):
    _refuse_with(lifecycle.DATABASE_TOO_NEW_EXIT_CODE, db_path, monkeypatch)
    fake = FakeStop(monkeypatch, refused="opening the service failed, Windows error 5")
    code = serve.main(["serve"], run=lambda *a, **k: None, environ=_service_environ(db_path), platform="win32")
    assert code == lifecycle.DATABASE_TOO_NEW_EXIT_CODE
    assert fake.waits == []
    err = capsys.readouterr().err
    assert "could not stop the Windows service BACpack (opening the service failed, Windows error 5)" in err
    assert "exit code 81" in err


def test_a_stop_that_never_comes_still_exits_with_the_real_code(db_path, monkeypatch):
    _refuse_with(lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE, db_path, monkeypatch)
    fake = FakeStop(monkeypatch)
    monkeypatch.setattr(serve, "wait_for_stop", lambda seconds: fake.waits.append(seconds))
    code = serve.main(["serve"], run=lambda *a, **k: None, environ=_service_environ(db_path), platform="win32")
    assert code == lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE
    assert fake.waits == [serve.STOP_WAIT_SECONDS]


@pytest.mark.parametrize("code", lifecycle.NOT_RETRIED_EXIT_CODES)
@pytest.mark.parametrize(
    ("platform", "service", "service_id"), [("linux", "1", "BACpack"), ("win32", "", "BACpack"), ("win32", "1", "")]
)
def test_systemd_a_console_run_or_no_service_id_never_stop_a_service(
    code, platform, service, service_id, db_path, monkeypatch
):
    _refuse_with(code, db_path, monkeypatch)
    fake = FakeStop(monkeypatch)
    environ = {
        "BACPACK_DB_PATH": str(db_path),
        lifecycle.SERVICE_ENV: service,
        windows_service.SERVICE_ID_ENV: service_id,
    }
    assert serve.main(["serve"], run=lambda *a, **k: None, environ=environ, platform=platform) == code
    assert fake.names == [] and fake.waits == []


def test_the_windows_service_still_retries_a_missing_address(db_path, monkeypatch):
    code = lifecycle.ADDRESS_MISSING_EXIT_CODE
    _refuse_with(code, db_path, monkeypatch)
    fake = FakeStop(monkeypatch)
    assert serve.main(["serve"], run=lambda *a, **k: None, environ=_service_environ(db_path), platform="win32") == code
    assert fake.names == []
    assert lifecycle.ADDRESS_MISSING_EXIT_CODE not in lifecycle.NOT_RETRIED_EXIT_CODES
    assert lifecycle.RESTART_UI_EXIT_CODE not in lifecycle.NOT_RETRIED_EXIT_CODES


def test_stop_own_service_reports_a_refusal_instead_of_raising():
    problem = windows_service.stop_own_service("BACpack-zz-no-such-service")
    assert isinstance(problem, str) and problem
