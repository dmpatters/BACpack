"""Area 4, long uptime, at field size."""
from __future__ import annotations

import pytest

from tests.scale import scenarios_uptime as uptime
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


HISTORY_TABLES = {"traffic_log_entries", "log_entries", "client_requests"}


async def run(ctx, name):
    return await execute(SCENARIOS[name], ctx)


def _ok(response):
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize(
    "name, size",
    [
        ("uptime.traffic_api", 200),
        ("uptime.traffic_api_5000", 500),
        ("uptime.logs_api", 200),
        ("uptime.logs_api_warnings_5000", 5000),
        ("uptime.logs_api_all_devices", 200),
    ],
)
async def test_list_requests_answer_within_the_request_line(ctx_ro, name, size):
    client = web_client(ctx_ro)
    with LoopLagMonitor.on_portal(client.portal) as lag:
        seconds, response = await run(ctx_ro, name)
    assert len(_ok(response).json()) == size
    assert lag.max_gap < LOOP_BLOCK_LIMIT
    assert seconds < REQUEST_LIMIT


def _work(ctx, request):
    engine = web_engine(ctx)
    with StatementRecorder(engine) as rec:
        _ok(request())
    return engine, rec


def test_one_traffic_page_does_no_table_scan(ctx_ro):
    web_client(ctx_ro)
    engine, rec = _work(ctx_ro, lambda: uptime.get_traffic(ctx_ro, ctx_ro.ids.big_device_id))
    assert table_scans(engine, rec.statements, HISTORY_TABLES) == []


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_one_log_page_does_not_do_work_that_grows_with_the_table(ctx_ro, level):
    web_client(ctx_ro)
    big_engine, big = _work(ctx_ro, lambda: uptime.get_logs(ctx_ro, ctx_ro.ids.big_device_id, level))
    _, twin = _work(ctx_ro, lambda: uptime.get_logs(ctx_ro, ctx_ro.ids.twin_log_device_id, level))
    assert table_scans(big_engine, big.statements, HISTORY_TABLES) == []
    assert not grew_with_table(twin.vm_steps, big.vm_steps), (twin.vm_steps, big.vm_steps)


async def test_log_retention_prune(ctx_rw):
    seconds, result = await run(ctx_rw, "uptime.log_retention")
    assert 0.3 * result["before"] < result["deleted"] < 0.7 * result["before"]
    assert seconds < BIG_JOB_LIMIT


async def test_client_request_retention_prune(ctx_rw):
    seconds, result = await run(ctx_rw, "uptime.client_request_retention")
    assert 0.3 * result["before"] < result["deleted"] < 0.7 * result["before"]
    assert seconds < BIG_JOB_LIMIT


async def test_logging_traffic_on_a_full_table(ctx_rw):
    with StatementRecorder(ctx_rw.engine) as rec:
        async with LoopLagMonitor() as lag:
            seconds, count = await run(ctx_rw, "uptime.traffic_add_full_table")
    assert count == uptime.TRAFFIC_BURST
    assert table_scans(ctx_rw.engine, rec.statements, {"traffic_log_entries"}) == []
    assert lag.max_gap < LOOP_BLOCK_LIMIT
    assert seconds / count < LOOP_BLOCK_LIMIT
    with ctx_rw.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM history.traffic_log_entries WHERE device_id = ?", (ctx_rw.ids.big_device_id,)
        ).scalar()
    assert rows == 500
