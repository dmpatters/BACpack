from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bacpack import lifecycle
from bacpack.db import sqlite_version_problem


def test_service_mode_reads_the_environment(monkeypatch):
    monkeypatch.delenv("BACPACK_SERVICE", raising=False)
    assert lifecycle.service_mode() is False
    monkeypatch.setenv("BACPACK_SERVICE", "1")
    assert lifecycle.service_mode() is True
    monkeypatch.setenv("BACPACK_SERVICE", "0")
    assert lifecycle.service_mode() is False


def test_sqlite_version_problem_accepts_3_34_and_newer():
    assert sqlite_version_problem((3, 34, 0)) is None
    assert sqlite_version_problem((3, 46, 1)) is None


def test_sqlite_version_problem_names_both_versions():
    problem = sqlite_version_problem((3, 33, 1))
    assert "3.33.1" in problem and "3.34" in problem


def test_sqlite_without_its_json_functions_is_refused():
    problem = sqlite_version_problem((3, 37, 2), has_json=False)
    assert problem is not None and "JSON" in problem and "3.37.2" in problem


def test_this_pythons_sqlite_has_its_json_functions():
    assert sqlite_version_problem() is None


class _Exited(Exception):
    def __init__(self, code):
        self.code = code


def test_startup_exits_78_on_an_old_sqlite(tmp_path, monkeypatch):
    from bacpack.web import app as app_module

    def fake_exit(code):
        raise _Exited(code)

    monkeypatch.setattr(lifecycle, "exit_process", fake_exit)
    monkeypatch.setattr(app_module, "sqlite_version_problem", lambda: "SQLite 3.34.1 is too old")
    app = app_module.create_app(db_path=str(tmp_path / "old.db"))
    with pytest.raises(_Exited) as caught:
        with TestClient(app, base_url="http://127.0.0.1"):
            pass
    assert caught.value.code == lifecycle.SQLITE_TOO_OLD_EXIT_CODE


@pytest.fixture()
def slow_writer(monkeypatch):
    """Each batch takes 0.3 s to write, so a row is saved before the exit
    only if the exit waited for it."""
    import time

    from bacpack.history_writer import HistoryWriter

    real = HistoryWriter._write_batch

    def slow(self, batch):
        time.sleep(0.3)
        real(self, batch)

    monkeypatch.setattr(HistoryWriter, "_write_batch", slow)


def test_exit_process_flushes_the_history_writer_before_exiting(session_factory, slow_writer, monkeypatch):
    import logging

    from bacpack import history_writer
    from bacpack.log_capture import install_log_capture
    from bacpack.models import LogEntry

    seen = []

    def fake_exit(code):
        with session_factory() as session:
            seen.append((code, [r.message for r in session.query(LogEntry)]))

    monkeypatch.setattr(lifecycle.os, "_exit", fake_exit)
    install_log_capture(session_factory)
    try:
        logging.getLogger("bacpack.test").info("last words")
        lifecycle.exit_process(75)
    finally:
        logger = logging.getLogger("bacpack")
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logger.setLevel(logging.NOTSET)
        history_writer.close_current()

    assert seen == [(75, ["last words"])]


def test_flush_current_with_no_writer_returns_at_once(monkeypatch):
    import time

    from bacpack import history_writer

    monkeypatch.setattr(history_writer, "_current", None)
    started = time.perf_counter()
    assert history_writer.flush_current() is True
    assert time.perf_counter() - started < 0.5


def test_mark_running_and_running_web_roundtrip(monkeypatch):
    for name in (
        lifecycle.HTTPS_ENV,
        lifecycle.RUNNING_ADDRESS_ENV,
        lifecycle.RUNNING_PORT_ENV,
        lifecycle.RUNNING_FINGERPRINT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    lifecycle.mark_running("10.0.0.5", 8443, "AB:CD")
    try:
        assert lifecycle.https_only() is True
        assert lifecycle.running_web() == lifecycle.RunningWeb("10.0.0.5", 8443, "AB:CD")
    finally:
        import os

        for name in (
            lifecycle.HTTPS_ENV,
            lifecycle.RUNNING_ADDRESS_ENV,
            lifecycle.RUNNING_PORT_ENV,
            lifecycle.RUNNING_FINGERPRINT_ENV,
        ):
            os.environ.pop(name, None)


def test_running_web_is_none_without_markers_or_with_a_bad_port(monkeypatch):
    monkeypatch.delenv(lifecycle.RUNNING_ADDRESS_ENV, raising=False)
    monkeypatch.delenv(lifecycle.RUNNING_PORT_ENV, raising=False)
    monkeypatch.delenv(lifecycle.RUNNING_FINGERPRINT_ENV, raising=False)
    assert lifecycle.running_web() is None
    monkeypatch.setenv(lifecycle.RUNNING_ADDRESS_ENV, "0.0.0.0")
    monkeypatch.setenv(lifecycle.RUNNING_FINGERPRINT_ENV, "AB")
    assert lifecycle.running_web() is None
    monkeypatch.setenv(lifecycle.RUNNING_PORT_ENV, "eight")
    assert lifecycle.running_web() is None
    monkeypatch.setenv(lifecycle.RUNNING_PORT_ENV, "8000")
    assert lifecycle.running_web() == lifecycle.RunningWeb("0.0.0.0", 8000, "AB")


def test_https_only_follows_the_environment(monkeypatch):
    monkeypatch.delenv(lifecycle.HTTPS_ENV, raising=False)
    assert lifecycle.https_only() is False
    monkeypatch.setenv(lifecycle.HTTPS_ENV, "1")
    assert lifecycle.https_only() is True
    monkeypatch.setenv(lifecycle.HTTPS_ENV, "0")
    assert lifecycle.https_only() is False
