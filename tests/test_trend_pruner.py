"""TrendPruner: the web process's background prune after a lowered trend soft
limit."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

import pytest

from bacpack import trending
from bacpack.models import (
    BacnetObject,
    Device,
    TrendSample,
    get_or_create_history_counters,
    get_or_create_settings,
)
from bacpack.trending import TrendPruner


@pytest.fixture(autouse=True)
def _small_samples(monkeypatch):
    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 100_000)  # 1 MB = 10 samples
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_CHUNK_SIZE", 4)
    monkeypatch.setattr(trending, "_BACKGROUND_PRUNE_PAUSE_SECONDS", 0)


def _seed(session_factory, count, soft_limit_mb=1):
    with session_factory() as session:
        device = Device(id=1, name="D")
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add_all([device, obj])
        session.commit()
        object_id = obj.id
    with session_factory() as session:
        session.add_all(
            TrendSample(object_id=object_id, sequence=i, value=float(i)) for i in range(count)
        )
        session.commit()
    with session_factory() as session:
        get_or_create_settings(session).trend_soft_limit_mb = soft_limit_mb
        session.commit()
    with session_factory() as session:
        get_or_create_history_counters(session).trend_total_sample_count = count
        session.commit()


def _set_limit(session_factory, mb):
    with session_factory() as session:
        get_or_create_settings(session).trend_soft_limit_mb = mb
        session.commit()


def _rows(session_factory):
    with session_factory() as session:
        return session.query(TrendSample).count()


async def _idle(pruner):
    assert await asyncio.to_thread(pruner.wait_idle, 5)


async def test_the_pruner_runs_a_pass_at_start(session_factory):
    _seed(session_factory, 25)
    pruner = TrendPruner(session_factory)
    pruner.start()
    try:
        await _idle(pruner)
        assert _rows(session_factory) == 10
    finally:
        await pruner.stop()


async def test_a_kick_from_another_thread_runs_a_pass(session_factory):
    _seed(session_factory, 25, soft_limit_mb=3)  # room for 30
    pruner = TrendPruner(session_factory)
    pruner.start()
    try:
        await _idle(pruner)
        assert _rows(session_factory) == 25
        _set_limit(session_factory, 2)  # room for 20
        threading.Thread(target=pruner.kick).start()
        await _idle(pruner)
        assert _rows(session_factory) == 20
    finally:
        await pruner.stop()


async def test_kicks_during_a_pass_coalesce(session_factory, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def blocking(factory):
        calls.append(1)
        entered.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(trending, "prune_to_limit", blocking)
    pruner = TrendPruner(session_factory)
    pruner.start()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            pruner.kick()
        release.set()
        await _idle(pruner)
        assert len(calls) <= 2
    finally:
        release.set()
        await pruner.stop()


async def test_a_failing_pass_is_logged_and_retried_without_a_kick(session_factory, monkeypatch, caplog):
    # Otherwise the history would stay over its limit until the next daily
    # retention tick.
    _seed(session_factory, 15)
    monkeypatch.setattr(trending, "_PRUNER_RETRY_SECONDS", 0.05)
    real = trending.prune_to_limit
    state = {"n": 0}

    def flaky(factory):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("disk gone")
        return real(factory)

    monkeypatch.setattr(trending, "prune_to_limit", flaky)
    pruner = TrendPruner(session_factory)
    with caplog.at_level(logging.ERROR, logger=trending.logger.name):
        pruner.start()
        try:
            await _idle(pruner)
            assert "trend limit pruning failed" in caplog.text
            assert _rows(session_factory) == 10
        finally:
            await pruner.stop()


async def test_the_retention_tick_kicks_the_pruner(session_factory):
    kicks = []

    class FakePruner:
        def kick(self):
            kicks.append(1)

    task = asyncio.ensure_future(
        trending.run_retention_task(session_factory, pruner=FakePruner(), interval_seconds=0.01)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert kicks


def test_background_chunk_deletes_from_each_logs_oldest_end(session_factory):
    import datetime as dt

    from tests.test_trending import _assert_gap_free, _stamped_log

    t0 = dt.datetime(2026, 3, 1, 0, 0, 0)
    hour = dt.timedelta(hours=1)
    # The newest row by id of the first log has the globally-oldest stamp.
    stepped = _stamped_log(session_factory, 1, [t0 + (1 + i) * hour for i in range(5)] + [t0])
    other = _stamped_log(session_factory, 2, [t0 + (10 + i) * hour for i in range(5)])
    _set_limit(session_factory, 1)  # 10 samples; 11 stored

    assert trending.prune_chunk_to_limit(session_factory) == 1
    _assert_gap_free(session_factory, stepped, [2, 3, 4, 5, 6])
    _assert_gap_free(session_factory, other, [1, 2, 3, 4, 5])
    with session_factory() as session:
        assert get_or_create_history_counters(session).trend_total_sample_count == 10
