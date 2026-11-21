"""TrafficLog: the traffic log is per-device and relayed through SQLite.
"""
from __future__ import annotations

import time

import pytest

from bacpack.history_writer import HistoryWriter
from bacpack.models import Device, TrafficLogEntry
from bacpack.traffic import TrafficLog
from tests.history_lock import hold_history_lock


@pytest.fixture()
def writer(session_factory):
    w = HistoryWriter(session_factory)
    w.start()
    yield w
    w.close()


def test_add_without_a_writer_is_purely_in_memory_as_before(session_factory):
    log = TrafficLog()
    log.add("who-is", "1.2.3.4")

    assert len(log.recent()) == 1
    with session_factory() as session:
        assert session.query(TrafficLogEntry).count() == 0


def test_add_with_a_writer_also_persists_a_row(session_factory, writer):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    log = TrafficLog(writer=writer, device_id=1)
    log.add("who-is", "1.2.3.4")
    assert writer.flush()

    assert len(log.recent()) == 1
    with session_factory() as session:
        rows = session.query(TrafficLogEntry).filter_by(device_id=1).all()
        assert len(rows) == 1
        assert rows[0].event_type == "who-is"
        assert rows[0].detail == "1.2.3.4"


def test_add_keeps_the_newest_maxlen_persisted_rows(session_factory, writer):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    log = TrafficLog(maxlen=3, writer=writer, device_id=1)
    for i in range(5):
        log.add("who-is", str(i))
    assert writer.flush()

    with session_factory() as session:
        rows = (
            session.query(TrafficLogEntry)
            .filter_by(device_id=1)
            .order_by(TrafficLogEntry.id)
            .all()
        )
        assert [r.detail for r in rows] == ["2", "3", "4"]


def test_add_survives_a_writer_failure_and_retains_the_in_memory_entry(writer, monkeypatch):
    log = TrafficLog(writer=writer, device_id=1)

    def broken_put(row):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(writer, "put", broken_put)
    log.add("who-is", "1.2.3.4")

    assert [e["detail"] for e in log.recent()] == ["1.2.3.4"]


def test_add_returns_at_once_while_the_history_file_is_locked(session_factory, writer):
    log = TrafficLog(writer=writer, device_id=1)
    release = hold_history_lock(session_factory)
    try:
        started = time.perf_counter()
        log.add("who-is", "while locked")
        assert time.perf_counter() - started < 0.5
    finally:
        release()
    assert writer.flush()
    with session_factory() as session:
        assert [r.detail for r in session.query(TrafficLogEntry)] == ["while locked"]


def test_object_id_text_uses_the_uis_colon_notation():
    from bacpypes3.primitivedata import ObjectIdentifier

    from bacpack.traffic import object_id_text

    assert object_id_text(ObjectIdentifier("analog-value,1")) == "analog-value:1"
    assert object_id_text(ObjectIdentifier("device,17")) == "device:17"
