"""The per-device change counter: SQLite triggers bump
device_runtime.change_seq for every change to config a running device reads,
and for a queued write -- and for nothing the device writes about itself."""
from __future__ import annotations

import re

import pytest

from sqlalchemy import delete, event

from bacpack import migrations
from bacpack.bacnet_connectors import BacnetConnectorRunner
from bacpack.db import init_db, make_engine
from bacpack.models import (
    BacnetObject,
    Connector,
    ConnectorMapping,
    ConnectorRuntime,
    ConnectorWriteMapping,
    Device,
    DeviceLink,
    DeviceRuntime,
    ObjectRuntime,
)
from bacpack.simulation_runner import SimulationRunner
from tests.sqlite_compat import drop_column

_SQUARE = {"kind": "square", "low": 10, "high": 20, "period_s": 100, "interval_s": 1}


def _seq(session_factory, device_id):
    with session_factory() as session:
        return session.get(DeviceRuntime, device_id).change_seq


def _seqs(session_factory):
    return (_seq(session_factory, 1), _seq(session_factory, 2))


def _two_devices(session_factory):
    with session_factory() as session:
        session.add_all([Device(id=1, name="A"), Device(id=2, name="B")])
        session.commit()


def _object(session_factory, device_id=1, instance=1, **fields):
    with session_factory() as session:
        obj = BacnetObject(
            device_id=device_id, object_type="analog-value", instance_number=instance, name=f"AV-{instance}", **fields
        )
        session.add(obj)
        session.commit()
        return obj.id


def _connector(session_factory, device_id=1):
    with session_factory() as session:
        connector = Connector(name=f"C{device_id}", type="bacnet", url="", device_id=device_id, remote_device_instance=9)
        session.add(connector)
        session.commit()
        return connector.id


def _read_mapping(connector_id, object_id):
    return ConnectorMapping(
        connector_id=connector_id, object_id=object_id, remote_object_type="analog-value", remote_object_instance=1
    )


def _write_mapping(connector_id, object_id):
    return ConnectorWriteMapping(
        connector_id=connector_id, object_id=object_id, remote_object_type="analog-value", remote_object_instance=2
    )


def _trigger_names(engine):
    with engine.connect() as connection:
        return sorted(
            row[0]
            for row in connection.exec_driver_sql("SELECT name FROM main.sqlite_master WHERE type = 'trigger'")
        )


def test_init_db_creates_every_trigger_exactly_once(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    init_db(engine)
    assert _trigger_names(engine) == sorted(migrations.TRIGGERS)


def test_an_upgraded_file_gets_the_column_and_the_triggers(tmp_path):
    # A file from before the triggers: no change_seq column, no triggers.
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with engine.begin() as connection:
        for name in migrations.TRIGGERS:
            connection.exec_driver_sql(f"DROP TRIGGER main.{name}")
        drop_column(connection, "device_runtime", "change_seq")

    init_db(engine)

    assert _trigger_names(engine) == sorted(migrations.TRIGGERS)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA main.table_info(device_runtime)")}
    assert "change_seq" in columns


def test_migrations_run_with_no_triggers_in_place(tmp_path, monkeypatch):
    """A migration that drops or rebuilds a table must never meet a trigger
    naming it: init_db drops them first and recreates them last."""
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    seen = []
    real = migrations.run_pending_migrations

    def recording(engine_, schema="main"):
        seen.append(_trigger_names(engine_))
        return real(engine_, schema)

    monkeypatch.setattr(migrations, "run_pending_migrations", recording)
    init_db(engine)

    assert seen and all(names == [] for names in seen)
    assert _trigger_names(engine) == sorted(migrations.TRIGGERS)


def test_object_insert_update_delete_bump_only_its_device(session_factory):
    _two_devices(session_factory)
    start = _seqs(session_factory)

    object_id = _object(session_factory)
    assert _seqs(session_factory) == (start[0] + 1, start[1])

    with session_factory() as session:
        session.get(BacnetObject, object_id).description = "edited"
        session.commit()
    assert _seqs(session_factory) == (start[0] + 2, start[1])

    with session_factory() as session:
        session.delete(session.get(BacnetObject, object_id))
        session.commit()
    assert _seqs(session_factory) == (start[0] + 3, start[1])


def test_moving_an_object_bumps_both_devices(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    start = _seqs(session_factory)
    with session_factory() as session:
        session.get(BacnetObject, object_id).device_id = 2
        session.commit()
    assert _seqs(session_factory) == (start[0] + 1, start[1] + 1)


def test_a_core_delete_bumps_the_device(session_factory):
    _two_devices(session_factory)
    _object(session_factory)
    start = _seqs(session_factory)
    with session_factory() as session:
        session.execute(delete(ObjectRuntime))
        session.execute(delete(BacnetObject).where(BacnetObject.device_id == 1))
        session.commit()
    assert _seqs(session_factory) == (start[0] + 1, start[1])


def test_connector_and_mapping_changes_bump_the_connectors_device(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    connector_id = _connector(session_factory)
    after_connector = _seqs(session_factory)

    with session_factory() as session:
        session.add(_read_mapping(connector_id, object_id))
        session.add(_write_mapping(connector_id, object_id))
        session.commit()
    assert _seqs(session_factory) == (after_connector[0] + 2, after_connector[1])

    with session_factory() as session:
        session.get(Connector, connector_id).poll_interval_seconds = 45
        session.commit()
    assert _seqs(session_factory) == (after_connector[0] + 3, after_connector[1])


def test_moving_a_mapping_between_connectors_bumps_both_devices(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    first = _connector(session_factory, 1)
    second = _connector(session_factory, 2)
    with session_factory() as session:
        mapping = _read_mapping(first, object_id)
        session.add(mapping)
        session.commit()
        mapping_id = mapping.id
    start = _seqs(session_factory)
    with session_factory() as session:
        session.get(ConnectorMapping, mapping_id).connector_id = second
        session.commit()
    assert _seqs(session_factory) == (start[0] + 1, start[1] + 1)


def test_queueing_a_write_bumps_and_clearing_it_does_not(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    start = _seq(session_factory, 1)

    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_feed_write(42.0)
        session.commit()
    assert _seq(session_factory, 1) == start + 1

    # PendingWritePoller._clear's compare-and-clear.
    with session_factory() as session:
        runtime = session.get(ObjectRuntime, object_id)
        runtime.pending_feed_value = None
        runtime.pending_feed_queued = False
        session.commit()
    assert _seq(session_factory, 1) == start + 1


def test_requeueing_while_queued_bumps_again(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_manual_write(1.0)
        session.commit()
    start = _seq(session_factory, 1)
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_manual_write(2.0)
        session.commit()
    assert _seq(session_factory, 1) == start + 1


def _config_seq(session_factory, device_id):
    with session_factory() as session:
        return session.get(DeviceRuntime, device_id).config_seq


def test_a_queued_write_moves_change_seq_but_not_config_seq(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    start = (_seq(session_factory, 1), _config_seq(session_factory, 1))
    with session_factory() as session:
        session.get(BacnetObject, object_id).queue_feed_write(42.0)
        session.commit()
    assert (_seq(session_factory, 1), _config_seq(session_factory, 1)) == (start[0] + 1, start[1])


def test_a_config_change_moves_both_counters(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    start = (_seq(session_factory, 1), _config_seq(session_factory, 1))
    with session_factory() as session:
        session.get(BacnetObject, object_id).description = "edited"
        session.commit()
    assert (_seq(session_factory, 1), _config_seq(session_factory, 1)) == (start[0] + 1, start[1] + 1)


def test_an_upgraded_file_gets_config_seq_with_a_sql_default(tmp_path):
    # A file from before config_seq: no config_seq column.
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with engine.begin() as connection:
        for name in migrations.TRIGGERS:
            connection.exec_driver_sql(f"DROP TRIGGER main.{name}")
        drop_column(connection, "device_runtime", "config_seq")

    init_db(engine)

    with engine.connect() as connection:
        columns = {row[1]: row for row in connection.exec_driver_sql("PRAGMA main.table_info(device_runtime)")}
    assert "config_seq" in columns
    assert columns["config_seq"][4] == "0"  # dflt_value
    assert _trigger_names(engine) == sorted(migrations.TRIGGERS)


def test_clearing_one_slot_while_the_other_is_queued_does_not_bump(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.queue_manual_write(1.0)
        obj.queue_feed_write(2.0)
        session.commit()
    start = _seq(session_factory, 1)

    # PendingWritePoller._clear of the manual slot; feed is still queued.
    with session_factory() as session:
        runtime = session.get(ObjectRuntime, object_id)
        runtime.pending_manual_value = None
        runtime.pending_manual_queued = False
        session.commit()
    assert _seq(session_factory, 1) == start


def test_a_devices_own_status_writes_do_not_bump(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    connector_id = _connector(session_factory)
    start = _seq(session_factory, 1)
    with session_factory() as session:
        session.get(ConnectorRuntime, connector_id).client_status = {"ok": True}
        session.get(ObjectRuntime, object_id).schedule_write_status = {"x": 1}
        runtime = session.get(DeviceRuntime, 1)
        runtime.process_status = "running"
        runtime.pending_clock_offset = 5.0
        session.commit()
    assert _seq(session_factory, 1) == start


def test_deleting_a_device_with_everything_still_works(session_factory):
    _two_devices(session_factory)
    object_id = _object(session_factory)
    connector_id = _connector(session_factory)
    with session_factory() as session:
        session.add(_read_mapping(connector_id, object_id))
        session.commit()
    start_other = _seq(session_factory, 2)
    with session_factory() as session:
        session.delete(session.get(Device, 1))
        session.commit()
    with session_factory() as session:
        assert session.get(Device, 1) is None
        assert session.get(DeviceRuntime, 1) is None
    assert _seq(session_factory, 2) == start_other


def _tables_read(engine, load):
    seen = set()

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.update(re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", statement))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        load()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return seen


# What a runner's load may read without a trigger: rows the device process
# itself writes (status, counters), which must never wake it.
_DEVICE_WRITTEN = {"device_runtime", "connector_runtime", "object_runtime", "history.trend_state"}


def test_every_table_a_runner_loads_has_a_trigger(session_factory):
    """A runner that starts reading a new table must get a trigger for it,
    or edits to that table would never reach a running device."""
    _two_devices(session_factory)
    object_id = _object(session_factory, simulation=_SQUARE)
    connector_id = _connector(session_factory)
    with session_factory() as session:
        session.add(_read_mapping(connector_id, object_id))
        session.add(_write_mapping(connector_id, object_id))
        session.commit()
    engine = session_factory.kw["bind"]
    triggered = {re.search(r"\bON main\.(\w+)", body).group(1) for body in migrations.TRIGGERS.values()}

    read = _tables_read(engine, BacnetConnectorRunner(session_factory, None, 1, client=None)._load)
    read |= _tables_read(engine, SimulationRunner(session_factory, None, 1)._load)
    from bacpack.live_config import ObjectReloader

    reloader = ObjectReloader.__new__(ObjectReloader)
    reloader.session_factory, reloader.device_id = session_factory, 1
    read |= _tables_read(engine, reloader._load)

    assert {"connectors", "bacnet_objects"} <= read  # the capture works
    assert read - triggered - _DEVICE_WRITTEN == set()


def test_a_failed_ensure_triggers_keeps_the_previous_set(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    # First, so the old code has already dropped everything when it fails.
    broken = {"bacpack_aa_broken": "AFTER INSERT ON main.no_such_table BEGIN SELECT 1; END"}
    monkeypatch.setattr(migrations, "TRIGGERS", {**broken, **migrations.TRIGGERS})
    with pytest.raises(Exception):
        migrations.ensure_triggers(engine)
    monkeypatch.undo()
    assert _trigger_names(engine) == sorted(migrations.TRIGGERS)


def test_a_fresh_file_gives_change_seq_a_sql_default(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with engine.connect() as connection:
        defaults = {row[1]: row[4] for row in connection.exec_driver_sql("PRAGMA main.table_info(device_runtime)")}
    assert defaults["change_seq"] == "0"


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", "A2"),
        ("description", "d"),
        ("location", "Roof"),
        ("vendor_identifier", 7),
        ("vendor_name", "V"),
        ("bind_address", "10.0.0.1/24"),
        ("backup_failure_timeout", 15),
    ],
)
def test_a_device_setting_edit_bumps_its_device(session_factory, field, value):
    _two_devices(session_factory)
    start = _seqs(session_factory)
    with session_factory() as session:
        setattr(session.get(Device, 1), field, value)
        session.commit()
    assert _seqs(session_factory) == (start[0] + 1, start[1])


def test_a_devices_own_device_row_writes_do_not_bump(session_factory):
    _two_devices(session_factory)
    start = _seq(session_factory, 1)
    with session_factory() as session:
        device = session.get(Device, 1)
        device.dcc_state = "disable"
        device.clock_offset_seconds = 5.0
        device.database_revision = 9
        session.commit()
    assert _seq(session_factory, 1) == start


# --- Device network role and links -------------------------------------------


def _three_devices(session_factory):
    with session_factory() as session:
        session.add_all([Device(id=1, name="A"), Device(id=2, name="B"), Device(id=3, name="C")])
        session.commit()


def _seqs3(session_factory):
    return tuple(_seq(session_factory, device_id) for device_id in (1, 2, 3))


def _link(session_factory, device_id, linked_device_id):
    with session_factory() as session:
        link = DeviceLink(device_id=device_id, linked_device_id=linked_device_id)
        session.add(link)
        session.commit()
        return link.id


def test_a_link_insert_and_delete_bump_the_bbmd_only(session_factory):
    _three_devices(session_factory)
    start = _seqs3(session_factory)

    link_id = _link(session_factory, 1, 2)
    assert _seqs3(session_factory) == (start[0] + 1, start[1], start[2])

    with session_factory() as session:
        session.delete(session.get(DeviceLink, link_id))
        session.commit()
    assert _seqs3(session_factory) == (start[0] + 2, start[1], start[2])


def test_a_linked_devices_bind_address_change_bumps_itself_and_its_bbmds(session_factory):
    _three_devices(session_factory)
    _link(session_factory, 1, 2)
    start = _seqs3(session_factory)

    with session_factory() as session:
        session.get(Device, 2).bind_address = "192.168.1.5/24:47811"
        session.commit()

    assert _seqs3(session_factory) == (start[0] + 1, start[1] + 1, start[2])


@pytest.mark.parametrize(
    "field, value",
    [("network_role", "foreign"), ("foreign_bbmd", "10.0.0.5"), ("foreign_ttl", 90), ("bbmd_peers", ["10.0.0.9"])],
)
def test_a_network_role_field_change_bumps_the_device(session_factory, field, value):
    _three_devices(session_factory)
    start = _seqs3(session_factory)
    with session_factory() as session:
        setattr(session.get(Device, 1), field, value)
        session.commit()
    assert _seqs3(session_factory) == (start[0] + 1, start[1], start[2])


def test_deleting_a_device_removes_its_links_on_either_side(session_factory):
    _three_devices(session_factory)
    _link(session_factory, 1, 2)
    _link(session_factory, 2, 3)
    _link(session_factory, 1, 3)
    start = _seqs3(session_factory)

    with session_factory() as session:
        session.execute(delete(DeviceRuntime).where(DeviceRuntime.device_id == 2))
        session.execute(delete(Device).where(Device.id == 2))
        session.commit()

    with session_factory() as session:
        remaining = sorted((link.device_id, link.linked_device_id) for link in session.query(DeviceLink))
    assert remaining == [(1, 3)]
    # Device 1 lost a link and so did 2 (gone), 3 was only ever linked to.
    assert _seq(session_factory, 1) == start[0] + 1
    assert _seq(session_factory, 3) == start[2]
