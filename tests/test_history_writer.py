"""HistoryWriter."""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from bacpack import history_writer
from bacpack.history_writer import HistoryWriter, LogRow, TrafficRow, produced_at
from bacpack.models import LogEntry, TrafficLogEntry
from tests.history_lock import hold_history_lock


def _log(message="m", *, device_id=None, level="INFO", seconds=None):
    return LogRow(
        device_id, level, "bacpack.test", message, produced_at(time.time() if seconds is None else seconds)
    )


def _traffic(detail, *, device_id=1, keep=500):
    return TrafficRow(device_id, "who-is", detail, produced_at(time.time()), keep)


def _messages(session_factory):
    with session_factory() as session:
        return [r.message for r in session.query(LogEntry).order_by(LogEntry.id)]


def _details(session_factory, device_id):
    with session_factory() as session:
        return [
            r.detail
            for r in session.query(TrafficLogEntry).filter_by(device_id=device_id).order_by(TrafficLogEntry.id)
        ]


@pytest.fixture()
def writer(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    yield w
    w.close()


def test_rows_are_written_in_put_order(writer, session_factory):
    for i in range(5):
        writer.put(_log(f"m{i}"))
    assert writer.flush()
    assert _messages(session_factory) == [f"m{i}" for i in range(5)]


def test_a_row_keeps_its_production_time(writer, session_factory):
    seconds = dt.datetime(2026, 3, 1, 10, 0, 0, 700000, tzinfo=dt.timezone.utc).timestamp()
    writer.put(_log("old", seconds=seconds))
    assert writer.flush()
    with session_factory() as session:
        assert session.query(LogEntry).one().timestamp == dt.datetime(2026, 3, 1, 10, 0, 0)


def test_a_burst_is_written_in_fewer_transactions_than_rows(session_factory):
    commits = []
    event.listen(session_factory.kw["bind"], "commit", lambda conn: commits.append(1))
    w = HistoryWriter(session_factory)
    for i in range(200):
        w.put(_log(f"m{i}"))
    w.start()
    assert w.flush()
    w.close()
    assert len(_messages(session_factory)) == 200
    assert len(commits) < 200


def test_put_returns_at_once_while_the_history_file_is_locked(writer, session_factory):
    release = hold_history_lock(session_factory)
    try:
        started = time.perf_counter()
        assert writer.put(_log("while locked"))
        assert time.perf_counter() - started < 0.5
    finally:
        release()
    assert writer.flush()
    assert _messages(session_factory) == ["while locked"]


def test_a_log_call_on_the_writer_thread_is_queued_not_recursed(writer, session_factory, monkeypatch):
    real = writer._write_batch
    calls = []

    def write_and_log(batch):
        if not calls:
            calls.append(1)
            writer.put(_log("from the writer thread"))
        real(batch)

    monkeypatch.setattr(writer, "_write_batch", write_and_log)
    writer.put(_log("first"))
    assert writer.flush(timeout=3)
    assert writer.flush(timeout=3)
    assert sorted(_messages(session_factory)) == ["first", "from the writer thread"]


def test_close_flushes_then_stops_the_thread(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    w.put(_log("last"))
    w.close()
    assert _messages(session_factory) == ["last"]
    assert not w._thread.is_alive()


def _locked(batch):
    raise OperationalError("INSERT", {}, Exception("database is locked"))


def test_flush_and_close_on_a_stopped_writer_return_at_once(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    w.close()
    started = time.perf_counter()
    assert w.flush(2.0) is False
    w.close(2.0)
    assert time.perf_counter() - started < 0.5


def test_close_holds_to_one_deadline(session_factory, monkeypatch):
    # A write that never finishes and a full queue: each wait inside close
    # used to take its own timeout.
    monkeypatch.setattr(history_writer, "MAX_QUEUED", 2)
    w = HistoryWriter(session_factory)
    stuck = threading.Event()
    monkeypatch.setattr(w, "_write_batch", lambda batch: stuck.wait(5))
    w.start()
    for i in range(4):
        w.put(_log(f"m{i}"))
    started = time.perf_counter()
    w.close(1.0)
    elapsed = time.perf_counter() - started
    stuck.set()
    assert elapsed < 1.4


def test_close_keeps_retrying_a_locked_batch_until_its_deadline(session_factory, monkeypatch):
    monkeypatch.setattr(history_writer, "LOCK_RETRY_SECONDS", 0.01)
    w = HistoryWriter(session_factory)
    real = w._write_batch
    cleared = threading.Event()
    threading.Timer(0.3, cleared.set).start()
    monkeypatch.setattr(w, "_write_batch", lambda batch: real(batch) if cleared.is_set() else _locked(batch))
    w.start()
    w.put(_log("kept"))
    w.close(3.0)
    assert _messages(session_factory) == ["kept"]


def test_close_gives_up_a_locked_batch_at_its_deadline(session_factory, monkeypatch):
    monkeypatch.setattr(history_writer, "LOCK_RETRY_SECONDS", 0.01)
    w = HistoryWriter(session_factory)
    monkeypatch.setattr(w, "_write_batch", _locked)
    w.start()
    w.put(_log("never written"))
    w.close(0.3)
    w._thread.join(2.0)
    assert not w._thread.is_alive()


def test_the_drop_summary_is_written_when_the_last_batch_ends_at_the_stop(session_factory, monkeypatch):
    monkeypatch.setattr(history_writer, "MAX_QUEUED", 3)
    w = HistoryWriter(session_factory, device_id=7)
    w.put(_log("a"))
    w.put(_log("b"))
    w._pending.put_nowait(history_writer._STOP)
    assert w.put(_traffic("over the bound")) is False
    w._run()
    assert _messages(session_factory) == [
        "a",
        "b",
        "0 log lines and 1 traffic entries were dropped: the log queue was full",
    ]


def test_rows_queued_behind_the_stop_are_written(session_factory):
    # A put that passed the closed check just before close queued its stop.
    w = HistoryWriter(session_factory)
    w.put(_log("before"))
    w._pending.put_nowait(history_writer._STOP)
    w._pending.put_nowait(_log("after"))
    w._run()
    assert _messages(session_factory) == ["before", "after"]


def test_a_lock_timeout_retries_the_batch_without_loss(writer, session_factory, monkeypatch):
    monkeypatch.setattr(history_writer, "LOCK_RETRY_SECONDS", 0.01)
    real = writer._write_batch
    attempts = []

    def locked_twice(batch):
        attempts.append(1)
        if len(attempts) <= 2:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        real(batch)

    monkeypatch.setattr(writer, "_write_batch", locked_twice)
    writer.put(_log("kept"))
    assert writer.flush(timeout=3)
    assert len(attempts) == 3
    assert _messages(session_factory) == ["kept"]


def test_another_error_drops_the_batch_and_reports_once_on_stderr(writer, session_factory, monkeypatch, capsys):
    real = writer._write_batch
    state = {"fail": 2}

    def boom(batch):
        if state["fail"]:
            state["fail"] -= 1
            raise ValueError("boom")
        real(batch)

    monkeypatch.setattr(writer, "_write_batch", boom)
    writer.put(_log("lost 1"))
    assert writer.flush()
    writer.put(_log("lost 2"))
    assert writer.flush()
    writer.put(_log("written"))
    assert writer.flush()
    assert capsys.readouterr().err.count("boom") == 1
    assert _messages(session_factory) == ["written"]


def test_a_full_queue_drops_counts_and_reports(session_factory, monkeypatch):
    monkeypatch.setattr(history_writer, "MAX_QUEUED", 3)
    w = HistoryWriter(session_factory)
    results = [
        w.put(_log("l0")),
        w.put(_traffic("t0")),
        w.put(_log("l1")),
        w.put(_log("l2")),
        w.put(_traffic("t1")),
    ]
    assert results == [True, True, True, False, False]
    assert w.dropped == (1, 1)
    w.start()
    assert w.flush()
    w.close()
    with session_factory() as session:
        rows = session.query(LogEntry).order_by(LogEntry.id).all()
    assert [r.message for r in rows] == [
        "l0",
        "l1",
        "1 log lines and 1 traffic entries were dropped: the log queue was full",
    ]
    assert rows[-1].level == "WARNING"
    assert w.dropped == (0, 0)


def test_put_after_close_is_dropped_without_raising(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    w.close()
    started = time.perf_counter()
    assert w.put(_log("late")) is False
    assert time.perf_counter() - started < 0.5
    assert w.dropped == (1, 0)


def test_traffic_keeps_each_devices_newest_rows(writer, session_factory):
    for i in range(7):
        writer.put(_traffic(f"d{i}", device_id=1, keep=3))
    for i in range(2):
        writer.put(_traffic(f"e{i}", device_id=2, keep=3))
    assert writer.flush()
    assert _details(session_factory, 1) == ["d4", "d5", "d6"]
    assert _details(session_factory, 2) == ["e0", "e1"]


def test_traffic_trim_runs_no_count(writer, session_factory):
    statements = []
    event.listen(
        session_factory.kw["bind"], "before_cursor_execute", lambda conn, cur, sql, *a: statements.append(sql)
    )
    for i in range(5):
        writer.put(_traffic(f"d{i}", keep=3))
    assert writer.flush()
    assert any("DELETE" in s for s in statements)
    assert not any("count(" in s.lower() for s in statements)


def test_traffic_trim_across_batches_keeps_the_cap(writer, session_factory):
    for i in range(4):
        writer.put(_traffic(f"a{i}", keep=3))
        assert writer.flush()
    assert _details(session_factory, 1) == ["a1", "a2", "a3"]
