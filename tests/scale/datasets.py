"""Field-sized datasets for the scale tests and their cache.

Rows go in by straight SQL on a database made by the real init_db: the ORM
is far too slow at ten million rows. A build writes into a temporary
directory and renames it into place last, so an interrupted build is never
picked up as cached.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import insert, select
from sqlalchemy.schema import CreateIndex, CreateTable, DropIndex

from bacpack import migrations, object_config, timeutil, trend_buffer
from bacpack.db import Base, history_db_path, init_db, make_engine, make_session_factory
from bacpack.models import (
    MAX_TREND_SAMPLE_CAP,
    BacnetObject,
    Device,
    DeviceRuntime,
    ObjectRuntime,
    TrendSample,
    effective_trend_sample_cap,
)
from bacpack.trending import BYTES_PER_TREND_SAMPLE, TREND_HARD_CAP_BYTES

# Bump on any change to what a build writes: it invalidates every cached dataset.
BUILDER_VERSION = 2

BIG_DEVICE_ID = 599100
_TWIN_LOG_DEVICE_ID = BIG_DEVICE_ID + 1
_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_SAMPLE_BATCH = 50_000
_COMMIT_EVERY = 1_000_000

# Seconds each build took, by dataset name, for the benchmark to report.
build_seconds: dict[str, float] = {}


@dataclass(frozen=True)
class Sizes:
    devices: int = 50
    big_device_objects: int = 5000
    full_logs: int = 5
    full_log_samples: int = MAX_TREND_SAMPLE_CAP
    trended_objects: int = 500
    samples_per_trended: int = 2000
    small_twin_samples: int = 3277
    objects_per_other_device: int = 100
    traffic_per_device: int = 500
    log_entries: int = 1_000_000
    small_twin_log_entries: int = 100_000
    client_requests: int = 100_000
    # Samples by the counter; None for the install.
    history_target: int | None = None


FIELD = Sizes()
FULL_HISTORY = replace(FIELD, history_target=TREND_HARD_CAP_BYTES // BYTES_PER_TREND_SAMPLE)
TINY = Sizes(
    devices=3,
    big_device_objects=60,
    full_logs=1,
    full_log_samples=30,
    trended_objects=5,
    samples_per_trended=20,
    small_twin_samples=10,
    objects_per_other_device=5,
    traffic_per_device=5,
    log_entries=200,
    small_twin_log_entries=20,
    client_requests=50,
)


@dataclass(frozen=True)
class DatasetIds:
    """Ids scenarios name things by. Object ids are BacnetObject.id."""

    big_device_id: int
    twin_log_device_id: int
    other_device_ids: tuple[int, ...]
    big_object_ids: range
    full_log_ids: tuple[int, ...]
    small_twin_id: int
    trended_ids: tuple[int, ...]
    extra_trended_ids: tuple[int, ...]

    @property
    def device_ids(self) -> tuple[int, ...]:
        return (self.big_device_id, *self.other_device_ids)


@dataclass(frozen=True)
class _Counts:
    schedules: int
    calendars: int
    notification_classes: int
    per_type: int
    analog_values: int
    alarming_each: int


def _counts(sizes: Sizes) -> _Counts:
    n = sizes.big_device_objects
    schedules = max(1, n // 50)
    calendars = max(1, n // 100)
    ncs = max(1, n // 500)
    values = n - schedules - calendars - ncs
    per_type = values // 5
    return _Counts(
        schedules, calendars, ncs, per_type, per_type + values - 5 * per_type, max(1, n // 50)
    )


def _installed_samples(sizes: Sizes) -> int:
    return (
        sizes.full_logs * sizes.full_log_samples
        + sizes.small_twin_samples
        + sizes.trended_objects * sizes.samples_per_trended
    )


def _extra_sample_counts(sizes: Sizes) -> list[int]:
    if sizes.history_target is None:
        return []
    remaining = sizes.history_target - _installed_samples(sizes)
    if remaining < 0:
        raise ValueError("history_target is below the install's own samples")
    per_log = sizes.full_log_samples
    counts = [per_log] * (remaining // per_log)
    if remaining % per_log:
        counts.append(remaining % per_log)
    return counts


def ids_for(sizes: Sizes) -> DatasetIds:
    counts = _counts(sizes)
    first = sizes.full_logs
    n_other = sizes.devices - 1
    other_ids = tuple(BIG_DEVICE_ID + i for i in range(1, sizes.devices))
    first_extra = sizes.big_device_objects + n_other * sizes.objects_per_other_device + 1
    return DatasetIds(
        big_device_id=BIG_DEVICE_ID,
        twin_log_device_id=_TWIN_LOG_DEVICE_ID,
        other_device_ids=other_ids,
        big_object_ids=range(1, sizes.big_device_objects + 1),
        full_log_ids=tuple(range(1, first + 1)),
        small_twin_id=first + 1,
        trended_ids=tuple(range(first + 2, first + 2 + sizes.trended_objects)),
        extra_trended_ids=tuple(range(first_extra, first_extra + len(_extra_sample_counts(sizes)))),
    )


# --- The cache ------------------------------------------------------------------


def cache_root() -> Path:
    override = os.environ.get("BACPACK_SCALE_CACHE")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "bacpack-scale"
    return Path.home() / ".cache" / "bacpack-scale"


def cache_key(sizes: Sizes) -> str:
    """Changes with the schema, the builder and the sizes, so a stale
    dataset is never reused."""
    from sqlalchemy.dialects import sqlite

    digest = hashlib.sha256()
    digest.update(f"{len(migrations.MIGRATIONS)}|{len(migrations.HISTORY_MIGRATIONS)}|".encode())
    for table in sorted(Base.metadata.tables.values(), key=lambda t: (t.schema or "", t.name)):
        digest.update(str(CreateTable(table).compile(dialect=sqlite.dialect())).encode())
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            digest.update(str(CreateIndex(index).compile(dialect=sqlite.dialect())).encode())
    digest.update(f"|{BUILDER_VERSION}|{sizes!r}".encode())
    return digest.hexdigest()[:16]


def dataset_name(sizes: Sizes) -> str:
    role = "install" if sizes.history_target is None else "full-history"
    return f"{role}-{sizes.devices}x{sizes.big_device_objects}"


def cache_dir(sizes: Sizes) -> Path:
    return cache_root() / f"{dataset_name(sizes)}-{cache_key(sizes)}"


def cached(sizes: Sizes, *, rebuild: bool = False) -> Path:
    """The dataset's main database file, built once and kept. Another key's
    directory for the same dataset (an older schema or builder) and any
    leftover partial build are removed, which also keeps the disk use down."""
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    final = cache_dir(sizes)
    name = dataset_name(sizes)
    for other in root.glob(f"{name}-*"):
        if other != final or rebuild:
            shutil.rmtree(other, ignore_errors=True)
    if final.is_dir():
        return final / "bacpack.db"
    partial = final.with_name(f"{final.name}.partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    started = time.perf_counter()
    try:
        build(partial / "bacpack.db", sizes)
        problems = check_dataset(partial / "bacpack.db")
        if problems:
            raise RuntimeError("the built dataset is inconsistent: " + "; ".join(problems[:5]))
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    build_seconds[name] = time.perf_counter() - started
    return final / "bacpack.db"


def working_copy(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "bacpack.db"
    # A previous copy's -wal would be replayed onto this one. unlink raises
    # if a connection still holds it, which is better than a silent replay.
    for path in (dest, history_db_path(dest)):
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    for source, target in ((src, dest), (history_db_path(src), history_db_path(dest))):
        shutil.copyfile(source, target)
        _read_through(target)
    return dest


def _read_through(path: Path) -> None:
    """Pull a copy's pages into the OS cache. On XFS `copyfile` makes a
    reflink that shares the cached file's extents but none of its cached
    pages, so write scenarios would time disk reads. A running
    install's database is warm."""
    buffer = bytearray(8 << 20)
    with open(path, "rb", buffering=0) as file:
        while file.readinto(buffer):
            pass


# --- The build ------------------------------------------------------------------


def _fmt(value) -> str:
    return value.strftime(_TS_FORMAT)


def _chunks(rows: Iterable[tuple], size: int) -> Iterator[list[tuple]]:
    iterator = iter(rows)
    while chunk := list(itertools.islice(iterator, size)):
        yield chunk


def _insert_grouped(connection, model, rows: list[dict]) -> None:
    """Rows with the same keys go in one executemany; omitted columns take
    their model defaults, as an ORM insert would give them."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(tuple(sorted(row)), []).append(row)
    for group in groups.values():
        connection.execute(insert(model.__table__), group)


def _object(device_id: int, object_id: int, object_type: str, instance: int, name: str, **extra) -> dict:
    return {
        "id": object_id,
        "device_id": device_id,
        "object_type": object_type,
        "instance_number": instance,
        "name": name,
        **extra,
    }


def _big_device_rows(sizes: Sizes) -> list[dict]:
    counts = _counts(sizes)
    rows: list[dict] = []
    next_id = itertools.count(1)
    trended_lo = sizes.full_logs + 2
    trended_hi = trended_lo + sizes.trended_objects
    alarm_lo = trended_hi
    alarm_hi = alarm_lo + counts.alarming_each

    def values(prefix: str, object_type: str, count: int, **extra_for) -> None:
        for instance in range(1, count + 1):
            extra = dict(extra_for)
            if object_type == "analog-value":
                extra["units"] = "noUnits"
                if instance < trended_hi:
                    extra.update(trended=True, trend_mode="interval", trend_interval_seconds=60)
                elif alarm_lo <= instance < alarm_hi:
                    extra.update(
                        alarming_enabled=True,
                        notification_class_instance=1,
                        alarm_high_limit=90.0,
                        alarm_low_limit=10.0,
                        alarm_deadband=1.0,
                    )
            elif object_type == "binary-value" and instance <= counts.alarming_each:
                extra.update(alarming_enabled=True, notification_class_instance=1, alarm_value=True)
            elif object_type == "analog-input":
                extra["units"] = "noUnits"
            rows.append(
                _object(BIG_DEVICE_ID, next(next_id), object_type, instance, f"{prefix}-{instance}", **extra)
            )

    values("AV", "analog-value", counts.analog_values)
    values("AI", "analog-input", counts.per_type)
    values("BV", "binary-value", counts.per_type)
    values("BI", "binary-input", counts.per_type)
    values("MSV", "multi-state-value", counts.per_type, state_text=["Off", "Low", "High"])
    weekly = [
        [{"time": "06:00", "value": "21"}, {"time": "18:00", "value": "16"}] for _ in range(7)
    ]
    for instance in range(1, counts.schedules + 1):
        rows.append(
            _object(
                BIG_DEVICE_ID,
                next(next_id),
                "schedule",
                instance,
                f"SCH-{instance}",
                schedule_value_type="analog",
                schedule_default="0",
                weekly_schedule=weekly,
            )
        )
    for instance in range(1, counts.calendars + 1):
        rows.append(
            _object(
                BIG_DEVICE_ID,
                next(next_id),
                "calendar",
                instance,
                f"CAL-{instance}",
                date_list=[{"kind": "date", "year": None, "month": 12, "day": 25, "day_of_week": None}],
            )
        )
    for instance in range(1, counts.notification_classes + 1):
        rows.append(
            _object(BIG_DEVICE_ID, next(next_id), "notification-class", instance, f"NC-{instance}")
        )
    return rows


def _other_device_rows(sizes: Sizes, ids: DatasetIds) -> list[dict]:
    rows = []
    next_id = sizes.big_device_objects + 1
    for device_id in ids.other_device_ids:
        for instance in range(1, sizes.objects_per_other_device + 1):
            rows.append(
                _object(device_id, next_id, "analog-value", instance, f"AV-{instance}", units="noUnits")
            )
            next_id += 1
    for position, object_id in enumerate(ids.extra_trended_ids):
        instance = 1000 + position
        rows.append(
            _object(
                ids.other_device_ids[position % len(ids.other_device_ids)],
                object_id,
                "analog-value",
                instance,
                f"AV-{instance}",
                units="noUnits",
                trended=True,
                trend_mode="interval",
                trend_interval_seconds=60,
            )
        )
    return rows


def _sample_plan(sizes: Sizes, ids: DatasetIds) -> list[tuple[int, int]]:
    """(object id, sample count) for every log, in insert order."""
    plan = [(object_id, sizes.full_log_samples) for object_id in ids.full_log_ids]
    plan.append((ids.small_twin_id, sizes.small_twin_samples))
    plan += [(object_id, sizes.samples_per_trended) for object_id in ids.trended_ids]
    plan += list(zip(ids.extra_trended_ids, _extra_sample_counts(sizes)))
    return plan


def _sample_rows(plan: list[tuple[int, int]], grid: list[str]) -> Iterator[tuple]:
    values = [float((j * 7) % 100) for j in range(100)]
    size = len(grid)
    for object_id, count in plan:
        offset = size - count
        for k in range(count):
            yield (object_id, k + 1, grid[offset + k], values[k % 100], 0.0, "value")


def _history_indexes() -> list:
    return [index for index in TrendSample.__table__.indexes]


def _build_samples(engine, sizes: Sizes, ids: DatasetIds, end) -> int:
    import datetime as dt

    plan = _sample_plan(sizes, ids)
    longest = max([count for _, count in plan] + [1])
    grid = [_fmt(end - dt.timedelta(seconds=60 * (longest - 1 - j))) for j in range(longest)]
    total = sum(count for _, count in plan)
    indexes = _history_indexes()
    sql = (
        "INSERT INTO history.trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    with engine.begin() as connection:
        for index in indexes:
            connection.execute(DropIndex(index))
    written = 0
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA history.synchronous=OFF")
        for chunk in _chunks(_sample_rows(plan, grid), _SAMPLE_BATCH):
            connection.exec_driver_sql(sql, chunk)
            written += len(chunk)
            if written % _COMMIT_EVERY < _SAMPLE_BATCH:
                connection.commit()
        connection.commit()
        for index in indexes:
            connection.execute(CreateIndex(index))
        connection.commit()
        state = [(object_id, count + 1, 0) for object_id, count in plan]
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO history.trend_state (object_id, next_sequence, last_notify_record) "
            "VALUES (?, ?, ?)",
            state,
        )
        trend_buffer.rebuild_tails(connection)
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO history.history_counters (id, trend_total_sample_count) VALUES (1, ?)",
            (total,),
        )
        connection.commit()
    finally:
        connection.close()
    return total


def _build_traffic_logs_requests(engine, sizes: Sizes, ids: DatasetIds, end) -> None:
    import datetime as dt

    detail = "who-is-request: low=0 high=4194303 from 192.168.1.50:47808 (segmentation not supported)"
    types = ("who-is", "i-am", "read-property", "write-property", "cov-notification")
    traffic = (
        (
            device_id,
            _fmt(end - dt.timedelta(seconds=(sizes.traffic_per_device - k) * 2)),
            types[k % len(types)],
            f"{detail} #{k}",
        )
        for device_id in ids.device_ids
        for k in range(sizes.traffic_per_device)
    )
    span = 14 * 86400

    def log_rows(device_id: int, count: int) -> Iterator[tuple]:
        step = span / max(count, 1)
        levels = ("INFO",) * 8 + ("WARNING", "ERROR")
        for k in range(count):
            yield (
                device_id,
                _fmt(end - dt.timedelta(seconds=span - k * step)),
                levels[k % 10],
                "bacpack.device_process",
                f"device {device_id} handled a request #{k} from 192.168.1.{k % 250} in {k % 40} ms",
            )

    big_logs = sizes.log_entries - sizes.small_twin_log_entries

    def requests() -> Iterator[tuple]:
        step = 86400 / max(sizes.client_requests, 1)
        for k in range(sizes.client_requests):
            created = end - dt.timedelta(seconds=86400 - k * step)
            yield (
                ids.big_device_id,
                "read",
                json.dumps({"address": "192.168.1.60", "object": f"analog-value:{k % 900}"}),
                "done",
                json.dumps({"value": float(k % 100)}),
                None,
                _fmt(created),
                _fmt(created + dt.timedelta(seconds=1)),
            )

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA history.synchronous=OFF")
        for chunk in _chunks(traffic, _SAMPLE_BATCH):
            connection.exec_driver_sql(
                "INSERT INTO history.traffic_log_entries (device_id, timestamp, event_type, detail) "
                "VALUES (?, ?, ?, ?)",
                chunk,
            )
        for rows in (log_rows(ids.big_device_id, big_logs), log_rows(ids.twin_log_device_id, sizes.small_twin_log_entries)):
            for chunk in _chunks(rows, _SAMPLE_BATCH):
                connection.exec_driver_sql(
                    "INSERT INTO history.log_entries (device_id, timestamp, level, logger_name, message) "
                    "VALUES (?, ?, ?, ?, ?)",
                    chunk,
                )
        for chunk in _chunks(requests(), _SAMPLE_BATCH):
            connection.exec_driver_sql(
                "INSERT INTO history.client_requests "
                "(device_id, kind, params, status, result, error, created_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                chunk,
            )
        connection.commit()


def build(path: Path, sizes: Sizes) -> None:
    ids = ids_for(sizes)
    engine = make_engine(path)
    try:
        init_db(engine)
        end = timeutil.db_utc_now().replace(microsecond=0)
        devices = [
            {"id": device_id, "name": f"Scale Device {device_id - BIG_DEVICE_ID}", "vendor_name": "BACpack"}
            for device_id in ids.device_ids
        ]
        objects = _big_device_rows(sizes) + _other_device_rows(sizes, ids)
        with engine.begin() as connection:
            connection.execute(insert(Device.__table__), devices)
            connection.execute(insert(DeviceRuntime.__table__), [{"device_id": d["id"]} for d in devices])
            for start in range(0, len(objects), 5000):
                _insert_grouped(connection, BacnetObject, objects[start : start + 5000])
            connection.execute(
                insert(ObjectRuntime.__table__), [{"object_id": row["id"]} for row in objects]
            )
        _build_samples(engine, sizes, ids, end)
        _build_traffic_logs_requests(engine, sizes, ids, end)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA main.wal_checkpoint(TRUNCATE)")
            connection.exec_driver_sql("PRAGMA history.wal_checkpoint(TRUNCATE)")
    finally:
        engine.dispose()


def check_dataset(path: Path) -> list[str]:
    """Problems with a built dataset, empty when it is consistent: what
    BACpack reads (the counter, each log's next sequence) must agree with
    the rows, and every device must build."""
    problems: list[str] = []
    engine = make_engine(path)
    try:
        with engine.connect() as connection:
            counter = connection.exec_driver_sql(
                "SELECT trend_total_sample_count FROM history.history_counters WHERE id = 1"
            ).scalar()
            actual = connection.exec_driver_sql("SELECT COUNT(*) FROM history.trend_samples").scalar()
            if counter != actual:
                problems.append(f"counter {counter} does not match {actual} samples")
            per_object = {
                object_id: (count, top)
                for object_id, count, top in connection.exec_driver_sql(
                    "SELECT object_id, COUNT(*), MAX(sequence) FROM history.trend_samples GROUP BY object_id"
                )
            }
            next_sequence = {
                object_id: sequence
                for object_id, sequence in connection.exec_driver_sql(
                    "SELECT object_id, next_sequence FROM history.trend_state"
                )
            }
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            devices = session.scalars(select(Device)).all()
            caps = {}
            for device in devices:
                rows = []
                for obj in device.objects:
                    row = object_config.row_from_object(obj)
                    rows.append(row)
                    caps[obj.id] = effective_trend_sample_cap(obj.trend_sample_cap)
                    try:
                        object_config.check_object(row)
                    except ValueError as exc:
                        problems.append(f"device {device.id} {obj.name}: {exc}")
                        break
                problem = object_config.check_device(device.name, rows)
                if problem:
                    problems.append(f"device {device.id}: {problem}")
        for object_id, (count, top) in per_object.items():
            if next_sequence.get(object_id) != top + 1:
                problems.append(
                    f"object {object_id}: next_sequence {next_sequence.get(object_id)} but last sequence {top}"
                )
            if count > caps.get(object_id, 0):
                problems.append(f"object {object_id}: {count} samples over its cap")
    finally:
        engine.dispose()
    return problems
