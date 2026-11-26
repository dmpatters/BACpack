"""Historical trending: sampling (interval + COV-driven) and retention
pruning.

TrendSample rows are the only store of an object's history: the Trends UI
and the Trend Log objects' ReadRange (application.py, objects/trend_log.py)
both read the rows this module writes.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import io
import logging
import threading
import time
import weakref
from collections import defaultdict, deque
from typing import Dict, List, Optional

from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import ObjectIdentifier
from sqlalchemy import delete, func, select, text, update

from . import bacpypes3_compat, db, device_clock, held_tasks, log_names, timeutil, trend_buffer
from .client_requests import prune_old_client_requests
from .db import delete_in_chunks, keep_history_wal_small
from .log_capture import prune_old_log_entries
from .models import (
    BacnetObject,
    Device,
    HistoryCounters,
    TrendSample,
    effective_trend_sample_cap,
    get_or_create_history_counters,
    get_or_create_settings,
    trend_state_of,
)

logger = logging.getLogger(__name__)

# One scheduler tick scans every interval-trended object, rather than one
# asyncio task per object.
SCHEDULER_TICK_SECONDS = 1.0

# The interval of an interval trend saved without one.
DEFAULT_TREND_INTERVAL_SECONDS = 60

_MAX_SEQUENCE = trend_buffer.MAX_SEQUENCE


def total_record_count(obj: BacnetObject) -> int:
    """Total_Record_Count for obj's Trend Log. `or 1`: no trend state yet,
    or an unflushed one with no column default yet."""
    next_sequence = obj.trend_state.next_sequence if obj.trend_state is not None else None
    return (next_sequence or 1) - 1


def pending_records(total: int, last_notify: int) -> int:
    """Records collected since Last_Notify_Record. Totals wrap within
    0.._MAX_SEQUENCE-1 (TrendState.next_sequence wraps back to 1), so the
    difference is taken modulo _MAX_SEQUENCE."""
    return (total - last_notify) % _MAX_SEQUENCE


def seed_last_notify_record(obj: BacnetObject, was_enabled: bool) -> None:
    """Turning BUFFER_READY reporting on starts from the log's current
    total, so the first notification doesn't cover its whole history.
    Does nothing unless reporting just went from off to on."""
    if obj.trend_reporting_enabled and not was_enabled:
        trend_state_of(obj).last_notify_record = total_record_count(obj)

# Estimated storage per trend sample, in bytes. The UI's two storage calculators
# (ObjectTrend.tsx, the Trend Storage page) use this same figure.
BYTES_PER_TREND_SAMPLE = 105

# Fixed on purpose, not user-configurable.
TREND_HARD_CAP_BYTES = 1024 * 1024 * 1024

def effective_trend_limit_bytes(settings) -> int:
    """The global ceiling record_sample enforces against: the configured
    soft limit if set (0 or None means disabled), else the fixed hard cap."""
    if settings.trend_soft_limit_mb:
        return settings.trend_soft_limit_mb * 1024 * 1024
    return TREND_HARD_CAP_BYTES


def _adjust_total_sample_count(session, delta: int) -> None:
    """Every code path that inserts or deletes a TrendSample row must call
    this. Clamped at 0: a negative running total would be worse than a clamp."""
    counters = get_or_create_history_counters(session)
    counters.trend_total_sample_count = max(0, counters.trend_total_sample_count + delta)


def prune_object_to_cap(session, object_id: int, cap: int) -> int:
    """Delete this object's oldest samples until it's at or under cap.
    Returns the rows deleted; the caller adjusts the running total, since
    callers differ in whether they're mid-way through their own +1."""
    excess = trend_buffer.count_records(session, object_id) - cap
    if excess <= 0:
        return 0
    stale_ids = [
        row.id
        for row in session.query(TrendSample.id)
        .filter_by(object_id=object_id)
        # By id, not sequence: sequence wraps back to 1 at _MAX_SEQUENCE.
        .order_by(TrendSample.id)
        .limit(excess)
    ]
    session.query(TrendSample).filter(TrendSample.id.in_(stale_ids)).delete(synchronize_session=False)
    return len(stale_ids)


def apply_sample_cap(session, obj: BacnetObject) -> int:
    """Trim `obj`'s samples to its saved cap and lower the running total, in
    the caller's transaction (which holds db.lock_both_files if it also
    changes the cap). Every save that can lower a cap calls this, or
    trim_devices_to_caps after it commits. Returns the rows deleted."""
    removed = prune_object_to_cap(session, obj.id, effective_trend_sample_cap(obj.trend_sample_cap))
    if removed:
        _adjust_total_sample_count(session, -removed)
    return removed


def trim_devices_to_caps(session, device_ids) -> int:
    """After a sync has committed caps (config import, CSV sync, restore):
    trim every object of these devices that holds more than its cap, one
    transaction per object (at most MAX_TREND_SAMPLE_CAP rows), so a big
    import never holds both files' locks while it deletes. A sample landing
    meanwhile is trimmed by its own insert. Returns the rows deleted."""
    capped = session.execute(
        select(BacnetObject.id)
        .where(BacnetObject.device_id.in_(list(device_ids)), BacnetObject.trend_sample_cap.is_not(None))
        .order_by(BacnetObject.id)
    ).scalars().all()
    session.rollback()
    total = 0
    for object_id in capped:
        obj = session.get(BacnetObject, object_id)
        if obj is None:
            continue
        removed = apply_sample_cap(session, obj)
        if removed:
            session.commit()
            total += removed
        else:
            session.rollback()
    return total


_OBJECT_ID_CHUNK = 500


def release_trend_samples_for_objects(session, object_ids: list[int]) -> int:
    """Call BEFORE deleting BacnetObject rows (directly or via a Device
    cascade): the cascade removes their TrendSample rows without going
    through this module, so this decrements trend_total_sample_count for
    them. Deletes nothing itself. Returns the rows counted."""
    if not object_ids:
        return 0
    # Distinct ids in slices: a batch delete's ids are client-supplied, and
    # SQLite refuses more than 32766 bound variables.
    distinct_ids = list(dict.fromkeys(object_ids))
    count = 0
    for start in range(0, len(distinct_ids), _OBJECT_ID_CHUNK):
        chunk = distinct_ids[start : start + _OBJECT_ID_CHUNK]
        count += session.query(TrendSample).filter(TrendSample.object_id.in_(chunk)).count()
    if count:
        _adjust_total_sample_count(session, -count)
    return count


def delete_trend_samples_before_locking(session_factory, object_ids: list[int]) -> int:
    """Delete the objects' trend samples in history-only chunks, lowering the
    running total with each, before a delete takes both files' locks
    (db.lock_both_files): the locked delete then finds only the few samples a
    sampler added meanwhile, so main isn't held for a delete of millions.
    Returns the rows deleted."""
    distinct_ids = list(dict.fromkeys(object_ids))
    total = 0
    for start in range(0, len(distinct_ids), _OBJECT_ID_CHUNK):
        chunk = distinct_ids[start : start + _OBJECT_ID_CHUNK]
        total += delete_in_chunks(
            session_factory,
            TrendSample,
            TrendSample.object_id.in_(chunk),
            on_chunk=lambda session, deleted: _adjust_total_sample_count(session, -deleted),
        )
    return total


def prune_global_to_limit(session, limit_bytes: int, *, max_rows: int | None = None) -> int:
    """Delete the globally-oldest samples across every object until the
    running total is at or under limit_bytes, in chunks so a big drop in the
    limit doesn't hold SQLite's write lock row by row. `id` breaks ties as
    `timestamp` has only second resolution. Returns the rows deleted; the
    caller adjusts the running total, as for prune_object_to_cap. `max_rows`
    bounds the delete, so a device's own write never carries a big prune."""
    counters = get_or_create_history_counters(session)
    limit_samples = limit_bytes // BYTES_PER_TREND_SAMPLE
    excess = counters.trend_total_sample_count - limit_samples
    if max_rows is not None:
        excess = min(excess, max_rows)
    if excess <= 0:
        return 0

    oldest = session.execute(
        select(TrendSample.object_id, TrendSample.id)
        .order_by(TrendSample.timestamp.asc(), TrendSample.id.asc())
        .limit(excess)
    )
    return _delete_oldest_per_log(session, oldest)


# SQLite refuses more than 32,766 bound variables in a statement.
_IDS_PER_STATEMENT = 10_000


def _delete_oldest_per_log(session, chosen) -> int:
    """The globally-oldest samples, `chosen` as (object_id, id), decide how
    many rows each log loses; each loses them from its own oldest end, so its
    sequence numbers stay gap-free (trend_buffer) even where a host clock
    stepped back. Usually a log's chosen rows already are its oldest, and all
    such logs' rows go in one statement: a statement per log made a chunk
    hold the history write lock several times longer. The rest are deleted
    log by log. If the counter claims more excess than the table holds,
    `chosen` is shorter and that is what goes."""
    by_log: dict[int, list[int]] = defaultdict(list)
    for object_id, row_id in chosen:
        by_log[object_id].append(row_id)
    led = trend_buffer.logs_led_by(
        session, {object_id: (max(ids), len(ids)) for object_id, ids in by_log.items()}, _IDS_PER_STATEMENT // 3
    )
    ids = [row_id for object_id in led for row_id in by_log[object_id]]
    deleted = 0
    for start in range(0, len(ids), _IDS_PER_STATEMENT):
        part = ids[start : start + _IDS_PER_STATEMENT]
        deleted += session.execute(delete(TrendSample).where(TrendSample.id.in_(part))).rowcount
    return deleted + sum(
        trend_buffer.delete_oldest(session, object_id, len(ids))
        for object_id, ids in by_log.items()
        if object_id not in led
    )


# One chunk's transaction holds the history file's write lock.
# On the slower disk measured (a VM), a device's write typically waited
# 0.15-0.2 s with chunks of 1,000 to 5,000 rows; at 10,000 it reached 0.4 s.
# The largest of those keeps the whole prune short.
_BACKGROUND_PRUNE_CHUNK_SIZE = 5_000
_BACKGROUND_PRUNE_PAUSE_SECONDS = 0.05

# A device's write trims at most this much of an excess; the background pruner
# removes the rest. Each row is scattered over the indexes: on that VM a write
# deleting 100 waited about 0.4 s, 10 about 0.25 s, 5 about 0.15 s.
_MAX_PRUNE_PER_WRITE = 5


def trend_over_limit(session) -> bool:
    """Whether the running total is above the effective limit (what the
    background pruner is working off)."""
    settings = get_or_create_settings(session)
    counters = get_or_create_history_counters(session)
    limit_samples = effective_trend_limit_bytes(settings) // BYTES_PER_TREND_SAMPLE
    return counters.trend_total_sample_count > limit_samples


def _chunk_wanted(session) -> int:
    settings = get_or_create_settings(session)
    counters = get_or_create_history_counters(session)
    limit_samples = effective_trend_limit_bytes(settings) // BYTES_PER_TREND_SAMPLE
    return min(counters.trend_total_sample_count - limit_samples, _BACKGROUND_PRUNE_CHUNK_SIZE)


def prune_chunk_to_limit(session_factory) -> int:
    """One transaction: delete up to a chunk of the globally-oldest samples
    toward the limit read fresh from the settings, lowering the running total
    with it. Returns the rows deleted."""
    with session_factory() as session:
        if _chunk_wanted(session) <= 0:
            return 0
        # The transaction only starts at the first write, so the read above
        # may be stale by a device's commit. A no-op update takes the history
        # write lock; the counter is then read, and later adjusted, under it.
        session.execute(
            update(HistoryCounters).values(trend_total_sample_count=HistoryCounters.trend_total_sample_count)
        )
        session.expire_all()
        wanted = _chunk_wanted(session)
        if wanted <= 0:
            session.rollback()
            return 0
        oldest = session.execute(
            select(TrendSample.object_id, TrendSample.id)
            .order_by(TrendSample.timestamp.asc(), TrendSample.id.asc())
            .limit(wanted)
        )
        deleted = _delete_oldest_per_log(session, oldest)
        _adjust_total_sample_count(session, -deleted)
        session.commit()
        keep_history_wal_small(session)
        return deleted


def prune_to_limit(session_factory) -> int:
    """Chunk the history down to the limit, pausing between chunks so device
    writes get the lock. Blocking: run it in a worker thread. A chunk that
    deletes nothing while the counter still claims an excess means the counter
    is wrong; it is resynced and the loop ends. Returns the rows deleted."""
    total = 0
    while True:
        deleted = prune_chunk_to_limit(session_factory)
        total += deleted
        if deleted == 0:
            with session_factory() as session:
                still_over = trend_over_limit(session)
            if still_over:
                resync_trend_total_sample_count(session_factory)
            return total
        time.sleep(_BACKGROUND_PRUNE_PAUSE_SECONDS)


def _numeric_present_value(live_obj) -> float:
    """Collapse an object's presentValue to a float for TrendSample.value.
    float() handles Real, BinaryPV (0/1), multi-state Enumerated and
    calendar Boolean as-is. A Schedule is the exception: bacpypes3's
    interpret_schedule stores its presentValue still wrapped in AnyAtomic
    (read_property returns it wrapped too), so it's unwrapped first.
    """
    value = live_obj.presentValue
    if isinstance(value, AnyAtomic):
        value = value.get_value()
    return float(value)


def _insert_row(session, obj: BacnetObject, value: float, *, kind: str, clock_offset: float) -> None:
    """Allocate obj's next sequence number, insert one TrendSample row and
    enforce obj's own sample-count cap. The global limit is left to the
    caller, once per commit."""
    state = trend_state_of(obj)
    sequence = state.next_sequence or 1
    state.next_sequence = sequence + 1 if sequence < _MAX_SEQUENCE else 1

    previous_id = session.scalar(select(func.max(TrendSample.id)).where(TrendSample.object_id == obj.id))
    sample = TrendSample(
        object_id=obj.id, sequence=sequence, value=value, kind=kind, clock_offset=clock_offset
    )
    session.add(sample)
    # The stored time is the column's server default, so it is read back.
    session.flush()
    session.refresh(sample, ["timestamp"])
    trend_buffer.advance_tail(state, previous_id, sample.id, sample.timestamp, clock_offset)
    _adjust_total_sample_count(session, 1)

    removed = prune_object_to_cap(session, obj.id, effective_trend_sample_cap(obj.trend_sample_cap))
    if removed:
        _adjust_total_sample_count(session, -removed)


def _enforce_global_limit(session, max_rows: Optional[int] = None) -> None:
    settings = get_or_create_settings(session)
    removed = prune_global_to_limit(
        session, effective_trend_limit_bytes(settings), max_rows=max_rows or _MAX_PRUNE_PER_WRITE
    )
    if removed:
        _adjust_total_sample_count(session, -removed)


def _existing_ids(session, object_ids: set) -> set:
    """The ids among `object_ids` still in bacnet_objects, read with SQL
    after flushing this transaction's history writes. The flush
    takes the history file's write lock, which a delete of one of these
    objects also needs (its before_delete listener clears their history
    rows), so no such delete can commit between this read and our commit;
    one that committed earlier shows here as a missing id."""
    session.flush()
    return set(session.scalars(select(BacnetObject.id).where(BacnetObject.id.in_(object_ids))))


def record_sample(
    session_factory, object_id: int, value: float, *, clock_offset: Optional[float] = None
) -> None:
    """Insert one TrendSample row, then enforce the object's sample cap and
    the global size limit. The object's
    id is re-read after the flush so a concurrent delete isn't resurrected
    (see _existing_ids). `clock_offset` defaults to this process's device
    clock.
    """
    if clock_offset is None:
        clock_offset = device_clock.offset()
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        if obj is None:
            return  # object was deleted out from under a still-running sampler

        _insert_row(session, obj, value, kind="value", clock_offset=clock_offset)
        _enforce_global_limit(session)
        if not _existing_ids(session, {object_id}):
            session.rollback()  # deleted meanwhile: nothing to record for it
            return
        session.commit()


# Samples per record_samples transaction, which holds the history file's write
# lock throughout: on the full-size dataset 50 took a median 0.07 s (0.37 s at
# worst on the slower disk); 25 didn't lower the worst case and doubled the
# pass.
_RECORD_BATCH_SIZE = 50


def record_samples(
    session_factory, samples: list[tuple[int, float]], *, clock_offset: Optional[float] = None
) -> list[int]:
    """record_sample for several objects in one transaction (one commit, the
    bulk of a sample's cost). An object deleted before the commit is left
    out. The global limit trims at least as many as this adds, so a device's
    writes never grow a total that is over it. Returns the ids recorded."""
    if clock_offset is None:
        clock_offset = device_clock.offset()
    skip: set = set()
    with session_factory() as session:
        # Bounded, as in record_time_change: each pass drops what it found gone.
        for _attempt in range(3):
            wanted = {object_id for object_id, _ in samples} - skip
            objects = {
                obj.id: obj
                for obj in session.scalars(select(BacnetObject).where(BacnetObject.id.in_(wanted)))
            }
            batch = [(object_id, value) for object_id, value in samples if object_id in objects]
            for object_id, value in batch:
                _insert_row(session, objects[object_id], value, kind="value", clock_offset=clock_offset)
            if batch:
                _enforce_global_limit(session, max_rows=max(_MAX_PRUNE_PER_WRITE, len(batch)))
            gone = set(objects) - _existing_ids(session, set(objects)) if objects else set()
            if not gone:
                session.commit()
                return [object_id for object_id, _ in batch]
            session.rollback()
            skip |= gone
        return []


def record_time_change(
    session_factory, device_id: int, delta_seconds: float, new_offset: float
) -> int:
    """One BACnet time-change log record (value = the clock jump in
    seconds) per trended object on this device -- a real buffer record on
    the wire, so it takes a sequence number and counts toward the caps like
    any sample. Returns the rows written."""
    skip: set = set()
    with session_factory() as session:
        # A trended object deleted between the query and the commit is left
        # out and the rest written again; bounded, each pass drops one.
        for _attempt in range(3):
            trended = [
                obj
                for obj in session.query(BacnetObject).filter_by(device_id=device_id, trended=True).all()
                if obj.id not in skip
            ]
            for obj in trended:
                _insert_row(session, obj, delta_seconds, kind="time-change", clock_offset=new_offset)
            if trended:
                _enforce_global_limit(session)
            ids = {obj.id for obj in trended}
            gone = ids - _existing_ids(session, ids) if trended else set()
            if not gone:
                session.commit()
                return len(trended)
            session.rollback()
            skip |= gone
        return 0


def prune_old_samples(session_factory) -> int:
    """Delete TrendSample rows older than ServerSettings.trend_retention_days
    (None keeps them forever), keeping trend_total_sample_count in sync.
    Returns the rows deleted.
    """
    with session_factory() as session:
        settings = get_or_create_settings(session)
        if settings.trend_retention_days is None:
            return 0

        cutoff = timeutil.db_cutoff(dt.timedelta(days=settings.trend_retention_days))
    with session_factory() as session:
        pending = deque(trend_buffer.logs_with_rows(session))
    # Each log from its oldest end (trend_buffer.delete_oldest_before). A
    # transaction fills up to a chunk of rows across logs, then commits, so
    # devices can write in between (one 8M-row DELETE locked them out for
    # 13 s) without a transaction per log.
    total = 0
    while pending:
        with session_factory() as session:
            # Take the history write lock before reading. The driver opens the
            # transaction only at the first write, so today the first read is
            # outside it; were it inside, a transaction that reads and then
            # writes would fail at once, without waiting, after another
            # connection's write.
            session.execute(
                update(HistoryCounters).values(trend_total_sample_count=HistoryCounters.trend_total_sample_count)
            )
            room = db.DELETE_CHUNK_SIZE
            while pending and room > 0:
                deleted, finished = trend_buffer.delete_oldest_before(session, pending[0], cutoff, room)
                room -= deleted
                if finished:
                    pending.popleft()
            removed = db.DELETE_CHUNK_SIZE - room
            if removed:
                _adjust_total_sample_count(session, -removed)
            session.commit()
            if removed:
                keep_history_wal_small(session)
        total += removed
    return total


def _snapshot_drift(session_factory) -> int:
    """The COUNT(*) of samples minus the counter, both read in one
    transaction (one WAL snapshot) so a device's write can't fall between the
    two reads. Every write moves the rows and the counter in one commit, so
    the snapshot is consistent and the difference is real drift."""
    with session_factory() as session:
        # Nothing here writes, so no lock; the snapshot starts at the first read.
        session.execute(text("BEGIN"))
        counter = session.scalar(select(HistoryCounters.trend_total_sample_count))
        actual = session.scalar(select(func.count()).select_from(TrendSample))
        session.rollback()
    return actual - (counter or 0)


def _apply_drift(session_factory, drift: int) -> None:
    """The correction as a delta, in one statement and no prior read: writes
    that committed since the snapshot are kept, and the busy timeout applies
    instead of a read-then-write failing at once."""
    with session_factory() as session:
        session.execute(
            update(HistoryCounters).values(
                trend_total_sample_count=func.max(0, HistoryCounters.trend_total_sample_count + drift)
            )
        )
        session.commit()


def resync_trend_total_sample_count(session_factory) -> int:
    """Correct HistoryCounters.trend_total_sample_count against a real
    COUNT(*), so a write path that misses _adjust_total_sample_count drifts
    for one retention tick at most instead of over-pruning forever. Daily,
    not per write: COUNT(*) is too slow for record_sample at ~10M rows, which
    is also why it takes no write lock. Returns the signed correction."""
    with session_factory() as session:
        get_or_create_history_counters(session)  # the row, if a fresh file lacks it
        session.commit()
    drift = _snapshot_drift(session_factory)
    if drift:
        _apply_drift(session_factory, drift)
    return drift


def fetch_samples(session_factory, object_id: int, *, limit: Optional[int] = None) -> List[dict]:
    """The most recent `limit` samples (all if omitted), oldest-first, for
    the web layer. Value rows only: time-change records are wire-only.
    """
    with session_factory() as session:
        query = (
            session.query(TrendSample)
            .filter_by(object_id=object_id, kind="value")
            # By id, not `sequence`: sequence wraps back to 1 at
            # _MAX_SEQUENCE, which would sort a post-wrap record first.
            .order_by(TrendSample.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        samples = list(reversed(query.all()))

    return [
        # TrendSample.timestamp is naive UTC; iso_utc adds the +00:00 Date.parse() needs.
        {"sequence": s.sequence, "timestamp": timeutil.iso_utc(s.timestamp), "value": s.value}
        for s in samples
    ]


def export_trend_csv(session_factory, object_id: int) -> str:
    with session_factory() as session:
        samples = (
            session.query(TrendSample)
            .filter_by(object_id=object_id, kind="value")
            .order_by(TrendSample.id)  # true chronological order -- see fetch_samples above
            .all()
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["sequence", "timestamp", "value"])
    for s in samples:
        writer.writerow([s.sequence, timeutil.iso_utc(s.timestamp), s.value])
    return buf.getvalue()


_PRUNER_RETRY_SECONDS = 60


class _PrunerStopped(Exception):
    """Raised inside a pass's worker thread once the pruner is stopping."""


class TrendPruner:
    """Runs `prune_to_limit` in a worker thread whenever kicked, and once at
    start, for the web process's lifetime.
    A kick during a pass makes exactly one more pass follow it, which reads the
    limit afresh, so kicks coalesce."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._lock = threading.Lock()
        self._kicks = 0
        self._idle = threading.Event()
        self._idle.set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle.clear()
        self._task = asyncio.ensure_future(self._run())

    def kick(self) -> None:
        """Safe from any thread. `_idle` drops here, in the caller's thread,
        so a wait_idle right after never sees the gap before the loop wakes."""
        if self._loop is None or self._stopping:
            return
        with self._lock:
            self._kicks += 1
            self._idle.clear()
        self._loop.call_soon_threadsafe(self._wake.set)

    def wait_idle(self, timeout: float) -> bool:
        """Blocks a non-loop thread until no pass is pending or running."""
        return self._idle.wait(timeout)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._wake.set()
            await self._task

    def _stoppable_factory(self):
        # The pass is a blocking loop in a thread that can't be cancelled; a
        # session factory that refuses once stopping ends it at its next chunk.
        def factory():
            if self._stopping:
                raise _PrunerStopped
            return self._session_factory()

        return factory

    async def _run(self) -> None:
        seen = 0
        while not self._stopping:
            failed = False
            try:
                deleted = await asyncio.to_thread(prune_to_limit, self._stoppable_factory())
                if deleted:
                    logger.info("trend limit: pruned %d samples", deleted)
            except _PrunerStopped:
                return
            except Exception:  # defensive: keep serving kicks, and retry
                logger.exception("trend limit pruning failed")
                failed = True
            with self._lock:
                if not failed and self._kicks == seen:
                    self._idle.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), _PRUNER_RETRY_SECONDS if failed else None)
            self._wake.clear()
            with self._lock:
                seen = self._kicks


async def run_retention_task(
    session_factory, *, pruner: TrendPruner | None = None, interval_seconds: float = 24 * 60 * 60
) -> None:
    """Daily retention for the parent process's lifetime (started from the
    FastAPI lifespan, so it runs with no device live): prunes trend samples,
    log entries and client requests, then resyncs the trend sample count.
    """
    while True:
        try:
            # In a worker thread: a big prune blocks the event loop (web UI,
            # connector poller) for its whole run otherwise.
            deleted = await asyncio.to_thread(prune_old_samples, session_factory)
            if deleted:
                logger.info("trend retention: pruned %d old %s", deleted, "sample" if deleted == 1 else "samples")
        except Exception:  # pragma: no cover - defensive, keep the loop alive
            logger.exception("trend retention pruning failed")
        try:
            deleted_logs = await asyncio.to_thread(prune_old_log_entries, session_factory)
            if deleted_logs:
                logger.info("log retention: pruned %d old log entr%s", deleted_logs, "y" if deleted_logs == 1 else "ies")
        except Exception:  # pragma: no cover - defensive, keep the loop alive
            logger.exception("log retention pruning failed")
        try:
            deleted_requests = await asyncio.to_thread(prune_old_client_requests, session_factory)
            if deleted_requests:
                logger.info(
                    "client request retention: pruned %d old %s",
                    deleted_requests,
                    "request" if deleted_requests == 1 else "requests",
                )
        except Exception:  # pragma: no cover - defensive, keep the loop alive
            logger.exception("client request pruning failed")
        try:
            drift = resync_trend_total_sample_count(session_factory)
            if drift:
                logger.warning(
                    "trend retention: trend_total_sample_count had drifted by %d, corrected",
                    drift,
                )
        except Exception:  # pragma: no cover - defensive, keep the loop alive
            logger.exception("trend total sample count resync failed")
        if pruner is not None:
            pruner.kick()
        await asyncio.sleep(interval_seconds)


# One per loop, so one per device process: every record write there (interval
# and COV samples, clock-change records) runs in a worker thread and takes a
# log's next sequence number, so two at once collide on it.
_write_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def write_lock() -> asyncio.Lock:
    """The lock a device process holds around each trend record write."""
    loop = asyncio.get_running_loop()
    lock = _write_locks.get(loop)
    if lock is None:
        lock = _write_locks[loop] = asyncio.Lock()
    return lock


class TrendSampler:
    """Interval-mode scheduling and COV-mode property-monitor hooks for every
    trended object on one running device."""

    def __init__(self, session_factory, app, device_id: int) -> None:
        self.session_factory = session_factory
        self.app = app
        self.device_id = device_id

        self._scheduler_task: Optional[asyncio.Task] = None
        # object_id -> next-due loop time, interval-mode objects only.
        self._due: Dict[int, float] = {}
        # object_id -> interval seconds, kept alongside _due for rescheduling.
        self._intervals: Dict[int, float] = {}
        # object_id -> live bacpypes3 object, interval-mode objects only.
        self._interval_objects: Dict[int, object] = {}
        # object_id -> (live object, callback), for unregistering.
        self._cov_hooks: Dict[int, tuple] = {}
        # object_id -> last recorded value, for the covIncrement filter.
        self._cov_last_recorded: Dict[int, float] = {}

    def start(self) -> None:
        self.resync()

    def resync(self) -> None:
        """(Re)resolve every trended object to the live one and hook it.

        A live object edit swaps in a new object,
        so ObjectReloader calls this after an apply; otherwise sampling stays
        on the retired object. An interval object keeps its due time unless
        its interval changed.
        """
        with self.session_factory() as session:
            device = session.get(Device, self.device_id)
            trended = [o for o in device.objects if o.trended] if device else []
            # detach the fields we need before the session closes
            rows = [
                (o.id, o.object_type, o.instance_number, o.name, o.trend_mode, o.trend_interval_seconds)
                for o in trended
            ]

        old_due, old_intervals = dict(self._due), dict(self._intervals)
        loop = asyncio.get_event_loop()
        # Everything is built before anything is swapped in, so an object
        # that can't be read costs only itself, never the hooks and timers
        # of the others (or the device's start).
        new_due: Dict[int, float] = {}
        new_intervals: Dict[int, float] = {}
        new_interval_objects: Dict[int, object] = {}
        new_hooks: Dict[int, tuple] = {}
        for object_id, object_type, instance_number, name, mode, interval_seconds in rows:
            live_obj = self.app.get_object_id(ObjectIdentifier((object_type, instance_number)))
            if live_obj is None:
                continue  # its build failed: it isn't served, so nothing to sample

            if mode == "cov":
                try:
                    new_hooks[object_id] = (live_obj, *self._prepare_cov_hook(object_id, live_obj))
                except Exception as exc:
                    logger.warning(
                        "trend sampling skipped for %s: %s",
                        log_names.object_ref(object_type, instance_number, name),
                        exc,
                    )
            else:
                interval = interval_seconds or DEFAULT_TREND_INTERVAL_SECONDS
                new_interval_objects[object_id] = live_obj
                new_intervals[object_id] = interval
                if old_intervals.get(object_id) == interval:
                    new_due[object_id] = old_due[object_id]
                else:
                    new_due[object_id] = loop.time() + interval

        self._clear()
        self._due.update(new_due)
        self._intervals.update(new_intervals)
        self._interval_objects.update(new_interval_objects)
        for object_id, (live_obj, callback, last_recorded) in new_hooks.items():
            self._cov_last_recorded[object_id] = last_recorded
            bacpypes3_compat.property_monitors(live_obj, "presentValue").append(callback)
            self._cov_hooks[object_id] = (live_obj, callback)

        if self._interval_objects and self._scheduler_task is None:
            self._scheduler_task = asyncio.ensure_future(self._scheduler_loop())
        elif not self._interval_objects and self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None

    def stop(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        self._clear()

    def _clear(self) -> None:
        for live_obj, callback in self._cov_hooks.values():
            bacpypes3_compat.remove_property_monitor(live_obj, "presentValue", callback)
        self._cov_hooks.clear()

        self._due.clear()
        self._intervals.clear()
        self._interval_objects.clear()
        self._cov_last_recorded.clear()

    # --- interval mode -----------------------------------------------------

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
            try:
                await self._sample_due()
            except Exception:
                # An escaped exception would end interval sampling for every
                # object until a restart.
                logger.exception("trend sampler scheduler tick failed")

    async def _sample_due(self) -> None:
        """Read every due object's value on the loop, then write them off it
        in record_samples transactions: 500 objects written one commit each
        held the device's loop for seconds."""
        now = asyncio.get_event_loop().time()
        samples = []
        for object_id, due_at in list(self._due.items()):
            if now < due_at:
                continue
            try:
                samples.append((object_id, _numeric_present_value(self._interval_objects[object_id])))
            except Exception:
                # One failing object mustn't stop the tick; its due time still
                # advances so it isn't retried (and logged) every second.
                logger.exception("trend sample failed for object %d", object_id)
            # Advance by the interval (no drift from a busy tick), but clamp to
            # `now` so after a long suspend it doesn't replay one sample of
            # today's value per missed tick.
            self._due[object_id] = max(due_at + self._intervals[object_id], now)
        clock_offset = device_clock.offset()
        for start in range(0, len(samples), _RECORD_BATCH_SIZE):
            batch = samples[start : start + _RECORD_BATCH_SIZE]
            try:
                async with write_lock():
                    recorded = await asyncio.to_thread(
                        record_samples, self.session_factory, batch, clock_offset=clock_offset
                    )
            except Exception:
                logger.exception("trend samples failed for %d objects", len(batch))
                continue
            if recorded:
                self.app.trend_records_added(recorded)

    # --- COV mode ------------------------------------------------------

    def _prepare_cov_hook(self, object_id: int, live_obj) -> tuple:
        """The callback for a presentValue property monitor and the value it
        starts from; raises if the object has no readable Present_Value.
        Changes nothing: resync installs it. A property monitor, not
        cov_notification(): bacpypes3 fires that once per subscriber and only
        builds COV detection on the first SubscribeCOV, so with no
        subscribers it never runs.
        """
        starting_value = _numeric_present_value(live_obj)
        increment = getattr(live_obj, "covIncrement", None)

        def _on_change(old_value, new_value) -> None:
            # Runs synchronously inside the property setter: filter inline,
            # defer the DB write.
            new_numeric = _numeric_present_value(live_obj)
            last = self._cov_last_recorded[object_id]
            significant = (
                abs(new_numeric - last) >= increment if increment is not None else new_numeric != last
            )
            if not significant:
                return
            self._cov_last_recorded[object_id] = new_numeric
            held_tasks.start(self._record_cov_sample(object_id, new_numeric, last))

        return _on_change, starting_value

    async def _record_cov_sample(self, object_id: int, value: float, previous: float) -> None:
        clock_offset = device_clock.offset()
        try:
            # Also keeps two quick COV changes from committing swapped.
            async with write_lock():
                await asyncio.to_thread(
                    record_sample, self.session_factory, object_id, value, clock_offset=clock_offset
                )
        except Exception:
            logger.exception("COV trend sample failed for object %d", object_id)
            # Later changes are measured against the last value actually
            # saved, unless a newer one has already moved it on.
            if self._cov_last_recorded.get(object_id) == value:
                self._cov_last_recorded[object_id] = previous
            return
        self.app.trend_records_added([object_id])
