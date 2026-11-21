"""db.py setup. WAL mode is needed because the parent and every running
device's process read and write the same SQLite files at once.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from bacpack.db import make_engine


def test_make_engine_turns_on_wal_journal_mode(tmp_path):
    engine = make_engine(tmp_path / "wal-test.db")
    with engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    assert mode.lower() == "wal"


# --- Database split -----------------------------------------------------

import sqlite3
from pathlib import Path

from bacpack.db import BUSY_TIMEOUT_SECONDS, history_db_path


def test_history_db_path_sits_next_to_the_main_file():
    assert history_db_path(Path("data") / "bacpack.db") == Path("data") / "bacpack-history.db"
    assert history_db_path("/x/foo.sqlite") == Path("/x/foo-history.sqlite")
    # No suffix at all: still a different file next to it.
    assert history_db_path("/x/bacpack") == Path("/x/bacpack-history")


def test_every_connection_attaches_the_history_file(tmp_path):
    engine = make_engine(tmp_path / "main.db")
    with engine.connect() as connection:
        databases = {row[1]: row[2] for row in connection.execute(text("PRAGMA database_list")).all()}
    assert Path(databases["history"]) == tmp_path / "main-history.db"
    assert (tmp_path / "main-history.db").exists()


def test_both_files_are_in_wal_mode(tmp_path):
    engine = make_engine(tmp_path / "main.db")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA main.journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA history.journal_mode")).scalar_one().lower() == "wal"


def test_busy_timeout_is_set_explicitly(tmp_path):
    engine = make_engine(tmp_path / "main.db")
    with engine.connect() as connection:
        timeout_ms = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
    assert timeout_ms == BUSY_TIMEOUT_SECONDS * 1000 == 10000


def test_a_data_directory_with_a_space_works(tmp_path):
    data_dir = tmp_path / "BACpack data"
    engine = make_engine(data_dir / "bacpack.db")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE history.probe (x INTEGER)"))
        connection.commit()
    probe = sqlite3.connect(data_dir / "bacpack-history.db")
    try:
        found = probe.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchone()
    finally:
        probe.close()
    assert found == ("probe",)


# --- Database_Revision (DM-BR-B) ------------------------------------------

from bacpack.models import BacnetObject, Device


def _device_with_objects(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.objects = [
            BacnetObject(object_type="binary-value", instance_number=1, name="BV-1"),
            BacnetObject(object_type="binary-value", instance_number=2, name="BV-2"),
        ]
        session.add(device)
        session.commit()


def _revision(session_factory):
    with session_factory() as session:
        return session.get(Device, 1).database_revision


def test_new_device_starts_at_revision_1(session_factory):
    _device_with_objects(session_factory)
    assert _revision(session_factory) == 1


def test_revision_bumps_on_object_create(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        session.add(BacnetObject(device_id=1, object_type="binary-value", instance_number=3, name="BV-3"))
        session.commit()
    assert _revision(session_factory) == 2


def test_two_saves_that_read_the_same_revision_both_count(session_factory):
    """Two object creates in parallel can each read revision N before either
    commits; the bump is made in SQL, so neither overwrites the other."""
    _device_with_objects(session_factory)
    with session_factory() as first:
        device = first.get(Device, 1)  # held, as a request handler holds it
        assert device.database_revision == 1
        with session_factory() as second:
            second.add(BacnetObject(device_id=1, object_type="binary-value", instance_number=3, name="BV-3"))
            second.commit()
        first.add(BacnetObject(device_id=1, object_type="binary-value", instance_number=4, name="BV-4"))
        first.commit()
        assert first.get(Device, 1).database_revision == 3
    assert _revision(session_factory) == 3


def test_revision_bumps_on_object_delete(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        obj = session.query(BacnetObject).filter_by(name="BV-1").one()
        session.delete(obj)
        session.commit()
    assert _revision(session_factory) == 2


def test_revision_bumps_when_an_object_is_removed_from_the_collection(session_factory):
    # sync_objects_for_device deletes through device.objects.remove(...) --
    # a delete-orphan, which is not in session.deleted at before_flush.
    _device_with_objects(session_factory)
    with session_factory() as session:
        device = session.get(Device, 1)
        device.objects.remove(device.objects[0])
        session.commit()
    assert _revision(session_factory) == 2


def test_revision_bumps_on_rename(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        session.query(BacnetObject).filter_by(name="BV-1").one().name = "BV-One"
        session.commit()
    assert _revision(session_factory) == 2


@pytest.mark.parametrize(
    "column, value",
    [("instance_number", 7), ("object_type", "analog-value"), ("trended", True)],
)
def test_revision_bumps_on_an_object_list_change(session_factory, column, value):
    # 135 12.11.35: an object created, deleted, renamed or given a new
    # Object_Identifier; `trended` adds or removes a Trend Log.
    _device_with_objects(session_factory)
    with session_factory() as session:
        setattr(session.query(BacnetObject).filter_by(name="BV-1").one(), column, value)
        session.commit()
    assert _revision(session_factory) == 2


def test_revision_bumps_on_device_rename(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        session.get(Device, 1).name = "D-renamed"
        session.commit()
    assert _revision(session_factory) == 2


def test_revision_does_not_bump_on_a_device_value_edit(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        session.get(Device, 1).description = "changed"
        session.commit()
    assert _revision(session_factory) == 1


def test_value_only_edit_does_not_bump(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        obj = session.query(BacnetObject).filter_by(name="BV-1").one()
        obj.description = "changed"
        obj.name = "BV-1"  # re-set to the same value: not a rename
        session.commit()
    assert _revision(session_factory) == 1


def test_one_flush_with_several_changes_bumps_once(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        device = session.get(Device, 1)
        # no_autoflush: session.delete's cascade lazy-loads the object's own
        # relationships, which would otherwise autoflush the rename early.
        with session.no_autoflush:
            device.objects[0].name = "Renamed"
            session.delete(device.objects[1])
            session.add(BacnetObject(device_id=1, object_type="binary-value", instance_number=9, name="BV-9"))
        session.commit()
    assert _revision(session_factory) == 2


def test_deleting_a_device_does_not_fail(session_factory):
    _device_with_objects(session_factory)
    with session_factory() as session:
        session.delete(session.get(Device, 1))
        session.commit()
    with session_factory() as session:
        assert session.get(Device, 1) is None


# --- Compacting a mostly-free main file (database split) ------------------

import logging

from bacpack import db as db_module
from bacpack.db import vacuum_if_mostly_free


def _mostly_free_main(tmp_path):
    engine = make_engine(tmp_path / "big.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE main.filler (x BLOB)"))
        connection.execute(text(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 500) "
            "INSERT INTO main.filler SELECT randomblob(4000) FROM n"
        ))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE main.filler"))
    return engine


def test_a_mostly_free_main_file_is_compacted(tmp_path):
    engine = _mostly_free_main(tmp_path)
    with engine.connect() as connection:
        before = connection.execute(text("PRAGMA main.page_count")).scalar_one()
    assert vacuum_if_mostly_free(engine) is True
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA main.page_count")).scalar_one() < before / 4


def test_a_healthy_file_is_left_alone(tmp_path):
    engine = make_engine(tmp_path / "small.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE main.t (x INTEGER)"))
    assert vacuum_if_mostly_free(engine) is False


def test_a_busy_file_is_skipped_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_SECONDS", 0.2)
    engine = _mostly_free_main(tmp_path)
    blocker = sqlite3.connect(tmp_path / "big.db", timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("CREATE TABLE hold (x INTEGER)")
        with caplog.at_level(logging.WARNING, logger="bacpack.db"):
            assert vacuum_if_mostly_free(engine) is False
    finally:
        blocker.rollback()
        blocker.close()
    assert "could not compact" in caplog.text


# --- Startup rows -------------------------------------------------------------


def test_init_db_creates_the_history_counters_row(tmp_path):
    from bacpack.db import init_db, make_session_factory
    from bacpack.models import HistoryCounters

    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as session:
        counters = session.get(HistoryCounters, 1)
        assert counters is not None
        assert counters.trend_total_sample_count == 0


def test_init_db_creates_the_server_settings_row(tmp_path):
    from bacpack.db import init_db, make_session_factory
    from bacpack.models import ServerSettings, get_or_create_settings

    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM main.server_settings")).scalars().all() == [1]
    # The same defaults as the row get_or_create_settings would add.
    columns = [c.name for c in ServerSettings.__table__.columns]
    with engine.begin() as connection:
        startup = connection.execute(text("SELECT * FROM main.server_settings")).mappings().one()
        connection.execute(text("DELETE FROM main.server_settings"))
    with make_session_factory(engine)() as session:
        created = get_or_create_settings(session)
        assert {c: getattr(created, c) for c in columns} == {c: startup[c] for c in columns}


def test_retention_never_writes_the_main_file(tmp_path, monkeypatch):
    # A write to main here, even one rolled back, made a concurrent save that
    # had already read main fail at once with "database is locked".
    from bacpack.client_requests import prune_old_client_requests
    from bacpack.db import init_db, make_session_factory
    from bacpack.log_capture import prune_old_log_entries
    from bacpack.trending import prune_old_samples, resync_trend_total_sample_count

    path = tmp_path / "t.db"
    init_db(make_engine(path))
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_SECONDS", 0.2)
    factory = make_session_factory(make_engine(path))
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        for prune in (prune_old_samples, prune_old_log_entries, prune_old_client_requests, resync_trend_total_sample_count):
            prune(factory)
    finally:
        blocker.rollback()
        blocker.close()


def test_init_db_restores_missing_runtime_rows_with_defaults(tmp_path):
    from bacpack.db import init_db, make_session_factory
    from bacpack.models import Connector, ConnectorRuntime, DeviceRuntime, ObjectRuntime

    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        device = Device(id=1, name="D")
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        connector = Connector(name="C", type="bacnet", url="", device=device, remote_device_instance=2)
        session.add_all([device, obj, connector])
        session.commit()
        object_id, connector_id = obj.id, connector.id
    with engine.begin() as connection:
        for table in ("device_runtime", "object_runtime", "connector_runtime"):
            connection.exec_driver_sql(f"DELETE FROM main.{table}")

    init_db(engine)

    with factory() as session:
        runtime = session.get(DeviceRuntime, 1)
        assert runtime is not None and runtime.process_status == "stopped" and runtime.change_seq == 0
        queued = session.get(ObjectRuntime, object_id)
        assert queued is not None and queued.pending_feed_queued is False
        assert session.get(ConnectorRuntime, connector_id) is not None


def test_init_db_keeps_existing_runtime_rows(tmp_path):
    from bacpack.db import init_db, make_session_factory
    from bacpack.models import DeviceRuntime

    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
        session.get(DeviceRuntime, 1).process_status = "running"
        session.commit()

    init_db(engine)

    with factory() as session:
        assert session.get(DeviceRuntime, 1).process_status == "running"


def test_compacting_also_empties_the_wal(tmp_path):
    engine = _mostly_free_main(tmp_path)
    assert vacuum_if_mostly_free(engine) is True
    assert (tmp_path / "big.db-wal").stat().st_size == 0


# --- lock_both_files --------------------------------------------------------


def _factory(path):
    from bacpack.db import init_db, make_session_factory

    engine = make_engine(path)
    init_db(engine)
    return make_session_factory(engine)


def test_lock_both_files_holds_both_write_locks(tmp_path):
    from bacpack.db import lock_both_files

    path = tmp_path / "t.db"
    factory = _factory(path)
    with factory() as session:
        lock_both_files(session)
        for file in (path, history_db_path(path)):
            other = sqlite3.connect(file, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
            finally:
                other.close()
        session.rollback()


def test_lock_both_files_waits_for_a_lock_held_elsewhere(tmp_path):
    from bacpack.db import lock_both_files

    path = tmp_path / "t.db"
    factory = _factory(path)
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.3, holder.rollback)
    timer.start()
    try:
        with factory() as session:
            lock_both_files(session)  # waits on the busy timeout, doesn't fail
            session.rollback()
    finally:
        timer.join()
        holder.close()


def test_lock_both_files_after_a_write_raises(tmp_path):
    from sqlalchemy.exc import OperationalError

    from bacpack.db import lock_both_files
    from bacpack.models import ServerSettings

    factory = _factory(tmp_path / "t.db")
    with factory() as session:
        session.get(ServerSettings, 1).trend_soft_limit_mb = 5
        session.flush()
        with pytest.raises(OperationalError, match="within a transaction"):
            lock_both_files(session)


def test_a_session_closed_without_commit_releases_both_locks(tmp_path):
    # A save refused (404/409) after taking the lock just closes its session.
    from bacpack.db import lock_both_files

    path = tmp_path / "t.db"
    factory = _factory(path)
    with factory() as session:
        lock_both_files(session)
    for file in (path, history_db_path(path)):
        other = sqlite3.connect(file, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()
