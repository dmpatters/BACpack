"""Area 1, trend logs, at field size."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.scale import scenarios_trends as trends
from tests.scale.harness import (
    BIG_JOB_LIMIT,
    LOOP_BLOCK_LIMIT,
    REQUEST_LIMIT,
    LoopLagMonitor,
    StatementRecorder,
    grew_with_table,
    table_scans,
)
from tests.scale.bounds import execute_within_bound as execute
from tests.scale.scenarios import SCENARIOS
from tests.scale.support import web_client, web_engine

FULL = 32767
BIG_TABLES = {"trend_samples"}


async def run(ctx, name):
    return await execute(SCENARIOS[name], ctx)


@pytest.mark.parametrize("kind", trends.KINDS)
async def test_full_pull_returns_every_record_without_blocking_the_loop(ctx_ro, kind):
    async with LoopLagMonitor() as lag:
        seconds, result = await run(ctx_ro, f"trends.readrange_pull_by_{kind}")
    assert result["records"] == FULL
    assert lag.max_gap < LOOP_BLOCK_LIMIT


@pytest.mark.parametrize("kind", trends.KINDS)
async def test_one_page_does_not_do_work_that_grows_with_the_log(ctx_ro, kind):
    trends.log_app(ctx_ro)
    app = ctx_ro.state["app"]
    work = {}
    for name, object_id in (("full", ctx_ro.ids.full_log_ids[0]), ("twin", ctx_ro.ids.small_twin_id)):
        reference = trends.middle_reference(ctx_ro, object_id, kind)
        with StatementRecorder(ctx_ro.engine) as rec:
            resp = await trends.read_range_page(app, object_id, kind, reference)
        assert int(resp.itemCount) > 0
        work[name] = rec
    assert table_scans(ctx_ro.engine, work["full"].statements, BIG_TABLES) == []
    assert len(work["full"].statements) == len(work["twin"].statements)
    assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
        work["twin"].vm_steps,
        work["full"].vm_steps,
    )


def _ok(response):
    assert response.status_code == 200, response.text
    return response


async def test_range_api_over_the_full_span(ctx_ro):
    seconds, response = await run(ctx_ro, "trends.range_api_full_span")
    assert len(_ok(response).json()["samples"]) <= 1000 + 2
    assert seconds < REQUEST_LIMIT


async def test_range_api_narrow_window_does_not_grow_with_the_log(ctx_ro):
    engine = web_engine(ctx_ro)
    work = {}
    for name, object_id in (("full", ctx_ro.ids.full_log_ids[0]), ("twin", ctx_ro.ids.small_twin_id)):
        params = trends.narrow_params(ctx_ro, object_id)
        with StatementRecorder(engine) as rec:
            # An empty reply would be quick for any table size.
            assert _ok(trends.get_range(ctx_ro, object_id, params)).json()["samples"]
        work[name] = rec
    assert table_scans(engine, work["full"].statements, BIG_TABLES) == []
    assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
        work["twin"].vm_steps,
        work["full"].vm_steps,
    )
    seconds, response = await run(ctx_ro, "trends.range_api_narrow")
    _ok(response)
    assert seconds < REQUEST_LIMIT


async def test_default_trend_view_does_not_grow_with_the_log(ctx_ro):
    engine = web_engine(ctx_ro)
    work = {}
    for name, object_id in (("full", ctx_ro.ids.full_log_ids[0]), ("twin", ctx_ro.ids.small_twin_id)):
        with StatementRecorder(engine) as rec:
            assert len(_ok(trends.get_trend(ctx_ro, object_id)).json()) == 1000
        work[name] = rec
    assert table_scans(engine, work["full"].statements, BIG_TABLES) == []
    assert not grew_with_table(work["twin"].vm_steps, work["full"].vm_steps), (
        work["twin"].vm_steps,
        work["full"].vm_steps,
    )
    seconds, _ = await run(ctx_ro, "trends.trend_api_default")
    assert seconds < REQUEST_LIMIT


@pytest.mark.parametrize("name", ["trends.trend_api_full", "trends.export_csv_full"])
async def test_full_log_downloads_do_not_block_the_web_loop(ctx_ro, name):
    client = web_client(ctx_ro)
    with LoopLagMonitor.on_portal(client.portal) as lag:
        seconds, response = await run(ctx_ro, name)
    _ok(response)
    assert lag.max_gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_prune_to_a_lower_cap(ctx_rw):
    seconds, removed = await run(ctx_rw, "trends.prune_to_cap")
    assert removed == FULL - 100
    assert seconds < BIG_JOB_LIMIT
    with ctx_rw.engine.connect() as connection:
        counter = connection.exec_driver_sql(
            "SELECT trend_total_sample_count FROM history.history_counters"
        ).scalar()
        actual = connection.exec_driver_sql("SELECT COUNT(*) FROM history.trend_samples").scalar()
    assert counter == actual


async def test_retention_prune(ctx_rw):
    seconds, result = await run(ctx_rw, "trends.retention_one_day")
    assert 0.2 * result["before"] < result["deleted"] < 0.8 * result["before"]
    assert seconds < BIG_JOB_LIMIT
    with ctx_rw.engine.connect() as connection:
        counter = connection.exec_driver_sql(
            "SELECT trend_total_sample_count FROM history.history_counters"
        ).scalar()
        actual = connection.exec_driver_sql("SELECT COUNT(*) FROM history.trend_samples").scalar()
    assert counter == actual == result["before"] - result["deleted"]


async def test_deleting_a_full_log_does_not_block_the_web_loop(ctx_rw):
    client = web_client(ctx_rw)
    with LoopLagMonitor.on_portal(client.portal) as lag:
        seconds, response = await run(ctx_rw, "trends.delete_trended_object")
    assert response.status_code == 204, response.text
    assert lag.max_gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_a_sampler_pass_over_500_objects_stays_within_its_bound(ctx_rw):
    seconds, result = await run(ctx_rw, "trends.sampler_tick_500")
    assert result["due"] == 500
    assert result["added"] == 500
    assert seconds < BIG_JOB_LIMIT


async def test_a_sampler_pass_over_500_objects_does_not_block_the_device_loop(ctx_rw):
    scenario = SCENARIOS["trends.sampler_tick_500"]
    await scenario.setup(ctx_rw)
    async with LoopLagMonitor() as lag:
        _, result = await execute(replace(scenario, setup=None), ctx_rw)
    assert result["added"] == 500
    assert lag.max_gap < LOOP_BLOCK_LIMIT
