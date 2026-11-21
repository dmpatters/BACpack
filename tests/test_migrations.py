from __future__ import annotations

import json
import types

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from bacpack import migrations
from bacpack.db import make_engine, make_session_factory
from tests.sqlite_compat import drop_column


def test_is_fresh_database_true_for_empty_file(tmp_path):
    engine = make_engine(tmp_path / "empty.db")
    assert migrations.is_fresh_database(engine) is True


def test_is_fresh_database_false_once_a_table_exists(tmp_path):
    engine = make_engine(tmp_path / "used.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY)"))
    assert migrations.is_fresh_database(engine) is False


def test_schema_version_defaults_to_zero_and_round_trips(tmp_path):
    engine = make_engine(tmp_path / "version.db")
    with engine.begin() as connection:
        assert migrations.get_schema_version(connection) == 0
        migrations.set_schema_version(connection, 3)
        assert migrations.get_schema_version(connection) == 3


def test_run_pending_migrations_applies_in_order_and_bumps_version(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "pending.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)"))

    applied = []

    def add_nickname(connection):
        connection.execute(text("ALTER TABLE widgets ADD COLUMN nickname TEXT"))
        applied.append("add_nickname")

    def add_priority(connection):
        connection.execute(
            text("ALTER TABLE widgets ADD COLUMN priority INTEGER NOT NULL DEFAULT 5")
        )
        applied.append("add_priority")

    monkeypatch.setattr(migrations, "MIGRATIONS", [add_nickname, add_priority])

    migrations.run_pending_migrations(engine)

    assert applied == ["add_nickname", "add_priority"]
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 2
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert {"nickname", "priority"} <= columns


def test_run_pending_migrations_only_applies_versions_after_current(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "partial.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY)"))
        migrations.set_schema_version(connection, 1)

    applied = []
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        [
            lambda connection: applied.append("v1"),  # already applied, must be skipped
            lambda connection: applied.append("v2"),
        ],
    )

    migrations.run_pending_migrations(engine)

    assert applied == ["v2"]
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 2


def test_run_pending_migrations_rolls_back_and_raises_on_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "broken.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY)"))

    def broken_migration(connection):
        connection.execute(text("ALTER TABLE widgets ADD COLUMN nickname TEXT"))
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [broken_migration])

    with pytest.raises(migrations.MigrationError, match="boom"):
        migrations.run_pending_migrations(engine)

    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 0
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert "nickname" not in columns  # the failed migration's DDL was rolled back


# --- A locked database is transient, not a failed migration -------------------


def _locked_error():
    import sqlite3

    return sa.exc.OperationalError("ALTER TABLE widgets", {}, sqlite3.OperationalError("database is locked"))


def _widgets_db(tmp_path, name):
    engine = make_engine(tmp_path / name)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY)"))
    return engine


def _short_lock_wait(monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_RETRY_SECONDS", 0.01)


def test_a_migration_locked_twice_is_retried_and_applied(tmp_path, monkeypatch):
    engine = _widgets_db(tmp_path, "locked-twice.db")
    _short_lock_wait(monkeypatch)
    attempts = []

    def add_nickname(connection):
        attempts.append(1)
        if len(attempts) <= 2:
            raise _locked_error()
        connection.execute(text("ALTER TABLE widgets ADD COLUMN nickname TEXT"))

    monkeypatch.setattr(migrations, "MIGRATIONS", [add_nickname])
    migrations.run_pending_migrations(engine)

    assert len(attempts) == 3
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 1
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()}
        assert "nickname" in columns


def test_a_migration_that_stays_locked_says_so_and_changes_nothing(tmp_path, monkeypatch):
    engine = _widgets_db(tmp_path, "stays-locked.db")
    _short_lock_wait(monkeypatch)

    def add_nickname(connection):
        connection.execute(text("ALTER TABLE widgets ADD COLUMN nickname TEXT"))
        raise _locked_error()

    monkeypatch.setattr(migrations, "MIGRATIONS", [add_nickname])
    with pytest.raises(migrations.MigrationError) as raised:
        migrations.run_pending_migrations(engine)

    message = str(raised.value)
    assert "locked by another process" in message
    assert "Nothing was changed" in message
    assert "direct file or code access" not in message
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 0
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()}
        assert "nickname" not in columns


def test_a_non_lock_migration_failure_is_not_retried(tmp_path, monkeypatch):
    engine = _widgets_db(tmp_path, "not-locked.db")
    _short_lock_wait(monkeypatch)
    attempts = []

    def broken(connection):
        attempts.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [broken])
    with pytest.raises(migrations.MigrationError, match="direct file or code access"):
        migrations.run_pending_migrations(engine)
    assert len(attempts) == 1


def _build_old_widgets_table(engine):
    metadata = sa.MetaData()
    sa.Table(
        "widgets",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'existing-row')"))


def test_add_missing_columns_adds_nullable_column(tmp_path):
    engine = make_engine(tmp_path / "nullable.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("nickname", sa.String, nullable=True),
    )

    migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT nickname FROM widgets WHERE id = 1")).one()
        assert row.nickname is None


def test_add_missing_columns_backfills_notnull_column_with_scalar_default(tmp_path):
    engine = make_engine(tmp_path / "notnull.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, default=5),
    )

    migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT priority FROM widgets WHERE id = 1")).one()
        assert row.priority == 5


def test_add_missing_columns_raises_for_notnull_column_without_default(tmp_path):
    engine = make_engine(tmp_path / "unsafe.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("required_new_field", sa.String, nullable=False),
    )

    with pytest.raises(migrations.MigrationError, match="required_new_field"):
        migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert "required_new_field" not in columns  # nothing partially applied


def test_add_missing_columns_raises_for_unique_column(tmp_path):
    engine = make_engine(tmp_path / "unique.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("serial", sa.String, nullable=True, unique=True),
    )

    with pytest.raises(migrations.MigrationError, match="serial"):
        migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert "serial" not in columns


def test_add_missing_columns_raises_for_indexed_column(tmp_path):
    engine = make_engine(tmp_path / "indexed.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("lookup_key", sa.String, nullable=True, index=True),
    )

    with pytest.raises(migrations.MigrationError, match="lookup_key"):
        migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert "lookup_key" not in columns


def test_add_missing_columns_is_a_noop_when_schema_already_matches(tmp_path):
    engine = make_engine(tmp_path / "nop.db")
    _build_old_widgets_table(engine)

    metadata = sa.MetaData()
    sa.Table(
        "widgets",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
    )

    migrations.add_missing_columns(engine, metadata)  # should not raise or change anything

    with engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM widgets WHERE id = 1")).one()
        assert row.name == "existing-row"


def test_add_missing_columns_escapes_string_defaults_with_quotes(tmp_path):
    """Test that string defaults containing single quotes are properly escaped
    via _sql_literal's quote-doubling mechanism, and backfilled values round-trip
    correctly (not just that the operation doesn't crash)."""
    engine = make_engine(tmp_path / "string_escape.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("author", sa.String, nullable=False, default="O'Brien"),
    )

    migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT author FROM widgets WHERE id = 1")).one()
        assert row.author == "O'Brien"  # exact value, proving quote-doubling worked


def test_add_missing_columns_skips_nonexistent_tables(tmp_path):
    """Test that add_missing_columns handles the case where metadata includes
    a table that doesn't exist in the database yet -- it should skip that table,
    not raise an error, and correctly handle the existing table."""
    engine = make_engine(tmp_path / "missing_table.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    # Add the existing widgets table with a new column
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("nickname", sa.String, nullable=True),
    )
    # Also declare a completely nonexistent table in metadata
    # (never create it, just declare its schema)
    sa.Table(
        "gadgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
    )

    # Should not raise, even though gadgets doesn't exist
    migrations.add_missing_columns(engine, new_metadata)

    # Verify the existing widgets table was correctly processed
    with engine.connect() as connection:
        row = connection.execute(text("SELECT nickname FROM widgets WHERE id = 1")).one()
        assert row.nickname is None
        # Verify gadgets was never created
        tables = connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='gadgets'"
            )
        ).all()
        assert len(tables) == 0


def test_add_missing_columns_raises_for_notnull_nonconstant_server_default(tmp_path):
    """Test that a NOT-NULL column with a non-constant server_default (e.g. func.now())
    raises MigrationError, since SQLite's ALTER TABLE ADD COLUMN rejects non-constant
    defaults even though CREATE TABLE accepts them. This is a real limitation of SQLite."""
    engine = make_engine(tmp_path / "nonconstant.db")
    _build_old_widgets_table(engine)

    new_metadata = sa.MetaData()
    sa.Table(
        "widgets",
        new_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    with pytest.raises(migrations.MigrationError, match="created_at"):
        migrations.add_missing_columns(engine, new_metadata)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()
        }
        assert "created_at" not in columns  # SQLite's own rejection means nothing was added


from bacpack.db import init_db


def test_init_db_fresh_database_skips_migrations_and_stamps_latest_version(tmp_path, monkeypatch):
    # Monkeypatch a non-empty MIGRATIONS so this test actually exercises the
    # version-stamping path -- against real shipped code (MIGRATIONS empty
    # today), get_schema_version()==len(MIGRATIONS) would trivially hold as
    # 0==0 even with no implementation at all, which wouldn't prove anything.
    monkeypatch.setattr(migrations, "MIGRATIONS", [lambda connection: None])
    engine = make_engine(tmp_path / "fresh.db")

    init_db(engine)

    with engine.connect() as connection:
        assert migrations.get_schema_version(connection) == 1
        # Sanity: real tables were actually built.
        columns = {
            row[1]
            for row in connection.execute(text('PRAGMA table_info("bacnet_objects")')).all()
        }
        assert "bacnet_writable" in columns


def test_init_db_existing_database_applies_pending_migration_and_heals_columns_with_real_data(
    tmp_path, monkeypatch
):
    from bacpack.models import BacnetObject, Device

    # Build a real, populated database at the current schema first --
    # matching a genuinely deployed instance, not an empty table.
    engine = make_engine(tmp_path / "populated.db")
    init_db(engine)

    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=1, name="Real Device")
        session.add(device)
        session.add(
            BacnetObject(
                device=device, object_type="analog-input", instance_number=1, name="AI-1"
            )
        )
        session.commit()

    # Simulate schema drift needing both mechanisms in the same call: an
    # explicit migration adding one column, plus a plain missing nullable
    # column the safety net should heal on its own.
    with engine.begin() as connection:
        drop_column(connection, "bacnet_objects", "schedule_priority")

    applied = []

    def add_site_code(connection):
        connection.execute(text("ALTER TABLE devices ADD COLUMN site_code TEXT"))
        applied.append("add_site_code")

    # Additive, not a replacement: init_db(engine) above already ran against
    # the real (now non-empty) MIGRATIONS on a fresh database and stamped
    # its version to len(real MIGRATIONS) -- replacing the list here with a
    # shorter one would make run_pending_migrations see current_version >=
    # len(MIGRATIONS) and skip add_site_code entirely.
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [add_site_code])

    init_db(engine)  # existing DB, non-empty MIGRATIONS, real data present

    assert applied == ["add_site_code"]
    with engine.connect() as connection:
        # len(migrations.MIGRATIONS), not a hardcoded 1: the list here is
        # additive (real entries + add_site_code), so "fully migrated" is
        # its current length, not literally 1.
        assert migrations.get_schema_version(connection) == len(migrations.MIGRATIONS)
        device_columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("devices")')).all()
        }
        assert "site_code" in device_columns
        object_columns = {
            row[1]
            for row in connection.execute(text('PRAGMA table_info("bacnet_objects")')).all()
        }
        assert "schedule_priority" in object_columns  # safety net healed it

    # Existing data survived both the migration and the safety net.
    with session_factory() as session:
        fetched = session.get(Device, 1)
        assert fetched.name == "Real Device"
        assert len(fetched.objects) == 1
        assert fetched.objects[0].name == "AI-1"


def test_init_db_propagates_migration_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "will_fail.db")
    init_db(engine)  # build the current schema first

    def broken_migration(connection):
        raise RuntimeError("simulated migration failure")

    # Additive, not a replacement -- see the comment in
    # test_init_db_existing_database_applies_pending_migration_and_heals_columns_with_real_data
    # for why: init_db(engine) above already stamped the fresh database's
    # version to len(real MIGRATIONS).
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [broken_migration])

    with pytest.raises(migrations.MigrationError, match="simulated migration failure"):
        init_db(engine)


def test_init_db_heals_a_real_missing_column_on_an_existing_database(tmp_path):
    path = tmp_path / "existing.db"
    engine = make_engine(path)
    init_db(engine)  # build the full current schema first

    # Simulate a database from before schedule_priority existed by dropping
    # it -- the shape of gap a release that adds a column causes.
    with engine.begin() as connection:
        drop_column(connection, "bacnet_objects", "schedule_priority")

    # Re-running init_db (exactly what happens on every real app startup)
    # should heal this via the auto-add-column safety net alone, with no
    # explicit migration required.
    init_db(engine)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text('PRAGMA table_info("bacnet_objects")')).all()
        }
        assert "schedule_priority" in columns


def test_migration_creates_trend_samples_timestamp_index_on_an_existing_database(tmp_path):
    from bacpack.migrations import _create_trend_samples_timestamp_index

    engine = make_engine(tmp_path / "index.db")
    with engine.begin() as connection:
        # Build the pre-this-feature trend_samples shape directly (no
        # timestamp index) -- mirrors a real existing deployment, not the
        # current model (which already declares the index for a FRESH db).
        connection.execute(
            text(
                "CREATE TABLE trend_samples ("
                "id INTEGER PRIMARY KEY, object_id INTEGER, sequence INTEGER, "
                "timestamp TIMESTAMP, value REAL)"
            )
        )

    with engine.begin() as connection:
        _create_trend_samples_timestamp_index(connection)

    with engine.connect() as connection:
        indexes = {
            row[1] for row in connection.execute(text('PRAGMA index_list("trend_samples")')).all()
        }
    assert "ix_trend_samples_timestamp" in indexes


def test_migration_creates_trend_samples_timestamp_index_is_idempotent(tmp_path):
    from bacpack.migrations import _create_trend_samples_timestamp_index

    engine = make_engine(tmp_path / "index_idempotent.db")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE trend_samples (id INTEGER PRIMARY KEY, timestamp TIMESTAMP)")
        )

    with engine.begin() as connection:
        _create_trend_samples_timestamp_index(connection)
        _create_trend_samples_timestamp_index(connection)  # must not raise


def test_migration_adds_and_backfills_trend_total_sample_count(tmp_path):
    from bacpack.migrations import _add_and_backfill_trend_total_sample_count

    engine = make_engine(tmp_path / "backfill.db")
    with engine.begin() as connection:
        # A pre-this-feature server_settings row (no trend_total_sample_count
        # column yet) plus real pre-existing trend history -- the exact
        # scenario the plain add_missing_columns safety net would get wrong
        # (it would default this column to 0, discarding the real count).
        connection.execute(text("CREATE TABLE server_settings (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO server_settings (id) VALUES (1)"))
        connection.execute(
            text(
                "CREATE TABLE trend_samples (id INTEGER PRIMARY KEY, object_id INTEGER, "
                "sequence INTEGER, timestamp TIMESTAMP, value REAL)"
            )
        )
        for i in range(7):
            connection.execute(
                text(
                    "INSERT INTO trend_samples (object_id, sequence, timestamp, value) "
                    f"VALUES (1, {i}, CURRENT_TIMESTAMP, {float(i)})"
                )
            )

    with engine.begin() as connection:
        _add_and_backfill_trend_total_sample_count(connection)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("server_settings")')).all()
        }
        assert "trend_total_sample_count" in columns
        row = connection.execute(
            text("SELECT trend_total_sample_count FROM server_settings WHERE id = 1")
        ).one()
        assert row.trend_total_sample_count == 7


def test_migration_backfill_is_defensive_if_column_already_exists(tmp_path):
    """If add_missing_columns (or a prior partial run) already added the
    column, this migration must not try to ALTER TABLE ADD COLUMN it again
    -- SQLite would raise "duplicate column name". Still re-backfills the
    real count regardless."""
    from bacpack.migrations import _add_and_backfill_trend_total_sample_count

    engine = make_engine(tmp_path / "already_added.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE server_settings (id INTEGER PRIMARY KEY, "
                "trend_total_sample_count INTEGER NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(text("INSERT INTO server_settings (id) VALUES (1)"))
        connection.execute(
            text(
                "CREATE TABLE trend_samples (id INTEGER PRIMARY KEY, object_id INTEGER, "
                "sequence INTEGER, timestamp TIMESTAMP, value REAL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO trend_samples (object_id, sequence, timestamp, value) "
                "VALUES (1, 0, CURRENT_TIMESTAMP, 1.0)"
            )
        )

    with engine.begin() as connection:
        _add_and_backfill_trend_total_sample_count(connection)  # must not raise

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT trend_total_sample_count FROM server_settings WHERE id = 1")
        ).one()
        assert row.trend_total_sample_count == 1


def test_both_trend_storage_migrations_are_registered_in_migrations_list():
    from bacpack import migrations as migrations_module
    from bacpack.migrations import (
        _add_and_backfill_trend_total_sample_count,
        _create_trend_samples_timestamp_index,
    )

    assert _create_trend_samples_timestamp_index in migrations_module.MIGRATIONS
    assert _add_and_backfill_trend_total_sample_count in migrations_module.MIGRATIONS


def test_init_db_backfills_log_retention_days_on_an_existing_database(tmp_path):
    """A pre-existing server_settings row (from before log_retention_days
    existed) must be healed to the documented 14-day default when the
    column is auto-added, not silently left as NULL ("keep forever"). This
    requires ServerSettings.log_retention_days to declare a real
    server_default -- a Python-side `default=` alone only applies to rows
    the ORM constructs itself, never to the ALTER TABLE ADD COLUMN safety
    net in add_missing_columns()."""
    from bacpack.models import get_or_create_settings

    path = tmp_path / "existing_settings.db"
    engine = make_engine(path)
    init_db(engine)  # build the full current schema first

    session_factory = make_session_factory(engine)
    with session_factory() as session:
        get_or_create_settings(session)  # create the real, singleton row
        session.commit()

    # Simulate a database from before log_retention_days existed by
    # dropping it -- the shape of gap a release that adds a column causes.
    with engine.begin() as connection:
        drop_column(connection, "server_settings", "log_retention_days")

    # Re-running init_db (exactly what happens on every real app startup)
    # should heal this via the auto-add-column safety net, backfilling the
    # existing row to the documented default of 14 -- not NULL.
    init_db(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT log_retention_days FROM server_settings WHERE id = 1")
        ).one()
        assert row.log_retention_days == 14


def test_init_db_backfills_trend_retention_days_on_an_existing_database(tmp_path):
    """Same gap as log_retention_days above, for trend_retention_days: a
    pre-existing server_settings row (from before this column existed, or
    re-added by the auto-add safety net) must be healed to the documented
    90-day default, not silently left as NULL ("keep forever"). Requires
    ServerSettings.trend_retention_days to declare a real server_default --
    a Python-side `default=` alone only applies to rows the ORM constructs
    itself, never to the ALTER TABLE ADD COLUMN safety net in
    add_missing_columns()."""
    from bacpack.models import get_or_create_settings

    path = tmp_path / "existing_trend_settings.db"
    engine = make_engine(path)
    init_db(engine)  # build the full current schema first

    session_factory = make_session_factory(engine)
    with session_factory() as session:
        get_or_create_settings(session)  # create the real, singleton row
        session.commit()

    # Simulate a database from before trend_retention_days existed by
    # dropping it.
    with engine.begin() as connection:
        drop_column(connection, "server_settings", "trend_retention_days")

    # Re-running init_db (exactly what happens on every real app startup)
    # should heal this via the auto-add-column safety net, backfilling the
    # existing row to the documented default of 90 -- not NULL.
    init_db(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT trend_retention_days FROM server_settings WHERE id = 1")
        ).one()
        assert row.trend_retention_days == 90


def test_alarming_columns_auto_added_to_existing_bacnet_objects_table(tmp_path):
    """A pre-alarming database gets every new alarming column added by
    add_missing_columns with no explicit migration needed -- confirms the
    Global Constraints reasoning about NOT NULL columns with a plain
    Python-side default rendering a safe DDL default."""
    db_path = tmp_path / "pre-alarming.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "bacnet_objects", "alarming_enabled")
        drop_column(conn, "bacnet_objects", "alarm_time_delay")
        drop_column(conn, "bacnet_objects", "alarm_notify_type")
        drop_column(conn, "bacnet_objects", "alarm_deadband")
        drop_column(conn, "bacnet_objects", "notification_class_instance")
    init_db(engine)  # re-run: add_missing_columns should heal it
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("bacnet_objects")')).all()
    names = {r[1] for r in rows}
    assert "alarming_enabled" in names
    assert "alarm_time_delay" in names
    assert "alarm_notify_type" in names
    assert "alarm_deadband" in names
    assert "notification_class_instance" in names


def test_dcc_state_column_auto_added_to_existing_devices_table(tmp_path):
    """A pre-device-management database gets the new dcc_state column added
    by add_missing_columns with no explicit migration needed -- same pattern
    as test_alarming_columns_auto_added_to_existing_bacnet_objects_table
    above."""
    db_path = tmp_path / "pre-dcc.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "devices", "dcc_state")
    init_db(engine)  # re-run: add_missing_columns should heal it
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("devices")')).all()
    names = {r[1] for r in rows}
    assert "dcc_state" in names


def test_init_db_heals_the_time_sync_columns_on_an_existing_database(tmp_path):
    """The four time-sync columns ride the add_missing_columns safety net --
    no MIGRATIONS entry. Also plants a pre-existing trend_samples row
    (predating both dropped columns, as an older database's history would be)
    and confirms add_missing_columns' NOT NULL DEFAULT backfills it to
    kind='value'/clock_offset=0.0 -- not just that the columns exist."""
    path = tmp_path / "existing.db"
    engine = make_engine(path)
    init_db(engine)
    # A real BacnetObject via the ORM (full current schema, whose other NOT
    # NULL columns already have Python-side defaults) rather than a raw
    # INSERT naming every column -- SQLite doesn't enforce the FK to a real
    # Device row by default, and only trend_samples' own two dropped
    # columns need to predate this feature for what this test checks.
    from bacpack.models import BacnetObject

    with make_session_factory(engine)() as session:
        session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()

    # Drop first, then insert: create_all() renders the new NOT NULL columns
    # without a DDL default (the defaults are Python-side), so a raw INSERT
    # that omits them would fail while they still exist.
    with engine.begin() as connection:
        for table, column, schema in (
            ("devices", "clock_offset_seconds", "main"),
            ("trend_samples", "clock_offset", "history"),
            ("trend_samples", "kind", "history"),
        ):
            drop_column(connection, table, column, schema)
        connection.execute(text("INSERT INTO devices (id, name, vendor_identifier, dcc_state) VALUES (1, 'D', 999, 'enable')"))
        connection.execute(
            text(
                "INSERT INTO trend_samples (object_id, sequence, timestamp, value) "
                "VALUES (1, 0, CURRENT_TIMESTAMP, 42.0)"
            )
        )

    init_db(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT clock_offset_seconds FROM devices WHERE id = 1")
        ).one()
        assert row == (0.0,)
        columns = {r[1] for r in connection.execute(text('PRAGMA table_info("trend_samples")')).all()}
        assert {"clock_offset", "kind"} <= columns
        sample = connection.execute(
            text("SELECT clock_offset, kind FROM trend_samples WHERE object_id = 1 AND sequence = 0")
        ).one()
        assert sample == (0.0, "value")


def test_event_detection_enable_column_auto_added(tmp_path):
    """alarm_event_detection_enable is added to an older database
    by add_missing_columns, defaulting to True, with no explicit migration."""
    db_path = tmp_path / "pre-0.19.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "bacnet_objects", "alarm_event_detection_enable")
    init_db(engine)
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("bacnet_objects")')).all()
    column = {r[1]: r for r in rows}["alarm_event_detection_enable"]
    assert column[4] in ("1", "TRUE", "true", "'1'")  # dflt_value


def test_init_db_heals_the_backup_restore_columns(tmp_path):
    """The two backup/restore Device columns ride add_missing_columns -- no
    MIGRATIONS entry."""
    db_path = tmp_path / "pre-backup.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "devices", "database_revision")
        drop_column(conn, "devices", "last_restore_time")
    init_db(engine)
    with engine.connect() as conn:
        names = {r[1] for r in conn.execute(text('PRAGMA table_info("devices")')).all()}
    assert {"database_revision", "last_restore_time"} <= names


def test_trend_reporting_columns_have_their_defaults(tmp_path):
    from bacpack.models import BacnetObject, Device

    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=599001, name="D")
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        session.commit()
        assert obj.trend_reporting_enabled is False
        assert obj.trend_notification_threshold == 10
        assert obj.trend_notification_class_instance is None
        assert obj.trend_event_enable is None
        assert obj.trend_notify_type == "event"
        assert obj.trend_event_detection_enable is True


def test_init_db_adds_bacnet_client_columns_and_tables_to_a_0_22_database(tmp_path):
    # An older connectors/connector_mappings pair, hand-built (SQLite can't
    # DROP a foreign-key column, so the usual "init then drop" trick can't
    # simulate the old schema here). The version stamp says every explicit
    # migration already ran, as on a real older install.
    engine = make_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE connectors (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, "
                "type VARCHAR NOT NULL, enabled BOOLEAN NOT NULL, url VARCHAR NOT NULL, "
                "poll_interval_seconds INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL, "
                "response_format VARCHAR NOT NULL, auth_type VARCHAR NOT NULL, "
                "auth_username VARCHAR, auth_password VARCHAR, auth_token VARCHAR, headers JSON)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO connectors (name, type, enabled, url, poll_interval_seconds, "
                "timeout_seconds, response_format, auth_type) "
                "VALUES ('Old', 'http-poll', 1, 'http://x', 30, 10, 'json', 'none')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE connector_mappings (id INTEGER PRIMARY KEY, connector_id INTEGER, "
                "object_id INTEGER UNIQUE, json_path VARCHAR, scale FLOAT NOT NULL, "
                "offset FLOAT NOT NULL, value_map JSON)"
            )
        )
        migrations.set_schema_version(connection, len(migrations.MIGRATIONS))

    init_db(engine)

    with engine.connect() as connection:
        def columns(table):
            return {row[1] for row in connection.execute(text(f'PRAGMA table_info("{table}")')).all()}

        assert {
            "device_id", "remote_device_instance", "remote_address",
            "rewrite_interval_seconds",
        } <= columns("connectors")
        assert {
            "remote_object_type", "remote_object_instance", "remote_property",
            "remote_array_index", "mode",
        } <= columns("connector_mappings")
        assert "priority" in columns("connector_write_mappings")
        assert "kind" in columns("client_requests")
        row = connection.execute(
            text("SELECT type, rewrite_interval_seconds, device_id FROM connectors")
        ).one()
        assert tuple(row) == ("http-poll", 60, None)


# --- add_missing_columns waits out a lock too ------------------------------
# This release's own columns are added this way, not by a MIGRATIONS entry.

import sqlite3
import threading


def _busy_widgets_db(tmp_path):
    path = tmp_path / "busy.db"
    engine = sa.create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY)"))
    metadata = sa.MetaData()
    sa.Table("widgets", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("nickname", sa.Text))
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # another process's write lock
    return engine, metadata, holder


def _columns(engine):
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(text('PRAGMA table_info("widgets")')).all()}


def test_add_missing_columns_waits_out_a_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_RETRY_SECONDS", 0.02)
    engine, metadata, holder = _busy_widgets_db(tmp_path)
    release = threading.Timer(0.3, holder.rollback)
    release.start()
    try:
        migrations.add_missing_columns(engine, metadata)
    finally:
        release.join()
        holder.close()
    assert "nickname" in _columns(engine)


def test_add_missing_columns_still_locked_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_RETRY_SECONDS", 0.02)
    engine, metadata, holder = _busy_widgets_db(tmp_path)
    try:
        with pytest.raises(migrations.MigrationError, match="locked by another process"):
            migrations.add_missing_columns(engine, metadata)
    finally:
        holder.rollback()
        holder.close()
    assert "nickname" not in _columns(engine)


def test_verify_tls_column_auto_added_on_for_existing_connectors(tmp_path):
    """connectors.verify_tls rides add_missing_columns; an existing
    connector comes up with certificate checking still on."""
    db_path = tmp_path / "pre-0.25.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "connectors", "verify_tls")
        conn.execute(text("INSERT INTO connectors (name, type, enabled, url, poll_interval_seconds, "
                          "timeout_seconds, response_format, auth_type, rewrite_interval_seconds) "
                          "VALUES ('old', 'http-poll', 1, 'https://x.invalid/', 30, 10, 'json', 'none', 60)"))
    init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT verify_tls FROM connectors WHERE name = 'old'")).scalar_one() == 1


def test_mapping_direction_column_auto_added_as_read(tmp_path):
    """connector_mappings.direction rides add_missing_columns; an
    existing mapping comes up as a plain read mapping."""
    db_path = tmp_path / "pre-0.26.db"
    engine = make_engine(db_path)
    init_db(engine)
    with engine.begin() as conn:
        drop_column(conn, "connector_mappings", "direction")
        conn.execute(text("INSERT INTO connectors (name, type, enabled, url, poll_interval_seconds, "
                          "timeout_seconds, response_format, auth_type, rewrite_interval_seconds, verify_tls) "
                          "VALUES ('old', 'http-poll', 1, 'http://x.invalid/', 30, 10, 'json', 'none', 60, 1)"))
        conn.execute(text("INSERT INTO devices (id, name, vendor_identifier, dcc_state, "
                          "clock_offset_seconds) VALUES (1, 'D', 999, 'enable', 0)"))
        conn.execute(text("INSERT INTO bacnet_objects (device_id, object_type, instance_number, name, "
                          "bacnet_writable, out_of_service_default, schedule_priority, trended, trend_mode, "
                          "trend_reporting_enabled, trend_notification_threshold, "
                          "trend_notify_type, trend_event_detection_enable, "
                          "alarming_enabled, alarm_time_delay, alarm_notify_type, alarm_deadband, "
                          "alarm_event_detection_enable) "
                          "VALUES (1, 'analog-value', 1, 'AV-1', 0, 0, 16, 0, 'interval', 0, 0, 'alarm', 0, "
                          "0, 0, 'alarm', 0, 0)"))
        conn.execute(text("INSERT INTO connector_mappings (connector_id, object_id, scale, offset, mode) "
                          "VALUES (1, 1, 1.0, 0.0, 'poll')"))
    init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT direction FROM connector_mappings")).scalar_one() == "read"


def test_migration_clears_invalid_multistate_value_relinquish_defaults(tmp_path):
    from bacpack.migrations import _clear_invalid_multistate_value_relinquish_defaults

    engine = make_engine(tmp_path / "msv_rd.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE bacnet_objects (id INTEGER PRIMARY KEY, object_type TEXT, "
                "state_text TEXT, relinquish_default TEXT)"
            )
        )
        rows = [
            (1, "multi-state-value", '["a", "b", "c"]', "2"),     # valid: kept
            (2, "multi-state-value", '["a", "b", "c"]', "3"),     # valid edge: kept
            (3, "multi-state-value", '["a", "b", "c"]', "9"),     # above the state count
            (4, "multi-state-value", '["a", "b", "c"]', "0"),     # below 1
            (5, "multi-state-value", '["a", "b", "c"]', "foo"),   # not a number
            (6, "multi-state-value", '["a", "b", "c"]', "2.5"),   # not a whole number
            (7, "multi-state-value", None, "1"),                  # no states at all
            (8, "multi-state-value", '["a", "b"]', None),         # nothing stored
            (9, "multi-state-output", '["a", "b"]', "9"),         # other type: untouched
            (10, "analog-input", None, "not-a-number"),           # other type: untouched
        ]
        for row in rows:
            connection.execute(
                text(
                    "INSERT INTO bacnet_objects (id, object_type, state_text, relinquish_default) "
                    "VALUES (:id, :t, :s, :r)"
                ),
                {"id": row[0], "t": row[1], "s": row[2], "r": row[3]},
            )

    with engine.begin() as connection:
        _clear_invalid_multistate_value_relinquish_defaults(connection)

    with engine.connect() as connection:
        result = dict(
            connection.execute(text("SELECT id, relinquish_default FROM bacnet_objects")).all()
        )
    assert result == {
        1: "2", 2: "3", 3: None, 4: None, 5: None, 6: None, 7: None, 8: None,
        9: "9", 10: "not-a-number",
    }


def test_msv_relinquish_default_migration_is_the_third_entry():
    from bacpack.migrations import _clear_invalid_multistate_value_relinquish_defaults

    assert migrations.MIGRATIONS[2] is _clear_invalid_multistate_value_relinquish_defaults


def test_msv_relinquish_default_migration_skips_a_table_without_the_columns(tmp_path):
    # Migrations run before add_missing_columns: a database old enough to
    # predate relinquish_default/state_text has nothing to clear.
    from bacpack.migrations import _clear_invalid_multistate_value_relinquish_defaults

    engine = make_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE bacnet_objects (id INTEGER PRIMARY KEY, object_type TEXT)"))
        connection.execute(text("INSERT INTO bacnet_objects (id, object_type) VALUES (1, 'multi-state-value')"))
    with engine.begin() as connection:
        _clear_invalid_multistate_value_relinquish_defaults(connection)


# --- Per-schema bookkeeping (database split) ----------------------------------


def test_each_file_keeps_its_own_schema_version(tmp_path):
    engine = make_engine(tmp_path / "two.db")
    with engine.begin() as connection:
        migrations.set_schema_version(connection, 7, "main")
        migrations.set_schema_version(connection, 2, "history")
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection, "main") == 7
        assert migrations.get_schema_version(connection, "history") == 2


def test_is_fresh_database_looks_at_one_file(tmp_path):
    engine = make_engine(tmp_path / "two.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE main.only_here (x INTEGER)"))
    assert migrations.is_fresh_database(engine, "main") is False
    assert migrations.is_fresh_database(engine, "history") is True


def test_history_migrations_run_against_the_history_file(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "two.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE history.widgets (id INTEGER PRIMARY KEY)"))

    def add_colour(connection):
        connection.execute(text('ALTER TABLE history."widgets" ADD COLUMN colour TEXT'))

    monkeypatch.setattr(migrations, "HISTORY_MIGRATIONS", [add_colour])
    migrations.run_pending_migrations(engine, "history")
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection, "history") == 1
        assert migrations.get_schema_version(connection, "main") == 0
        columns = {r[1] for r in connection.execute(text('PRAGMA history.table_info("widgets")')).all()}
    assert "colour" in columns


def test_history_migration_indexes_trend_samples_by_object_and_id(tmp_path):
    engine = make_engine(tmp_path / "two.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE history.trend_samples (id INTEGER PRIMARY KEY, object_id INTEGER, "
                "sequence INTEGER, timestamp TIMESTAMP, value REAL)"
            )
        )
        # The same name in main must not take the index.
        connection.execute(text("CREATE TABLE main.trend_samples (id INTEGER PRIMARY KEY, object_id INTEGER)"))

    migrations.run_pending_migrations(engine, "history")
    with engine.begin() as connection:
        migrations._index_trend_samples_by_object_and_id(connection)  # idempotent
        history = {r[1] for r in connection.execute(text('PRAGMA history.index_list("trend_samples")')).all()}
        main = {r[1] for r in connection.execute(text('PRAGMA main.index_list("trend_samples")')).all()}
        columns = [r[2] for r in connection.execute(text('PRAGMA history.index_info("ix_trend_samples_object_id")')).all()]
    assert "ix_trend_samples_object_id" in history
    assert "ix_trend_samples_object_id" not in main
    assert columns == ["object_id", "id"]


def test_a_trend_logs_rows_are_read_in_id_order_without_a_sort(tmp_path):
    # A ReadRange walks one log's rows by id a chunk at a time; without the
    # index each chunk sorted the whole log.
    engine = make_engine(tmp_path / "fresh.db")
    init_db(engine)
    with engine.connect() as connection:
        plan = [
            row[3]
            for row in connection.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT * FROM history.trend_samples "
                    "WHERE object_id = 1 AND id > 5 ORDER BY id LIMIT 128"
                )
            ).all()
        ]
    assert not any("TEMP B-TREE" in step for step in plan), plan


def test_add_missing_columns_only_touches_its_own_schema(tmp_path):
    engine = make_engine(tmp_path / "two.db")
    with engine.begin() as connection:
        # Same table name in both files, both missing `note`.
        connection.execute(text("CREATE TABLE main.notes (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE history.notes (id INTEGER PRIMARY KEY)"))
    metadata = sa.MetaData()
    sa.Table(
        "notes",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("note", sa.String, nullable=True),
        schema="history",
    )
    migrations.add_missing_columns(engine, metadata, "history")
    with engine.connect() as connection:
        history_columns = {r[1] for r in connection.execute(text('PRAGMA history.table_info("notes")')).all()}
        main_columns = {r[1] for r in connection.execute(text('PRAGMA main.table_info("notes")')).all()}
    assert "note" in history_columns
    assert "note" not in main_columns


def test_init_db_stamps_both_files_on_a_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setattr(
        migrations, "HISTORY_MIGRATIONS", [lambda connection: None, lambda connection: None]
    )
    engine = make_engine(tmp_path / "fresh.db")
    init_db(engine)
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection, "main") == len(migrations.MIGRATIONS)
        assert migrations.get_schema_version(connection, "history") == 2


# --- The database split upgrade -------------------------------------------

from bacpack.models import (
    BacnetObject, Connector, ConnectorRuntime, Device, DeviceRuntime, ObjectRuntime, TrendSample,
)

_OLD_COLUMNS = (
    ("bacnet_objects", "pending_manual_value", "JSON"),
    ("bacnet_objects", "pending_manual_queued", "BOOLEAN NOT NULL DEFAULT 0"),
    ("bacnet_objects", "pending_feed_value", "JSON"),
    ("bacnet_objects", "pending_feed_queued", "BOOLEAN NOT NULL DEFAULT 0"),
    ("bacnet_objects", "schedule_write_status", "JSON"),
    ("bacnet_objects", "trend_next_sequence", "INTEGER NOT NULL DEFAULT 1"),
    ("bacnet_objects", "trend_last_notify_record", "INTEGER NOT NULL DEFAULT 0"),
    ("bacnet_objects", "pending_value", "JSON"),
    ("bacnet_objects", "pending_value_priority", "INTEGER"),
    ("devices", "process_pid", "INTEGER"),
    ("devices", "process_started_at", "DATETIME"),
    ("devices", "process_boot_id", "VARCHAR"),
    ("devices", "process_started_since_boot", "FLOAT"),
    ("devices", "process_status", "VARCHAR NOT NULL DEFAULT 'stopped'"),
    ("devices", "process_error", "VARCHAR"),
    ("devices", "pending_clock_offset", "FLOAT"),
    ("connectors", "client_status", "JSON"),
    ("server_settings", "trend_total_sample_count", "INTEGER NOT NULL DEFAULT 0"),
)
_OLD_HISTORY_DDL = (
    "CREATE TABLE main.trend_samples (id INTEGER PRIMARY KEY, object_id INTEGER, sequence INTEGER, "
    "timestamp DATETIME, value FLOAT)",
    "CREATE TABLE main.traffic_log_entries (id INTEGER PRIMARY KEY, device_id INTEGER, timestamp DATETIME, "
    "event_type VARCHAR, detail VARCHAR)",
    "CREATE TABLE main.log_entries (id INTEGER PRIMARY KEY, device_id INTEGER, timestamp DATETIME, "
    "level VARCHAR, logger_name VARCHAR, message VARCHAR)",
    "CREATE TABLE main.client_requests (id INTEGER PRIMARY KEY, device_id INTEGER, kind VARCHAR, status VARCHAR)",
)


# The split drops columns with ALTER TABLE ... DROP COLUMN, which SQLite has
# from 3.35; an older SQLite refuses that upgrade with a message (tested below).
needs_drop_column = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="the split's upgrade of a pre-0.28.5 database needs SQLite 3.35 or newer to drop columns",
)


def _pre_split_database(tmp_path, *, omit=()):
    """A database shaped like one from before the split: built at the current
    schema with one device, two objects and a connector, then the runtime
    tables removed and the moved columns and old history tables put back in
    main, stamped at schema version 3. `omit` leaves some old columns out (an
    older DB)."""
    engine = make_engine(tmp_path / "old.db")
    init_db(engine)
    with make_session_factory(engine)() as session:
        device = Device(id=1, name="D")
        device.objects = [
            BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"),
            BacnetObject(object_type="schedule", instance_number=1, name="SCH-1"),
        ]
        session.add(device)
        session.add(Connector(name="C", url="", type="bacnet", device=device))
        session.commit()
    with engine.begin() as connection:
        migrations.drop_triggers(connection)  # a database from before the split has none
        for table in ("object_runtime", "device_runtime", "connector_runtime"):
            connection.execute(text(f"DROP TABLE main.{table}"))
        for table, column, ddl in _OLD_COLUMNS:
            if column not in omit:
                connection.execute(text(f'ALTER TABLE main."{table}" ADD COLUMN {column} {ddl}'))
        for ddl in _OLD_HISTORY_DDL:
            connection.execute(text(ddl))
        connection.execute(text("INSERT INTO main.trend_samples (object_id, sequence, value) VALUES (1, 1, 1.0)"))
        connection.execute(text(
            "UPDATE main.devices SET process_pid = 4242, process_started_at = '2026-10-04 12:00:00.000000', "
            "process_boot_id = 'boot', process_started_since_boot = 12.5, process_status = 'running'"
            + ("" if "pending_clock_offset" in omit else ", pending_clock_offset = 30.0")
        ))
        connection.execute(text(
            "UPDATE main.bacnet_objects SET pending_manual_value = '21.5', pending_manual_queued = 1 "
            "WHERE name = 'AV-1'"
        ))
        connection.execute(text(
            "UPDATE main.bacnet_objects SET schedule_write_status = '{\"t\": \"ok\"}' WHERE name = 'SCH-1'"
        ))
        connection.execute(text("UPDATE main.connectors SET client_status = '{\"state\": \"bound\"}'"))
        migrations.set_schema_version(connection, 3, "main")
    engine.dispose()
    return tmp_path / "old.db"


def _main_names(connection, kind="table"):
    return {r[0] for r in connection.execute(text(f"SELECT name FROM main.sqlite_master WHERE type = '{kind}'")).all()}


def _main_columns(connection, table):
    return {r[1] for r in connection.execute(text(f'PRAGMA main.table_info("{table}")')).all()}


@needs_drop_column
def test_the_split_moves_runtime_values_and_drops_the_old_columns_and_tables(tmp_path):
    engine = make_engine(_pre_split_database(tmp_path))
    init_db(engine)

    with engine.connect() as connection:
        assert migrations.get_schema_version(connection, "main") == len(migrations.MIGRATIONS)
        assert not ({"trend_samples", "traffic_log_entries", "log_entries", "client_requests"} & _main_names(connection))
        assert not ({"pending_manual_value", "pending_manual_queued", "pending_feed_value", "pending_feed_queued",
                     "schedule_write_status", "trend_next_sequence", "trend_last_notify_record",
                     "pending_value", "pending_value_priority"} & _main_columns(connection, "bacnet_objects"))
        assert not ({"process_pid", "process_started_at", "process_boot_id", "process_started_since_boot",
                     "process_status", "process_error", "pending_clock_offset"} & _main_columns(connection, "devices"))
        assert "client_status" not in _main_columns(connection, "connectors")
        assert "trend_total_sample_count" not in _main_columns(connection, "server_settings")

    with make_session_factory(engine)() as session:
        runtime = session.get(Device, 1).runtime
        assert (runtime.process_pid, runtime.process_status, runtime.process_boot_id) == (4242, "running", "boot")
        assert runtime.process_started_since_boot == 12.5 and runtime.pending_clock_offset == 30.0
        assert runtime.process_started_at is not None
        objects = {o.name: o for o in session.query(BacnetObject)}
        assert (objects["AV-1"].runtime.pending_manual_value, objects["AV-1"].runtime.pending_manual_queued) == (21.5, True)
        assert objects["SCH-1"].runtime.schedule_write_status == {"t": "ok"}
        assert objects["SCH-1"].runtime.pending_manual_queued is False
        assert session.query(Connector).one().runtime.client_status == {"state": "bound"}
        assert session.query(TrendSample).count() == 0  # history starts fresh


@needs_drop_column
def test_the_split_runs_once(tmp_path):
    engine = make_engine(_pre_split_database(tmp_path))
    init_db(engine)
    init_db(engine)
    with make_session_factory(engine)() as session:
        assert session.query(DeviceRuntime).count() == 1
        assert session.query(ObjectRuntime).count() == 2
        assert session.query(ConnectorRuntime).count() == 1


@needs_drop_column
def test_an_older_database_missing_a_moved_column_gets_its_default(tmp_path):
    engine = make_engine(_pre_split_database(tmp_path, omit=("pending_clock_offset", "pending_value", "pending_value_priority")))
    init_db(engine)
    with make_session_factory(engine)() as session:
        runtime = session.get(Device, 1).runtime
        assert runtime.pending_clock_offset is None
        assert runtime.process_status == "running"


def test_the_split_on_sqlite_without_drop_column_says_so_and_changes_nothing(tmp_path, monkeypatch):
    path = _pre_split_database(tmp_path)
    monkeypatch.setattr(
        migrations,
        "sqlite3",
        types.SimpleNamespace(
            sqlite_version_info=(3, 34, 1), sqlite_version="3.34.1", OperationalError=sqlite3.OperationalError
        ),
    )
    engine = make_engine(path)
    with pytest.raises(migrations.MigrationError) as raised:
        init_db(engine)
    message = str(raised.value)
    assert "from an old BACpack release" in message
    assert "needs SQLite 3.35 or newer (this Python has 3.34.1)" in message
    assert "database left at schema version 3" in message
    assert "DROP" not in message
    with engine.connect() as connection:
        assert migrations.get_schema_version(connection, "main") == 3
        assert "process_pid" in _main_columns(connection, "devices")


def test_the_split_is_registered_fourth():
    assert migrations.MIGRATIONS[3] is migrations._split_runtime_and_history_out_of_main


def test_tail_migration_adds_and_fills_the_columns(tmp_path):
    import datetime as dt

    from bacpack.db import init_db

    engine = make_engine(tmp_path / "tails.db")
    init_db(engine)
    tail_columns = ("ordered_from_id", "tail_last_id", "tail_max_timestamp", "tail_min_offset", "tail_max_offset")
    with engine.begin() as connection:
        for column in tail_columns:
            drop_column(connection, "trend_state", column, "history")
        migrations.set_schema_version(connection, len(migrations.HISTORY_MIGRATIONS) - 1, "history")
        start = dt.datetime(2026, 1, 1, 10, 0, 0)
        back = start - dt.timedelta(hours=2)
        stamps = {
            # Log 1: the device clock set back two hours after two rows.
            1: [start, start + dt.timedelta(minutes=1), back, back + dt.timedelta(minutes=1)],
            2: [start + dt.timedelta(minutes=i) for i in range(3)],
        }
        for object_id, rows in stamps.items():
            for sequence, stamp in enumerate(rows, start=1):
                connection.execute(
                    text(
                        "INSERT INTO history.trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
                        "VALUES (:o, :s, :t, 1.0, 0.0, 'value')"
                    ),
                    {"o": object_id, "s": sequence, "t": stamp.isoformat(sep=" ", timespec="microseconds")},
                )
            connection.execute(
                text("INSERT INTO history.trend_state (object_id, next_sequence, last_notify_record) VALUES (:o, :n, 0)"),
                {"o": object_id, "n": len(rows) + 1},
            )

    init_db(engine)

    with engine.connect() as connection:
        columns = {r[1] for r in connection.execute(text('PRAGMA history.table_info("trend_state")')).all()}
        assert set(tail_columns) <= columns
        ids = {
            object_id: [
                r[0]
                for r in connection.execute(
                    text("SELECT id FROM history.trend_samples WHERE object_id = :o ORDER BY id"), {"o": object_id}
                )
            ]
            for object_id in (1, 2)
        }
        rows = {
            r[0]: tuple(r[1:])
            for r in connection.execute(
                text(
                    "SELECT object_id, ordered_from_id, tail_last_id, tail_max_timestamp, tail_min_offset, "
                    "tail_max_offset FROM history.trend_state"
                )
            )
        }
    # Every log's tail starts at its newest row; older rows are head.
    assert rows[1][0] == ids[1][3] and rows[1][1] == ids[1][3]
    assert str(rows[1][2]).startswith("2026-01-01 08:01:00")
    assert rows[2][0] == ids[2][2] and rows[2][1] == ids[2][2]
    assert str(rows[2][2]).startswith("2026-01-01 10:02:00")
    assert rows[1][3:] == (0.0, 0.0) and rows[2][3:] == (0.0, 0.0)


def _database_before_network_roles(tmp_path, role, *, foreign_bbmd=None, foreign_ttl=60, peers=None):
    """A current database taken back to the schema before the per-device network
    role: no role columns on devices, the role in server_settings, two devices."""
    engine = make_engine(tmp_path / "roles.db")
    init_db(engine)
    with make_session_factory(engine)() as session:
        session.add_all([Device(id=1, name="A"), Device(id=2, name="B")])
        session.commit()
    with engine.begin() as connection:
        migrations.drop_triggers(connection)
        for column in ("network_role", "foreign_bbmd", "foreign_ttl", "bbmd_peers"):
            drop_column(connection, "devices", column)
        connection.execute(text("DROP TABLE main.device_links"))
        connection.execute(
            text(
                "UPDATE main.server_settings SET network_role = :role, foreign_bbmd = :bbmd, "
                "foreign_ttl = :ttl, bbmd_peers = :peers"
            ),
            {"role": role, "bbmd": foreign_bbmd, "ttl": foreign_ttl, "peers": None if peers is None else json.dumps(peers)},
        )
        migrations.set_schema_version(
            connection, migrations.MIGRATIONS.index(migrations._move_network_role_to_devices)
        )
    return engine


def _device_networks(engine):
    init_db(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, network_role, foreign_bbmd, foreign_ttl, bbmd_peers FROM main.devices ORDER BY id")
        ).all()
        links = connection.execute(text("SELECT count(*) FROM main.device_links")).scalar_one()
    return [(r[0], r[1], r[2], r[3], None if r[4] is None else json.loads(r[4])) for r in rows], links


def test_network_role_moves_to_every_device_foreign(tmp_path):
    engine = _database_before_network_roles(tmp_path, "foreign", foreign_bbmd="10.0.0.5:47808", foreign_ttl=120)
    rows, links = _device_networks(engine)
    assert rows == [(1, "foreign", "10.0.0.5:47808", 120, None), (2, "foreign", "10.0.0.5:47808", 120, None)]
    assert links == 0


def test_network_role_moves_to_every_device_bbmd_as_typed_peers(tmp_path):
    peers = ["192.168.1.200:47810", "10.0.0.9:47808"]
    engine = _database_before_network_roles(tmp_path, "bbmd", peers=peers)
    rows, links = _device_networks(engine)
    assert [(r[0], r[1], r[4]) for r in rows] == [(1, "bbmd", peers), (2, "bbmd", peers)]
    assert links == 0


@pytest.mark.parametrize(
    "role, fields",
    [("bbmd", {"peers": None}), ("bbmd", {"peers": []}), ("foreign", {"foreign_bbmd": None})],
)
def test_role_without_its_setting_becomes_normal(tmp_path, role, fields):
    engine = _database_before_network_roles(tmp_path, role, **fields)
    rows, _ = _device_networks(engine)
    assert [r[1] for r in rows] == ["normal", "normal"]


def test_normal_role_leaves_devices_normal(tmp_path):
    engine = _database_before_network_roles(tmp_path, "normal", foreign_bbmd="10.0.0.5", peers=["10.0.0.9"])
    rows, _ = _device_networks(engine)
    assert rows == [(1, "normal", None, 60, None), (2, "normal", None, 60, None)]


def test_the_network_role_move_is_registered_fifth():
    assert migrations.MIGRATIONS[4] is migrations._move_network_role_to_devices


# --- login mode -------------------------------------------------------------


def _old_settings_db(tmp_path, row_sql):
    engine = make_engine(tmp_path / "login.db")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE server_settings (id INTEGER PRIMARY KEY, auth_enabled BOOLEAN, "
            "auth_username VARCHAR, auth_password_hash VARCHAR)"
        ))
        if row_sql:
            connection.execute(text(row_sql))
    return engine


def _login_row(engine):
    with engine.begin() as connection:
        migrations._add_login_mode(connection)
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT login_mode, login_username, login_password_hash FROM server_settings WHERE id = 1"
        )).one()


def test_existing_database_migrates_to_login_mode_none(tmp_path):
    engine = _old_settings_db(tmp_path, "INSERT INTO server_settings (id, auth_enabled) VALUES (1, 0)")
    assert tuple(_login_row(engine)) == ("none", None, None)


def test_existing_database_that_lost_its_settings_row_still_gets_none(tmp_path):
    # The real schema: the row insert has to work with every column the
    # current models have, which a hand-built table doesn't show.
    engine = make_engine(tmp_path / "lost-row.db")
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM main.server_settings"))
    init_db(engine)
    with engine.connect() as connection:
        mode = connection.execute(text("SELECT login_mode FROM main.server_settings WHERE id = 1")).scalar_one()
    assert mode == "none"


def test_old_basic_auth_credentials_become_a_login(tmp_path):
    engine = _old_settings_db(
        tmp_path, "INSERT INTO server_settings VALUES (1, 1, 'admin', 'h')"
    )
    assert tuple(_login_row(engine)) == ("login", "admin", "h")


def test_auth_enabled_without_a_hash_becomes_none(tmp_path):
    engine = _old_settings_db(
        tmp_path, "INSERT INTO server_settings VALUES (1, 1, 'admin', NULL)"
    )
    assert tuple(_login_row(engine)) == ("none", None, None)


def test_login_migration_is_registered_and_rerunnable(tmp_path):
    assert migrations._add_login_mode in migrations.MIGRATIONS
    engine = _old_settings_db(tmp_path, "INSERT INTO server_settings VALUES (1, 1, 'a', 'h')")
    _login_row(engine)
    assert tuple(_login_row(engine)) == ("login", "a", "h")


def test_fresh_database_has_no_login_mode_chosen(tmp_path):
    engine = make_engine(tmp_path / "fresh-login.db")
    init_db(engine)
    with make_session_factory(engine)() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        assert settings.login_mode is None
        assert settings.login_epoch == 0


def test_old_auth_columns_are_kept(tmp_path):
    engine = make_engine(tmp_path / "keep.db")
    init_db(engine)
    with engine.connect() as connection:
        columns = {r[1] for r in connection.execute(text('PRAGMA main.table_info("server_settings")')).all()}
    assert {"auth_enabled", "auth_username", "auth_password_hash"} <= columns


def test_old_database_gains_web_listen_address_with_the_default(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    init_db(engine)
    from bacpack.models import get_or_create_settings

    with make_session_factory(engine)() as session:
        get_or_create_settings(session)
        session.commit()
    with engine.begin() as connection:
        drop_column(connection, "server_settings", "web_listen_address")
    init_db(engine)
    with engine.connect() as connection:
        value = connection.execute(text("SELECT web_listen_address FROM server_settings WHERE id = 1")).scalar_one()
    assert value == "0.0.0.0"


# --- a file saved by a newer release ------------------------------------------


def _set_version(db_file, version):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_file)) as connection:
        connection.execute(f"PRAGMA user_version = {int(version)}")
        connection.commit()


def _file_state(*paths):
    import hashlib
    import sqlite3
    from contextlib import closing

    state = []
    for path in paths:
        if path.exists():
            state.append(hashlib.sha256(path.read_bytes()).hexdigest())
            with closing(sqlite3.connect(path)) as connection:
                state.append(sorted(r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")))
    return state


@pytest.fixture()
def current_pair(tmp_path, _template_db):
    from bacpack.db import history_db_path
    from tests.conftest import _copy_template

    main = _copy_template(_template_db, tmp_path / "bacpack.db")
    return main, history_db_path(main)


def test_init_db_refuses_a_main_file_from_a_newer_release(current_pair):
    from bacpack.db import init_db

    main, _history = current_pair
    known = len(migrations.MIGRATIONS)
    _set_version(main, known + 1)
    engine = make_engine(main)
    with pytest.raises(migrations.DatabaseTooNew) as raised:
        init_db(engine)
    assert raised.value.schema == "main"
    assert raised.value.found == known + 1
    assert raised.value.known == known
    message = str(raised.value)
    assert "bacpack.db" in message and "newer BACpack" in message
    assert str(known + 1) in message and str(known) in message


def test_init_db_refuses_a_history_file_from_a_newer_release(current_pair):
    from bacpack.db import init_db

    main, history = current_pair
    _set_version(history, len(migrations.HISTORY_MIGRATIONS) + 1)
    with pytest.raises(migrations.DatabaseTooNew) as raised:
        init_db(make_engine(main))
    assert raised.value.schema == "history"
    assert "bacpack-history.db" in str(raised.value)


def test_init_db_refuses_a_deleted_main_beside_a_newer_history(current_pair):
    from bacpack.db import init_db

    main, history = current_pair
    _set_version(history, len(migrations.HISTORY_MIGRATIONS) + 1)
    for suffix in ("", "-wal", "-shm"):
        main.with_name(main.name + suffix).unlink(missing_ok=True)
    before = _file_state(history)
    with pytest.raises(migrations.DatabaseTooNew):
        init_db(make_engine(main))
    assert _file_state(history) == before


def test_a_refused_init_db_changes_nothing(current_pair):
    from bacpack.db import init_db

    main, history = current_pair
    _set_version(main, len(migrations.MIGRATIONS) + 1)
    before = _file_state(main, history)
    engine = make_engine(main)
    with pytest.raises(migrations.DatabaseTooNew):
        init_db(engine)
    engine.dispose()
    assert _file_state(main, history) == before
    assert before[1], "the template has change-counter triggers to lose"


def test_database_too_new_is_not_a_migration_error():
    assert not issubclass(migrations.DatabaseTooNew, migrations.MigrationError)


def test_a_file_at_the_known_version_is_accepted(current_pair):
    from bacpack.db import init_db

    main, _history = current_pair
    engine = make_engine(main)
    init_db(engine)
    engine.dispose()


def test_drop_column_refuses_a_column_another_tables_trigger_uses(tmp_path):
    engine = make_engine(tmp_path / "drop.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE a (id INTEGER PRIMARY KEY, x INTEGER, y INTEGER)"))
        connection.execute(text("CREATE TABLE b (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TRIGGER t AFTER INSERT ON b BEGIN UPDATE a SET x = x + 1; END"))
        with pytest.raises(AssertionError, match="uses a.x"):
            drop_column(connection, "a", "x")
        drop_column(connection, "a", "y")
        assert _main_columns(connection, "a") == {"id", "x"}
        assert "t" in _main_names(connection, "trigger")


# --- all-day recipient windows ------------------------------------------------


def _nc_db(tmp_path, rows, *, extra_columns=""):
    engine = make_engine(tmp_path / "nc.db")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE bacnet_objects (id INTEGER PRIMARY KEY, object_type VARCHAR, "
            f"notification_recipient_list JSON{extra_columns})"
        ))
        for object_id, object_type, value in rows:
            connection.execute(
                text("INSERT INTO bacnet_objects (id, object_type, notification_recipient_list) VALUES (:i, :t, :v)"),
                {"i": object_id, "t": object_type, "v": value if value is None or isinstance(value, str) else json.dumps(value)},
            )
    return engine


def _entry(from_time, to_time, **extra):
    return {
        "recipient": {"kind": "address", "address": "192.168.1.77"},
        "valid_days": [True] * 7, "from_time": from_time, "to_time": to_time,
        "transitions": [True] * 3, "confirmed": False, "process_identifier": 5, **extra,
    }


def _widen(engine):
    with engine.begin() as connection:
        migrations._widen_all_day_recipient_windows(connection)
    with engine.connect() as connection:
        return {
            row[0]: row[1] if row[1] is None else json.loads(row[1])
            for row in connection.execute(text("SELECT id, notification_recipient_list FROM bacnet_objects"))
        }


@pytest.mark.parametrize("start", ["00:00", "00:00:00", "00:00:00.00"])
@pytest.mark.parametrize("end", ["23:59", "23:59:00", "23:59:00.00"])
def test_all_day_recipient_windows_ending_at_23_59_are_widened(tmp_path, start, end):
    engine = _nc_db(tmp_path, [(1, "notification-class", [_entry(start, end)])])
    assert _widen(engine)[1][0]["to_time"] == "23:59:59.99"
    assert _widen(engine)[1][0]["from_time"] == start


def test_other_windows_are_left_alone(tmp_path):
    kept = [
        _entry("06:00", "23:59"), _entry("00:00", "18:00"), _entry("00:00:00.00", "23:59:59.99"),
        {**_entry("00:00", "23:59"), "recipient": {"kind": "device", "instance_number": 7}},
    ]
    engine = _nc_db(tmp_path, [(1, "notification-class", kept[:3]), (2, "notification-class", [_entry("00:00", "23:59"), kept[1]])])
    result = _widen(engine)
    assert result[1] == kept[:3]
    assert [e["to_time"] for e in result[2]] == ["23:59:59.99", "18:00"]


def test_the_widen_migration_ignores_other_object_types_and_bad_json(tmp_path):
    listed = [_entry("00:00", "23:59")]
    engine = _nc_db(tmp_path, [
        (1, "analog-input", listed), (2, "notification-class", None),
        (3, "notification-class", "not json"), (4, "notification-class", "{}"),
    ])
    with engine.connect() as connection:
        before = connection.execute(text("SELECT id, notification_recipient_list FROM bacnet_objects")).all()
    with engine.begin() as connection:
        migrations._widen_all_day_recipient_windows(connection)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, notification_recipient_list FROM bacnet_objects")).all() == before


def test_the_widen_migration_is_registered_and_rerunnable(tmp_path):
    assert migrations.MIGRATIONS.index(migrations._widen_all_day_recipient_windows) == 6
    engine = _nc_db(tmp_path, [(1, "notification-class", [_entry("00:00", "23:59")])])
    first = _widen(engine)
    assert _widen(engine) == first


def test_the_widen_migration_stays_instant_on_a_big_table(tmp_path):
    import time

    rows = [(i, "analog-value", None) for i in range(1, 20001)]
    rows += [(30000 + i, "notification-class", [_entry("00:00", "23:59")]) for i in range(3)]
    engine = _nc_db(tmp_path, rows)
    started = time.perf_counter()
    result = _widen(engine)
    assert time.perf_counter() - started < 2
    assert all(result[30000 + i][0]["to_time"] == "23:59:59.99" for i in range(3))


def test_an_old_database_without_the_recipient_column_is_a_no_op(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE bacnet_objects (id INTEGER PRIMARY KEY, object_type VARCHAR)"))
        connection.execute(text("INSERT INTO bacnet_objects VALUES (1, 'notification-class')"))
        migrations._widen_all_day_recipient_windows(connection)


# --- a Notification Class can't be trended -----------------------------------


def _trended_db(tmp_path, with_reporting=True):
    engine = make_engine(tmp_path / "trended.db")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE bacnet_objects (id INTEGER PRIMARY KEY, object_type VARCHAR, trended BOOLEAN"
            + (", trend_reporting_enabled BOOLEAN)" if with_reporting else ")")
        ))
        rows = [(1, "notification-class", 1, 1), (2, "notification-class", 0, 0), (3, "analog-value", 1, 1)]
        for object_id, object_type, trended, reporting in rows:
            if with_reporting:
                connection.execute(text("INSERT INTO bacnet_objects VALUES (:i, :t, :a, :b)"),
                                   {"i": object_id, "t": object_type, "a": trended, "b": reporting})
            else:
                connection.execute(text("INSERT INTO bacnet_objects VALUES (:i, :t, :a)"),
                                   {"i": object_id, "t": object_type, "a": trended})
    return engine


def test_trended_flag_is_cleared_on_notification_classes_only(tmp_path):
    engine = _trended_db(tmp_path)
    with engine.begin() as connection:
        migrations._clear_trended_on_notification_classes(connection)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, trended, trend_reporting_enabled FROM bacnet_objects ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(1, 0, 0), (2, 0, 0), (3, 1, 1)]


def test_the_clear_migration_works_without_the_reporting_column(tmp_path):
    engine = _trended_db(tmp_path, with_reporting=False)
    with engine.begin() as connection:
        migrations._clear_trended_on_notification_classes(connection)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, trended FROM bacnet_objects ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(1, 0), (2, 0), (3, 1)]


def test_the_clear_migration_is_registered_after_the_widen_migration():
    assert migrations.MIGRATIONS[-1] is migrations._clear_trended_on_notification_classes
    assert migrations.MIGRATIONS[-2] is migrations._widen_all_day_recipient_windows


def test_a_database_with_a_trended_notification_class_opens_and_builds_after_the_migration(tmp_path):
    from bacpack import object_config
    from bacpack.db import init_db
    from bacpack.models import BacnetObject, Device

    engine = make_engine(tmp_path / "upgrade.db")
    init_db(engine)
    with make_session_factory(engine)() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(
            device=device, object_type="notification-class", instance_number=1, name="NC-1",
            trended=True, trend_reporting_enabled=True,
        ))
        session.commit()
    with engine.begin() as connection:
        migrations.set_schema_version(connection, len(migrations.MIGRATIONS) - 1)
    init_db(engine)
    with make_session_factory(engine)() as session:
        device = session.get(Device, 1)
        rows = [object_config.row_from_object(o) for o in device.objects]
        assert [o.trended for o in device.objects] == [False]
        assert object_config.check_device(device.name, rows) is None
