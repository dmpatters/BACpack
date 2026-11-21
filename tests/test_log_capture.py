"""SQLiteLogHandler: application logging in the UI.
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from bacpack import history_writer
from bacpack.history_writer import HistoryWriter
from bacpack.log_capture import SQLiteLogHandler, install_log_capture, prune_old_log_entries
from bacpack.models import Device, LogEntry, get_or_create_settings
from tests.history_lock import hold_history_lock


@pytest.fixture(autouse=True)
def _reset_bacpack_logger():
    yield
    logger = logging.getLogger("bacpack")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.NOTSET)


@pytest.fixture()
def writer(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    yield w
    w.close()


def _bacpack_logger():
    return logging.getLogger("bacpack")


def _make_record(logger_name="bacpack.engine", level=logging.INFO, msg="hello %s", args=None):
    if args is None:
        args = ("world",) if msg == "hello %s" else ()
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_emit_persists_a_row_with_the_handlers_default_device_id(session_factory, writer):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    handler = SQLiteLogHandler(writer, device_id=1)
    handler.emit(_make_record())
    assert writer.flush()

    with session_factory() as session:
        rows = session.query(LogEntry).all()
        assert len(rows) == 1
        assert rows[0].device_id == 1
        assert rows[0].level == "INFO"
        assert rows[0].logger_name == "bacpack.engine"
        assert rows[0].message == "hello world"


def test_emit_prefers_extra_device_id_over_the_handlers_default(session_factory, writer):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.add(Device(id=2, name="E"))
        session.commit()

    # Handler has no default device (parent-process-wide handler); one
    # specific record is tagged with extra={"device_id": 2} the way
    # engine.py's device-scoped messages are.
    handler = SQLiteLogHandler(writer, device_id=None)
    record = _make_record(msg="device-specific")
    record.device_id = 2
    handler.emit(record)
    assert writer.flush()

    with session_factory() as session:
        row = session.query(LogEntry).one()
        assert row.device_id == 2
        assert row.message == "device-specific"


def test_emit_stores_none_device_id_when_neither_default_nor_extra_is_set(session_factory, writer):
    handler = SQLiteLogHandler(writer)
    handler.emit(_make_record(msg="global event"))
    assert writer.flush()

    with session_factory() as session:
        row = session.query(LogEntry).one()
        assert row.device_id is None


def test_emit_appends_exception_text_for_logger_exception_calls(session_factory, writer):
    handler = SQLiteLogHandler(writer)
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="bacpack.engine",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="something failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )
    handler.emit(record)
    assert writer.flush()

    with session_factory() as session:
        row = session.query(LogEntry).one()
        assert row.level == "ERROR"
        assert "something failed" in row.message
        assert "ValueError: boom" in row.message


def test_emit_does_not_raise_on_a_db_failure(writer, monkeypatch):
    handler = SQLiteLogHandler(writer)

    def broken_put(row):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(writer, "put", broken_put)
    # handleError's default behavior (print to stderr) is fine here -- the
    # only real requirement is that emit() itself never propagates.
    handler.emit(_make_record())  # must not raise


def test_install_log_capture_sets_info_level_and_installs_both_handlers(session_factory):
    install_log_capture(session_factory)
    logger = _bacpack_logger()

    assert logger.level == logging.INFO
    kinds = [type(h).__name__ for h in logger.handlers]
    assert "SQLiteLogHandler" in kinds
    assert "StreamHandler" in kinds
    stream_handlers = [h for h in logger.handlers if type(h) is logging.StreamHandler]
    assert stream_handlers[0].level == logging.WARNING


def test_install_log_capture_actually_captures_info_and_above(session_factory):
    install_log_capture(session_factory, device_id=1)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    logging.getLogger("bacpack.engine").info("hello from a real call site")
    assert history_writer.flush_current()

    with session_factory() as session:
        rows = session.query(LogEntry).all()
        assert len(rows) == 1
        assert rows[0].message == "hello from a real call site"
        assert rows[0].device_id == 1


def test_install_log_capture_does_not_capture_below_info(session_factory):
    install_log_capture(session_factory)
    logging.getLogger("bacpack.engine").debug("should not be stored")
    assert history_writer.flush_current()

    with session_factory() as session:
        assert session.query(LogEntry).count() == 0


def test_install_log_capture_is_idempotent_and_repoints_at_the_new_session_factory(
    session_factory, tmp_path
):
    from bacpack.db import init_db, make_engine, make_session_factory

    install_log_capture(session_factory, device_id=1)
    logger = _bacpack_logger()
    first_handler_count = len(logger.handlers)

    other_engine = make_engine(tmp_path / "other.db")
    init_db(other_engine)
    other_session_factory = make_session_factory(other_engine)

    install_log_capture(other_session_factory, device_id=2)
    # Still exactly one SQLiteLogHandler + one StreamHandler -- the first
    # call's handlers were replaced, not stacked alongside the new ones.
    assert len(logger.handlers) == first_handler_count

    logging.getLogger("bacpack.engine").info("goes to the new db only")
    assert history_writer.flush_current()
    with session_factory() as session:
        assert session.query(LogEntry).count() == 0
    with other_session_factory() as session:
        rows = session.query(LogEntry).all()
        assert len(rows) == 1
        assert rows[0].device_id == 2


def test_emit_returns_at_once_while_the_history_file_is_locked(session_factory):
    import time

    install_log_capture(session_factory)
    release = hold_history_lock(session_factory)
    try:
        started = time.perf_counter()
        logging.getLogger("bacpack.engine").info("while locked")
        assert time.perf_counter() - started < 0.5
    finally:
        release()
    assert history_writer.flush_current()
    with session_factory() as session:
        assert [r.message for r in session.query(LogEntry)] == ["while locked"]


def test_reinstall_while_the_old_writer_is_stuck_on_a_lock(session_factory, tmp_path, monkeypatch):
    import time

    from bacpack.db import init_db, make_engine, make_session_factory

    monkeypatch.setattr(history_writer, "EXIT_FLUSH_SECONDS", 0.3)
    install_log_capture(session_factory)
    release = hold_history_lock(session_factory)
    try:
        logging.getLogger("bacpack.engine").info("stuck behind the lock")
        other_engine = make_engine(tmp_path / "other.db")
        init_db(other_engine)
        other = make_session_factory(other_engine)
        started = time.perf_counter()
        install_log_capture(other)
        assert time.perf_counter() - started < 4.0
    finally:
        release()
    logging.getLogger("bacpack.engine").info("now in the second database")
    # Its own timeout: the default is EXIT_FLUSH_SECONDS, cut to 0.3 s above, and a
    # fresh file's first write can take longer on a loaded machine.
    assert history_writer.flush_current(timeout=10.0)
    with other() as session:
        assert [r.message for r in session.query(LogEntry)] == ["now in the second database"]


def test_prune_old_log_entries_respects_retention_days(session_factory):
    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.log_retention_days = 7
        session.add(
            LogEntry(
                device_id=None,
                level="INFO",
                logger_name="x",
                message="old",
                timestamp=now - dt.timedelta(days=10),
            )
        )
        session.add(
            LogEntry(
                device_id=None,
                level="INFO",
                logger_name="x",
                message="recent",
                timestamp=now - dt.timedelta(days=1),
            )
        )
        session.commit()

    deleted = prune_old_log_entries(session_factory)
    assert deleted == 1

    with session_factory() as session:
        remaining = session.query(LogEntry).all()
    assert [r.message for r in remaining] == ["recent"]


def test_prune_old_log_entries_keeps_everything_when_retention_is_none(session_factory):
    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.log_retention_days = None
        session.add(
            LogEntry(
                device_id=None,
                level="INFO",
                logger_name="x",
                message="ancient",
                timestamp=now - dt.timedelta(days=9999),
            )
        )
        session.commit()

    assert prune_old_log_entries(session_factory) == 0
    with session_factory() as session:
        assert session.query(LogEntry).count() == 1


def test_logs_page_logging_flushes_on_exit(session_factory):
    from bacpack.log_capture import logs_page_logging

    with logs_page_logging(session_factory):
        logging.getLogger("bacpack.x").info("written inside the block")
    assert not _bacpack_logger().handlers
    with session_factory() as session:
        assert any("written inside the block" in e.message for e in session.query(LogEntry))
