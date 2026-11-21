"""A Trend Log's buffer as a gap-free run of sequence numbers.

Every row takes the next sequence number, and rows leave a log only from its
oldest end (the per-object cap, retention and the global prune all delete the
oldest by id). So a log is always one unbroken run of sequence numbers, and
its record count and the row at a position come from arithmetic on the two
ends instead of a COUNT or an OFFSET over the log.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import bindparam, delete, select, text

from .models import TrendSample, TrendState

# Sequence numbers are Unsigned32 per spec; wrap back to 1 rather than overflow.
MAX_SEQUENCE = 2**32 - 1


@dataclass(frozen=True)
class LogEnd:
    id: int
    sequence: int


def log_ends(session, object_id: int) -> tuple[LogEnd, LogEnd] | None:
    """The oldest and newest row of a log by id, None when it is empty. Two
    separate seeks: SQLite's min/max shortcut applies only to a query that
    asks for one of them alone."""
    oldest = session.execute(
        select(TrendSample.id, TrendSample.sequence)
        .where(TrendSample.object_id == object_id)
        .order_by(TrendSample.id)
        .limit(1)
    ).first()
    if oldest is None:
        return None
    newest = session.execute(
        select(TrendSample.id, TrendSample.sequence)
        .where(TrendSample.object_id == object_id)
        .order_by(TrendSample.id.desc())
        .limit(1)
    ).first()
    return LogEnd(*oldest), LogEnd(*newest)


def record_count(ends: tuple[LogEnd, LogEnd] | None) -> int:
    if ends is None:
        return 0
    first, last = ends
    return (last.sequence - first.sequence) % MAX_SEQUENCE + 1


def sequence_at(first: LogEnd, position: int) -> int:
    """The sequence number of the record at `position` (1 = the oldest)."""
    return (first.sequence - 1 + position - 1) % MAX_SEQUENCE + 1


def count_records(session, object_id: int) -> int:
    return record_count(log_ends(session, object_id))


def delete_oldest(session, object_id: int, count: int) -> int:
    """Delete the `count` oldest rows of a log by id; returns the rows deleted."""
    if count <= 0:
        return 0
    oldest = (
        select(TrendSample.id)
        .where(TrendSample.object_id == object_id)
        .order_by(TrendSample.id)
        .limit(count)
    )
    return session.execute(delete(TrendSample).where(TrendSample.id.in_(oldest))).rowcount


def logs_led_by(session, chosen: dict[int, tuple[int, int]], logs_per_query: int) -> set[int]:
    """The logs among `chosen` ({object_id: (max_id, count)}) whose `count`
    oldest rows end at `max_id`, so deleting their `count` rows up to it
    leaves them gap-free. One query per `logs_per_query` logs, each log a
    range count on the (object_id, id) index."""
    led: set[int] = set()
    items = list(chosen.items())
    for start in range(0, len(items), logs_per_query):
        params = {}
        rows = []
        for i, (object_id, (max_id, count)) in enumerate(items[start : start + logs_per_query]):
            rows.append(f"(:o{i}, :m{i}, :c{i})")
            params.update({f"o{i}": object_id, f"m{i}": max_id, f"c{i}": count})
        query = text(
            f"WITH bounds(object_id, max_id, chosen) AS (VALUES {', '.join(rows)}) "
            "SELECT object_id FROM bounds WHERE chosen = ("
            "SELECT count(*) FROM history.trend_samples t "
            "WHERE t.object_id = bounds.object_id AND t.id <= bounds.max_id)"
        )
        led.update(session.scalars(query, params))
    return led


# Rows read per query while looking for the end of the run of expired rows,
# so a log with nothing expired reads a few rows, not a whole chunk.
_EXPIRY_LOOK_AHEAD = 256


def delete_oldest_before(session, object_id: int, cutoff: dt.datetime, limit: int) -> tuple[int, bool]:
    """Delete the leading run of a log's oldest rows stamped before `cutoff`,
    at most `limit` of them. Stops at the first row within retention,
    whatever lies behind it: a host clock that stepped back can leave an old
    stamp after a newer one, and cutting it out of the middle would leave a
    gap in the sequence numbers. Returns the rows deleted and whether the run
    ended (a row kept, or the log's end) rather than the limit."""
    run = 0
    last_id = None
    finished = False
    while run < limit and not finished:
        wanted = min(_EXPIRY_LOOK_AHEAD, limit - run)
        query = (
            select(TrendSample.id, TrendSample.timestamp)
            .where(TrendSample.object_id == object_id)
            .order_by(TrendSample.id)
            .limit(wanted)
        )
        if last_id is not None:
            query = query.where(TrendSample.id > last_id)
        rows = session.execute(query).all()
        for row in rows:
            if row.timestamp >= cutoff:
                finished = True
                break
            run += 1
            last_id = row.id
        else:
            finished = len(rows) < wanted  # the log ended
    if run:
        # The run is the log's leading rows, so everything up to its last id.
        session.execute(
            delete(TrendSample).where(TrendSample.object_id == object_id, TrendSample.id <= last_id)
        )
    return run, finished


# A loose index scan: one seek per log instead of a walk over every row.
_LOGS_WITH_ROWS = text(
    "WITH RECURSIVE logs(object_id) AS ("
    "SELECT min(object_id) FROM history.trend_samples "
    "UNION ALL "
    "SELECT (SELECT min(object_id) FROM history.trend_samples WHERE object_id > logs.object_id) "
    "FROM logs WHERE logs.object_id IS NOT NULL) "
    "SELECT object_id FROM logs WHERE object_id IS NOT NULL"
)


def logs_with_rows(session) -> list[int]:
    return list(session.execute(_LOGS_WITH_ROWS).scalars())


# --- The ordered tail ---------------------------------------------------------
# byTime needs the first or last row by wire time, and wire times aren't
# sorted over a whole log (a device clock set back, a DST change, a host clock
# stepping back). A log is therefore split into an unordered head and an
# ordered tail (TrendState.ordered_from_id on). Within a tail no stored
# timestamp is more than STEP_BACK_TOLERANCE below the highest before it, and
# its clock offsets differ by at most OFFSET_SPREAD_LIMIT: sub-second host
# corrections and TimeSynchronization offsets stay in one tail, a deliberate
# clock change starts a new one.
STEP_BACK_TOLERANCE = dt.timedelta(seconds=60)
OFFSET_SPREAD_LIMIT = 60.0


def advance_tail(
    state, previous_id: int | None, row_id: int, timestamp: dt.datetime, clock_offset: float | None
) -> None:
    """Fold the row just written into `state` (a TrendState). `previous_id` is
    the log's newest row before it. A row starts a new tail when the
    bookkeeping didn't see the row before it (written by something that
    doesn't maintain it, or the log was emptied) or when it breaks either
    tolerance."""
    offset = clock_offset or 0.0
    if (
        state.tail_max_timestamp is None
        or (previous_id or 0) != (state.tail_last_id or 0)
        or timestamp < state.tail_max_timestamp - STEP_BACK_TOLERANCE
        or max(state.tail_max_offset, offset) - min(state.tail_min_offset, offset) > OFFSET_SPREAD_LIMIT
    ):
        state.ordered_from_id = row_id
        state.tail_max_timestamp = timestamp
        state.tail_min_offset = offset
        state.tail_max_offset = offset
    else:
        state.tail_max_timestamp = max(state.tail_max_timestamp, timestamp)
        state.tail_min_offset = min(state.tail_min_offset, offset)
        state.tail_max_offset = max(state.tail_max_offset, offset)
    state.tail_last_id = row_id


def _stored(value: dt.datetime) -> str:
    return value.isoformat(sep=" ", timespec="microseconds")


_WHOLE_LOG_SUMMARY = (
    "SELECT object_id, min(id), max(id), min(off), max(off), min(back), max(ts) FROM ("
    "SELECT object_id, id, coalesce(clock_offset, 0.0) AS off, timestamp AS ts, "
    "julianday(timestamp) - max(julianday(timestamp)) OVER (PARTITION BY object_id ORDER BY id) AS back "
    "FROM history.trend_samples{where}) GROUP BY object_id"
)

_WRITE_TAIL = text(
    "UPDATE history.trend_state SET ordered_from_id = :ordered_from, tail_last_id = :last, "
    "tail_max_timestamp = :max_timestamp, tail_min_offset = :min_offset, tail_max_offset = :max_offset "
    "WHERE object_id = :object_id"
)


def rebuild_tails(connection, object_ids: list[int] | None = None) -> None:
    """Compute the tail bookkeeping of existing logs (every log, or just
    `object_ids`) from their rows, by the rules advance_tail applies one row at
    a time. A log that never breaks a tolerance, nearly all of them, is
    summarised by one pass of aggregates; only the rest are walked row by row.
    Logs without a trend_state row are left (the first insert creates one)."""
    where = ""
    params: dict = {}
    if object_ids is not None:
        if not object_ids:
            return
        where = " WHERE object_id IN :ids"
    summary = text(_WHOLE_LOG_SUMMARY.format(where=where))
    if object_ids is not None:
        summary = summary.bindparams(bindparam("ids", expanding=True, value=list(object_ids)))
    writes = []
    walk = []
    clean_seconds = STEP_BACK_TOLERANCE.total_seconds() - 1  # leave julianday's rounding to the exact walk
    for object_id, first_id, last_id, low, high, back, newest in connection.execute(summary, params):
        if -back * 86400.0 <= clean_seconds and high - low <= OFFSET_SPREAD_LIMIT:
            writes.append(
                {
                    "object_id": object_id,
                    "ordered_from": first_id,
                    "last": last_id,
                    "max_timestamp": _stored(dt.datetime.fromisoformat(newest)),
                    "min_offset": low,
                    "max_offset": high,
                }
            )
        else:
            walk.append(object_id)
    for object_id in walk:
        state = _Accumulator()
        previous = None
        rows = connection.execute(
            text(
                "SELECT id, timestamp, clock_offset FROM history.trend_samples "
                "WHERE object_id = :o ORDER BY id"
            ),
            {"o": object_id},
        )
        for row_id, stamp, offset in rows:
            advance_tail(state, previous, row_id, dt.datetime.fromisoformat(stamp), offset)
            previous = row_id
        if state.tail_max_timestamp is None:
            continue  # the log has no rows
        writes.append(
            {
                "object_id": object_id,
                "ordered_from": state.ordered_from_id,
                "last": state.tail_last_id,
                "max_timestamp": _stored(state.tail_max_timestamp),
                "min_offset": state.tail_min_offset,
                "max_offset": state.tail_max_offset,
            }
        )
    if writes:
        connection.execute(_WRITE_TAIL, writes)


class _Accumulator:
    """advance_tail's state for a walk that has no TrendState row to hold it."""

    ordered_from_id = 0
    tail_last_id = 0
    tail_max_timestamp: dt.datetime | None = None
    tail_min_offset: float | None = None
    tail_max_offset: float | None = None


# --- Starting byTime inside the ordered tail ----------------------------------


@dataclass(frozen=True)
class Tail:
    ordered_from_id: int
    min_offset: float
    max_offset: float


def trusted_tail(session, object_id: int, last: LogEnd) -> Tail | None:
    """The log's ordered tail, or None when the whole log has to be treated as
    unordered: the bookkeeping hasn't seen the log's newest row, so rows were
    added since it last ran."""
    state = session.get(TrendState, object_id)
    if (
        state is None
        or not state.tail_last_id
        or state.tail_last_id != last.id
        or not state.ordered_from_id
        or state.tail_min_offset is None
        or state.tail_max_offset is None
    ):
        return None
    return Tail(state.ordered_from_id, state.tail_min_offset, state.tail_max_offset)


def newer_start(session, object_id: int, tail: Tail, bound: dt.datetime) -> int:
    """Where a forward byTime walk can start in the tail. A candidate's stored
    time exceeds X = bound - the tail's highest offset. m is the tail row with
    the greatest (timestamp, id) at most X - STEP_BACK_TOLERANCE; every tail
    row before it was stored at most STEP_BACK_TOLERANCE after it (the tail's
    ordering rule), so at most X and no candidate. Without such a row, the
    tail's first."""
    limit = bound - dt.timedelta(seconds=tail.max_offset) - STEP_BACK_TOLERANCE
    found = session.execute(
        select(TrendSample.id)
        .where(
            TrendSample.object_id == object_id,
            TrendSample.id >= tail.ordered_from_id,
            TrendSample.timestamp <= limit,
        )
        .order_by(TrendSample.timestamp.desc(), TrendSample.id.desc())
        .limit(1)
    ).scalar()
    return tail.ordered_from_id if found is None else found


def older_start(session, object_id: int, tail: Tail, bound: dt.datetime) -> int | None:
    """Where a backward byTime walk can start in the tail: the mirror of
    newer_start. A candidate's stored time is below Y = bound - the tail's
    lowest offset; the tail row with the smallest (timestamp, id) at least
    Y + STEP_BACK_TOLERANCE has only rows after it stored at least Y, none a
    candidate. None when there is no such row: start at the newest."""
    limit = bound - dt.timedelta(seconds=tail.min_offset) + STEP_BACK_TOLERANCE
    return session.execute(
        select(TrendSample.id)
        .where(
            TrendSample.object_id == object_id,
            TrendSample.id >= tail.ordered_from_id,
            TrendSample.timestamp >= limit,
        )
        .order_by(TrendSample.timestamp, TrendSample.id)
        .limit(1)
    ).scalar()
