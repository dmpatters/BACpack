"""SQLite database setup for BACpack.

One configurable path (BACPACK_DB_PATH), so a bare install and a service
install work alike. Its directory holds two files: the main database and
its history file (history_db_path).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, Optional, Union

from sqlalchemy import create_engine, delete, event, func, insert, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import secrets_store

logger = logging.getLogger(__name__)

MIN_SQLITE_VERSION = (3, 34, 0)


def _sqlite_has_json() -> bool:
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("SELECT json_valid('[]')")
    except sqlite3.OperationalError:
        return False
    return True


def sqlite_version_problem(version_info=None, has_json: Optional[bool] = None) -> Optional[str]:
    """Why this Python's SQLite can't run BACpack (3.34 is RHEL 9's, and the
    JSON functions are built in from 3.38 but optional before), or None. Linux
    Pythons often link the system library."""
    found = tuple(version_info or sqlite3.sqlite_version_info)
    have = ".".join(str(part) for part in found)
    if found < MIN_SQLITE_VERSION:
        need = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        return (
            f"SQLite {have} is too old: BACpack needs {need} or newer. Use a Python "
            "whose sqlite3 module bundles a newer SQLite."
        )
    if not (_sqlite_has_json() if has_json is None else has_json):
        return (
            f"SQLite {have} was built without its JSON functions, which BACpack needs. "
            "Use a Python whose sqlite3 module bundles SQLite 3.38 or newer."
        )
    return None

DEFAULT_DB_PATH = Path("data") / "bacpack.db"

# The history file is attached to every connection under this name.
HISTORY_SCHEMA = "history"

# pysqlite's busy handler, per connection, so it covers both files.
BUSY_TIMEOUT_SECONDS = 10


class Base(DeclarativeBase):
    """Declarative base for all BACpack ORM models."""


def get_db_path() -> Path:
    """BACPACK_DB_PATH, default ./data/bacpack.db."""
    return Path(os.environ.get("BACPACK_DB_PATH", DEFAULT_DB_PATH))


def history_db_path(db_path: Union[Path, str]) -> Path:
    """`data/bacpack.db` -> `data/bacpack-history.db`. Derived, never
    configured, so every process finds it from the main path alone."""
    path = Path(db_path)
    return path.with_name(f"{path.stem}-history{path.suffix}")


def make_engine(db_path: Union[Path, str, None] = None, *, echo: bool = False) -> Engine:
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    history_path = str(history_db_path(path))
    engine = create_engine(
        f"sqlite:///{path}", echo=echo, connect_args={"timeout": BUSY_TIMEOUT_SECONDS}
    )
    secrets_store.attach(engine, path.parent)

    @event.listens_for(engine, "connect")
    def _attach_history_and_set_wal(dbapi_connection, connection_record) -> None:
        # The history file has its own write lock, so recording history never
        # waits on, or holds up, a config edit or device write. ATTACH creates
        # it if missing; a bound parameter handles any path.
        dbapi_connection.execute(f"ATTACH DATABASE ? AS {HISTORY_SCHEMA}", (history_path,))
        # The parent and every device child hold these files open at once;
        # WAL lets readers and one writer proceed concurrently. WAL persists
        # in the file, but re-asserting it per connection is cheap.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA main.journal_mode=WAL")
        cursor.execute(f"PRAGMA {HISTORY_SCHEMA}.journal_mode=WAL")
        cursor.close()

    return engine


# The columns that change a device's object list as a client sees it (135
# 12.11.35: an object's name or Object_Identifier; `trended` adds or removes
# its Trend Log, as do the Trend Log's own instance and name).
_OBJECT_LIST_COLUMNS = ("name", "object_type", "instance_number", "trended", "trend_log_instance", "trend_log_name")


def _bump_database_revision(session: Session, flush_context, instances) -> None:
    """Database_Revision (DM-BR-B): +1 per
    flush for every existing device renamed, or with an object created,
    deleted, renamed or renumbered. A flush listener so every save path is
    covered. A device created or deleted in the same flush is skipped."""
    from .models import BacnetObject, Device

    device_ids = set()
    for obj in session.new:
        if isinstance(obj, BacnetObject):
            device_ids.add(obj.device.id if obj.device is not None else obj.device_id)
    for obj in session.deleted:
        if isinstance(obj, BacnetObject):
            device_ids.add(obj.device_id)
    for obj in session.dirty:
        if isinstance(obj, BacnetObject):
            attrs = inspect(obj).attrs
            if any(getattr(attrs, column).history.has_changes() for column in _OBJECT_LIST_COLUMNS):
                device_ids.add(obj.device_id)
        elif isinstance(obj, Device):
            attrs = inspect(obj).attrs
            # attrs.objects.history.deleted: device.objects.remove(...), a
            # delete-orphan not yet in session.deleted at this point.
            if attrs.name.history.has_changes() or attrs.objects.history.deleted:
                device_ids.add(obj.id)
    device_ids.discard(None)
    devices = []
    with session.no_autoflush:
        for device_id in device_ids:
            device = session.get(Device, device_id)
            if device is not None and device not in session.new and device not in session.deleted:
                devices.append(device)
    session.info[_REVISION_BUMPS] = devices


_REVISION_BUMPS = "bacpack_revision_bumps"


def _write_revision_bumps(session: Session, flush_context) -> None:
    """The bump is made in SQL, since the session's copy can be stale: two
    saves that read the same revision would both write N+1. After the
    flush's own writes, so it doesn't change which statement opens the
    transaction (tests/_db_guard.py)."""
    from .models import Device

    for device in session.info.get(_REVISION_BUMPS, ()):
        session.execute(
            update(Device)
            .where(Device.id == device.id)
            .values(database_revision=func.coalesce(Device.database_revision, 1) + 1)
            .execution_options(synchronize_session=False)
        )


def _expire_bumped_revisions(session: Session, flush_context) -> None:
    for device in session.info.pop(_REVISION_BUMPS, ()):
        session.expire(device, ["database_revision"])


def make_session_factory(engine: Engine) -> sessionmaker:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event.listen(factory, "before_flush", _bump_database_revision)
    event.listen(factory, "after_flush", _write_revision_bumps)
    event.listen(factory, "after_flush_postexec", _expire_bumped_revisions)
    return factory


def lock_both_files(session: Session) -> None:
    """Start the session's transaction holding both files' write locks.

    Call it before the first change on a save that writes both files. SQLite
    waits on the busy timeout only while a connection holds no read snapshot
    of the file it locks. Without this, a transaction that writes one file,
    then reads the other and writes it, fails at once with "database is
    locked" whenever another process is writing that file. Called after a
    change has started the transaction, it raises.
    """
    session.execute(text("BEGIN IMMEDIATE"))


def _drop_all_tables(engine: Engine, schema: str) -> None:
    with engine.begin() as connection:
        names = [
            row[0]
            for row in connection.exec_driver_sql(
                f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in names:
            connection.exec_driver_sql(f'DROP TABLE {schema}."{name}"')


def _ensure_startup_rows(engine: Engine, *, main_was_fresh: bool) -> None:
    """Rows the code assumes exist: one runtime row per device, object and
    connector (restoring any lost), the history counters row, created
    here rather than raced for by two devices' first trend samples (written
    only when missing, so a restart never waits on the history file), and the
    server settings row, so retention never writes the main file (a save
    that had already read main failed at once on that lock). INSERT OR
    IGNORE leaves existing rows untouched. A settings row made in a main file
    that already existed starts as "no login" (a fresh install's row waits
    for the setup screen): an upgrade must never become claimable."""
    from .models import (
        BacnetObject,
        Connector,
        ConnectorRuntime,
        Device,
        DeviceRuntime,
        HistoryCounters,
        ObjectRuntime,
        ServerSettings,
    )

    with engine.begin() as connection:
        for runtime, key, owner in (
            (DeviceRuntime, "device_id", Device),
            (ObjectRuntime, "object_id", BacnetObject),
            (ConnectorRuntime, "connector_id", Connector),
        ):
            connection.execute(
                insert(runtime.__table__)
                .from_select([key], select(owner.__table__.c.id))
                .prefix_with("OR IGNORE")
            )
        settings_row = {"id": 1, "login_mode": None if main_was_fresh else "none"}
        connection.execute(insert(ServerSettings.__table__).values(**settings_row).prefix_with("OR IGNORE"))
    # Its own transaction, and only when missing: even an INSERT OR IGNORE
    # takes the history write lock, which a device still running from before a
    # parent restart may hold past the busy timeout.
    with engine.connect() as connection:
        has_counters = connection.execute(select(HistoryCounters.id).limit(1)).first()
    if has_counters is None:
        with engine.begin() as connection:
            connection.execute(insert(HistoryCounters.__table__).values(id=1).prefix_with("OR IGNORE"))


def init_db(engine: Engine) -> None:
    """Create/upgrade both files (see migrations.py): a new file gets the
    current schema directly, an existing one runs pending migrations, then
    missing columns are added. Freshness is decided per file before
    create_all. The change-counter triggers are recreated last.
    """
    # Import models so they're registered on Base.metadata before create_all.
    from . import migrations, models  # noqa: F401

    # First: nothing may change in a file this code cannot read.
    migrations.check_not_too_new(engine)
    fresh = {schema: migrations.is_fresh_database(engine, schema) for schema in migrations.SCHEMAS}
    if fresh["main"] and not fresh["history"]:
        # bacpack.db deleted to start over: history rows key on plain ids the
        # new config reuses, so the old history would attach to new objects.
        logger.warning("new database beside an old history file: clearing the old history")
        _drop_all_tables(engine, HISTORY_SCHEMA)
        fresh["history"] = True
    # Out of the migrations' way; ensure_triggers puts them back last. Only
    # when main has a migration to run: a plain restart leaves them alone.
    with engine.connect() as connection:
        main_version = migrations.get_schema_version(connection, "main")
    if not fresh["main"] and main_version < len(migrations.migrations_for("main")):
        with engine.begin() as connection:
            migrations.drop_triggers(connection)
    for schema in migrations.SCHEMAS:
        if not fresh[schema]:
            migrations.run_pending_migrations(engine, schema)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for schema in migrations.SCHEMAS:
            if fresh[schema]:
                migrations.set_schema_version(
                    connection, len(migrations.migrations_for(schema)), schema
                )
    for schema in migrations.SCHEMAS:
        migrations.add_missing_columns(engine, Base.metadata, schema)
    _ensure_startup_rows(engine, main_was_fresh=fresh["main"])
    secrets_store.ensure_key(secrets_store.data_dir_of(engine))
    secrets_store.rewrite_plaintext(engine)
    migrations.ensure_triggers(engine)


# Compact the main file at startup when more than this fraction of its pages
# is free, e.g. after a migration drops large tables. VACUUM copies only live pages.
VACUUM_FREE_FRACTION = 0.25


def vacuum_if_mostly_free(engine: Engine) -> bool:
    """VACUUM main when it's mostly free pages; True if it ran. Best effort:
    a device child holding the file makes it busy, logged and retried next
    start. Parent startup only, never a device child."""
    with engine.connect() as connection:
        free = connection.exec_driver_sql("PRAGMA main.freelist_count").scalar_one()
        pages = connection.exec_driver_sql("PRAGMA main.page_count").scalar_one()
    if not pages or free <= pages * VACUUM_FREE_FRACTION:
        return False
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql("VACUUM main")
            # In WAL mode VACUUM writes the whole file through the WAL, which
            # keeps that peak size until the last connection closes. Best
            # effort: a reading device child can block the checkpoint past
            # BUSY_TIMEOUT_SECONDS, leaving the WAL as it was.
            connection.exec_driver_sql("PRAGMA main.wal_checkpoint(TRUNCATE)")
    except OperationalError as exc:
        logger.warning(
            "could not compact the database (%d of %d pages free), will retry on the next start: %s",
            free,
            pages,
            exc,
        )
        return False
    logger.info("compacted the database (%d of %d pages were free)", free, pages)
    return True


# Frames (4 KiB each) the history WAL may hold after a chunk before it is
# restarted.
_WAL_RESTART_FRAMES = 5_000
# A restart blocks every writer while it waits for readers, so it waits only
# this long and is tried again after the next chunk.
_WAL_RESTART_WAIT_MS = 100


def keep_history_wal_small(session: Session) -> None:
    """Call after each committed chunk of a big history delete.

    SQLite rewinds the WAL only for a transaction that starts with the log
    fully copied back. With a device writing between chunks it never is: the
    device commits while the chunk's auto-checkpoint is still copying, its
    own auto-checkpoint finds the checkpoint lock taken and gives up, and the
    next chunk starts with those frames outstanding, so the log grew by every
    chunk (gigabytes, measured). A checkpoint here, after the auto-checkpoint,
    copies the rest; a restart past _WAL_RESTART_FRAMES backs it up."""
    try:
        # wal_checkpoint always returns one row.
        frames = session.execute(text("PRAGMA history.wal_checkpoint(PASSIVE)")).fetchone()[1]  # pyright: ignore[reportOptionalSubscript]
        if frames > _WAL_RESTART_FRAMES:
            session.execute(text(f"PRAGMA busy_timeout = {_WAL_RESTART_WAIT_MS}"))
            try:
                session.execute(text("PRAGMA history.wal_checkpoint(RESTART)")).fetchall()
            finally:
                session.execute(text(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}"))
    except OperationalError:
        logger.debug("history WAL checkpoint skipped", exc_info=True)
    finally:
        session.rollback()


# Rows per statement for the retention prunes. Each chunk is its own
# transaction, so a waiting device process gets the lock between chunks
# instead of failing on the busy timeout.
DELETE_CHUNK_SIZE = 5000


def delete_in_chunks(
    session_factory,
    model,
    *criteria,
    on_chunk: Optional[Callable[[Session, int], None]] = None,
) -> int:
    """Delete history `model` rows matching `criteria`, DELETE_CHUNK_SIZE at
    a time, committing after each, and return the total. `on_chunk(session,
    deleted)` runs inside each chunk's transaction. One unchunked DELETE of
    8M trend samples held the write lock for 13 s and failed device writes."""
    total = 0
    while True:
        with session_factory() as session:
            ids = select(model.id).where(*criteria).limit(DELETE_CHUNK_SIZE).scalar_subquery()
            deleted = session.execute(delete(model).where(model.id.in_(ids))).rowcount
            if deleted and on_chunk is not None:
                on_chunk(session, deleted)
            session.commit()
            if deleted:
                keep_history_wal_small(session)
        total += deleted
        if deleted < DELETE_CHUNK_SIZE:
            return total
