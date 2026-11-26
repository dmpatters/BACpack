"""Hand-rolled, forward-only SQLite schema migrations (no Alembic).
Both mechanisms run from db.py's init_db() on every startup:

1. Explicit migrations (MIGRATIONS) for what the safety net can't do:
   renames, computed backfills, drops, data transforms. Tracked by PRAGMA
   user_version.
2. add_missing_columns, for a new column that's nullable or has a scalar
   default. Runs every startup regardless of version, after the migrations
   and create_all().

Each file has its own user_version and list (MIGRATIONS for main,
HISTORY_MIGRATIONS for history). Every PRAGMA
and DDL here names its schema: with both files attached an unqualified name
can resolve into the other file.

The lists are append-only: each entry assumes the exact schema the one before
it left. A migration gets a Connection already inside its own transaction and
applies one change in raw SQL, e.g.:

    def _rename_foo_to_bar(connection: Connection) -> None:
        connection.execute(text("ALTER TABLE widgets RENAME COLUMN foo TO bar"))

Because add_missing_columns runs independently of user_version, a database
may already have a column a migration touches; SQLite has no `ADD COLUMN IF
NOT EXISTS`, so check `PRAGMA table_info(...)` first. RENAME COLUMN needs
SQLite >= 3.25. DROP COLUMN needs 3.35 and BACpack runs on 3.34, so a new
migration that must drop a column rebuilds the table instead. A column the
device processes map may be dropped only in a release whose upgrade note says
to stop every device first.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Callable

from sqlalchemy import MetaData, Table, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import Column, CreateColumn


class MigrationError(RuntimeError):
    """A migration failed, or add_missing_columns met a column it can't add
    safely (NOT NULL with no usable default)."""


class DatabaseTooNew(RuntimeError):
    """A database file was saved by a newer BACpack than this one. Not a
    MigrationError, so a handler for those never swallows it."""

    def __init__(self, schema: str, found: int, known: int, file_name: str) -> None:
        self.schema = schema
        self.found = found
        self.known = known
        self.file_name = file_name
        super().__init__(
            f"{file_name} was saved with database version {found}, but this BACpack only knows up to {known}. "
            "This database was saved by a newer BACpack. Install that release (or a newer one) again, "
            "or restore a backup from before the upgrade."
        )


# A migration hitting "database is locked"/busy is rolled back and retried for
# up to this long: the lock is another process's write (typically a device
# child still running from before an upgrade) outlasting pysqlite's 5 s busy
# timeout, and nothing was changed.
MIGRATION_LOCK_WAIT_SECONDS = 30.0
MIGRATION_LOCK_RETRY_SECONDS = 0.5


# index 0 = schema version 1, index 1 = version 2, etc.
MIGRATIONS: list[Callable[[Connection], None]] = []

# Tracked by PRAGMA history.user_version. Same rules as MIGRATIONS.
HISTORY_MIGRATIONS: list[Callable[[Connection], None]] = []

# Both files, in the order init_db processes them.
SCHEMAS = ("main", "history")


def migrations_for(schema: str) -> list:
    """Looked up at call time, so tests can monkeypatch either list."""
    return MIGRATIONS if schema == "main" else HISTORY_MIGRATIONS


def _schema_of(table: Table) -> str:
    return table.schema or "main"


def is_fresh_database(engine: Engine, schema: str = "main") -> bool:
    """True if the file has no user tables yet: a new install gets the
    current schema directly instead of replaying migrations."""
    with engine.connect() as connection:
        count = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {schema}.sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ).scalar_one()
    return count == 0


def get_schema_version(connection: Connection, schema: str = "main") -> int:
    """PRAGMA user_version of one file (0 if never set)."""
    return connection.execute(text(f"PRAGMA {schema}.user_version")).scalar_one()


def set_schema_version(connection: Connection, version: int, schema: str = "main") -> None:
    # PRAGMA takes no bound parameters; version is an int computed here and
    # schema is one of SCHEMAS, so interpolation is safe.
    connection.execute(text(f"PRAGMA {schema}.user_version = {int(version)}"))


def _is_locked_error(exc: BaseException) -> bool:
    """SQLite's locked/busy error, raw or wrapped by SQLAlchemy."""
    original = getattr(exc, "orig", exc)
    if not isinstance(original, sqlite3.OperationalError):
        return False
    message = str(original).lower()
    return "locked" in message or "busy" in message


def _apply_migration(
    engine: Engine, migration: Callable[[Connection], None], version: int, schema: str = "main"
) -> None:
    """Run one migration and bump user_version to `version` in one explicit
    transaction; rolled back and re-raised on any failure."""
    with engine.connect() as connection:
        try:
            # Raw BEGIN/COMMIT/ROLLBACK: under engine.begin() the stdlib
            # sqlite3 driver doesn't reliably roll back DDL, and the change
            # and its version bump must commit or roll back together.
            connection.execute(text("BEGIN"))
            migration(connection)
            set_schema_version(connection, version, schema)
            connection.execute(text("COMMIT"))
        except Exception:
            try:
                connection.execute(text("ROLLBACK"))
            except Exception:
                pass
            raise


def run_pending_migrations(engine: Engine, schema: str = "main") -> None:
    """Apply each migration the file hasn't seen, in order, each in its own
    transaction (_apply_migration). On failure raises MigrationError and
    leaves the file at the last applied version: running against a
    half-migrated schema isn't safe. A locked database is retried for up to
    MIGRATION_LOCK_WAIT_SECONDS.
    """
    with engine.connect() as connection:
        current_version = get_schema_version(connection, schema)

    pending = migrations_for(schema)
    label = "Migration" if schema == "main" else "History migration"
    deadline = time.monotonic() + MIGRATION_LOCK_WAIT_SECONDS
    for index in range(current_version, len(pending)):
        migration = pending[index]
        while True:
            try:
                _apply_migration(engine, migration, index + 1, schema)
                break
            except Exception as exc:
                if not _is_locked_error(exc):
                    raise MigrationError(
                        f"{label} #{index + 1} ({migration.__name__}) failed: "
                        f"database left at schema version {index}. BACpack will not "
                        f"start against a partially migrated database. "
                        f"Original error: {exc} The web UI will not start until this "
                        "is fixed; recovery requires direct file or code access, not "
                        "the settings screen."
                    ) from exc
                if time.monotonic() >= deadline:
                    raise MigrationError(
                        f"{label} #{index + 1} ({migration.__name__}) could not run: "
                        "the database was locked by another process (possibly a BACpack "
                        "device process still running from before the upgrade) for "
                        f"{MIGRATION_LOCK_WAIT_SECONDS:g} s. Nothing was changed; the "
                        f"database is still at schema version {index}. Start BACpack "
                        f"again once that process has finished. Original error: {exc}"
                    ) from exc
                time.sleep(MIGRATION_LOCK_RETRY_SECONDS)


def check_not_too_new(engine: Engine) -> None:
    """Raise DatabaseTooNew for a file whose user_version is above what this
    code has migrations for. Reads only: run before anything is changed."""
    with engine.connect() as connection:
        for schema in SCHEMAS:
            found = get_schema_version(connection, schema)
            known = len(migrations_for(schema))
            if found > known:
                path = next(
                    (row[2] for row in connection.execute(text("PRAGMA database_list")).all() if row[1] == schema),
                    schema,
                )
                raise DatabaseTooNew(schema, found, known, os.path.basename(path or schema))


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_column_ddl(table: Table, column: Column, dialect) -> str:
    if column.unique or column.index:
        raise MigrationError(
            f'Column "{table.name}.{column.name}" declares unique=True or index=True; '
            "the automatic safety net can only ADD a bare column; it can't also add a "
            "UNIQUE constraint or index this way (SQLAlchemy's CreateColumn doesn't emit "
            "either for an ALTER-added column, and create_all() doesn't retrofit indexes "
            "onto an existing table), which would otherwise leave an upgraded database "
            "silently different from a fresh install. Write an explicit migration in "
            "migrations.py that adds the column and its constraint/index, then append it "
            "to MIGRATIONS."
        )
    if column.nullable or column.server_default is not None:
        rendered = str(CreateColumn(column).compile(dialect=dialect))
        return f'ALTER TABLE {_schema_of(table)}."{table.name}" ADD COLUMN {rendered}'
    if column.default is not None and column.default.is_scalar:
        # A Python-side mapped_column(default=...): SQLAlchemy leaves it out
        # of DDL, but SQLite requires a DEFAULT on a NOT NULL column added by
        # ALTER TABLE, so render the same value here.
        column_type = column.type.compile(dialect=dialect)
        return (
            f'ALTER TABLE {_schema_of(table)}."{table.name}" ADD COLUMN "{column.name}" {column_type} '
            f"NOT NULL DEFAULT {_sql_literal(column.default.arg)}"
        )
    raise MigrationError(
        f'Column "{table.name}.{column.name}" is declared NOT NULL with no '
        "usable scalar default (either none at all, or one that isn't a "
        "plain literal, e.g. a callable) and is missing from the database "
        "-- the automatic safety net can't guess a safe backfill value. "
        "Write an explicit migration in migrations.py that adds and "
        "backfills this column, then append it to MIGRATIONS."
    )


def _create_trend_samples_timestamp_index(connection: Connection) -> None:
    """Plain index on trend_samples.timestamp for the globally-oldest-sample
    query (trending.prune_global_to_limit)."""
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_trend_samples_timestamp ON trend_samples (timestamp)")
    )


def _add_and_backfill_trend_total_sample_count(connection: Connection) -> None:
    """Adds server_settings.trend_total_sample_count unless already there,
    and always backfills it with a real COUNT(*): the safety net's literal 0
    would be wrong for a database with existing trend history."""
    existing = {
        row[1] for row in connection.execute(text('PRAGMA table_info("server_settings")')).all()
    }
    if "trend_total_sample_count" not in existing:
        connection.execute(
            text(
                'ALTER TABLE "server_settings" ADD COLUMN trend_total_sample_count '
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
    connection.execute(
        text(
            "UPDATE server_settings SET trend_total_sample_count = "
            "(SELECT COUNT(*) FROM trend_samples)"
        )
    )


def _clear_invalid_multistate_value_relinquish_defaults(connection: Connection) -> None:
    """Clears a multi-state-value relinquish_default that isn't a whole
    number from 1 to its state count. Such values were stored unchecked
    before Multi-state Value became commandable; the builder's int() would
    now stop the device starting. The rule is inlined on purpose: a
    migration stays fixed if the validator changes. A database lacking
    either column has nothing to clear."""
    existing = {
        row[1] for row in connection.execute(text('PRAGMA table_info("bacnet_objects")')).all()
    }
    if not {"state_text", "relinquish_default"} <= existing:
        return
    rows = connection.execute(
        text(
            "SELECT id, state_text, relinquish_default FROM bacnet_objects "
            "WHERE object_type = 'multi-state-value' AND relinquish_default IS NOT NULL"
        )
    ).all()
    for object_id, state_text, relinquish_default in rows:
        try:
            states = json.loads(state_text) if state_text else []
        except ValueError:
            states = []
        count = len(states) if isinstance(states, list) else 0
        try:
            valid = 1 <= int(relinquish_default) <= count
        except (TypeError, ValueError):
            valid = False
        if not valid:
            connection.execute(
                text("UPDATE bacnet_objects SET relinquish_default = NULL WHERE id = :id"),
                {"id": object_id},
            )


# Database split. Literal DDL, not the models:
# a migration stays fixed when models change.
# (runtime table, its key, owner table, ((column, DDL type, default SQL), ...))
_SPLIT_RUNTIME_TABLES = (
    ("object_runtime", "object_id", "bacnet_objects", (
        ("pending_manual_value", "JSON", "NULL"),
        ("pending_manual_queued", "BOOLEAN NOT NULL", "0"),
        ("pending_feed_value", "JSON", "NULL"),
        ("pending_feed_queued", "BOOLEAN NOT NULL", "0"),
        ("schedule_write_status", "JSON", "NULL"),
    )),
    ("device_runtime", "device_id", "devices", (
        ("process_pid", "INTEGER", "NULL"),
        ("process_started_at", "DATETIME", "NULL"),
        ("process_boot_id", "VARCHAR", "NULL"),
        ("process_started_since_boot", "FLOAT", "NULL"),
        ("process_status", "VARCHAR NOT NULL", "'stopped'"),
        ("process_error", "VARCHAR", "NULL"),
        ("pending_clock_offset", "FLOAT", "NULL"),
    )),
    ("connector_runtime", "connector_id", "connectors", (
        ("client_status", "JSON", "NULL"),
    )),
)
_SPLIT_DROPPED_COLUMNS = (
    ("bacnet_objects", (
        "pending_manual_value", "pending_manual_queued", "pending_feed_value", "pending_feed_queued",
        "schedule_write_status", "trend_next_sequence", "trend_last_notify_record",
        # An unused older write-queue pair.
        "pending_value", "pending_value_priority",
    )),
    ("devices", (
        "process_pid", "process_started_at", "process_boot_id", "process_started_since_boot",
        "process_status", "process_error", "pending_clock_offset",
    )),
    ("connectors", ("client_status",)),
    ("server_settings", ("trend_total_sample_count",)),
)
_SPLIT_OLD_HISTORY_TABLES = ("trend_samples", "traffic_log_entries", "log_entries", "client_requests")


def _main_columns(connection: Connection, table: str) -> set:
    return {row[1] for row in connection.execute(text(f'PRAGMA main.table_info("{table}")')).all()}


def _split_runtime_and_history_out_of_main(connection: Connection) -> None:
    """The database split. Runtime state
    moves into one row per owner in the *_runtime tables (copying current
    values, so a pre-upgrade child is reattached and can be stopped); the
    moved columns and old history tables are dropped from main, and history
    starts fresh in the history file. The 0.28.5 upgrade note says to stop
    every device first: a child left running still maps the dropped columns.
    Every name is main-qualified, as the history file is attached."""
    for table, key, owner, columns in _SPLIT_RUNTIME_TABLES:
        column_ddl = ", ".join(f"{name} {ddl}" for name, ddl, _ in columns)
        connection.execute(text(
            f"CREATE TABLE IF NOT EXISTS main.{table} ({key} INTEGER NOT NULL, {column_ddl}, "
            f"PRIMARY KEY ({key}), FOREIGN KEY({key}) REFERENCES {owner} (id))"
        ))
        existing = _main_columns(connection, owner)
        if not existing:
            continue
        selected = ", ".join(
            (f"COALESCE({name}, {default})" if default != "NULL" else name) if name in existing else default
            for name, _, default in columns
        )
        names = ", ".join(name for name, _, _ in columns)
        connection.execute(text(
            f"INSERT OR IGNORE INTO main.{table} ({key}, {names}) SELECT id, {selected} FROM main.{owner}"
        ))
    for table, columns in _SPLIT_DROPPED_COLUMNS:
        existing = _main_columns(connection, table)
        for column in columns:
            if column in existing:
                if sqlite3.sqlite_version_info < (3, 35):
                    raise RuntimeError(
                        "this database is from an old BACpack release, and its upgrade needs SQLite "
                        f"3.35 or newer (this Python has {sqlite3.sqlite_version}). Run this one "
                        "upgrade by running the installer with --python pointing at a Python whose "
                        "SQLite is 3.35 or newer."
                    )
                connection.execute(text(f'ALTER TABLE main."{table}" DROP COLUMN "{column}"'))
    for table in _SPLIT_OLD_HISTORY_TABLES:
        connection.execute(text(f'DROP TABLE IF EXISTS main."{table}"'))


MIGRATIONS.append(_create_trend_samples_timestamp_index)
MIGRATIONS.append(_add_and_backfill_trend_total_sample_count)
MIGRATIONS.append(_clear_invalid_multistate_value_relinquish_defaults)
MIGRATIONS.append(_split_runtime_and_history_out_of_main)


def _index_trend_samples_by_object_and_id(connection: Connection) -> None:
    """One log's rows in id order without a sort, for ReadRange
    (objects/trend_log.py)."""
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS history.ix_trend_samples_object_id ON trend_samples (object_id, id)")
    )


HISTORY_MIGRATIONS.append(_index_trend_samples_by_object_and_id)


_TAIL_COLUMNS = (
    ("ordered_from_id", "INTEGER NOT NULL DEFAULT 0"),
    ("tail_last_id", "INTEGER NOT NULL DEFAULT 0"),
    ("tail_max_timestamp", "DATETIME"),
    ("tail_min_offset", "FLOAT"),
    ("tail_max_offset", "FLOAT"),
)


def _add_and_fill_trend_tails(connection: Connection) -> None:
    """The ordered-tail columns of trend_state.
    Each existing log's tail starts at its newest row, one seek per log: every
    older row is head, which byTime scans as before, so old data keeps its
    speed and new records get the fast path at once. (Working out the real
    tails took 14 s on a 1 GiB history file.)"""
    existing = {row[1] for row in connection.execute(text('PRAGMA history.table_info("trend_state")')).all()}
    if not existing:
        return  # no table yet: create_all makes it with the columns
    for name, ddl in _TAIL_COLUMNS:
        if name not in existing:
            connection.execute(text(f'ALTER TABLE history."trend_state" ADD COLUMN "{name}" {ddl}'))
    connection.execute(
        text(
            "UPDATE history.trend_state SET tail_last_id = coalesce("
            "(SELECT max(id) FROM history.trend_samples WHERE object_id = trend_state.object_id), 0)"
        )
    )
    connection.execute(
        text(
            "UPDATE history.trend_state SET ordered_from_id = tail_last_id, "
            "tail_max_timestamp = (SELECT timestamp FROM history.trend_samples WHERE id = trend_state.tail_last_id), "
            "tail_min_offset = (SELECT coalesce(clock_offset, 0.0) FROM history.trend_samples "
            "WHERE id = trend_state.tail_last_id), "
            "tail_max_offset = (SELECT coalesce(clock_offset, 0.0) FROM history.trend_samples "
            "WHERE id = trend_state.tail_last_id) "
            "WHERE tail_last_id > 0"
        )
    )


HISTORY_MIGRATIONS.append(_add_and_fill_trend_tails)


_NETWORK_ROLE_COLUMNS = (
    ("network_role", "TEXT NOT NULL DEFAULT 'normal'"),
    ("foreign_bbmd", "TEXT"),
    ("foreign_ttl", "INTEGER NOT NULL DEFAULT 60"),
    ("bbmd_peers", "JSON"),
)


def _move_network_role_to_devices(connection: Connection) -> None:
    """The server-wide network role becomes each device's own:
    every device runs with exactly the
    role and BBMD table it had. A role missing the setting it needs ran as
    Normal (build_network_port_object), so it becomes Normal. The
    server_settings columns stay, unread."""
    existing = _main_columns(connection, "devices")
    if not existing:
        return  # no table yet: create_all makes it with the columns
    for name, ddl in _NETWORK_ROLE_COLUMNS:
        if name not in existing:
            connection.execute(text(f'ALTER TABLE main."devices" ADD COLUMN "{name}" {ddl}'))
    # One statement each, so the file isn't read before it is written.
    connection.execute(text(
        "UPDATE main.devices SET network_role = 'foreign', "
        "foreign_bbmd = (SELECT foreign_bbmd FROM main.server_settings LIMIT 1), "
        "foreign_ttl = (SELECT coalesce(foreign_ttl, 60) FROM main.server_settings LIMIT 1) "
        "WHERE EXISTS (SELECT 1 FROM main.server_settings "
        "WHERE network_role = 'foreign' AND coalesce(foreign_bbmd, '') != '')"
    ))
    connection.execute(text(
        "UPDATE main.devices SET network_role = 'bbmd', "
        "bbmd_peers = (SELECT bbmd_peers FROM main.server_settings LIMIT 1) "
        "WHERE EXISTS (SELECT 1 FROM main.server_settings WHERE network_role = 'bbmd' "
        "AND json_valid(bbmd_peers) AND json_array_length(bbmd_peers) > 0)"
    ))


MIGRATIONS.append(_move_network_role_to_devices)


# --- Change counter triggers --------------------
# device_runtime.change_seq moves when config a running device reads changes or
# a write is queued for it. Triggers, not Python bumps, so no write path (ORM,
# Core, imports, restore, the device's own BACnet writes) can forget. Not
# MIGRATIONS entries: ensure_triggers recreates them on every init_db. Bodies
# name tables unqualified: SQLite refuses `main.` inside a trigger.


def _bump(*owners: str, config: bool = True) -> str:
    """change_seq always; config_seq too unless it's only a queued write:
    the device reloads objects and runners on config_seq."""
    counters = "change_seq = change_seq + 1" + (", config_seq = config_seq + 1" if config else "")
    return f"UPDATE device_runtime SET {counters} WHERE device_id IN ({', '.join(owners)});"


def _own_device(row: str) -> str:
    return f"{row}.device_id"


def _connector_device(row: str) -> str:
    return f"(SELECT device_id FROM connectors WHERE id = {row}.connector_id)"


def _object_device(row: str) -> str:
    return f"(SELECT device_id FROM bacnet_objects WHERE id = {row}.object_id)"


def _config_triggers(table: str, owner: Callable[[str], str]) -> dict:
    """Any insert, update or delete: the owning device -- both, when an
    update moves the row to another device (IN bumps one device once)."""
    return {
        f"bacpack_{table}_insert": f"AFTER INSERT ON main.{table} BEGIN {_bump(owner('NEW'))} END",
        f"bacpack_{table}_update": f"AFTER UPDATE ON main.{table} BEGIN {_bump(owner('OLD'), owner('NEW'))} END",
        f"bacpack_{table}_delete": f"AFTER DELETE ON main.{table} BEGIN {_bump(owner('OLD'))} END",
    }


# Trigger name -> the rest of its CREATE TRIGGER main.<name> statement.
TRIGGERS: dict = {
    **_config_triggers("bacnet_objects", _own_device),
    **_config_triggers("connectors", _own_device),
    **_config_triggers("connector_mappings", _connector_device),
    **_config_triggers("connector_write_mappings", _connector_device),
    # Device settings a running device reads;
    # not the columns the device writes itself (dcc_state, clock_offset_seconds,
    # database_revision -- which only moves with an object change anyway).
    "bacpack_devices_update": (
        "AFTER UPDATE OF name, description, location, vendor_identifier, vendor_name, bind_address, "
        "network_role, foreign_bbmd, foreign_ttl, bbmd_peers, backup_failure_timeout ON main.devices "
        f"BEGIN {_bump('NEW.id')} END"
    ),
    # A BBMD's table holds its linked devices' sockets.
    "bacpack_devices_bind_links": (
        "AFTER UPDATE OF bind_address ON main.devices "
        f"BEGIN {_bump('SELECT device_id FROM device_links WHERE linked_device_id = NEW.id')} END"
    ),
    **_config_triggers("device_links", _own_device),
    "bacpack_devices_delete_links": (
        "AFTER DELETE ON main.devices "
        "BEGIN DELETE FROM device_links WHERE device_id = OLD.id OR linked_device_id = OLD.id; END"
    ),
    # A slot becoming queued, or its value changing while queued -- not a
    # clear: PendingWritePoller's compare-and-clear must not wake the device,
    # even while the other slot is still queued.
    "bacpack_object_runtime_queued": (
        "AFTER UPDATE OF pending_manual_value, pending_manual_queued, pending_feed_value, pending_feed_queued "
        "ON main.object_runtime WHEN "
        "(NEW.pending_manual_queued AND (NOT OLD.pending_manual_queued "
        "OR NEW.pending_manual_value IS NOT OLD.pending_manual_value)) "
        "OR (NEW.pending_feed_queued AND (NOT OLD.pending_feed_queued "
        "OR NEW.pending_feed_value IS NOT OLD.pending_feed_value)) "
        f"BEGIN {_bump(_object_device('NEW'), config=False)} END"
    ),
}


def drop_triggers(connection: Connection) -> None:
    """Drop every bacpack_ trigger in main, including renamed or removed
    ones. init_db runs this before migrations, so a migration that drops or
    rebuilds a table never meets a trigger naming it."""
    names = connection.exec_driver_sql(
        "SELECT name FROM main.sqlite_master WHERE type = 'trigger' AND name LIKE 'bacpack\\_%' ESCAPE '\\'"
    ).scalars().all()
    for name in names:
        connection.exec_driver_sql(f"DROP TRIGGER main.{name}")


def _triggers_current(connection: Connection) -> bool:
    """True when main holds exactly the TRIGGERS set. SQLite stores each
    statement as written, minus the `main.` after CREATE TRIGGER."""
    stored = dict(
        connection.exec_driver_sql(
            "SELECT name, sql FROM main.sqlite_master WHERE type = 'trigger' AND name LIKE 'bacpack\\_%' ESCAPE '\\'"
        ).all()
    )
    return stored == {name: f"CREATE TRIGGER {name} {body}" for name, body in TRIGGERS.items()}


def ensure_triggers(engine: Engine) -> None:
    """Recreate exactly the TRIGGERS set; run last in init_db. One raw-SQL
    transaction (pysqlite commits each DDL under engine.begin()), so a
    failure leaves the previous set, never none. init_db's early
    drop_triggers can't share it: a parent dying during the migrations
    leaves no triggers until the next successful start."""
    with engine.connect() as connection:
        if _triggers_current(connection):
            # BEGIN IMMEDIATE would also take the history file's write lock,
            # which a device still running may hold past the busy timeout.
            return
        version = get_schema_version(connection, "main")
        try:
            # A deferred BEGIN, then a main-only write before any read, takes
            # main's write lock alone (BEGIN IMMEDIATE would also lock the
            # history file) and can't fail at once on a read-then-write race.
            connection.execute(text("BEGIN"))
            set_schema_version(connection, version, "main")
            drop_triggers(connection)
            for name, body in TRIGGERS.items():
                connection.exec_driver_sql(f"CREATE TRIGGER main.{name} {body}")
            connection.execute(text("COMMIT"))
        except Exception:
            try:
                connection.execute(text("ROLLBACK"))
            except Exception:
                pass
            raise


def add_missing_columns(engine: Engine, metadata: MetaData, schema: str = "main") -> None:
    """Add every column a model declares but its table lacks. A locked
    database is retried as in run_pending_migrations: a release that only
    adds columns upgrades through here."""
    deadline = time.monotonic() + MIGRATION_LOCK_WAIT_SECONDS
    while True:
        try:
            _add_missing_columns_once(engine, metadata, schema)
            return
        except Exception as exc:
            if not _is_locked_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise MigrationError(
                    "Could not add the new columns: the database was locked by another "
                    "process (possibly a BACpack device process still running from before "
                    f"the upgrade) for {MIGRATION_LOCK_WAIT_SECONDS:g} s. No data was lost; "
                    "start BACpack again once that process has finished. "
                    f"Original error: {exc}"
                ) from exc
            time.sleep(MIGRATION_LOCK_RETRY_SECONDS)


def _add_missing_columns_once(engine: Engine, metadata: MetaData, schema: str = "main") -> None:
    """Only adds columns, never alters or drops, so it can't lose data.
    Raises MigrationError for a column it can't add safely (_add_column_ddl)
    or one SQLite rejects (e.g. a non-constant server_default); columns
    added earlier in the call stay."""
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if _schema_of(table) != schema:
                continue
            rows = connection.execute(text(f'PRAGMA {schema}.table_info("{table.name}")')).all()
            if not rows:
                # Missing table: create_all() handles that.
                continue
            existing_columns = {row[1] for row in rows}
            for column in table.columns:
                if column.name not in existing_columns:
                    ddl = _add_column_ddl(table, column, engine.dialect)
                    try:
                        connection.execute(text(ddl))
                    except Exception as exc:
                        if _is_locked_error(exc):
                            raise  # add_missing_columns waits it out
                        raise MigrationError(
                            f'Could not add column "{table.name}.{column.name}" '
                            "automatically: SQLite rejected it (most likely a "
                            "non-constant default, e.g. CURRENT_TIMESTAMP/func.now(), "
                            "which ALTER TABLE ADD COLUMN doesn't accept even though "
                            "CREATE TABLE does). Write an explicit migration in "
                            "migrations.py that adds and backfills this column, then "
                            f"append it to MIGRATIONS. Original error: {exc}"
                        ) from exc


def _add_login_mode(connection: Connection) -> None:
    """Adds the login columns (the safety net runs after migrations) and
    settles the mode of an existing install: a Basic-auth login becomes a
    login, anything else "none", so an upgrade keeps running as it did. A
    fresh database never gets here and keeps NULL (the setup screen)."""
    existing = {row[1] for row in connection.execute(text('PRAGMA main.table_info("server_settings")')).all()}
    for column in ("login_mode", "login_username", "login_password_hash"):
        if column not in existing:
            connection.execute(text(f'ALTER TABLE main."server_settings" ADD COLUMN {column} VARCHAR'))
    connection.execute(text(
        "UPDATE main.server_settings SET login_mode = 'login', login_username = auth_username, "
        "login_password_hash = auth_password_hash WHERE login_mode IS NULL AND auth_enabled "
        "AND COALESCE(auth_username, '') <> '' AND COALESCE(auth_password_hash, '') <> ''"
    ))
    connection.execute(text("UPDATE main.server_settings SET login_mode = 'none' WHERE login_mode IS NULL"))
    connection.execute(text("INSERT OR IGNORE INTO main.server_settings (id, login_mode) VALUES (1, 'none')"))


MIGRATIONS.append(_add_login_mode)


_ALL_DAY_STARTS = ("00:00", "00:00:00", "00:00:00.00")
_LAST_MINUTE_ENDS = ("23:59", "23:59:00", "23:59:00.00")


def _widen_all_day_recipient_windows(connection: Connection) -> None:
    """A recipient the web UI created ended at 23:59, so nothing was sent in
    the day's last minute: a window from midnight to 23:59 (any stored form)
    now ends at 23:59:59.99. Only notification-class rows are read, so it
    stays instant on any table size; every other window is left as saved.
    The rule is inlined on purpose: a migration stays fixed if the UI's
    default changes."""
    existing = {row[1] for row in connection.execute(text('PRAGMA main.table_info("bacnet_objects")')).all()}
    if "notification_recipient_list" not in existing:
        return
    rows = connection.execute(
        text(
            "SELECT id, notification_recipient_list FROM main.bacnet_objects "
            "WHERE object_type = 'notification-class' AND notification_recipient_list IS NOT NULL"
        )
    ).all()
    for object_id, raw in rows:
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(entries, list):
            continue
        changed = False
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("from_time") in _ALL_DAY_STARTS
                and entry.get("to_time") in _LAST_MINUTE_ENDS
            ):
                entry["to_time"] = "23:59:59.99"
                changed = True
        if changed:
            connection.execute(
                text("UPDATE main.bacnet_objects SET notification_recipient_list = :value WHERE id = :id"),
                {"value": json.dumps(entries), "id": object_id},
            )


MIGRATIONS.append(_widen_all_day_recipient_windows)


def _clear_trended_on_notification_classes(connection: Connection) -> None:
    """A Notification Class has no Present_Value, so it can't be trended:
    the flag (and trend reporting, which needs it) is cleared on rows an
    earlier release let be saved, or they would stop their device building.
    A database lacking the reporting column has nothing to clear there."""
    existing = {row[1] for row in connection.execute(text('PRAGMA main.table_info("bacnet_objects")')).all()}
    if "trended" not in existing:
        return
    if "trend_reporting_enabled" in existing:
        connection.execute(text(
            "UPDATE main.bacnet_objects SET trended = 0, trend_reporting_enabled = 0 "
            "WHERE object_type = 'notification-class' AND (trended OR trend_reporting_enabled)"
        ))
    else:
        connection.execute(text(
            "UPDATE main.bacnet_objects SET trended = 0 WHERE object_type = 'notification-class' AND trended"
        ))


MIGRATIONS.append(_clear_trended_on_notification_classes)
