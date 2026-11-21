"""Area 3, the history file at its cap, at field size."""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.exc import OperationalError

from bacpack import trending

from tests.scale import scenarios_history as history
from tests.scale.harness import (
    BIG_JOB_LIMIT,
    LOOP_BLOCK_LIMIT,
    REQUEST_LIMIT,
    LoopLagMonitor,
    StatementRecorder,
    table_scans,
)
from tests.scale.bounds import execute_within_bound as execute
from tests.scale.scenarios import SCENARIOS
from tests.scale.support import web_client


WORST_WRITE_LIMIT = 1.0
# A device's trend write during the background prune: a set line, not a
# measured one. Writes run off the device's loop, so a wait delays only that
# sample. On a Linux VM's virtual disk a commit's sync queues behind the chunk's
# checkpoint: 99th percentile 0.34 s, 0.19 s with no prune running at all.
PRUNE_WRITE_P99_LIMIT = 0.5


async def run(ctx, name):
    return await execute(SCENARIOS[name], ctx)


def _counter_and_rows(ctx):
    with ctx.engine.connect() as connection:
        counter = connection.exec_driver_sql(
            "SELECT trend_total_sample_count FROM history.history_counters"
        ).scalar()
        rows = connection.exec_driver_sql("SELECT COUNT(*) FROM history.trend_samples").scalar()
    return counter, rows


async def test_the_history_is_at_its_cap(ctx_hist):
    counter, rows = _counter_and_rows(ctx_hist)
    assert counter == rows == 1024 * 1024 * 1024 // 105


async def test_startup_against_the_full_history(ctx_hist):
    seconds, _ = await run(ctx_hist, "history.init_db_at_cap")
    assert seconds < BIG_JOB_LIMIT


async def test_the_storage_page_answers_within_the_request_line(ctx_hist):
    seconds, response = await run(ctx_hist, "history.storage_api")
    assert response.status_code == 200, response.text
    assert seconds < REQUEST_LIMIT


async def test_the_storage_page_does_not_block_the_web_loop(ctx_hist):
    client = web_client(ctx_hist)
    with LoopLagMonitor.on_portal(client.portal) as lag:
        await run(ctx_hist, "history.storage_api")
    assert lag.max_gap < LOOP_BLOCK_LIMIT


async def test_lowering_the_soft_limit_to_half_answers_within_the_request_line(ctx_hist):
    seconds, response = await run(ctx_hist, "history.prune_global_half")
    assert response.status_code == 200, response.text
    assert seconds < REQUEST_LIMIT


async def test_devices_keep_recording_while_the_background_prune_runs(ctx_hist):
    """While the pruner works through the excess, a device's trend writes
    (the sampler makes them in a worker thread; here each is timed directly)
    never fail on the lock,
    99 in 100 wait under PRUNE_WRITE_P99_LIMIT, and none waits near the 10 s
    busy timeout."""
    scenario = SCENARIOS["history.background_prune_half"]
    scenario.setup(ctx_hist)
    # The first write and the first chunk on a fresh copy pay a one-time cost.
    trending.record_sample(ctx_hist.session_factory, ctx_hist.ids.extra_trended_ids[0], 1.0)
    trending.prune_chunk_to_limit(ctx_hist.session_factory)
    prune = asyncio.create_task(asyncio.to_thread(scenario.run, ctx_hist))
    waits, failed = [], 0
    while not prune.done():
        started = time.perf_counter()
        try:
            trending.record_sample(ctx_hist.session_factory, ctx_hist.ids.extra_trended_ids[0], 1.0)
        except OperationalError:
            failed += 1
        waits.append(time.perf_counter() - started)
        await asyncio.sleep(0.1)
    await prune
    waits.sort()
    p99, worst = waits[int(len(waits) * 0.99)], waits[-1]
    assert failed == 0, f"{failed} writes failed on the lock"
    assert p99 < PRUNE_WRITE_P99_LIMIT, f"99th percentile wait {p99:.2f} s over {len(waits)} writes"
    assert worst < WORST_WRITE_LIMIT, f"worst wait {worst:.2f} s"
    counter, rows = _counter_and_rows(ctx_hist)
    assert counter == rows
    assert counter <= 1024 * 1024 * 1024 // 2 // 105


async def test_the_recount_stays_within_its_bound(ctx_hist):
    seconds, drift = await run(ctx_hist, "history.recount")
    assert drift == 0
    assert seconds < BIG_JOB_LIMIT


async def test_the_recount_does_not_block_the_loop_it_is_started_from(ctx_hist):
    # The retention task runs it in a worker thread; called as the task does,
    # the loop must stay free.
    import asyncio

    async with LoopLagMonitor() as lag:
        await asyncio.to_thread(history.trending.resync_trend_total_sample_count, ctx_hist.session_factory)
    assert lag.max_gap < LOOP_BLOCK_LIMIT


async def test_one_sample_at_the_cap_does_no_table_scan(ctx_hist):
    with StatementRecorder(ctx_hist.engine) as rec:
        async with LoopLagMonitor() as lag:
            seconds, _ = await run(ctx_hist, "history.record_sample_at_cap")
    assert table_scans(ctx_hist.engine, rec.statements, {"trend_samples"}) == []
    assert lag.max_gap < LOOP_BLOCK_LIMIT
    counter, rows = _counter_and_rows(ctx_hist)
    assert counter == rows
    assert seconds < REQUEST_LIMIT
