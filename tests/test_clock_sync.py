"""clock_sync -- the one path every device clock change takes."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time

from bacpack import device_clock
from bacpack.clock_sync import apply_clock_offset, bacnet_datetime_fields
from bacpack.models import BacnetObject, Device, TrendSample
from bacpack.trending import TrendSampler, record_sample
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked


def _device_with_trended_object(session_factory, device_id=1):
    with session_factory() as session:
        device = Device(id=device_id, name="D")
        session.add(device)
        session.add(
            BacnetObject(
                device=device, object_type="analog-value", instance_number=1, name="AV-1", trended=True
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_apply_moves_the_clock_saves_it_and_records_a_time_change(session_factory):
    _device_with_trended_object(session_factory)

    delta = await apply_clock_offset(session_factory, None, 1, 3600.0, "test")

    assert delta == 3600.0
    assert device_clock.offset() == 3600.0
    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == 3600.0
        row = session.query(TrendSample).one()
        assert (row.kind, row.value, row.clock_offset) == ("time-change", 3600.0, 3600.0)


@pytest.mark.asyncio
async def test_apply_below_the_threshold_updates_the_clock_but_records_nothing(session_factory):
    _device_with_trended_object(session_factory)

    delta = await apply_clock_offset(session_factory, None, 1, 0.4, "test")

    assert delta == 0.4
    assert device_clock.offset() == 0.4
    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == 0.4
        assert session.query(TrendSample).count() == 0


@pytest.mark.asyncio
async def test_apply_delta_is_relative_to_the_current_offset(session_factory):
    _device_with_trended_object(session_factory)
    device_clock.set_offset(100.0)

    assert await apply_clock_offset(session_factory, None, 1, 40.0, "test") == -60.0


@pytest.mark.asyncio
async def test_apply_without_a_session_factory_only_moves_the_clock():
    assert await apply_clock_offset(None, None, 1, 90.0, "test") == 90.0
    assert device_clock.offset() == 90.0


@pytest.mark.asyncio
async def test_apply_logs_the_source_at_info(session_factory, caplog):
    _device_with_trended_object(session_factory)
    with caplog.at_level(logging.INFO, logger="bacpack.clock_sync"):
        await apply_clock_offset(session_factory, None, 1, 3600.0, "UTCTimeSynchronization")
    assert "UTCTimeSynchronization" in caplog.text


def _dt(date_tuple, time_tuple):
    return DateTime(date=Date(date_tuple), time=Time(time_tuple))


def test_bacnet_datetime_fields_reads_a_full_datetime():
    value = _dt((126, 9, 25, 5), (14, 3, 12, 50))
    assert bacnet_datetime_fields(value) == (2026, 9, 25, 14, 3, 12, 50)


def test_bacnet_datetime_fields_allows_wildcard_day_of_week_and_hundredths():
    value = _dt((126, 9, 25, 255), (14, 3, 12, 255))
    assert bacnet_datetime_fields(value) == (2026, 9, 25, 14, 3, 12, 0)


def test_bacnet_datetime_fields_rejects_any_other_wildcard():
    assert bacnet_datetime_fields(_dt((255, 9, 25, 5), (14, 3, 12, 0))) is None
    assert bacnet_datetime_fields(_dt((126, 255, 25, 5), (14, 3, 12, 0))) is None
    assert bacnet_datetime_fields(_dt((126, 9, 255, 5), (14, 3, 12, 0))) is None
    assert bacnet_datetime_fields(_dt((126, 9, 25, 5), (255, 3, 12, 0))) is None
    assert bacnet_datetime_fields(_dt((126, 9, 25, 5), (14, 255, 12, 0))) is None
    assert bacnet_datetime_fields(_dt((126, 9, 25, 5), (14, 3, 255, 0))) is None


def test_bacnet_datetime_fields_rejects_special_month_and_day_values():
    assert bacnet_datetime_fields(_dt((126, 13, 25, 5), (14, 3, 12, 0))) is None  # odd months
    assert bacnet_datetime_fields(_dt((126, 14, 25, 5), (14, 3, 12, 0))) is None  # even months
    assert bacnet_datetime_fields(_dt((126, 9, 32, 5), (14, 3, 12, 0))) is None  # last day
    assert bacnet_datetime_fields(_dt((126, 9, 33, 5), (14, 3, 12, 0))) is None  # odd days
    assert bacnet_datetime_fields(_dt((126, 9, 34, 5), (14, 3, 12, 0))) is None  # even days


def test_bacnet_datetime_fields_rejects_an_impossible_real_date():
    """Month/day are only checked against the
    generic <=12/<=31 bounds and the special wildcard values above -- an
    impossible but in-bounds real date (Feb 30 doesn't exist) used to slip
    through and get silently rolled forward by mktime/timegm downstream."""
    assert bacnet_datetime_fields(_dt((126, 2, 30, 5), (14, 3, 12, 0))) is None  # Feb 30


def test_bacnet_datetime_fields_rejects_an_impossible_real_time():
    """As above, for an impossible time-of-day (hour 24 doesn't exist and
    was never checked against any bound at all)."""
    assert bacnet_datetime_fields(_dt((126, 9, 25, 5), (24, 3, 12, 0))) is None  # hour 24


@pytest.mark.asyncio
async def test_a_time_sync_saves_off_the_loop(session_factory):
    _device_with_trended_object(session_factory)

    async def sync_and_look_at_the_clock():
        task = asyncio.ensure_future(apply_clock_offset(session_factory, None, 1, 0.4, "test"))
        await asyncio.sleep(0)
        assert device_clock.offset() == 0.4  # moved before the save lands
        return await task

    run = await run_while_locked(session_factory, sync_and_look_at_the_clock)

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == 0.4


@pytest.mark.asyncio
async def test_time_change_records_save_off_the_loop(session_factory):
    _device_with_trended_object(session_factory)

    run = await run_while_locked(
        session_factory,
        lambda: apply_clock_offset(session_factory, None, 1, 3600.0, "test"),
        history=True,
    )

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.query(TrendSample).filter_by(kind="time-change").count() == 1
        assert session.get(Device, 1).clock_offset_seconds == 3600.0


@pytest.mark.asyncio
async def test_two_clock_changes_save_the_last(session_factory):
    _device_with_trended_object(session_factory)
    first_save = []
    first_committed = threading.Event()

    def slow_first_save():
        # Saves that weren't queued would let the quick second one commit
        # before this one, leaving the first offset in the row.
        session = session_factory()
        if not first_save:
            first_save.append(True)
            time.sleep(0.2)
            event.listen(session, "after_commit", lambda _session: first_committed.set())
        return session

    first = asyncio.ensure_future(apply_clock_offset(slow_first_save, None, 1, 0.3, "test"))
    await asyncio.sleep(0)  # the first change is under way
    await apply_clock_offset(slow_first_save, None, 1, 0.5, "test")
    await first
    assert await asyncio.to_thread(first_committed.wait, 5)

    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == 0.5


@pytest.mark.asyncio
async def test_a_small_correction_after_a_big_jump_saves_last(session_factory):
    # The big jump's record waits on the busy history file; the correction
    # has no record to write and must not save its offset first.
    _device_with_trended_object(session_factory)

    async def jump_then_correct():
        first = asyncio.ensure_future(apply_clock_offset(session_factory, None, 1, 3600.0, "jump"))
        await asyncio.sleep(0)
        await apply_clock_offset(session_factory, None, 1, 3600.5, "correction")
        await first

    await run_while_locked(session_factory, jump_then_correct, history=True, seconds=0.5)

    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == device_clock.offset() == 3600.5


@pytest.mark.asyncio
async def test_two_big_jumps_both_record(session_factory):
    # Records written in parallel threads collide on the log's sequence numbers.
    _device_with_trended_object(session_factory)

    async def two_jumps():
        first = asyncio.ensure_future(apply_clock_offset(session_factory, None, 1, 3600.0, "first"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(apply_clock_offset(session_factory, None, 1, -3600.0, "second"))
        return await asyncio.gather(first, second)

    await run_while_locked(session_factory, two_jumps, history=True, seconds=0.5)

    with session_factory() as session:
        assert session.query(TrendSample).filter_by(kind="time-change").count() == 2
        assert session.get(Device, 1).clock_offset_seconds == device_clock.offset() == -3600.0


class _App:
    def trend_records_added(self, object_ids=None) -> None:
        pass


def _trended_object_id(session_factory) -> int:
    """The trended object, its log already holding one record."""
    with session_factory() as session:
        object_id = session.query(BacnetObject).filter_by(name="AV-1").one().id
    record_sample(session_factory, object_id, 20.0)
    return object_id


def _sequences(session_factory) -> list:
    with session_factory() as session:
        return sorted((row.sequence, row.kind) for row in session.query(TrendSample))


@pytest.mark.asyncio
async def test_a_clock_change_and_an_interval_sample_batch_both_record(session_factory):
    # Both take the log's next sequence number in their own worker thread.
    _device_with_trended_object(session_factory)
    object_id = _trended_object_id(session_factory)
    sampler = TrendSampler(session_factory, _App(), 1)
    sampler._due = {object_id: 0.0}
    sampler._intervals = {object_id: 60.0}
    sampler._interval_objects = {object_id: SimpleNamespace(presentValue=21.5)}

    async def sample_and_jump():
        batch = asyncio.ensure_future(sampler._sample_due())
        await asyncio.sleep(0)
        await apply_clock_offset(session_factory, None, 1, 3600.0, "test")
        await batch

    await run_while_locked(session_factory, sample_and_jump, history=True, seconds=0.5)

    assert _sequences(session_factory) == [(1, "value"), (2, "value"), (3, "time-change")]


@pytest.mark.asyncio
async def test_a_clock_change_and_a_cov_sample_both_record(session_factory):
    _device_with_trended_object(session_factory)
    object_id = _trended_object_id(session_factory)
    sampler = TrendSampler(session_factory, _App(), 1)

    async def sample_and_jump():
        sample = asyncio.ensure_future(sampler._record_cov_sample(object_id, 21.5, 20.0))
        await asyncio.sleep(0)
        await apply_clock_offset(session_factory, None, 1, 3600.0, "test")
        await sample

    await run_while_locked(session_factory, sample_and_jump, history=True, seconds=0.5)

    assert _sequences(session_factory) == [(1, "value"), (2, "value"), (3, "time-change")]
