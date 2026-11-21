"""A Trend Log's record count and positions come from its gap-free run of
sequence numbers, and byTime starts from the timestamp index."""
from __future__ import annotations

import datetime as dt

import pytest
from bacpypes3.basetypes import Range, RangeByPosition, RangeBySequenceNumber, RangeByTime
from sqlalchemy import text

from bacpack import trend_buffer
from bacpack.models import BacnetObject, Device, TrendSample
from bacpack.objects import trend_log
from bacpack.trending import _MAX_SEQUENCE
from tests.scale.harness import StatementRecorder, grew_with_table

FULL = 32767
TWIN = 3277
START = dt.datetime(2026, 1, 1, 0, 0, 0)


def stored(value: dt.datetime) -> str:
    return value.isoformat(sep=" ", timespec="microseconds")


def make_object(session_factory, instance: int = 1, *, cap: int | None = None) -> int:
    with session_factory() as session:
        device = session.get(Device, 599001)
        if device is None:
            device = Device(id=599001, name="Test Device")
            session.add(device)
        obj = BacnetObject(
            device=device,
            object_type="analog-value",
            instance_number=instance,
            name=f"AV-{instance}",
            units="percent",
            trended=True,
            trend_mode="interval",
            trend_interval_seconds=60,
            trend_sample_cap=cap,
        )
        session.add(obj)
        session.commit()
        return obj.id


def insert_rows(session_factory, object_id: int, rows) -> None:
    """rows: (sequence, utc timestamp, clock_offset, kind) in id order, written
    raw (no bookkeeping, no cap)."""
    with session_factory() as session:
        session.connection().execute(
            text(
                "INSERT INTO history.trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
                "VALUES (:o, :s, :t, :v, :c, :k)"
            ),
            [
                {"o": object_id, "s": s, "t": stored(t), "v": float(s % 100), "c": c, "k": k}
                for s, t, c, k in rows
            ],
        )
        last = rows[-1][0] if rows else 0
        session.connection().execute(
            text(
                "INSERT INTO history.trend_state (object_id, next_sequence, last_notify_record) "
                "VALUES (:o, :n, 0) ON CONFLICT (object_id) DO UPDATE SET next_sequence = excluded.next_sequence"
            ),
            {"o": object_id, "n": last % _MAX_SEQUENCE + 1},
        )
        session.commit()


def minutely(count: int, *, first_sequence: int = 1):
    return [
        ((first_sequence - 1 + i) % _MAX_SEQUENCE + 1, START + dt.timedelta(minutes=i), 0.0, "value")
        for i in range(count)
    ]


def engine_of(session_factory):
    return session_factory.kw["bind"]


def test_record_count_of_an_empty_log_is_zero(session_factory):
    object_id = make_object(session_factory)
    with session_factory() as session:
        assert trend_buffer.count_records(session, object_id) == 0
        assert trend_buffer.log_ends(session, object_id) is None


def test_record_count_from_the_sequence_span(session_factory):
    object_id = make_object(session_factory)
    insert_rows(session_factory, object_id, minutely(10, first_sequence=5))
    with session_factory() as session:
        assert trend_buffer.count_records(session, object_id) == 10


def test_record_count_across_the_wrap(session_factory):
    object_id = make_object(session_factory)
    sequences = [_MAX_SEQUENCE - 1, _MAX_SEQUENCE, 1, 2]
    rows = [(s, START + dt.timedelta(minutes=i), 0.0, "value") for i, s in enumerate(sequences)]
    insert_rows(session_factory, object_id, rows)
    with session_factory() as session:
        assert trend_buffer.count_records(session, object_id) == 4


def test_sequence_at_across_the_wrap():
    first = trend_buffer.LogEnd(id=1, sequence=_MAX_SEQUENCE - 1)
    assert [trend_buffer.sequence_at(first, position) for position in (1, 2, 3, 4)] == [
        _MAX_SEQUENCE - 1,
        _MAX_SEQUENCE,
        1,
        2,
    ]


def test_log_ends_ignore_other_logs(session_factory):
    one = make_object(session_factory, 1)
    two = make_object(session_factory, 2)
    insert_rows(session_factory, one, minutely(3, first_sequence=1))
    insert_rows(session_factory, two, minutely(5, first_sequence=10))
    insert_rows(session_factory, one, minutely(2, first_sequence=4))
    with session_factory() as session:
        first, last = trend_buffer.log_ends(session, one)
        assert (first.sequence, last.sequence) == (1, 5)
        first, last = trend_buffer.log_ends(session, two)
        assert (first.sequence, last.sequence) == (10, 14)


def test_by_position_and_sequence_page_work_does_not_grow_with_the_log(session_factory):
    twin_id = make_object(session_factory, 1)
    full_id = make_object(session_factory, 2)
    insert_rows(session_factory, twin_id, minutely(TWIN))
    insert_rows(session_factory, full_id, minutely(FULL))
    for choice in (
        lambda n: Range(byPosition=RangeByPosition(referenceIndex=n // 2, count=20)),
        lambda n: Range(bySequenceNumber=RangeBySequenceNumber(referenceSequenceNumber=n // 2, count=20)),
    ):
        work = {}
        for name, object_id, size in (("twin", twin_id, TWIN), ("full", full_id, FULL)):
            with StatementRecorder(engine_of(session_factory)) as rec:
                window = trend_log.read_trend_window(
                    session_factory, object_id, "analog-value", choice(size), 1400
                )
            assert len(window.records) == 20
            work[name] = rec
        assert len(work["full"].statements) == len(work["twin"].statements)
        assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
            work["twin"].vm_steps,
            work["full"].vm_steps,
        )


def test_the_whole_log_page_work_does_not_grow_with_the_log(session_factory):
    twin_id = make_object(session_factory, 1)
    full_id = make_object(session_factory, 2)
    insert_rows(session_factory, twin_id, minutely(TWIN))
    insert_rows(session_factory, full_id, minutely(FULL))
    work = {}
    for name, object_id in (("twin", twin_id), ("full", full_id)):
        with StatementRecorder(engine_of(session_factory)) as rec:
            window = trend_log.read_trend_window(session_factory, object_id, "analog-value", None, 1400)
        assert window.records
        work[name] = rec
    assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps)


def test_a_position_page_reads_the_record_it_names(session_factory):
    object_id = make_object(session_factory)
    insert_rows(session_factory, object_id, minutely(50, first_sequence=_MAX_SEQUENCE - 9))
    choice = Range(byPosition=RangeByPosition(referenceIndex=12, count=2))
    window = trend_log.read_trend_window(session_factory, object_id, "analog-value", choice, 1400)
    assert window.first_sequence == 2  # position 12 = sequence MAX-9+11 wrapped
    assert not window.first_item


# --- Ordered-tail bookkeeping -------------------------------------------------

from bacpack import trending  # noqa: E402
from bacpack.models import TrendState  # noqa: E402

SECOND = dt.timedelta(seconds=1)


def advance(state, previous_id, row_id, seconds, offset):
    trend_buffer.advance_tail(state, previous_id, row_id, START + seconds * SECOND, offset)


def run_of(offsets_and_seconds):
    """Feed consecutive rows (ids 1..) into one TrendState."""
    state = TrendState()
    previous = None
    for row_id, (seconds, offset) in enumerate(offsets_and_seconds, start=1):
        advance(state, previous, row_id, seconds, offset)
        previous = row_id
    return state


def test_sub_second_offset_jitter_stays_one_tail():
    state = run_of([(0, 0.0), (60, 0.3), (120, -0.2), (180, 0.4)])
    assert state.ordered_from_id == 1
    assert (state.tail_min_offset, state.tail_max_offset) == (-0.2, 0.4)
    assert state.tail_last_id == 4


def test_offset_spread_past_the_limit_starts_a_new_tail():
    state = run_of([(0, 0.0), (60, 61.0)])
    assert state.ordered_from_id == 2
    assert (state.tail_min_offset, state.tail_max_offset) == (61.0, 61.0)


def test_offset_spread_at_the_limit_stays_one_tail():
    assert run_of([(0, 0.0), (60, 60.0)]).ordered_from_id == 1


def test_a_one_second_step_back_stays_one_tail():
    state = run_of([(0, 0.0), (60, 0.0), (59, 0.0), (120, 0.0)])
    assert state.ordered_from_id == 1
    assert state.tail_max_timestamp == START + 120 * SECOND


def test_a_step_back_past_the_tolerance_starts_a_new_tail():
    state = run_of([(1000, 0.0), (1060, 0.0), (1060 - 61, 0.0)])
    assert state.ordered_from_id == 3
    assert state.tail_max_timestamp == START + (1060 - 61) * SECOND


def test_a_null_offset_counts_as_zero():
    state = run_of([(0, None), (60, 0.5)])
    assert state.ordered_from_id == 1
    assert (state.tail_min_offset, state.tail_max_offset) == (0.0, 0.5)


def test_rows_written_without_the_bookkeeping_start_a_new_tail():
    state = run_of([(0, 0.0), (60, 0.0)])
    advance(state, 9, 10, 120, 0.0)  # row 9 was written by someone else
    assert state.ordered_from_id == 10
    assert state.tail_last_id == 10


def test_the_first_row_of_an_emptied_log_starts_a_new_tail():
    state = run_of([(0, 0.0), (60, 0.0)])
    advance(state, None, 3, 120, 0.0)  # the log was emptied: no previous row
    assert state.ordered_from_id == 3


def tail_columns(session_factory, object_id):
    with session_factory() as session:
        state = session.get(TrendState, object_id)
        return (
            state.ordered_from_id,
            state.tail_last_id,
            state.tail_max_timestamp,
            state.tail_min_offset,
            state.tail_max_offset,
        )


def newest_id(session_factory, object_id):
    with session_factory() as session:
        return trend_buffer.log_ends(session, object_id)[1].id


def test_record_samples_keeps_the_tail(session_factory):
    object_id = make_object(session_factory)
    for value in (1.0, 2.0, 3.0):
        trending.record_samples(session_factory, [(object_id, value)], clock_offset=0.0)
    ordered_from, last, max_timestamp, low, high = tail_columns(session_factory, object_id)
    with session_factory() as session:
        first_id = trend_buffer.log_ends(session, object_id)[0].id
    assert ordered_from == first_id
    assert last == newest_id(session_factory, object_id)
    assert max_timestamp is not None
    assert (low, high) == (0.0, 0.0)


def test_pre_sync_offset_committed_after_a_time_change_stays_correct(session_factory):
    object_id = make_object(session_factory)
    trending.record_samples(session_factory, [(object_id, 1.0)], clock_offset=0.0)
    trending.record_time_change(session_factory, 599001, -3600.0, -3600.0)
    time_change_id = newest_id(session_factory, object_id)
    assert tail_columns(session_factory, object_id)[0] == time_change_id
    # A sampler batch read before the sync commits after it, with the old offset.
    trending.record_samples(session_factory, [(object_id, 2.0)], clock_offset=0.0)
    late_id = newest_id(session_factory, object_id)
    assert tail_columns(session_factory, object_id)[0] == late_id


def test_rebuild_tails_matches_incremental_bookkeeping(session_factory):
    object_id = make_object(session_factory)
    for offset in (0.0, 0.3, -0.2, 100.0, 100.2, 0.0):
        trending.record_samples(session_factory, [(object_id, 1.0)], clock_offset=offset)
    before = tail_columns(session_factory, object_id)
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE history.trend_state SET ordered_from_id = 0, tail_last_id = 0, "
                "tail_max_timestamp = NULL, tail_min_offset = NULL, tail_max_offset = NULL"
            )
        )
        session.commit()
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection)
    assert tail_columns(session_factory, object_id) == before


def test_rebuild_tails_finds_the_tail_after_a_clock_set_back(session_factory):
    back = START - dt.timedelta(hours=2)
    rows = [
        (1, START, 0.0, "value"),
        (2, START + dt.timedelta(minutes=1), 0.0, "value"),
        (3, back, 0.0, "value"),
        (4, back + dt.timedelta(minutes=1), 0.0, "value"),
    ]
    object_id = make_object(session_factory)
    insert_rows(session_factory, object_id, rows)
    other = make_object(session_factory, 2)
    insert_rows(session_factory, other, minutely(5))
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection)
    with session_factory() as session:
        log_ids = [r.id for r in session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id)]
        other_first = session.query(TrendSample).filter_by(object_id=other).order_by(TrendSample.id).first().id
    ordered_from, last, max_timestamp, _low, _high = tail_columns(session_factory, object_id)
    assert (ordered_from, last) == (log_ids[2], log_ids[3])
    assert max_timestamp == back + dt.timedelta(minutes=1)
    assert tail_columns(session_factory, other)[0] == other_first


def test_rebuild_tails_can_be_limited_to_some_logs(session_factory):
    one = make_object(session_factory, 1)
    two = make_object(session_factory, 2)
    insert_rows(session_factory, one, minutely(3))
    insert_rows(session_factory, two, minutely(3))
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection, [two])
    assert tail_columns(session_factory, one)[0] == 0
    assert tail_columns(session_factory, two)[0] != 0


# --- byTime from the timestamp index ------------------------------------------

import random  # noqa: E402


def oracle(session, object_id):
    """Today's rule as a plain scan over every row, oldest first."""
    rows = session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id).all()
    return [(row.id, trend_log._wire_timestamp(row)) for row in rows]


def oracle_first_newer(rows, reference):
    return next((row_id for row_id, wire in rows if wire > reference), None)


def oracle_last_older(rows, reference):
    return next((row_id for row_id, wire in reversed(rows) if wire < reference), None)


def messy_rows(rng, count=300):
    """Rows with sub-second offset jitter and host clock steps back of 5 s
    (inside the tolerance); a two-hour device clock set back (with its
    time-change row), a 70 s host clock step back, then the clock set forward
    90 minutes and rows two minutes apart: the last tail is long and its
    offset is large."""
    rows = []
    stamp = START
    offset = 0.0
    for i in range(count):
        stamp += dt.timedelta(seconds=10 if i < 3 * count // 4 else 120)
        kind = "value"
        if rng.random() < 0.1:
            stamp -= dt.timedelta(seconds=15)
        if i % 40 == 0 and i < count // 4:
            offset = rng.uniform(-0.4, 0.4)
        if i == count // 4:
            offset = -7200.0
            kind = "time-change"
        if i == count // 2:
            stamp -= dt.timedelta(seconds=70)
        if i == 3 * count // 4:
            offset = 5400.0
        rows.append((i + 1, stamp, offset + rng.uniform(-0.2, 0.2), kind))
    return rows


def check_against_oracle(session_factory, object_id, rng, references=200):
    with session_factory() as session:
        rows = oracle(session, object_id)
        buffer = trend_log._Buffer(session, object_id)
        # Random references, and ones a fraction of a second either side of
        # rows' own times, where a step back decides the answer.
        picks = [START + dt.timedelta(seconds=rng.uniform(-2.5 * 3600, 5 * 3600)) for _ in range(references)]
        for _row_id, wire in rows[::2]:
            picks += [wire + dt.timedelta(seconds=delta) for delta in (-1.5, -0.5, 0.5, 1.5)]
        for reference in picks:
            assert buffer.first_newer(reference) == oracle_first_newer(rows, reference), reference
            assert buffer.last_older(reference) == oracle_last_older(rows, reference), reference


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_by_time_matches_the_full_scan_on_randomised_logs(session_factory, seed):
    rng = random.Random(seed)
    object_id = make_object(session_factory)
    insert_rows(session_factory, object_id, messy_rows(rng))
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection)
    with session_factory() as session:
        tail = trend_buffer.trusted_tail(session, object_id, trend_buffer.log_ends(session, object_id)[1])
    assert tail is not None and tail.ordered_from_id > 1  # a real head and a real tail
    check_against_oracle(session_factory, object_id, rng)


def test_by_time_with_untrusted_bookkeeping_still_answers_correctly(session_factory):
    rng = random.Random(7)
    object_id = make_object(session_factory)
    rows = messy_rows(rng)
    insert_rows(session_factory, object_id, rows[:250])
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection)
    # Rows written by something that doesn't keep the bookkeeping, with a
    # clock offset the tail has never seen.
    stamp = rows[249][1]
    late = [
        (251 + i, stamp + dt.timedelta(minutes=1 + i), 7200.0 if i < 10 else 0.0, "value") for i in range(49)
    ]
    insert_rows(session_factory, object_id, late)
    with session_factory() as session:
        assert trend_buffer.trusted_tail(session, object_id, trend_buffer.log_ends(session, object_id)[1]) is None
    check_against_oracle(session_factory, object_id, rng)


def test_by_time_on_a_log_the_upgrade_migration_started(session_factory):
    rng = random.Random(11)
    object_id = make_object(session_factory)
    insert_rows(session_factory, object_id, messy_rows(rng))
    from bacpack import migrations

    with engine_of(session_factory).begin() as connection:
        migrations._add_and_fill_trend_tails(connection)
    with session_factory() as session:
        tail = trend_buffer.trusted_tail(session, object_id, trend_buffer.log_ends(session, object_id)[1])
        assert tail.ordered_from_id == trend_buffer.log_ends(session, object_id)[1].id  # old rows are head
    check_against_oracle(session_factory, object_id, rng)
    # New records land in the tail and answers stay exact.
    for value in (1.0, 2.0, 3.0):
        trending.record_samples(session_factory, [(object_id, value)], clock_offset=0.0)
    check_against_oracle(session_factory, object_id, rng)


def jittered_log(size):
    """Offset jitter changing every 60 rows and a one-second step back every 500."""
    rng = random.Random(size)
    rows = []
    offset = 0.0
    stamp = START
    for i in range(size):
        stamp += dt.timedelta(minutes=1)
        if i % 60 == 0:
            offset = rng.uniform(-0.4, 0.4)
        shown = stamp - dt.timedelta(seconds=1) if i % 500 == 499 else stamp
        rows.append((i + 1, shown, offset, "value"))
    return rows


def test_by_time_page_work_with_clock_jitter_does_not_grow_with_the_log(session_factory):
    twin_id = make_object(session_factory, 1)
    full_id = make_object(session_factory, 2)
    insert_rows(session_factory, twin_id, jittered_log(TWIN))
    insert_rows(session_factory, full_id, jittered_log(FULL))
    with engine_of(session_factory).begin() as connection:
        trend_buffer.rebuild_tails(connection)
    for count in (20, -20):
        work = {}
        for name, object_id, size in (("twin", twin_id, TWIN), ("full", full_id, FULL)):
            reference = START + dt.timedelta(minutes=size // 2)
            choice = Range(byTime=RangeByTime(referenceTime=reference.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None), count=count))
            with StatementRecorder(engine_of(session_factory)) as rec:
                window = trend_log.read_trend_window(session_factory, object_id, "analog-value", choice, 1400)
            assert len(window.records) == 20
            work[name] = rec
        assert len(work["full"].statements) == len(work["twin"].statements)
        assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
            count,
            work["twin"].vm_steps,
            work["full"].vm_steps,
        )
