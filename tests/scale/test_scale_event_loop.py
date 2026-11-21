"""Area 5, the device event loop, at field size."""
from __future__ import annotations

import pytest

from tests.scale import scenarios_devices  # noqa: F401  (registers its scenarios)
from tests.scale import scenarios_event_loop as event_loop  # noqa: F401
from tests.scale.harness import BIG_JOB_LIMIT, LOOP_BLOCK_LIMIT, collector_off
from tests.scale.bounds import execute_within_bound as execute
from tests.scale.scenarios import SCENARIOS


async def run(ctx, name):
    return await execute(SCENARIOS[name], ctx)


async def test_readproperty_during_a_full_readrange_pull(ctx_ro):
    seconds, result = await run(ctx_ro, "event_loop.readproperty_during_readrange")
    assert result["result"]["records"] == 32767
    assert result["max_latency"] < LOOP_BLOCK_LIMIT
    assert result["max_gap"] < LOOP_BLOCK_LIMIT


async def test_readproperty_during_a_live_edit_of_100_objects_stays_within_its_bound(ctx_rw):
    seconds, result = await run(ctx_rw, "event_loop.readproperty_during_live_edit")
    assert result["result"] is True


async def test_readproperty_during_a_live_edit_of_100_objects(ctx_rw):
    with collector_off():
        _, result = await run(ctx_rw, "event_loop.readproperty_during_live_edit")
    assert result["max_latency"] < LOOP_BLOCK_LIMIT
    assert result["max_gap"] < LOOP_BLOCK_LIMIT


async def test_readproperty_during_a_backup_stays_within_its_bound(ctx_ro):
    seconds, result = await run(ctx_ro, "event_loop.readproperty_during_backup")
    assert result["result"] > 100_000


async def test_readproperty_during_a_backup(ctx_ro):
    with collector_off():
        _, result = await run(ctx_ro, "event_loop.readproperty_during_backup")
    assert result["max_latency"] < LOOP_BLOCK_LIMIT
    assert result["max_gap"] < LOOP_BLOCK_LIMIT
