from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from bacpack.db import make_engine
from tests.scale.harness import LoopLagMonitor, StatementRecorder, scale_collection, table_scans


async def test_loop_lag_monitor_sees_a_blocking_call():
    async with LoopLagMonitor() as lag:
        await asyncio.sleep(0.05)
        time.sleep(0.3)
        await asyncio.sleep(0.05)
    assert lag.max_gap >= 0.25


async def test_loop_lag_monitor_quiet_loop():
    async with LoopLagMonitor() as lag:
        await asyncio.sleep(0.2)
    assert lag.max_gap < 0.1


def test_statement_recorder_and_table_scans(session_factory):
    engine = session_factory.kw["bind"]
    with StatementRecorder(engine) as rec:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT * FROM history.trend_samples").fetchall()
            connection.exec_driver_sql(
                "SELECT * FROM history.trend_samples WHERE object_id = 1 ORDER BY id"
            ).fetchall()
    assert len(rec.statements) == 2
    scans = table_scans(engine, rec.statements, {"trend_samples"})
    assert len(scans) == 1
    assert "SCAN" in scans[0]


class _Item:
    def __init__(self, path):
        self.path = path
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def _config(scale, numprocesses=None):
    options = {"scale": scale, "numprocesses": numprocesses}
    return SimpleNamespace(
        getoption=lambda name, default=None: options.get(name, default),
        pluginmanager=SimpleNamespace(hasplugin=lambda name: numprocesses is not None),
    )


def _names(item):
    return [m.name for m in item.markers]


def test_scale_tests_skip_without_the_flag():
    from tests.scale import harness

    scale_item = _Item(harness.SCALE_DIR / "test_scale_trends.py")
    other = _Item(harness.SCALE_DIR.parent / "test_db.py")
    scale_collection(_config(False), [scale_item, other])
    assert _names(scale_item) == ["scale", "skip"]
    assert _names(other) == []

    scale_item = _Item(harness.SCALE_DIR / "test_scale_trends.py")
    scale_collection(_config(True), [scale_item])
    assert _names(scale_item) == ["scale"]


def test_scale_refuses_xdist():
    with pytest.raises(pytest.exit.Exception, match="-p no:xdist"):
        scale_collection(_config(True, numprocesses=2), [])
    scale_collection(_config(False, numprocesses=2), [])


async def test_scenario_registers_and_times(monkeypatch):
    from tests.scale import scenarios

    monkeypatch.setattr(scenarios, "SCENARIOS", {})

    @scenarios.scenario("demo.sync", area="trends", dataset="install", writes=False)
    def sync_one(ctx):
        return 1

    @scenarios.scenario("demo.async", area="trends", dataset="install", writes=True)
    async def async_one(ctx):
        await asyncio.sleep(0.01)
        return 2

    seconds, result = await scenarios.execute(scenarios.SCENARIOS["demo.sync"], None)
    assert seconds >= 0 and result == 1
    seconds, result = await scenarios.execute(scenarios.SCENARIOS["demo.async"], None)
    assert seconds >= 0.01 and result == 2
    assert scenarios.SCENARIOS["demo.async"].writes is True


def test_duplicate_scenario_name_refused(monkeypatch):
    from tests.scale import scenarios

    monkeypatch.setattr(scenarios, "SCENARIOS", {})
    scenarios.scenario("demo.twice", area="trends", dataset="install")(lambda ctx: None)
    with pytest.raises(ValueError, match="already registered"):
        scenarios.scenario("demo.twice", area="trends", dataset="install")(lambda ctx: None)


async def test_setup_runs_before_the_timed_action_and_is_not_timed(monkeypatch):
    from tests.scale import scenarios

    monkeypatch.setattr(scenarios, "SCENARIOS", {})
    ctx = scenarios.Context(None, None, None, None, None)

    async def setup(c):
        await asyncio.sleep(0.2)
        c.state["ready"] = True

    @scenarios.scenario("demo.setup", area="trends", dataset="install", setup=setup)
    def run(c):
        return c.state["ready"]

    seconds, result = await scenarios.execute(scenarios.SCENARIOS["demo.setup"], ctx)
    assert result is True and seconds < 0.1


def test_context_close_runs_closers_once():
    from tests.scale import scenarios

    ctx = scenarios.Context(None, None, None, None, None)
    calls = []
    ctx.on_close(lambda: calls.append("a"))
    ctx.on_close(lambda: calls.append("b"))
    ctx.close()
    ctx.close()
    assert calls == ["b", "a"]


def test_statement_recorder_counts_database_work(session_factory):
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO history.trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
            "VALUES (?, ?, '2026-01-01 00:00:00.000000', 1.0, 0.0, 'value')",
            [(1, n) for n in range(1, 5001)] + [(2, 1)],
        )

    def steps(object_id):
        with StatementRecorder(engine) as rec:
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM history.trend_samples WHERE object_id = ?", (object_id,)
                ).scalar()
        return rec.vm_steps

    assert steps(1) > 10 * max(steps(2), 1)


async def test_loop_lag_monitor_sees_a_block_that_ends_the_measurement():
    async with LoopLagMonitor() as lag:
        await asyncio.sleep(0.05)
        time.sleep(0.3)
    assert lag.max_gap >= 0.25


def test_every_scenario_has_a_regression_bound_and_none_is_stale():
    from tests.scale.bounds import BACKGROUND_JOB_BOUND, BOUNDS
    from tests.scale.harness import BIG_JOB_LIMIT
    from tests.scale.scenarios import SCENARIOS, load_all

    load_all()
    assert set(BOUNDS) == set(SCENARIOS)
    # Only a job nobody waits on may run past the big-job line.
    assert all(
        0 < bound <= (BACKGROUND_JOB_BOUND if name == "history.background_prune_half" else BIG_JOB_LIMIT)
        for name, bound in BOUNDS.items()
    )
