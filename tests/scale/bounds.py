"""Regression bounds per scenario, seconds: about 10x the slower machine's
measurement, rounded up, never above the scenario's fixed line. They catch an
order of magnitude, not drift; the benchmark is for drift. Re-measure on both
machines before changing one."""
from __future__ import annotations

from .harness import BIG_JOB_LIMIT, REQUEST_LIMIT
from .scenarios import Scenario, execute

# A floor for requests of a few milliseconds: 10x of those is inside the
# scheduler jitter of a 1-vCPU VM (a 6 ms request once took 118 ms there).
MIN_BOUND = 0.5

# A job no one waits on (the background prune of a full history) has no
# big-job line; its bound only catches it getting far slower.
BACKGROUND_JOB_BOUND = 3600.0

BOUNDS: dict[str, float] = {
    # trends
    "trends.readrange_pull_by_position": BIG_JOB_LIMIT,  # measured 7.3 s Windows, 7.8 s Linux box
    "trends.readrange_pull_by_sequence": BIG_JOB_LIMIT,  # measured 7.1 s Windows, 7.7 s Linux box
    "trends.readrange_pull_by_time": BIG_JOB_LIMIT,  # measured 11.4 s Windows, 12.3 s Linux box
    "trends.readrange_page_middle_by_position": MIN_BOUND,  # measured 0.018 s Windows, 0.019 s Linux box
    "trends.readrange_page_middle_by_sequence": MIN_BOUND,  # measured 0.018 s Windows, 0.021 s Linux box
    "trends.readrange_page_middle_by_time": MIN_BOUND,  # measured 0.023 s Windows, 0.025 s Linux box
    "trends.range_api_full_span": REQUEST_LIMIT,  # measured 0.16 s Windows, 0.16 s Linux box
    "trends.range_api_narrow": MIN_BOUND,  # measured 0.008 s Windows, 0.008 s Linux box
    "trends.trend_api_default": MIN_BOUND,  # measured 0.015 s Windows, 0.017 s Linux box
    "trends.trend_api_full": 7.0,  # measured 0.54 s Windows, 0.66 s Linux box
    "trends.export_csv_full": 5.0,  # measured 0.41 s Windows, 0.43 s Linux box
    "trends.prune_to_cap": 15.0,  # measured 0.37 s Windows, 1.3 s Linux box
    "trends.retention_one_day": BIG_JOB_LIMIT,  # measured 8.6 s Windows, 40.5 s Linux box
    "trends.delete_trended_object": 20.0,  # measured 0.59 s Windows, 1.6 s Linux box
    "trends.sampler_tick_500": 25.0,  # one pass, writes included; measured 0.88 s Windows, 2.3 s Linux box
    # devices
    "devices.build_big_device": BIG_JOB_LIMIT,  # measured 5.3 s Windows, 6.0 s Linux box
    "devices.object_list_whole": MIN_BOUND,  # measured 0.008 s Windows, 0.011 s Linux box
    "devices.object_list_by_index": MIN_BOUND,  # measured 0.006 s Windows, 0.007 s Linux box
    "devices.rpm_all_many": 9.0,  # measured 0.54-0.68 s Windows, 0.61 s Linux box
    "devices.live_edit_one_object": 3.0,  # measured 0.25 s Windows, 0.29 s Linux box
    "devices.config_export_50": 7.0,  # measured 0.53 s Windows, 0.62 s Linux box
    "devices.config_import_dry_run_50": 20.0,  # measured 1.7 s Windows, 1.9 s Linux box
    "devices.config_import_apply_50": 45.0,  # measured 3.8 s Windows, 4.4 s Linux box
    "devices.csv_export_big": 4.0,  # measured 0.30 s Windows, 0.37 s Linux box
    "devices.csv_sync_dry_run_big": 10.0,  # measured 0.82 s Windows, 0.94 s Linux box
    "devices.csv_sync_apply_big": 25.0,  # measured 1.8 s Windows, 2.2 s Linux box
    "devices.backup_big": 25.0,  # measured 2.1 s Windows, 1.1 s Linux box
    "devices.restore_big": 50.0,  # measured 4.6 s Windows, 3.9 s Linux box
    "devices.list_api_50": REQUEST_LIMIT,  # measured 0.37 s Windows, 0.44 s Linux box
    "devices.engine_status_50": MIN_BOUND,  # measured 0.004 s Windows, 0.007 s Linux box
    "devices.objects_list_api_big": REQUEST_LIMIT,  # measured 0.28 s Windows, 0.31 s Linux box
    # history
    "history.init_db_at_cap": MIN_BOUND,  # measured 0.047 s Windows, 0.045 s Linux box
    "history.storage_api": REQUEST_LIMIT,  # measured 0.80 s Windows, 0.64 s Linux box
    "history.prune_global_half": REQUEST_LIMIT,  # the settings save alone; measured 0.037 s Windows, 0.046 s Linux box
    # Nobody waits on the background prune; this only catches it becoming far slower.
    "history.background_prune_half": BACKGROUND_JOB_BOUND,  # measured 165 s Windows, 579 s Linux box
    "history.recount": 3.0,  # measured 0.22 s Windows, 0.074 s Linux box
    "history.record_sample_at_cap": 0.8,  # measured 0.022 s Windows, 0.079 s Linux box
    # uptime
    "uptime.traffic_api": MIN_BOUND,  # measured 0.005 s Windows, 0.006 s Linux box
    "uptime.traffic_api_5000": MIN_BOUND,  # measured 0.008 s Windows, 0.010 s Linux box
    "uptime.logs_api": MIN_BOUND,  # measured 0.006 s Windows, 0.006 s Linux box
    "uptime.logs_api_warnings_5000": 0.9,  # measured 0.085 s Windows, 0.086 s Linux box
    "uptime.logs_api_all_devices": MIN_BOUND,  # measured 0.005 s Windows, 0.007 s Linux box
    "uptime.log_retention": 40.0,  # measured 2.5 s Windows, 3.6 s Linux box
    "uptime.client_request_retention": 4.0,  # measured 0.32 s Windows, 0.37 s Linux box
    "uptime.traffic_add_full_table": 7.0,  # 200 packets; measured 0.63 s Windows, 0.66 s Linux box
    # event loop
    "event_loop.readproperty_during_readrange": BIG_JOB_LIMIT,  # measured 7.7 s Windows, 8.8 s Linux box
    "event_loop.readproperty_during_live_edit": 25.0,  # measured 2.3 s Windows, 2.3 s Linux box
    "event_loop.readproperty_during_backup": 30.0,  # measured 2.5 s Windows, 1.9 s Linux box
}


async def execute_within_bound(scenario: Scenario, ctx):
    """execute, then hold the scenario to its regression bound. The tests use
    this; the benchmark uses plain execute, since it records instead."""
    seconds, result = await execute(scenario, ctx)
    bound = BOUNDS[scenario.name]
    assert seconds < bound, f"{scenario.name}: {seconds:.3f} s against a bound of {bound} s"
    return seconds, result
