"""Covers historical trending sampling and retention.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from bacpack import trending
from bacpack.object_builder import build_application
from bacpack.bacnet import write_feed_value
from bacpack.models import BacnetObject, Device, TrendSample, get_or_create_settings
from bacpack.models import get_or_create_history_counters, trend_state_of, TrendState
from bacpack.trending import (
    TrendSampler,
    _numeric_present_value,
    export_trend_csv,
    fetch_samples,
    prune_old_samples,
    record_sample,
    resync_trend_total_sample_count,
)
from bacpack.trending import (
    BYTES_PER_TREND_SAMPLE,
    TREND_HARD_CAP_BYTES,
    effective_trend_limit_bytes,
    prune_global_to_limit,
    prune_object_to_cap,
    release_trend_samples_for_objects,
)


def _make_device_and_object(session_factory, **object_kwargs):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device,
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            **object_kwargs,
        )
        session.add(device)
        session.add(obj)
        session.commit()
        return obj.id


async def _wait_for_samples(session_factory, object_id, minimum, timeout=5.0):
    """Polls until at least `minimum` samples exist (or `timeout` passes),
    rather than sleeping a fixed interval -- a fixed 0.16s sleep came up one
    sample short under full-suite load."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        with session_factory() as session:
            samples = session.query(TrendSample).filter_by(object_id=object_id).all()
        if len(samples) >= minimum or loop.time() >= deadline:
            return samples
        await asyncio.sleep(0.02)


def test_record_sample_allocates_sequence_and_persists(session_factory):
    object_id = _make_device_and_object(session_factory)

    record_sample(session_factory, object_id, 10.0)
    record_sample(session_factory, object_id, 20.0)

    with session_factory() as session:
        samples = (
            session.query(TrendSample)
            .filter_by(object_id=object_id)
            .order_by(TrendSample.sequence)
            .all()
        )
        obj = session.get(BacnetObject, object_id)

    assert [(s.sequence, s.value) for s in samples] == [(1, 10.0), (2, 20.0)]
    assert obj.trend_state.next_sequence == 3


def test_record_sample_missing_object_is_noop(session_factory):
    record_sample(session_factory, 999999, 10.0)  # no such object -- must not raise

    with session_factory() as session:
        assert session.query(TrendSample).count() == 0


def test_sequence_wraps_at_max(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        trend_state_of(obj).next_sequence = trending._MAX_SEQUENCE
        session.commit()

    record_sample(session_factory, object_id, 1.0)
    record_sample(session_factory, object_id, 2.0)

    with session_factory() as session:
        samples = (
            session.query(TrendSample)
            .filter_by(object_id=object_id)
            .order_by(TrendSample.id)
            .all()
        )
        obj = session.get(BacnetObject, object_id)

    assert [s.sequence for s in samples] == [trending._MAX_SEQUENCE, 1]
    assert obj.trend_state.next_sequence == 2


def test_prune_old_samples_respects_retention_days(session_factory):
    object_id = _make_device_and_object(session_factory)
    now = dt.datetime.now(dt.timezone.utc)

    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_retention_days = 7
        session.add(
            TrendSample(
                object_id=object_id, sequence=1, value=1.0, timestamp=now - dt.timedelta(days=10)
            )
        )
        session.add(
            TrendSample(
                object_id=object_id, sequence=2, value=2.0, timestamp=now - dt.timedelta(days=1)
            )
        )
        session.commit()

    deleted = prune_old_samples(session_factory)
    assert deleted == 1

    with session_factory() as session:
        remaining = session.query(TrendSample).filter_by(object_id=object_id).all()
    assert [s.value for s in remaining] == [2.0]


def test_prune_keeps_everything_when_retention_is_none(session_factory):
    object_id = _make_device_and_object(session_factory)
    now = dt.datetime.now(dt.timezone.utc)

    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_retention_days = None
        session.add(
            TrendSample(
                object_id=object_id, sequence=1, value=1.0, timestamp=now - dt.timedelta(days=9999)
            )
        )
        session.commit()

    assert prune_old_samples(session_factory) == 0
    with session_factory() as session:
        assert session.query(TrendSample).count() == 1


def test_prune_old_samples_decrements_the_running_counter(session_factory):
    object_id = _make_device_and_object(session_factory)
    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        settings.trend_retention_days = 7
        counters.trend_total_sample_count = 2
        session.add(
            TrendSample(object_id=object_id, sequence=1, value=1.0, timestamp=now - dt.timedelta(days=10))
        )
        session.add(
            TrendSample(object_id=object_id, sequence=2, value=2.0, timestamp=now - dt.timedelta(days=1))
        )
        session.commit()

    prune_old_samples(session_factory)

    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 1


# --- Self-heal for trend_total_sample_count drift (a future missed
# cascade-delete path should only drift the counter for at most one
# retention tick, not forever) -----------------------------------------


def test_resync_trend_total_sample_count_corrects_drift(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 999  # deliberately wrong
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.0))
        session.add(TrendSample(object_id=object_id, sequence=2, value=2.0))
        session.commit()

    drift = resync_trend_total_sample_count(session_factory)
    assert drift == 2 - 999

    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 2


def test_resync_trend_total_sample_count_is_a_noop_when_already_correct(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 1
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.0))
        session.commit()

    assert resync_trend_total_sample_count(session_factory) == 0

    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 1


# --- Naive-UTC timestamps need an explicit offset -------------------------
# TrendSample.timestamp is server_default=func.now() -> SQLite
# CURRENT_TIMESTAMP, which is UTC but stored naive. A bare .isoformat() on
# that produces an offset-less string, which JS Date.parse() reads as
# *local* -- shifting every chart point and CSV row by the server's UTC
# offset. TrafficLog.add (traffic.py) already stamps tzinfo=utc correctly;
# fetch_samples/export_trend_csv hadn't.


def test_fetch_samples_returns_explicit_utc_offset(session_factory):
    object_id = _make_device_and_object(session_factory)
    naive = dt.datetime(2026, 8, 28, 19, 19, 28)
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.0, timestamp=naive))
        session.commit()

    samples = fetch_samples(session_factory, object_id)
    assert samples[0]["timestamp"] == "2026-08-28T19:19:28+00:00"


def test_export_trend_csv_returns_explicit_utc_offset(session_factory):
    object_id = _make_device_and_object(session_factory)
    naive = dt.datetime(2026, 8, 28, 19, 19, 28)
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.0, timestamp=naive))
        session.commit()

    csv_text = export_trend_csv(session_factory, object_id)
    assert "2026-08-28T19:19:28+00:00" in csv_text
    assert "2026-08-28T19:19:28,1.0" not in csv_text  # the old offset-less form


@pytest.mark.asyncio
async def test_interval_sampler_records_due_samples(session_factory, monkeypatch):
    object_id = _make_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=0.05
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
    )
    av = app.get_object_name("AV-1")
    await write_feed_value(av, 42.0)

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    samples = await _wait_for_samples(session_factory, object_id, 2)
    sampler.stop()

    assert len(samples) >= 2
    assert all(s.value == 42.0 for s in samples)


@pytest.mark.asyncio
async def test_sampler_stop_cancels_interval_task(session_factory, monkeypatch):
    _make_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=0.05
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
    )

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    task = sampler._scheduler_task
    sampler.stop()
    await asyncio.sleep(0)

    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_interval_sampler_clamps_catch_up_after_a_long_gap(session_factory, monkeypatch):
    # Before the fix, `due_at + interval` (not clamped to `now`) replayed
    # one back-filled sample per scheduler tick -- every SCHEDULER_TICK_SECONDS,
    # not every configured interval -- all holding today's current value,
    # until it caught all the way back up. Simulates a laptop-sleep-sized
    # gap (100 missed intervals) by backdating a live sampler's `_due` entry
    # directly, same private-attribute-poking convention already used by
    # test_sampler_stop_cancels_interval_task above.
    object_id = _make_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=5
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
    )
    av = app.get_object_name("AV-1")
    await write_feed_value(av, 1.0)

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    loop = asyncio.get_event_loop()
    sampler._due[object_id] = loop.time() - 100 * 5  # 100 intervals overdue

    def count():
        with session_factory() as session:
            return session.query(TrendSample).filter_by(object_id=object_id).count()

    # Samples are written in a worker thread: wait for the first, not a fixed
    # time, then give the bug ~20 ticks to replay more. A write still in
    # flight at the count can only lower it.
    deadline = loop.time() + 10
    while count() == 0:
        assert loop.time() < deadline, "no sample written"
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    sampler.stop()

    # Clamping settles the schedule within a couple of ticks, not ~100.
    assert 1 <= count() <= 3


@pytest.mark.asyncio
async def test_sampler_scheduler_survives_an_unexpected_exception(session_factory, monkeypatch):
    object_id = _make_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=0.02
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")]
    )
    av = app.get_object_name("AV-1")
    await write_feed_value(av, 7.0)

    # record_samples is resolved from trending's own globals at call time, so
    # monkeypatching the module attribute intercepts it.
    call_count = 0
    real_record_samples = trending.record_samples
    loop = asyncio.get_running_loop()
    recorded_after_failure = asyncio.Event()

    def flaky_record_samples(session_factory, samples, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom -- simulated mid-tick failure")
        recorded = real_record_samples(session_factory, samples, **kwargs)
        loop.call_soon_threadsafe(recorded_after_failure.set)
        return recorded

    monkeypatch.setattr(trending, "record_samples", flaky_record_samples)

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    # Waits for the tick rather than sleeping a fixed time, which a loaded
    # parallel run could outlast.
    await asyncio.wait_for(recorded_after_failure.wait(), timeout=10)
    task = sampler._scheduler_task

    with session_factory() as session:
        samples = session.query(TrendSample).filter_by(object_id=object_id).all()

    # A later tick, after the first (raising) one, still recorded samples --
    # the loop kept ticking rather than dying on the first exception.
    assert call_count >= 2
    assert len(samples) >= 1
    assert not task.done()

    sampler.stop()


@pytest.mark.asyncio
async def test_cov_mode_sampler_records_on_significant_change(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")]
    )
    av = app.get_object_name("AV-1")

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()

    await write_feed_value(av, 42.0)  # jump of 42 >> default covIncrement of 1.0
    # The sample is saved in a worker thread: wait for it, not a fixed time.
    samples = await _wait_for_samples(session_factory, object_id, 1)

    sampler.stop()

    assert len(samples) == 1
    assert samples[0].value == 42.0


@pytest.mark.asyncio
async def test_cov_mode_sampler_ignores_insignificant_change(session_factory):
    """A change smaller than covIncrement shouldn't be recorded -- matches
    what a real COV subscriber would (not) be notified about either.
    """
    object_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")]
    )
    av = app.get_object_name("AV-1")

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()

    await write_feed_value(av, 0.1)  # default covIncrement is 1.0 -- below threshold
    await asyncio.sleep(0.05)

    sampler.stop()

    with session_factory() as session:
        samples = session.query(TrendSample).filter_by(object_id=object_id).all()

    assert samples == []


@pytest.mark.asyncio
async def test_cov_mode_sampler_hook_removed_on_stop(session_factory):
    _make_device_and_object(session_factory, trended=True, trend_mode="cov")

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")]
    )
    av = app.get_object_name("AV-1")

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    assert len(av._property_monitors["presentValue"]) == 1
    sampler.stop()
    assert len(av._property_monitors["presentValue"]) == 0


# --- Trended schedule/calendar ----------------------------------------------
# Schedule and calendar are the first trended types whose presentValue isn't
# a plain concretely-typed Atomic -- schedule's is polymorphic (AnyAtomic,
# unwrapped via .get_value()) and calendar's is a live-computed Boolean
# property, neither exercised by _numeric_present_value before this feature.
# A real bug was found here: interpret_schedule's own `self.presentValue =
# current_value` assignment leaves the value AnyAtomic-wrapped (confirmed
# live, even through a real read_property() call) -- float() on it raised
# TypeError before _numeric_present_value's AnyAtomic-aware unwrap was added.


def _make_schedule_device_and_object(session_factory, **object_kwargs):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=[[{"time": "00:00", "value": "55.0"}] for _ in range(7)],
            schedule_priority=8,
            **object_kwargs,
        )
        session.add(device)
        session.add(obj)
        session.commit()
        return obj.id


def _make_calendar_device_and_object(session_factory, **object_kwargs):
    today = dt.date.today()
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device,
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[
                {"kind": "date", "year": today.year, "month": today.month, "day": today.day}
            ],
            **object_kwargs,
        )
        session.add(device)
        session.add(obj)
        session.commit()
        return obj.id


@pytest.mark.asyncio
async def test_numeric_present_value_unwraps_schedules_any_atomic():
    # Direct regression test for the bug itself, isolated from the sampler
    # scheduling machinery: before the AnyAtomic-aware unwrap was added,
    # this raised `TypeError: float() argument must be a string or a real
    # number, not 'AnyAtomic'`.
    device = Device(id=599001, name="Test Device")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=[[{"time": "00:00", "value": "55.0"}] for _ in range(7)],
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    await asyncio.sleep(0.05)  # let interpret_schedule's first evaluation run
    sched_obj = app.get_object_name("Sched-1")

    assert _numeric_present_value(sched_obj) == 55.0


@pytest.mark.asyncio
async def test_interval_sampler_records_schedule_present_value(session_factory, monkeypatch):
    object_id = _make_schedule_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=0.05
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    device = Device(id=599001, name="Test Device")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=[[{"time": "00:00", "value": "55.0"}] for _ in range(7)],
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    # Let interpret_schedule's own call_soon-scheduled first evaluation run
    # before the sampler starts, same as the write-forwarding test in
    # test_bacnet.py.
    await asyncio.sleep(0.05)

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    samples = await _wait_for_samples(session_factory, object_id, 2)
    sampler.stop()

    assert len(samples) >= 2
    assert all(s.value == 55.0 for s in samples)


@pytest.mark.asyncio
async def test_interval_sampler_records_calendar_present_value(session_factory, monkeypatch):
    object_id = _make_calendar_device_and_object(
        session_factory, trended=True, trend_mode="interval", trend_interval_seconds=0.05
    )
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)

    today = dt.date.today()
    device = Device(id=599001, name="Test Device")
    cal = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        date_list=[
            {"kind": "date", "year": today.year, "month": today.month, "day": today.day}
        ],
    )
    app = build_application(device, [cal])

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    samples = await _wait_for_samples(session_factory, object_id, 2)
    sampler.stop()

    assert len(samples) >= 2
    # dateList matches today -> presentValue True -> coerced to 1.0, same as
    # every other trended type's float() normalization.
    assert all(s.value == 1.0 for s in samples)


# --- Trend storage sizing (enforcement) -------------------------------------


def test_effective_trend_limit_bytes_uses_soft_limit_when_set(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_soft_limit_mb = 10
        assert effective_trend_limit_bytes(settings) == 10 * 1024 * 1024


def test_effective_trend_limit_bytes_falls_back_to_hard_cap_when_unset(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_soft_limit_mb = None
        assert effective_trend_limit_bytes(settings) == TREND_HARD_CAP_BYTES


def test_effective_trend_limit_bytes_treats_zero_as_disabled(session_factory):
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_soft_limit_mb = 0
        assert effective_trend_limit_bytes(settings) == TREND_HARD_CAP_BYTES


def test_prune_object_to_cap_deletes_oldest_first(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        for i in range(5):
            session.add(TrendSample(object_id=object_id, sequence=i, value=float(i)))
        session.commit()

    with session_factory() as session:
        deleted = prune_object_to_cap(session, object_id, 3)
        session.commit()
    assert deleted == 2

    with session_factory() as session:
        remaining = (
            session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.sequence).all()
        )
    assert [s.sequence for s in remaining] == [2, 3, 4]  # oldest two (0, 1) removed


def test_prune_object_to_cap_is_a_noop_when_under_cap(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=0, value=1.0))
        session.commit()

    with session_factory() as session:
        deleted = prune_object_to_cap(session, object_id, 10)
    assert deleted == 0


def test_prune_global_to_limit_deletes_globally_oldest_across_two_objects(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj_a = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        obj_b = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        session.add_all([obj_a, obj_b])
        session.commit()
        object_a_id, object_b_id = obj_a.id, obj_b.id

    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as session:
        # obj_a's sample is the oldest overall, despite belonging to a
        # different object than obj_b's two samples -- proves the prune is
        # genuinely cross-object, not per-object.
        session.add(TrendSample(object_id=object_a_id, sequence=0, value=1.0, timestamp=now - dt.timedelta(hours=2)))
        session.add(TrendSample(object_id=object_b_id, sequence=0, value=2.0, timestamp=now - dt.timedelta(hours=1)))
        session.add(TrendSample(object_id=object_b_id, sequence=1, value=3.0, timestamp=now))
        session.commit()
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 3
        session.commit()

    with session_factory() as session:
        limit_bytes = 2 * BYTES_PER_TREND_SAMPLE  # room for exactly 2 samples
        deleted = prune_global_to_limit(session, limit_bytes)
        session.commit()
    assert deleted == 1

    with session_factory() as session:
        remaining = session.query(TrendSample).order_by(TrendSample.timestamp).all()
    assert [s.object_id for s in remaining] == [object_b_id, object_b_id]


def test_prune_global_to_limit_deletes_a_batch_of_oldest_when_excess_is_large(session_factory):
    # prune_global_to_limit used to delete one row per DB round trip in a
    # Python loop -- fine for excess==1 (the normal record_sample hot
    # path), pathological if the soft limit is lowered well below the
    # current total. This exercises an excess > 1 (10 samples down to 3)
    # to confirm the batched rewrite still deletes exactly the right count
    # and specifically the globally-oldest ones, across two objects.
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj_a = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        obj_b = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        session.add_all([obj_a, obj_b])
        session.commit()
        object_a_id, object_b_id = obj_a.id, obj_b.id

    now = dt.datetime.now(dt.timezone.utc)
    with session_factory() as session:
        # 10 samples total, alternating objects, strictly increasing
        # timestamps -- oldest 7 (index 0-6) should be the ones pruned.
        for i in range(10):
            object_id = object_a_id if i % 2 == 0 else object_b_id
            session.add(
                TrendSample(
                    object_id=object_id,
                    sequence=i,
                    value=float(i),
                    timestamp=now - dt.timedelta(hours=10 - i),
                )
            )
        session.commit()
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 10
        session.commit()

    with session_factory() as session:
        limit_bytes = 3 * BYTES_PER_TREND_SAMPLE  # room for exactly 3 samples
        deleted = prune_global_to_limit(session, limit_bytes)
        session.commit()
    assert deleted == 7

    with session_factory() as session:
        remaining = session.query(TrendSample).order_by(TrendSample.timestamp).all()
    assert [s.value for s in remaining] == [7.0, 8.0, 9.0]


def test_prune_global_to_limit_is_a_noop_when_under_limit(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=0, value=1.0))
        session.commit()
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 1
        session.commit()

    with session_factory() as session:
        deleted = prune_global_to_limit(session, TREND_HARD_CAP_BYTES)
    assert deleted == 0


def test_record_sample_enforces_per_trend_cap(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.trend_sample_cap = 2
        session.commit()

    for value in (1.0, 2.0, 3.0):
        record_sample(session_factory, object_id, value)

    with session_factory() as session:
        remaining = (
            session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.sequence).all()
        )
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert [s.value for s in remaining] == [2.0, 3.0]
    assert counters.trend_total_sample_count == 2


def test_record_sample_enforces_global_limit(session_factory, monkeypatch):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        settings = get_or_create_settings(session)
        settings.trend_soft_limit_mb = None  # disabled -- exercise the hard-cap fallback path
        session.commit()

    import bacpack.trending as trending_module

    # monkeypatch auto-restores the module attribute after this test, same
    # pattern already used elsewhere in this codebase (e.g.
    # test_device_process.py's STOP_CHECK_TICK_SECONDS patching).
    monkeypatch.setattr(trending_module, "TREND_HARD_CAP_BYTES", 2 * BYTES_PER_TREND_SAMPLE)
    for value in (1.0, 2.0, 3.0):
        record_sample(session_factory, object_id, value)

    with session_factory() as session:
        remaining = session.query(TrendSample).filter_by(object_id=object_id).all()
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert len(remaining) == 2
    assert counters.trend_total_sample_count == 2


def test_record_sample_skips_both_checks_when_both_knobs_unset(session_factory):
    """No per-trend cap, no soft limit -- the default cap and the hard cap are
    large enough that ordinary test volumes never approach them, so neither
    prune path does anything, and the counter simply grows by exactly 1 per
    call."""
    object_id = _make_device_and_object(session_factory)
    for value in (1.0, 2.0, 3.0):
        record_sample(session_factory, object_id, value)

    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 3


def test_release_trend_samples_for_objects_decrements_by_the_real_count(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        obj_a = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        obj_b = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        session.add_all([obj_a, obj_b])
        session.commit()
        object_a_id, object_b_id = obj_a.id, obj_b.id
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
        counters.trend_total_sample_count = 3
        session.add(TrendSample(object_id=object_a_id, sequence=0, value=1.0))
        session.add(TrendSample(object_id=object_a_id, sequence=1, value=2.0))
        session.add(TrendSample(object_id=object_b_id, sequence=0, value=3.0))
        session.commit()

    with session_factory() as session:
        removed = release_trend_samples_for_objects(session, [object_a_id])
        session.commit()
    assert removed == 2

    with session_factory() as session:
        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 1


def test_release_trend_samples_for_objects_handles_an_object_with_no_samples(session_factory):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        removed = release_trend_samples_for_objects(session, [object_id])
    assert removed == 0


# --- Time sync -----------------------------------------------------------

from bacpack import device_clock
from bacpack.trending import export_trend_csv, fetch_samples, record_sample, record_time_change


def test_record_sample_stores_the_current_device_clock_offset(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True)
    device_clock.set_offset(3600.0)

    record_sample(session_factory, object_id, 10.0)

    with session_factory() as session:
        sample = session.query(TrendSample).filter_by(object_id=object_id).one()
        assert sample.clock_offset == 3600.0
        assert sample.kind == "value"


def test_record_sample_accepts_an_explicit_clock_offset(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True)

    record_sample(session_factory, object_id, 10.0, clock_offset=-120.0)

    with session_factory() as session:
        assert session.query(TrendSample).filter_by(object_id=object_id).one().clock_offset == -120.0


def test_record_time_change_writes_one_row_per_trended_object(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        session.add(device)
        for i in (1, 2):
            session.add(
                BacnetObject(
                    device=device, object_type="analog-value", instance_number=i,
                    name=f"AV-{i}", trended=True,
                )
            )
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=3, name="AV-3")
        )
        session.commit()

    written = record_time_change(session_factory, 599001, -3600.0, -3600.0)

    assert written == 2
    with session_factory() as session:
        rows = session.query(TrendSample).all()
        assert len(rows) == 2
        assert {r.kind for r in rows} == {"time-change"}
        assert {r.value for r in rows} == {-3600.0}
        assert {r.clock_offset for r in rows} == {-3600.0}
        assert {r.sequence for r in rows} == {1}  # each object's own first sequence
        assert get_or_create_history_counters(session).trend_total_sample_count == 2


def test_record_time_change_respects_the_per_trend_cap(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True, trend_sample_cap=2)
    record_sample(session_factory, object_id, 1.0)
    record_sample(session_factory, object_id, 2.0)

    record_time_change(session_factory, 599001, 60.0, 60.0)

    with session_factory() as session:
        rows = session.query(TrendSample).order_by(TrendSample.id).all()
        assert [(r.kind, r.value) for r in rows] == [("value", 2.0), ("time-change", 60.0)]


def test_web_history_views_exclude_time_change_rows(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True)
    record_sample(session_factory, object_id, 1.0)
    record_time_change(session_factory, 599001, 60.0, 60.0)
    record_sample(session_factory, object_id, 2.0)

    assert [s["value"] for s in fetch_samples(session_factory, object_id)] == [1.0, 2.0]
    assert [s["sequence"] for s in fetch_samples(session_factory, object_id)] == [1, 3]
    csv_rows = export_trend_csv(session_factory, object_id).strip().splitlines()
    assert [row.split(",")[2] for row in csv_rows[1:]] == ["1.0", "2.0"]


# --- Edge cases ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_sampler_one_failing_object_does_not_starve_the_rest(session_factory, monkeypatch):
    """One exception used to abort the whole tick before the later objects
    were reached -- and since the failing object's due time never advanced,
    it did so again on every tick."""
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        bad = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1",
            trended=True, trend_mode="interval", trend_interval_seconds=0.05,
        )
        good = BacnetObject(
            device=device, object_type="analog-value", instance_number=2, name="AV-2",
            trended=True, trend_mode="interval", trend_interval_seconds=0.05,
        )
        session.add(device)
        session.commit()
        bad_id, good_id = bad.id, good.id

    app = build_application(
        Device(id=599001, name="Test Device"),
        [
            BacnetObject(object_type="analog-value", instance_number=1, name="AV-1"),
            BacnetObject(object_type="analog-value", instance_number=2, name="AV-2"),
        ],
    )
    failing = app.get_object_name("AV-1")
    real_numeric = trending._numeric_present_value

    def numeric(live_obj):
        if live_obj is failing:
            raise TypeError("not a number")
        return real_numeric(live_obj)

    monkeypatch.setattr(trending, "_numeric_present_value", numeric)

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    await _wait_for_samples(session_factory, good_id, 2)
    sampler.stop()

    with session_factory() as session:
        assert session.query(TrendSample).filter_by(object_id=good_id).count() >= 2
        assert session.query(TrendSample).filter_by(object_id=bad_id).count() == 0


def test_prune_object_to_cap_deletes_oldest_by_insertion_across_a_sequence_wrap(session_factory):
    """sequence wraps back to 1 after _MAX_SEQUENCE; ordering the prune by it
    deleted the newest (post-wrap) samples instead of the oldest."""
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        for sequence in (trending._MAX_SEQUENCE - 1, trending._MAX_SEQUENCE, 1, 2):
            session.add(TrendSample(object_id=object_id, sequence=sequence, value=float(sequence)))
            session.flush()
        session.commit()

    with session_factory() as session:
        prune_object_to_cap(session, object_id, 2)
        session.commit()

    with session_factory() as session:
        remaining = session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id).all()
    assert [s.sequence for s in remaining] == [1, 2]


# --- BUFFER_READY helpers ----------------------------------------------

from bacpack.trending import _MAX_SEQUENCE, pending_records, seed_last_notify_record, total_record_count


def test_pending_records_plain_and_across_the_wrap():
    assert pending_records(10, 7) == 3
    assert pending_records(0, 0) == 0
    # Totals run 0.._MAX_SEQUENCE-1: records MAX-1, MAX, 1 after a last
    # notify at MAX-2 leave the total at 1.
    assert pending_records(1, _MAX_SEQUENCE - 2) == 3


def test_seed_sets_last_notify_to_the_current_total_only_when_turned_on():
    obj = BacnetObject(trend_reporting_enabled=True, trend_state=TrendState(next_sequence=51, last_notify_record=0))
    seed_last_notify_record(obj, was_enabled=False)
    assert obj.trend_state.last_notify_record == 50
    assert total_record_count(obj) == 50

    trend_state_of(obj).last_notify_record = 40
    seed_last_notify_record(obj, was_enabled=True)  # already on: untouched
    assert obj.trend_state.last_notify_record == 40

    off = BacnetObject(trend_reporting_enabled=False, trend_state=TrendState(next_sequence=51, last_notify_record=0))
    seed_last_notify_record(off, was_enabled=False)
    assert off.trend_state.last_notify_record == 0


def test_total_record_count_of_an_unflushed_row_is_zero():
    assert total_record_count(BacnetObject()) == 0


# --- Big trend tables --------------------------------------------------------
# Deleting a device or object went through the ORM cascade, which loaded
# every trend sample / log row as an object first (600k rows: minutes and
# hundreds of MB); the daily retention prune was one unchunked DELETE that
# held the write lock long enough to fail device writes.

import contextlib

from sqlalchemy import event

from bacpack.models import ClientRequest, LogEntry, TrafficLogEntry


@contextlib.contextmanager
def _count_loads(*models):
    loads = []

    def on_load(target, context):
        loads.append(target)

    for model in models:
        event.listen(model, "load", on_load)
    try:
        yield loads
    finally:
        for model in models:
            event.remove(model, "load", on_load)


def _add_samples(session_factory, object_id, count, *, days_old=0, first_sequence=1):
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_old)
    with session_factory() as session:
        for i in range(count):
            session.add(TrendSample(object_id=object_id, sequence=first_sequence + i, value=float(i), timestamp=stamp))
        get_or_create_history_counters(session).trend_total_sample_count += count
        session.commit()


def test_deleting_an_object_bulk_deletes_its_samples_without_loading_them(session_factory):
    object_id = _make_device_and_object(session_factory)
    _add_samples(session_factory, object_id, 5)

    with _count_loads(TrendSample) as loads:
        with session_factory() as session:
            # As every delete path does: the counter is lowered by the release
            # call, the rows by the bulk-delete event.
            trending.release_trend_samples_for_objects(session, [object_id])
            session.delete(session.get(BacnetObject, object_id))
            session.commit()

    assert loads == []
    with session_factory() as session:
        assert session.query(TrendSample).count() == 0
        assert get_or_create_history_counters(session).trend_total_sample_count == 0


def test_deleting_a_device_bulk_deletes_its_history_without_loading_it(session_factory):
    object_id = _make_device_and_object(session_factory)
    _add_samples(session_factory, object_id, 3)
    with session_factory() as session:
        session.add(LogEntry(device_id=599001, level="INFO", logger_name="bacpack.x", message="m"))
        session.add(LogEntry(device_id=None, level="INFO", logger_name="bacpack.x", message="server"))
        session.add(TrafficLogEntry(device_id=599001, event_type="who-is", detail="d"))
        session.add(ClientRequest(device_id=599001, kind="read"))
        session.commit()

    with _count_loads(TrendSample, LogEntry, TrafficLogEntry, ClientRequest) as loads:
        with session_factory() as session:
            trending.release_trend_samples_for_objects(session, [object_id])
            session.delete(session.get(Device, 599001))
            session.commit()

    assert loads == []
    with session_factory() as session:
        assert session.query(TrendSample).count() == 0
        assert [e.message for e in session.query(LogEntry)] == ["server"]
        assert session.query(TrafficLogEntry).count() == 0
        assert session.query(ClientRequest).count() == 0
        assert get_or_create_history_counters(session).trend_total_sample_count == 0


def test_prune_old_samples_deletes_in_chunks(session_factory, monkeypatch):
    from bacpack import db

    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 2)
    object_id = _make_device_and_object(session_factory)
    _add_samples(session_factory, object_id, 5, days_old=10)
    _add_samples(session_factory, object_id, 1, days_old=0, first_sequence=6)
    with session_factory() as session:
        get_or_create_settings(session).trend_retention_days = 7
        session.commit()
        engine = session.get_bind()

    statements = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM HISTORY.TREND_SAMPLES"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        assert prune_old_samples(session_factory) == 5
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)

    assert len(statements) >= 3  # 2 + 2 + 1
    with session_factory() as session:
        assert session.query(TrendSample).count() == 1
        assert get_or_create_history_counters(session).trend_total_sample_count == 1


@pytest.mark.asyncio
async def test_retention_task_prunes_off_the_event_loop(session_factory, monkeypatch):
    threads = []
    import threading

    def fake_prune(factory):
        threads.append(threading.current_thread())
        return 0

    monkeypatch.setattr(trending, "prune_old_samples", fake_prune)
    task = asyncio.ensure_future(trending.run_retention_task(session_factory, interval_seconds=3600))
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert threads and threads[0] is not threading.main_thread()


# --- Database split -----------------------------------------------------

import sqlite3
import time

from bacpack.models import TrendState, get_or_create_history_counters


def _trended_object(session_factory):
    with session_factory() as session:
        get_or_create_settings(session)
        device = Device(id=1, name="D")
        device.objects = [
            BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", trended=True)
        ]
        session.add(device)
        session.commit()
        return device.objects[0].id


def test_first_sample_creates_the_trend_state_in_the_history_file(session_factory):
    object_id = _trended_object(session_factory)
    record_sample(session_factory, object_id, 1.5, clock_offset=0.0)
    record_sample(session_factory, object_id, 2.5, clock_offset=0.0)
    with session_factory() as session:
        state = session.get(TrendState, object_id)
        assert state.next_sequence == 3
        assert get_or_create_history_counters(session).trend_total_sample_count == 2


def test_record_sample_commits_while_the_config_file_is_write_locked(session_factory, tmp_path):
    object_id = _trended_object(session_factory)
    record_sample(session_factory, object_id, 1.0, clock_offset=0.0)  # counters row exists
    blocker = sqlite3.connect(tmp_path / "test.db", timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE devices SET name = 'busy'")
        started = time.monotonic()
        record_sample(session_factory, object_id, 2.0, clock_offset=0.0)
        assert time.monotonic() - started < 1.0
    finally:
        blocker.rollback()
        blocker.close()
    assert [s["value"] for s in fetch_samples(session_factory, object_id)] == [1.0, 2.0]


def test_deleting_an_object_removes_its_trend_state(session_factory):
    object_id = _trended_object(session_factory)
    record_sample(session_factory, object_id, 1.0, clock_offset=0.0)
    with session_factory() as session:
        session.delete(session.get(BacnetObject, object_id))
        session.commit()
    with session_factory() as session:
        assert session.get(TrendState, object_id) is None


# --- A sample racing a delete of its object ---------------------------------


def _delete_object(session_factory, object_id):
    with session_factory() as session:
        session.delete(session.get(BacnetObject, object_id))
        session.commit()


def _history_rows(session_factory, object_id):
    with session_factory() as session:
        return (
            session.query(TrendSample).filter_by(object_id=object_id).count(),
            session.query(TrendState).filter_by(object_id=object_id).count(),
        )


def test_a_sample_whose_object_is_deleted_meanwhile_leaves_no_rows(session_factory, monkeypatch):
    object_id = _make_device_and_object(session_factory, trended=True)
    real_insert = trending._insert_row

    def delete_first(session, obj, value, **kwargs):
        _delete_object(session_factory, object_id)  # commits before this transaction writes
        real_insert(session, obj, value, **kwargs)

    monkeypatch.setattr(trending, "_insert_row", delete_first)
    record_sample(session_factory, object_id, 10.0)

    assert _history_rows(session_factory, object_id) == (0, 0)
    with session_factory() as session:
        assert get_or_create_history_counters(session).trend_total_sample_count == 0


def test_a_time_change_skips_an_object_deleted_meanwhile(session_factory, monkeypatch):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        session.add(device)
        for i in (1, 2):
            session.add(
                BacnetObject(device=device, object_type="analog-value", instance_number=i, name=f"AV-{i}", trended=True)
            )
        session.commit()
        ids = {obj.name: obj.id for obj in session.query(BacnetObject).all()}
    real_insert = trending._insert_row
    deleted = []

    def delete_av2_once(session, obj, value, **kwargs):
        if not deleted:
            deleted.append(True)
            _delete_object(session_factory, ids["AV-2"])
        real_insert(session, obj, value, **kwargs)

    monkeypatch.setattr(trending, "_insert_row", delete_av2_once)
    written = record_time_change(session_factory, 599001, 60.0, 60.0)

    assert written == 1
    assert _history_rows(session_factory, ids["AV-2"]) == (0, 0)
    assert _history_rows(session_factory, ids["AV-1"]) == (1, 1)
    with session_factory() as session:
        assert get_or_create_history_counters(session).trend_total_sample_count == 1


def test_record_sample_without_a_cap_prunes_to_the_default_cap(session_factory, monkeypatch):
    import bacpack.models as models_module

    monkeypatch.setattr(models_module, "MAX_TREND_SAMPLE_CAP", 2)
    object_id = _make_device_and_object(session_factory)
    for value in (1.0, 2.0, 3.0):
        record_sample(session_factory, object_id, value)

    with session_factory() as session:
        remaining = (
            session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.sequence).all()
        )
        counters = get_or_create_history_counters(session)
    assert [s.value for s in remaining] == [2.0, 3.0]
    assert counters.trend_total_sample_count == 2


@pytest.mark.asyncio
async def test_cov_mode_sample_that_fails_to_save_is_logged_and_not_counted(session_factory, monkeypatch, caplog):
    object_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")
    device = Device(id=599001, name="Test Device")
    app = build_application(
        device, [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")]
    )
    av = app.get_object_name("AV-1")

    real_record_sample = trending.record_sample
    calls = []

    def failing_once(session_factory, object_id, value, **kwargs):
        calls.append(value)
        if len(calls) == 1:
            raise RuntimeError("simulated save failure")
        real_record_sample(session_factory, object_id, value, **kwargs)

    monkeypatch.setattr(trending, "record_sample", failing_once)
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()

    await write_feed_value(av, 42.0)  # fails to save
    deadline = asyncio.get_running_loop().time() + 5
    while "COV trend sample failed for object" not in caplog.text:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    # Below covIncrement from 42, but 42 never reached the trend: measured
    # against the last value saved, it's a significant change.
    await write_feed_value(av, 42.5)
    samples = await _wait_for_samples(session_factory, object_id, 1)
    sampler.stop()

    assert [s.value for s in samples] == [42.5]


# --- bounded device prune and the chunked background prune ----------


def _seed_samples(session_factory, count, *, objects=1, soft_limit_mb=1, counter=None):
    """`count` samples with strictly increasing timestamps (value = index),
    spread round-robin over `objects` objects; returns the object ids."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        rows = [
            BacnetObject(device=device, object_type="analog-value", instance_number=n + 1, name=f"AV-{n + 1}")
            for n in range(objects)
        ]
        session.add_all(rows)
        session.commit()
        ids = [row.id for row in rows]
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=count + 1)
    with session_factory() as session:
        for i in range(count):
            session.add(
                TrendSample(
                    object_id=ids[i % objects],
                    sequence=i // objects + 1,
                    value=float(i),
                    timestamp=start + dt.timedelta(hours=i),
                )
            )
        # A log is a gap-free run of sequence numbers, and the next one follows it.
        for n, object_id in enumerate(ids):
            session.add(TrendState(object_id=object_id, next_sequence=len(range(n, count, objects)) + 1))
        session.commit()
    with session_factory() as session:
        get_or_create_settings(session).trend_soft_limit_mb = soft_limit_mb
        session.commit()
    with session_factory() as session:
        get_or_create_history_counters(session).trend_total_sample_count = count if counter is None else counter
        session.commit()
    return ids


def _sample_values(session_factory):
    with session_factory() as session:
        return [s.value for s in session.query(TrendSample).order_by(TrendSample.timestamp, TrendSample.id)]


def _counter(session_factory):
    with session_factory() as session:
        return get_or_create_history_counters(session).trend_total_sample_count


def test_record_sample_prunes_at_most_max_per_write_over_the_limit(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)  # 1 MB = 10 samples
    object_id = _seed_samples(session_factory, 300)[0]
    per_write = trending._MAX_PRUNE_PER_WRITE
    assert 1 < per_write < 300

    record_sample(session_factory, object_id, 999.0)

    assert len(_sample_values(session_factory)) == 300 + 1 - per_write
    assert _counter(session_factory) == 300 + 1 - per_write


def test_record_sample_at_the_limit_removes_one_for_one(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)
    object_id = _seed_samples(session_factory, 10)[0]

    record_sample(session_factory, object_id, 999.0)

    values = _sample_values(session_factory)
    assert len(values) == 10
    assert 0.0 not in values and values[-1] == 999.0
    assert _counter(session_factory) == 10


def test_prune_to_limit_deletes_the_globally_oldest_in_chunks(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 262_144)  # 1 MB = 4 samples
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 3)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_PAUSE_SECONDS", 0)
    _seed_samples(session_factory, 10, objects=2)
    real = trending.prune_chunk_to_limit
    seen = []

    def spy(factory):
        seen.append(real(factory))
        return seen[-1]

    monkeypatch.setattr(trending, "prune_chunk_to_limit", spy)

    assert trending.prune_to_limit(session_factory) == 6
    assert seen == [3, 3, 0]
    assert _sample_values(session_factory) == [6.0, 7.0, 8.0, 9.0]
    assert _counter(session_factory) == 4


def test_prune_to_limit_follows_a_limit_raised_mid_prune(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 2)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_PAUSE_SECONDS", 0)
    _seed_samples(session_factory, 20)  # room for 10
    real = trending.prune_chunk_to_limit
    calls = []

    def spy(factory):
        deleted = real(factory)
        calls.append(deleted)
        if len(calls) == 1:
            with factory() as session:
                get_or_create_settings(session).trend_soft_limit_mb = 2  # room for 20
                session.commit()
        return deleted

    monkeypatch.setattr(trending, "prune_chunk_to_limit", spy)

    assert trending.prune_to_limit(session_factory) == 2
    assert calls == [2, 0]
    with session_factory() as session:
        assert not trending.trend_over_limit(session)


def test_prune_to_limit_resyncs_a_drifted_counter_and_stops(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_PAUSE_SECONDS", 0)
    _seed_samples(session_factory, 1, counter=50)

    assert trending.prune_to_limit(session_factory) == 1
    assert _counter(session_factory) == 0
    with session_factory() as session:
        assert not trending.trend_over_limit(session)


@pytest.mark.parametrize("soft_limit_mb", [None, 0])
def test_over_limit_uses_the_hard_cap_when_the_soft_limit_is_off(session_factory, soft_limit_mb):
    _seed_samples(session_factory, 0, soft_limit_mb=soft_limit_mb)
    hard_samples = TREND_HARD_CAP_BYTES // BYTES_PER_TREND_SAMPLE
    for total, expected in ((hard_samples, False), (hard_samples + 1, True)):
        with session_factory() as session:
            get_or_create_history_counters(session).trend_total_sample_count = total
            session.commit()
        with session_factory() as session:
            assert trending.trend_over_limit(session) is expected


def _a_device_write_before_the_first(session_factory, verb: str) -> list:
    """A device's sample write (its row and the counter, one commit) from a
    second connection, attempted just before the first `verb` statement
    (SELECT or DELETE) on the samples runs. A chunk that holds the history write lock by then makes it wait and
    give up, as a device's busy wait would; one that doesn't lets it commit.
    Returns one entry per attempt, True when it committed."""
    import sqlite3

    from sqlalchemy import event

    from bacpack.db import history_db_path

    engine = session_factory.kw["bind"]
    path = history_db_path(engine.url.database)
    attempts = []

    def device_write(conn, cursor, statement, parameters, context, executemany):
        if attempts or not statement.lstrip().upper().startswith(verb) or "TREND_SAMPLES" not in statement.upper():
            return
        other = sqlite3.connect(str(path), timeout=0.2)
        try:
            other.execute(
                "INSERT INTO trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
                "VALUES (999, 1, '2099-01-01 00:00:00', 1.0, 0.0, 'value')"
            )
            other.execute("UPDATE history_counters SET trend_total_sample_count = trend_total_sample_count + 1")
            other.commit()
            attempts.append(True)
        except sqlite3.OperationalError:
            attempts.append(False)
        finally:
            other.close()

    event.listen(engine, "before_cursor_execute", device_write)
    return attempts


def test_prune_chunk_does_not_lose_a_device_write_that_lands_before_its_delete(session_factory, monkeypatch):
    # The counter is adjusted from a value read earlier in the chunk; a device
    # write that committed after that read would be overwritten, so the chunk
    # must hold the write lock from before its read.
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)  # 1 MB = 10 samples
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 4)
    _seed_samples(session_factory, 15)
    attempts = _a_device_write_before_the_first(session_factory, "DELETE")

    assert trending.prune_chunk_to_limit(session_factory) == 4

    assert attempts == [False]
    assert _counter(session_factory) == len(_sample_values(session_factory)) == 11


def _statements(session_factory):
    from sqlalchemy import event

    seen = []
    engine = session_factory.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    return seen


def test_prune_chunk_restarts_a_large_history_wal(session_factory, monkeypatch):
    # With a device writing meanwhile SQLite never restarts the log by itself
    # and the WAL grows by every chunk's pages (gigabytes over a big prune).
    from bacpack import db

    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 4)
    _seed_samples(session_factory, 15)
    seen = _statements(session_factory)

    monkeypatch.setattr(db, "_WAL_RESTART_FRAMES", 10**9)
    trending.prune_chunk_to_limit(session_factory)
    assert any("wal_checkpoint(PASSIVE)" in s for s in seen)
    assert not any("wal_checkpoint(RESTART)" in s for s in seen)

    seen.clear()
    monkeypatch.setattr(db, "_WAL_RESTART_FRAMES", 0)
    trending.prune_chunk_to_limit(session_factory)
    assert any("wal_checkpoint(RESTART)" in s for s in seen)


def test_a_chunked_delete_checkpoints_the_history_wal_after_each_chunk(session_factory, monkeypatch):
    # The retention prune and the pre-lock sample deletes chunk through
    # delete_in_chunks; without this the WAL grew by every chunk's pages.
    from bacpack import db

    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 2)
    monkeypatch.setattr(db, "_WAL_RESTART_FRAMES", 10**9)
    object_id = _make_device_and_object(session_factory)
    _add_samples(session_factory, object_id, 5, days_old=10)
    with session_factory() as session:
        get_or_create_settings(session).trend_retention_days = 7
        session.commit()
    seen = _statements(session_factory)

    assert prune_old_samples(session_factory) == 5
    assert sum("history.wal_checkpoint(PASSIVE)" in s for s in seen) == 3  # 2 + 2 + 1
    assert not any("wal_checkpoint(RESTART)" in s for s in seen)

    seen.clear()
    monkeypatch.setattr(db, "_WAL_RESTART_FRAMES", 0)
    _add_samples(session_factory, object_id, 1, days_old=10, first_sequence=6)
    assert trending.delete_trend_samples_before_locking(session_factory, [object_id]) == 1
    assert any("history.wal_checkpoint(RESTART)" in s for s in seen)


def test_the_wal_restart_gives_up_at_once_while_a_reader_holds_the_log(session_factory, monkeypatch):
    # A RESTART blocks every writer while it waits for readers, so it must not
    # wait the busy timeout behind a long read (the storage page's, say).
    from bacpack import db
    from bacpack.db import BUSY_TIMEOUT_SECONDS, history_db_path

    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 4)
    monkeypatch.setattr(db, "_WAL_RESTART_FRAMES", 0)
    _seed_samples(session_factory, 15)
    engine = session_factory.kw["bind"]
    reader = sqlite3.connect(str(history_db_path(engine.url.database)), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM trend_samples").fetchone()
        started = time.perf_counter()
        trending.prune_chunk_to_limit(session_factory)
        assert time.perf_counter() - started < 2
    finally:
        reader.close()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_SECONDS * 1000


# --- A sampler pass off the device's loop ------------------------------------

import threading


def test_record_samples_writes_every_object_in_one_transaction(session_factory):
    ids = _seed_samples(session_factory, 0, objects=3)
    engine = session_factory.kw["bind"]
    commits = []

    def on_commit(conn):
        commits.append(1)

    event.listen(engine, "commit", on_commit)
    try:
        assert trending.record_samples(session_factory, [(i, 10.0 + n) for n, i in enumerate(ids)]) == ids
        assert trending.record_samples(session_factory, [(i, 20.0) for i in ids]) == ids
    finally:
        event.remove(engine, "commit", on_commit)
    assert len(commits) == 2
    with session_factory() as session:
        rows = session.query(TrendSample).order_by(TrendSample.object_id, TrendSample.sequence).all()
    assert [(r.object_id, r.sequence, r.value) for r in rows] == [
        (ids[0], 1, 10.0), (ids[0], 2, 20.0),
        (ids[1], 1, 11.0), (ids[1], 2, 20.0),
        (ids[2], 1, 12.0), (ids[2], 2, 20.0),
    ]
    assert _counter(session_factory) == 6


def test_record_samples_enforces_each_objects_cap(session_factory):
    object_id = _make_device_and_object(session_factory, trend_sample_cap=2)
    for value in (1.0, 2.0, 3.0):
        trending.record_samples(session_factory, [(object_id, value)])
    with session_factory() as session:
        remaining = session.query(TrendSample).order_by(TrendSample.sequence).all()
    assert [s.value for s in remaining] == [2.0, 3.0]
    assert _counter(session_factory) == 2


def test_record_samples_over_the_limit_removes_at_least_what_it_adds(session_factory, monkeypatch):
    # A device's writes must never grow a total that is over the limit, even
    # with the web process (and its background pruner) down.
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)  # 1 MB = 10 samples
    ids = _seed_samples(session_factory, 300, objects=8)
    assert len(ids) > trending._MAX_PRUNE_PER_WRITE

    trending.record_samples(session_factory, [(i, 999.0) for i in ids])

    assert len(_sample_values(session_factory)) == 300
    assert _counter(session_factory) == 300


def test_record_samples_leaves_out_an_object_that_is_gone(session_factory):
    object_id = _make_device_and_object(session_factory)
    assert trending.record_samples(session_factory, [(999999, 1.0), (object_id, 2.0)]) == [object_id]
    assert _sample_values(session_factory) == [2.0]
    assert _counter(session_factory) == 1


def test_record_samples_leaves_out_an_object_deleted_before_its_commit(session_factory, monkeypatch):
    ids = _seed_samples(session_factory, 0, objects=2)
    real_existing = trending._existing_ids
    calls = []

    def deleted_meanwhile(session, object_ids):
        calls.append(1)
        found = real_existing(session, object_ids)
        return found - {ids[0]} if len(calls) == 1 else found

    monkeypatch.setattr(trending, "_existing_ids", deleted_meanwhile)
    assert trending.record_samples(session_factory, [(ids[0], 1.0), (ids[1], 2.0)]) == [ids[1]]
    assert _sample_values(session_factory) == [2.0]
    assert _counter(session_factory) == 1


def _interval_objects(session_factory, count, interval):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        session.add(device)
        for n in range(count):
            session.add(BacnetObject(
                device=device, object_type="analog-value", instance_number=n + 1, name=f"AV-{n + 1}",
                trended=True, trend_mode="interval", trend_interval_seconds=interval,
            ))
        session.commit()
    return build_application(
        Device(id=599001, name="Test Device"),
        [BacnetObject(object_type="analog-value", instance_number=n + 1, name=f"AV-{n + 1}") for n in range(count)],
    )


@pytest.mark.asyncio
async def test_a_sampler_pass_writes_off_the_loop_in_batches(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)
    monkeypatch.setattr(trending, "_RECORD_BATCH_SIZE", 2)
    app = _interval_objects(session_factory, 5, 60)
    loop_thread = threading.current_thread()
    calls = []
    real = trending.record_samples

    def spy(session_factory, samples, **kwargs):
        calls.append((threading.current_thread() is loop_thread, len(samples)))
        return real(session_factory, samples, **kwargs)

    monkeypatch.setattr(trending, "record_samples", spy)
    added = []
    monkeypatch.setattr(app, "trend_records_added", lambda ids=None: added.extend(ids))

    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    trended_ids = sorted(sampler._due)
    now = asyncio.get_running_loop().time()
    for object_id in trended_ids:
        sampler._due[object_id] = now
    deadline = now + 5
    while len(added) < 5 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    sampler.stop()

    assert calls == [(False, 2), (False, 2), (False, 1)]
    assert sorted(added) == trended_ids
    assert len(_sample_values(session_factory)) == 5


@pytest.mark.asyncio
async def test_a_cov_sample_is_written_off_the_loop(session_factory, monkeypatch):
    object_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")
    app = build_application(
        Device(id=599001, name="Test Device"),
        [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")],
    )
    loop_thread = threading.current_thread()
    threads = []
    real = trending.record_sample

    def spy(*args, **kwargs):
        threads.append(threading.current_thread() is loop_thread)
        return real(*args, **kwargs)

    monkeypatch.setattr(trending, "record_sample", spy)
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    await write_feed_value(app.get_object_name("AV-1"), 42.0)
    samples = await _wait_for_samples(session_factory, object_id, 1)
    sampler.stop()

    assert threads == [False]
    assert [s.value for s in samples] == [42.0]


@pytest.mark.asyncio
async def test_cov_samples_are_written_in_the_order_they_changed(session_factory, monkeypatch):
    # Off the loop, two quick changes must not race each other into the log.
    object_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")
    app = build_application(
        Device(id=599001, name="Test Device"),
        [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")],
    )
    real = trending.record_sample

    def slow_first(session_factory, object_id, value, **kwargs):
        if value == 10.0:
            time.sleep(0.2)
        return real(session_factory, object_id, value, **kwargs)

    monkeypatch.setattr(trending, "record_sample", slow_first)
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    av = app.get_object_name("AV-1")
    await write_feed_value(av, 10.0)
    await write_feed_value(av, 20.0)
    samples = await _wait_for_samples(session_factory, object_id, 2)
    sampler.stop()

    assert [(s.sequence, s.value) for s in sorted(samples, key=lambda s: s.sequence)] == [(1, 10.0), (2, 20.0)]


def test_per_sample_cap_check_does_not_grow_with_the_log(session_factory):
    from tests.scale.harness import StatementRecorder, grew_with_table
    from tests.test_trend_buffer import FULL, TWIN, insert_rows, make_object, minutely

    work = {}
    for name, size, instance in (("twin", TWIN, 1), ("full", FULL, 2)):
        object_id = make_object(session_factory, instance, cap=size)
        insert_rows(session_factory, object_id, minutely(size))
        with StatementRecorder(session_factory.kw["bind"]) as rec:
            trending.record_samples(session_factory, [(object_id, 1.0)], clock_offset=0.0)
        work[name] = rec
        with session_factory() as session:
            assert session.query(TrendSample).filter_by(object_id=object_id).count() == size
    assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
        work["twin"].vm_steps,
        work["full"].vm_steps,
    )


# --- Deletes leave only from a log's oldest end -------------------------------

def _stamped_log(session_factory, instance, stamps, *, first_sequence=1):
    """A log whose rows (in id order) carry these UTC stamps."""
    from tests.test_trend_buffer import insert_rows, make_object

    object_id = make_object(session_factory, instance)
    insert_rows(
        session_factory,
        object_id,
        [(first_sequence + i, stamp, 0.0, "value") for i, stamp in enumerate(stamps)],
    )
    with session_factory() as session:
        get_or_create_history_counters(session).trend_total_sample_count += len(stamps)
        session.commit()
    return object_id


def _assert_gap_free(session_factory, object_id, expected_sequences):
    from bacpack import trend_buffer

    with session_factory() as session:
        rows = session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id).all()
        assert [r.sequence for r in rows] == expected_sequences
        assert trend_buffer.count_records(session, object_id) == len(rows)


def _retention_days(session_factory, days):
    with session_factory() as session:
        get_or_create_settings(session).trend_retention_days = days
        session.commit()


def test_retention_after_a_host_clock_step_back_keeps_the_log_gap_free(session_factory, monkeypatch):
    from bacpack import timeutil

    t0 = dt.datetime(2026, 3, 1, 10, 0, 0)
    minutes = dt.timedelta(minutes=1)
    object_id = _stamped_log(session_factory, 1, [t0, t0 + 5 * minutes, t0 - 10 * minutes, t0 + 10 * minutes])
    _retention_days(session_factory, 1)
    day = dt.timedelta(days=1)

    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 - 5 * minutes + day)
    assert prune_old_samples(session_factory) == 0  # the oldest row is within retention
    _assert_gap_free(session_factory, object_id, [1, 2, 3, 4])

    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 + 6 * minutes + day)
    assert prune_old_samples(session_factory) == 3  # 10:00, 10:05 and the step-back row 09:50
    _assert_gap_free(session_factory, object_id, [4])
    with session_factory() as session:
        assert get_or_create_history_counters(session).trend_total_sample_count == 1


def test_retention_deletes_only_from_the_oldest_end_across_objects(session_factory, monkeypatch):
    from bacpack import timeutil

    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    hour = dt.timedelta(hours=1)
    one = _stamped_log(session_factory, 1, [t0 + i * hour for i in range(6)])
    two = _stamped_log(session_factory, 2, [t0 + (i + 3) * hour for i in range(6)])
    _retention_days(session_factory, 1)
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 + 5 * hour + dt.timedelta(days=1))

    assert prune_old_samples(session_factory) == 5 + 2  # stamps before t0 + 5h
    _assert_gap_free(session_factory, one, [6])
    _assert_gap_free(session_factory, two, [3, 4, 5, 6])
    with session_factory() as session:
        assert get_or_create_history_counters(session).trend_total_sample_count == 12 - 7


def test_retention_walks_a_long_run_in_chunks(session_factory, monkeypatch):
    from bacpack import db, timeutil

    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 3)
    object_id = _stamped_log(session_factory, 1, [t0 + dt.timedelta(minutes=i) for i in range(10)])
    _retention_days(session_factory, 1)
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 + dt.timedelta(minutes=7) + dt.timedelta(days=1))
    assert prune_old_samples(session_factory) == 7
    _assert_gap_free(session_factory, object_id, [8, 9, 10])


def test_global_prune_after_a_host_clock_step_back_keeps_each_log_gap_free(session_factory):
    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    hour = dt.timedelta(hours=1)
    # The newest row by id has the globally-oldest stamp.
    stepped = _stamped_log(session_factory, 1, [t0 + hour, t0 + 2 * hour, t0 + 3 * hour, t0])
    other = _stamped_log(session_factory, 2, [t0 + (10 + i) * hour for i in range(3)])
    with session_factory() as session:
        removed = prune_global_to_limit(session, 6 * BYTES_PER_TREND_SAMPLE, max_rows=1)
        session.commit()
    assert removed == 1
    _assert_gap_free(session_factory, stepped, [2, 3, 4])
    _assert_gap_free(session_factory, other, [1, 2, 3])


def test_global_prune_can_empty_a_log(session_factory):
    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    small = _stamped_log(session_factory, 1, [t0])
    _stamped_log(session_factory, 2, [t0 + dt.timedelta(hours=1 + i) for i in range(3)])
    with session_factory() as session:
        assert prune_global_to_limit(session, 3 * BYTES_PER_TREND_SAMPLE) == 1
        session.commit()
    _assert_gap_free(session_factory, small, [])

    trending.record_samples(session_factory, [(small, 5.0)], clock_offset=0.0)
    _assert_gap_free(session_factory, small, [2])


def _many_logs(session_factory, count, rows_each):
    """`count` logs whose rows interleave in time: row i of every log is older
    than row i + 1 of any, so the globally-oldest rows are each log's oldest."""
    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    return [
        _stamped_log(
            session_factory,
            instance,
            [t0 + dt.timedelta(minutes=row * count + instance) for row in range(rows_each)],
        )
        for instance in range(1, count + 1)
    ]


def _trend_sample_deletes(session_factory):
    statements = []
    engine = session_factory.kw["bind"]

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM HISTORY.TREND_SAMPLES"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", on_execute)
    return statements, lambda: event.remove(engine, "before_cursor_execute", on_execute)


def test_global_prune_across_many_logs_deletes_in_one_statement(session_factory):
    # One statement per log made a 5,000-row chunk over ~280 logs hold the
    # history write lock ~0.14 s longer.
    logs = _many_logs(session_factory, 30, 3)
    statements, stop = _trend_sample_deletes(session_factory)
    try:
        with session_factory() as session:
            assert prune_global_to_limit(session, 30 * BYTES_PER_TREND_SAMPLE) == 60
            session.commit()
    finally:
        stop()
    assert len(statements) == 1
    for object_id in logs:
        _assert_gap_free(session_factory, object_id, [3])


def test_global_prune_with_a_stepped_log_among_others_keeps_every_log_gap_free(session_factory):
    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    hour = dt.timedelta(hours=1)
    # Its newest row is the globally oldest, so the rows chosen for it aren't its oldest.
    stepped = _stamped_log(session_factory, 1, [t0 + 2 * hour, t0 + 3 * hour, t0 + 4 * hour, t0])
    plain = _stamped_log(session_factory, 2, [t0 + hour, t0 + 5 * hour, t0 + 6 * hour])
    with session_factory() as session:
        assert prune_global_to_limit(session, 5 * BYTES_PER_TREND_SAMPLE) == 2
        session.commit()
    _assert_gap_free(session_factory, stepped, [2, 3, 4])
    _assert_gap_free(session_factory, plain, [2, 3])


def test_global_prune_slices_its_id_lists(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "_IDS_PER_STATEMENT", 7)
    logs = _many_logs(session_factory, 10, 4)
    statements, stop = _trend_sample_deletes(session_factory)
    try:
        with session_factory() as session:
            assert prune_global_to_limit(session, 10 * BYTES_PER_TREND_SAMPLE) == 30
            session.commit()
    finally:
        stop()
    assert len(statements) == 5  # 30 ids, 7 a statement
    for object_id in logs:
        _assert_gap_free(session_factory, object_id, [4])


def test_a_retention_chunk_takes_the_history_lock_before_it_reads(session_factory, monkeypatch):
    # The chunk's first statement on the samples finds the write lock already
    # held: a device's write at that moment waits instead of committing between
    # the chunk's reads and its delete.
    from bacpack import timeutil

    t0 = dt.datetime(2026, 3, 1, 10, 0, 0)
    object_id = _stamped_log(session_factory, 1, [t0, t0 + dt.timedelta(minutes=1), t0 + dt.timedelta(days=5)])
    _retention_days(session_factory, 1)
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 + dt.timedelta(days=2))
    attempts = _a_device_write_before_the_first(session_factory, "SELECT")

    assert prune_old_samples(session_factory) == 2

    assert attempts == [False]
    _assert_gap_free(session_factory, object_id, [3])


def test_retention_fills_a_chunk_across_logs(session_factory, monkeypatch):
    from bacpack import db, timeutil

    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    minute = dt.timedelta(minutes=1)
    logs = [_stamped_log(session_factory, i + 1, [t0 + j * minute for j in range(4)] + [t0 + dt.timedelta(days=9)]) for i in range(4)]
    _retention_days(session_factory, 1)
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: t0 + dt.timedelta(days=2))
    monkeypatch.setattr(db, "DELETE_CHUNK_SIZE", 6)
    commits = []
    engine = session_factory.kw["bind"]
    from sqlalchemy import event

    event.listen(engine, "commit", lambda conn: commits.append(1))
    assert prune_old_samples(session_factory) == 16
    # 16 rows in chunks of 6: three transactions that delete, not one per log.
    assert len(commits) == 3
    for object_id in logs:
        _assert_gap_free(session_factory, object_id, [5])


# --- a lowered sample cap trims the stored samples --------------------------

from bacpack import db
from bacpack.trending import apply_sample_cap, trim_devices_to_caps


def _values(session_factory, object_id):
    with session_factory() as session:
        return [
            s.value
            for s in session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id)
        ]


def _total(session_factory):
    with session_factory() as session:
        return get_or_create_history_counters(session).trend_total_sample_count


def test_apply_sample_cap_drops_the_oldest_and_lowers_the_total(session_factory):
    object_id = _make_device_and_object(session_factory, trended=True)
    for value in range(5):
        record_sample(session_factory, object_id, float(value))
    with session_factory() as session:
        db.lock_both_files(session)
        obj = session.get(BacnetObject, object_id)
        obj.trend_sample_cap = 2
        assert apply_sample_cap(session, obj) == 3
        session.commit()

    assert _values(session_factory, object_id) == [3.0, 4.0]
    assert _total(session_factory) == 2


def test_trim_devices_to_caps_trims_each_object_over_its_cap(session_factory):
    capped = _make_device_and_object(session_factory, trended=True)
    with session_factory() as session:
        uncapped = BacnetObject(
            device_id=599001, object_type="analog-value", instance_number=2, name="AV-2", trended=True
        )
        session.add(uncapped)
        session.commit()
        uncapped = uncapped.id
    for value in range(4):
        record_sample(session_factory, capped, float(value))
        record_sample(session_factory, uncapped, float(value))
    with session_factory() as session:
        session.get(BacnetObject, capped).trend_sample_cap = 1  # saved the way a sync saves it
        session.commit()

    with session_factory() as session:
        assert trim_devices_to_caps(session, [599001]) == 3

    assert _values(session_factory, capped) == [3.0]
    assert len(_values(session_factory, uncapped)) == 4
    assert _total(session_factory) == 5


def _commit_one_sample(session_factory, object_id, sequence):
    """What a device's trend write does: row and counter in one commit."""
    from sqlalchemy import update

    from bacpack.models import HistoryCounters

    with session_factory() as session:
        session.execute(
            update(HistoryCounters).values(trend_total_sample_count=HistoryCounters.trend_total_sample_count)
        )
        session.add(TrendSample(object_id=object_id, sequence=sequence, value=1.0))
        trending._adjust_total_sample_count(session, 1)
        session.commit()


def _counter_and_rows(session_factory):
    with session_factory() as session:
        return (
            get_or_create_history_counters(session).trend_total_sample_count,
            session.query(TrendSample).count(),
        )


def test_resync_reports_no_drift_for_a_write_that_lands_between_its_reads(session_factory):
    from sqlalchemy import event

    object_id = _make_device_and_object(session_factory)
    _commit_one_sample(session_factory, object_id, 1)
    engine = session_factory.kw["bind"]
    fired = []
    reads = []

    @event.listens_for(engine, "after_cursor_execute")
    def write_after_the_counter_is_read(conn, cursor, statement, parameters, context, executemany):
        if "history_counters" in statement and statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)
            # The first read ensures the row exists; the second is the
            # snapshot's counter read.
            if len(reads) == 2:
                fired.append(True)
                _commit_one_sample(session_factory, object_id, 2)

    try:
        assert resync_trend_total_sample_count(session_factory) == 0
    finally:
        event.remove(engine, "after_cursor_execute", write_after_the_counter_is_read)

    assert fired
    counter, rows = _counter_and_rows(session_factory)
    assert counter == rows == 2


def test_resync_keeps_a_write_that_lands_between_its_snapshot_and_its_correction(session_factory, monkeypatch):
    object_id = _make_device_and_object(session_factory)
    with session_factory() as session:
        get_or_create_history_counters(session).trend_total_sample_count = 999  # deliberately wrong
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.0))
        session.commit()
    real_apply = trending._apply_drift

    def apply_after_a_device_write(factory, drift):
        _commit_one_sample(session_factory, object_id, 2)
        real_apply(factory, drift)

    monkeypatch.setattr(trending, "_apply_drift", apply_after_a_device_write)

    assert resync_trend_total_sample_count(session_factory) == 1 - 999

    counter, rows = _counter_and_rows(session_factory)
    assert counter == rows == 2


# --- An object the sampler can't read doesn't stop the rest ------------------


def _device_with_nc_and_av(session_factory, *, nc_mode="cov"):
    """The rows an old database could hold: a trended Notification Class
    (first, so it sorts before the point) and a trended point."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        nc = BacnetObject(
            device=device, object_type="notification-class", instance_number=1, name="NC-1",
            trended=True, trend_mode=nc_mode,
        )
        av = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1", units="percent",
            trended=True, trend_mode="cov",
        )
        session.add_all([device, nc, av])
        session.commit()
        return av.id


def _app_with_nc_and_av():
    device = Device(id=599001, name="Test Device")
    return build_application(
        device,
        [
            BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"),
            BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent"),
        ],
    )


@pytest.mark.asyncio
async def test_resync_skips_an_object_it_cannot_read_and_keeps_the_rest(session_factory, caplog):
    av_id = _device_with_nc_and_av(session_factory)
    app = _app_with_nc_and_av()
    av = app.get_object_name("AV-1")
    sampler = TrendSampler(session_factory, app, device_id=599001)
    with caplog.at_level("WARNING", logger="bacpack.trending"):
        sampler.start()
    assert len(av._property_monitors["presentValue"]) == 1
    assert any('notification-class:1 "NC-1"' in r.getMessage() for r in caplog.records)

    await write_feed_value(av, 42.0)
    samples = await _wait_for_samples(session_factory, av_id, 1)
    sampler.stop()
    assert [s.value for s in samples] == [42.0]


@pytest.mark.asyncio
async def test_a_resync_that_meets_a_bad_object_keeps_the_good_hooks_it_already_had(session_factory):
    av_id = _make_device_and_object(session_factory, trended=True, trend_mode="cov")
    app = _app_with_nc_and_av()
    av = app.get_object_name("AV-1")
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    assert len(av._property_monitors["presentValue"]) == 1

    with session_factory() as session:
        session.add(
            BacnetObject(
                device_id=599001, object_type="notification-class", instance_number=1, name="NC-1",
                trended=True, trend_mode="cov",
            )
        )
        session.commit()
    sampler.resync()
    assert len(av._property_monitors["presentValue"]) == 1
    await write_feed_value(av, 42.0)
    assert len(await _wait_for_samples(session_factory, av_id, 1)) == 1
    sampler.stop()


@pytest.mark.asyncio
async def test_interval_sampler_start_does_not_fail_for_a_notification_class(session_factory):
    _device_with_nc_and_av(session_factory, nc_mode="interval")
    app = _app_with_nc_and_av()
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    sampler.stop()
