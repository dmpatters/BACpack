from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from bacpack.models import BacnetObject, Device, get_or_create_history_counters, get_or_create_settings


def test_create_device_with_objects(session):
    device = Device(id=1, name="Test Device")
    session.add(device)
    session.add(
        BacnetObject(
            device=device,
            object_type="analog-input",
            instance_number=1,
            name="AI-1",
            units="degreesFahrenheit",
        )
    )
    session.commit()

    fetched = session.get(Device, 1)
    assert fetched.name == "Test Device"
    assert len(fetched.objects) == 1
    assert fetched.objects[0].object_type == "analog-input"


def test_duplicate_object_identifier_rejected(session):
    device = Device(id=1, name="Test Device")
    session.add(device)
    session.add(
        BacnetObject(device=device, object_type="analog-input", instance_number=1, name="AI-1")
    )
    session.commit()

    session.add(
        BacnetObject(
            device=device, object_type="analog-input", instance_number=1, name="AI-1-dup"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_object_name_rejected(session):
    device = Device(id=1, name="Test Device")
    session.add(device)
    session.add(
        BacnetObject(device=device, object_type="analog-input", instance_number=1, name="AI-1")
    )
    session.commit()

    session.add(
        BacnetObject(device=device, object_type="analog-output", instance_number=1, name="AI-1")
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_multistate_state_text_round_trips(session):
    device = Device(id=1, name="Test Device")
    session.add(device)
    session.add(
        BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
        )
    )
    session.commit()

    fetched = session.get(Device, 1)
    assert fetched.objects[0].state_text == ["Off", "Low", "High"]


# Model-level defaults for concurrent multi-device hosting's new columns.


def test_device_process_tracking_columns_default_to_stopped_and_none(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.commit()
        session.refresh(device)

        assert device.runtime.process_status == "stopped"
        assert device.runtime.process_pid is None
        assert device.runtime.process_started_at is None
        assert device.runtime.process_error is None
        assert device.bind_address is None


def test_bacnet_object_pending_write_columns_default_to_none(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1"
        )
        session.add(obj)
        session.commit()
        session.refresh(obj)

        assert obj.runtime.pending_manual_queued is False and obj.runtime.pending_manual_value is None
        assert obj.runtime.pending_feed_queued is False and obj.runtime.pending_feed_value is None


def test_traffic_log_entry_round_trips_and_cascade_deletes_with_its_device(session_factory):
    from bacpack.models import TrafficLogEntry

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(TrafficLogEntry(device_id=1, event_type="who-is", detail="1.2.3.4"))
        session.commit()

        assert session.query(TrafficLogEntry).count() == 1

        session.delete(device)
        session.commit()
        assert session.query(TrafficLogEntry).count() == 0


def test_log_entry_round_trips_and_cascade_deletes_with_its_device(session_factory):
    from bacpack.models import LogEntry

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            LogEntry(device_id=1, level="INFO", logger_name="bacpack.engine", message="hello")
        )
        session.commit()

        assert session.query(LogEntry).count() == 1

        session.delete(device)
        session.commit()
        assert session.query(LogEntry).count() == 0


def test_log_entry_device_id_is_nullable_for_global_entries(session_factory):
    from bacpack.models import LogEntry

    with session_factory() as session:
        session.add(
            LogEntry(device_id=None, level="WARNING", logger_name="bacpack.connectors", message="hi")
        )
        session.commit()

        entry = session.query(LogEntry).one()
        assert entry.device_id is None
        assert entry.level == "WARNING"
        assert entry.logger_name == "bacpack.connectors"
        assert entry.message == "hi"


def test_server_settings_log_retention_days_defaults_to_14(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        assert settings.log_retention_days == 14


def test_bacnet_object_trend_sample_cap_defaults_to_none(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        session.commit()
        session.refresh(obj)
        assert obj.trend_sample_cap is None


def test_bacnet_object_trend_sample_cap_round_trips(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        obj = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1",
            trend_sample_cap=500,
        )
        session.add(obj)
        session.commit()
        session.refresh(obj)
        assert obj.trend_sample_cap == 500


def test_server_settings_trend_storage_columns_default(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        assert settings.trend_soft_limit_mb is None
        assert get_or_create_history_counters(session).trend_total_sample_count == 0


# --- BACnet client role -------------------------------------------


def _bacnet_connector_setup(session):
    from bacpack.models import ClientRequest, Connector, ConnectorMapping, ConnectorWriteMapping

    device = Device(id=1, name="D")
    obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
    connector = Connector(
        name="Remote", type="bacnet", url="", device=device, remote_device_instance=2
    )
    session.add_all(
        [
            device,
            obj,
            connector,
            ConnectorMapping(
                connector=connector,
                object=obj,
                remote_object_type="analog-input",
                remote_object_instance=1,
            ),
            ConnectorWriteMapping(
                connector=connector,
                object=obj,
                remote_object_type="analog-value",
                remote_object_instance=3,
            ),
            ClientRequest(device=device, kind="who-is", params={}),
        ]
    )
    session.commit()
    return device, obj, connector


def test_deleting_a_device_deletes_its_bacnet_connectors_and_client_requests(session):
    from bacpack.models import ClientRequest, Connector, ConnectorMapping, ConnectorWriteMapping

    device, _, _ = _bacnet_connector_setup(session)

    session.delete(device)
    session.commit()

    assert session.query(Connector).count() == 0
    assert session.query(ConnectorMapping).count() == 0
    assert session.query(ConnectorWriteMapping).count() == 0
    assert session.query(ClientRequest).count() == 0


def test_deleting_an_object_deletes_its_write_mappings_but_not_the_connector(session):
    from bacpack.models import Connector, ConnectorWriteMapping

    _, obj, _ = _bacnet_connector_setup(session)

    session.delete(obj)
    session.commit()

    assert session.query(ConnectorWriteMapping).count() == 0
    assert session.query(Connector).count() == 1


def test_new_connector_columns_have_their_defaults(session):
    from bacpack.models import ClientRequest, Connector, ConnectorMapping, ConnectorWriteMapping

    device, obj, connector = _bacnet_connector_setup(session)
    http = Connector(name="Http", url="http://example.invalid")
    session.add(http)
    session.commit()

    assert http.type == "http-poll"
    assert http.device_id is None
    assert http.rewrite_interval_seconds == 60
    assert http.runtime.client_status is None
    assert connector.device_id == 1
    assert session.query(ConnectorMapping).one().mode == "poll"
    write = session.query(ConnectorWriteMapping).one()
    assert (write.remote_property, write.priority, write.remote_array_index) == ("present-value", 8, None)
    request = session.query(ClientRequest).one()
    assert request.status == "pending"
    assert request.created_at is not None
    assert request.finished_at is None


# --- Database split -----------------------------------------------------

import sqlite3
import time

from bacpack.db import init_db, make_engine, make_session_factory
from bacpack.models import ClientRequest, LogEntry, TrafficLogEntry, TrendSample

_HISTORY_TABLES = {"trend_samples", "traffic_log_entries", "log_entries", "client_requests"}


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def test_history_tables_live_in_the_history_file(session_factory, tmp_path):
    assert _HISTORY_TABLES <= _tables(tmp_path / "test-history.db")
    assert not (_HISTORY_TABLES & _tables(tmp_path / "test.db"))


def test_deleting_a_device_removes_its_history_from_the_other_file(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.objects = [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
        session.add(device)
        session.commit()
        object_id = device.objects[0].id
        session.add_all([
            TrendSample(object_id=object_id, sequence=1, value=1.0),
            TrafficLogEntry(device_id=1, event_type="rx", detail="x"),
            LogEntry(device_id=1, level="INFO", logger_name="t", message="m"),
            ClientRequest(device_id=1, kind="who-is"),
        ])
        session.commit()
    with session_factory() as session:
        session.delete(session.get(Device, 1))
        session.commit()
    with session_factory() as session:
        for model in (TrendSample, TrafficLogEntry, LogEntry, ClientRequest):
            assert session.query(model).count() == 0


def test_a_config_write_commits_while_the_history_file_is_write_locked(session_factory, tmp_path):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
    blocker = sqlite3.connect(tmp_path / "test-history.db", timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO log_entries (level, logger_name, message, timestamp) "
            "VALUES ('INFO', 't', 'm', CURRENT_TIMESTAMP)"
        )
        started = time.monotonic()
        with session_factory() as session:
            session.get(Device, 1).name = "Renamed"
            session.commit()
        assert time.monotonic() - started < 1.0
    finally:
        blocker.rollback()
        blocker.close()


def test_a_deleted_history_file_comes_back_empty_on_the_next_start(tmp_path):
    path = tmp_path / "bacpack.db"
    engine = make_engine(path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.add(TrafficLogEntry(device_id=1, event_type="rx", detail="x"))
        session.commit()
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"bacpack-history.db{suffix}").unlink(missing_ok=True)

    engine = make_engine(path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        assert session.get(Device, 1).name == "D"
        assert session.query(TrafficLogEntry).count() == 0
        session.add(TrafficLogEntry(device_id=1, event_type="rx", detail="y"))
        session.commit()


from bacpack.models import DeviceRuntime


def test_a_new_device_gets_its_runtime_row(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
    with session_factory() as session:
        runtime = session.get(DeviceRuntime, 1)
        assert runtime.process_status == "stopped"
        assert runtime.process_pid is None and runtime.pending_clock_offset is None
        assert session.get(Device, 1).runtime is runtime


def test_a_device_built_with_a_runtime_keeps_it(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D", runtime=DeviceRuntime(process_status="running", process_pid=42)))
        session.commit()
    with session_factory() as session:
        assert session.get(DeviceRuntime, 1).process_pid == 42


def test_deleting_a_device_removes_its_runtime_row(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
    with session_factory() as session:
        session.delete(session.get(Device, 1))
        session.commit()
    with session_factory() as session:
        assert session.get(DeviceRuntime, 1) is None


def test_device_runtime_is_in_the_main_file(session_factory, tmp_path):
    assert "device_runtime" in _tables(tmp_path / "test.db")


from bacpack.models import Connector, ConnectorRuntime, ObjectRuntime


def test_new_objects_and_connectors_get_runtime_rows(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.objects = [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
        session.add(device)
        session.add(Connector(name="C", url="", type="bacnet", device=device))
        session.commit()
        object_id = device.objects[0].id
    with session_factory() as session:
        runtime = session.get(ObjectRuntime, object_id)
        assert runtime.pending_manual_queued is False and runtime.schedule_write_status is None
        assert session.query(ConnectorRuntime).one().client_status is None


def test_queued_writes_land_in_object_runtime(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        device.objects = [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
        session.add(device)
        session.commit()
        obj = device.objects[0]
        obj.queue_manual_write(21.5)
        obj.queue_feed_write(None)
        session.commit()
        object_id = obj.id
    with session_factory() as session:
        runtime = session.get(ObjectRuntime, object_id)
        assert (runtime.pending_manual_value, runtime.pending_manual_queued) == (21.5, True)
        assert (runtime.pending_feed_value, runtime.pending_feed_queued) == (None, True)


def test_a_new_main_file_does_not_inherit_a_surviving_history_file(tmp_path):
    """Deleting bacpack.db to start over must not hand the new config the
    old history: history rows key on plain ids, which the new config reuses
    """
    from bacpack.models import TrendState, get_or_create_history_counters

    path = tmp_path / "bacpack.db"
    engine = make_engine(path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=1, name="Old")
        device.objects = [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
        session.add(device)
        session.commit()
        object_id = device.objects[0].id
        session.add_all([
            TrendSample(object_id=object_id, sequence=1, value=1.0),
            TrafficLogEntry(device_id=1, event_type="rx", detail="old"),
            TrendState(object_id=object_id, next_sequence=2),
        ])
        get_or_create_history_counters(session).trend_total_sample_count = 1
        session.commit()
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"bacpack.db{suffix}").unlink(missing_ok=True)

    engine = make_engine(path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=1, name="New")
        device.objects = [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
        session.add(device)
        session.commit()
        assert device.objects[0].id == object_id  # the id really is reused
        assert session.query(TrendSample).count() == 0
        assert session.query(TrafficLogEntry).count() == 0
        assert session.query(TrendState).count() == 0
        assert get_or_create_history_counters(session).trend_total_sample_count == 0


def test_the_bacnet_instance_limit_is_written_once():
    """Every range check imports models.MAX_INSTANCE (a message may name the
    range through it), so the limit can't drift between save paths."""
    import re
    from pathlib import Path

    import bacpack
    from bacpack.models import MAX_INSTANCE

    assert MAX_INSTANCE == 4194302
    package = Path(bacpack.__file__).parent
    found = [
        f"{path.relative_to(package)}:{number}"
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"4_?194_?302", line)
    ]
    assert len(found) == 1 and found[0].startswith("models.py:"), found


def test_an_old_history_file_gains_the_clone_job_columns(tmp_path):
    import sqlalchemy as sa

    from bacpack import migrations
    from bacpack.db import make_engine
    from bacpack.models import Base

    engine = make_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE history.client_requests (id INTEGER PRIMARY KEY, device_id INTEGER, kind VARCHAR, "
                "params JSON, status VARCHAR, result JSON, error VARCHAR, created_at DATETIME, finished_at DATETIME)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO history.client_requests (id, device_id, kind, status) VALUES (1, 1, 'read', 'done')")
        )
    migrations.add_missing_columns(engine, Base.metadata, "history")
    with engine.connect() as connection:
        columns = {r[1] for r in connection.execute(sa.text('PRAGMA history.table_info("client_requests")')).all()}
        cancel = connection.execute(sa.text("SELECT cancel_requested FROM history.client_requests")).scalar()
    assert {"progress", "cancel_requested"} <= columns
    assert not cancel


def test_web_sessions_and_api_keys_tables_exist(session):
    from bacpack.models import ApiKey, WebSession

    session.add(WebSession(token_hash="a" * 64))
    session.add(ApiKey(name="k", key_hash="b" * 64, prefix="bpk_12345678"))
    session.commit()
    assert session.query(WebSession).count() == 1
    assert session.query(ApiKey).one().revoked_at is None


def test_server_settings_has_web_listen_address_column_with_a_server_default():
    from bacpack.models import ServerSettings

    column = ServerSettings.__table__.c.web_listen_address
    assert not column.nullable
    assert str(column.server_default.arg) == "'0.0.0.0'"
