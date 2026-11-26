"""Area 2, devices and objects, at field size."""
from __future__ import annotations

import pathlib
import subprocess
import sys
from dataclasses import replace

import pytest
from bacpypes3.apdu import ReadPropertyACK
from bacpypes3.primitivedata import ObjectIdentifier

from tests.scale import scenarios_devices as devices
from tests.scale.harness import (
    BIG_JOB_LIMIT,
    LOOP_BLOCK_LIMIT,
    REQUEST_LIMIT,
    LoopLagMonitor,
    collector_off,
    StatementRecorder,
    table_scans,
)
from tests.scale.bounds import execute_within_bound as execute
from tests.scale.scenarios import SCENARIOS
from tests.scale.support import web_client


async def run(ctx, name):
    return await execute(SCENARIOS[name], ctx)


async def run_on_device_loop(ctx, name):
    """The scenario with a ticker on the loop it runs on (a device's loop)."""
    devices_setup = SCENARIOS[name].setup
    if devices_setup is not None:
        devices_setup(ctx)
    async with LoopLagMonitor() as lag:
        seconds, result = await execute(_without_setup(name), ctx)
    return seconds, result, lag.max_gap


def _without_setup(name):
    """The scenario with its setup already run by the caller, so the monitor
    wraps only the timed action."""
    return replace(SCENARIOS[name], setup=None)


async def run_on_web_loop(ctx, name):
    client = web_client(ctx)
    scenario_setup = SCENARIOS[name].setup
    if scenario_setup is not None:
        scenario_setup(ctx)
    with LoopLagMonitor.on_portal(client.portal) as lag:
        seconds, result = await execute(_without_setup(name), ctx)
    return seconds, result, lag.max_gap


def _ok(response):
    assert response.status_code == 200, response.text
    return response


async def test_starting_the_big_device(ctx_ro):
    seconds, app = await run(ctx_ro, "devices.build_big_device")
    assert len(list(app.objectIdentifier)) >= 5000
    assert seconds < BIG_JOB_LIMIT


async def test_reading_the_whole_object_list_does_not_block_the_loop(ctx_ro):
    seconds, resp, gap = await run_on_device_loop(ctx_ro, "devices.object_list_whole")
    # A fast Error or Abort would otherwise pass the timing bounds.
    assert isinstance(resp, ReadPropertyACK), resp
    assert resp.propertyValue is not None
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_reading_one_object_list_entry_is_quick(ctx_ro):
    seconds, resp, gap = await run_on_device_loop(ctx_ro, "devices.object_list_by_index")
    assert isinstance(resp, ReadPropertyACK), resp
    assert int(resp.propertyArrayIndex) == 2500
    assert resp.propertyValue is not None
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < LOOP_BLOCK_LIMIT


async def test_rpm_all_of_500_objects_stays_within_its_bound(ctx_ro):
    seconds, resp, _ = await run_on_device_loop(ctx_ro, "devices.rpm_all_many")
    assert len(resp.listOfReadAccessResults) == devices.RPM_OBJECTS
    assert seconds < BIG_JOB_LIMIT


async def test_rpm_all_of_500_objects_does_not_block_the_loop(ctx_ro):
    with collector_off():
        _, _, gap = await run_on_device_loop(ctx_ro, "devices.rpm_all_many")
    assert gap < LOOP_BLOCK_LIMIT


def test_a_full_collection_on_the_big_devices_process_does_not_block_the_loop(ctx_ro):
    probe = subprocess.run(
        [sys.executable, "-m", "tests.scale.gc_probe", str(ctx_ro.db_path), str(ctx_ro.ids.big_device_id)],
        capture_output=True, text=True, check=True, cwd=pathlib.Path(__file__).parents[2],
    )
    assert float(probe.stdout.split()[-1]) < LOOP_BLOCK_LIMIT


async def _live_edit(ctx):
    devices.live_edit_setup(ctx)
    with StatementRecorder(ctx.engine) as rec:
        async with LoopLagMonitor() as lag:
            seconds, handled = await execute(_without_setup("devices.live_edit_one_object"), ctx)
    return seconds, handled, lag.max_gap, rec


async def test_a_live_edit_of_one_object(ctx_rw):
    seconds, handled, _, rec = await _live_edit(ctx_rw)
    app = ctx_rw.state["app"]
    assert handled is True
    with ctx_rw.engine.connect() as connection:
        object_type, instance = connection.exec_driver_sql(
            "SELECT object_type, instance_number FROM bacnet_objects WHERE id = ?",
            (devices.live_edit_object_id(ctx_rw),),
        ).one()
    assert app.get_object_id(ObjectIdentifier((object_type, instance))).description == devices.LIVE_EDIT_DESCRIPTION
    assert table_scans(ctx_rw.engine, rec.statements, {"bacnet_objects"}) == []
    assert seconds < BIG_JOB_LIMIT


async def test_a_live_edit_of_one_object_does_not_block_the_loop(ctx_rw):
    with collector_off():
        _, _, gap, _ = await _live_edit(ctx_rw)
    assert gap < LOOP_BLOCK_LIMIT


async def test_config_export_of_50_devices(ctx_ro):
    seconds, response, gap = await run_on_web_loop(ctx_ro, "devices.config_export_50")
    assert len(_ok(response).json()["devices"]) == 50
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_config_import_dry_run_of_50_devices(ctx_ro):
    seconds, response, gap = await run_on_web_loop(ctx_ro, "devices.config_import_dry_run_50")
    assert len(_ok(response).json()["devices_updated"]) >= 1
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_config_import_apply_of_50_devices(ctx_rw):
    seconds, response, gap = await run_on_web_loop(ctx_rw, "devices.config_import_apply_50")
    body = _ok(response).json()
    assert body["dry_run"] is False
    # The apply changed something: every big-device object was edited.
    assert len(body["devices_updated"]) >= 1
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_csv_export_of_the_big_device(ctx_ro):
    seconds, response, gap = await run_on_web_loop(ctx_ro, "devices.csv_export_big")
    assert _ok(response).text.count("\n") > 5000
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_csv_sync_dry_run_of_the_big_device(ctx_ro):
    seconds, response, gap = await run_on_web_loop(ctx_ro, "devices.csv_sync_dry_run_big")
    assert len(_ok(response).json()["updated"]) == 5000
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_csv_sync_apply_of_the_big_device(ctx_rw):
    seconds, response, gap = await run_on_web_loop(ctx_rw, "devices.csv_sync_apply_big")
    assert len(_ok(response).json()["updated"]) == 5000
    assert gap < LOOP_BLOCK_LIMIT
    assert seconds < BIG_JOB_LIMIT


async def test_bacnet_backup_of_the_big_device_stays_within_its_bound(ctx_ro):
    seconds, data, _ = await run_on_device_loop(ctx_ro, "devices.backup_big")
    assert len(data) > 100_000
    assert seconds < BIG_JOB_LIMIT


async def test_bacnet_backup_of_the_big_device_does_not_block_the_loop(ctx_ro):
    with collector_off():
        _, _, gap = await run_on_device_loop(ctx_ro, "devices.backup_big")
    assert gap < LOOP_BLOCK_LIMIT


async def test_bacnet_restore_of_the_big_device_stays_within_its_bound(ctx_rw):
    seconds, size, _ = await run_on_device_loop(ctx_rw, "devices.restore_big")
    assert size > 100_000
    assert seconds < BIG_JOB_LIMIT


async def test_bacnet_restore_of_the_big_device_does_not_block_the_loop(ctx_rw):
    with collector_off():
        _, _, gap = await run_on_device_loop(ctx_rw, "devices.restore_big")
    assert gap < LOOP_BLOCK_LIMIT


LISTS = ["devices.list_api_50", "devices.engine_status_50", "devices.objects_list_api_big"]


@pytest.mark.parametrize("name", LISTS)
async def test_list_endpoints_answer_within_the_request_line(ctx_ro, name):
    # The objects list is the whole list by design, so work that grows with
    # it is expected; only its time is held.
    seconds, response, _ = await run_on_web_loop(ctx_ro, name)
    _ok(response)
    assert seconds < REQUEST_LIMIT


@pytest.mark.parametrize(
    "name",
    [
        "devices.list_api_50",
        "devices.engine_status_50",
        "devices.objects_list_api_big",
    ],
)
async def test_list_endpoints_do_not_block_the_web_loop(ctx_ro, name):
    _, _, gap = await run_on_web_loop(ctx_ro, name)
    assert gap < LOOP_BLOCK_LIMIT


async def test_the_exported_config_of_the_install_fits_the_import_limit(ctx_ro):
    from bacpack.web.api._uploads import MAX_UPLOAD_BYTES

    body = web_client(ctx_ro).get("/api/config/export").content
    assert len(body) <= MAX_UPLOAD_BYTES, f"{len(body)} bytes against a limit of {MAX_UPLOAD_BYTES}"


async def test_the_big_devices_backup_fits_the_restore_limit(ctx_ro):
    from bacpack.backup_restore import MAX_RESTORE_BYTES

    _, data, _ = await run_on_device_loop(ctx_ro, "devices.backup_big")
    assert len(data) <= MAX_RESTORE_BYTES, f"{len(data)} bytes against a limit of {MAX_RESTORE_BYTES}"
