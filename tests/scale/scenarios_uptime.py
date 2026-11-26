"""Area 4, long uptime: the traffic, log and client-request tables at their
retention sizes."""
from __future__ import annotations

import asyncio
import datetime as dt
from unittest import mock

from bacpack import client_requests, timeutil
from bacpack.history_writer import HistoryWriter
from bacpack.log_capture import prune_old_log_entries
from bacpack.traffic import TrafficLog

from .scenarios import Context, scenario
from .support import web_client

TRAFFIC_BURST = 200


def _client(ctx: Context) -> None:
    web_client(ctx)


def get_traffic(ctx: Context, device_id: int, limit: int = 200):
    return ctx.state["client"].get("/api/traffic", params={"device_id": device_id, "limit": limit})


def get_logs(ctx: Context, device_id: int | None, level: str = "INFO", limit: int = 200):
    params = {"level": level, "limit": limit}
    if device_id is not None:
        params["device_id"] = device_id
    return ctx.state["client"].get("/api/logs", params=params)


@scenario("uptime.traffic_api", area="uptime", dataset="install", setup=_client)
def traffic_api(ctx: Context):
    return get_traffic(ctx, ctx.ids.big_device_id)


@scenario("uptime.traffic_api_5000", area="uptime", dataset="install", setup=_client)
def traffic_api_5000(ctx: Context):
    return get_traffic(ctx, ctx.ids.big_device_id, limit=5000)


@scenario("uptime.logs_api", area="uptime", dataset="install", setup=_client)
def logs_api(ctx: Context):
    return get_logs(ctx, ctx.ids.big_device_id)


@scenario("uptime.logs_api_warnings_5000", area="uptime", dataset="install", setup=_client)
def logs_api_warnings_5000(ctx: Context):
    return get_logs(ctx, ctx.ids.big_device_id, level="WARNING", limit=5000)


@scenario("uptime.logs_api_all_devices", area="uptime", dataset="install", setup=_client)
def logs_api_all_devices(ctx: Context):
    return get_logs(ctx, None)


def _count_and_pin_now(ctx: Context, table: str, column: str) -> None:
    """"Now" is the table's newest row: from the real now, the share past
    retention grows with the cached dataset's age."""
    with ctx.engine.connect() as connection:
        before, newest = connection.exec_driver_sql(f"SELECT COUNT(*), MAX({column}) FROM history.{table}").one()
    ctx.state["before"] = before
    patch = mock.patch.object(timeutil, "db_utc_now", return_value=dt.datetime.fromisoformat(newest))
    patch.start()
    ctx.on_close(patch.stop)


def _half_log_retention(ctx: Context) -> None:
    with ctx.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE server_settings SET log_retention_days = 7")
    _count_and_pin_now(ctx, "log_entries", "timestamp")


@scenario("uptime.log_retention", area="uptime", dataset="install", writes=True, setup=_half_log_retention)
def log_retention(ctx: Context):
    """Log retention with half the entries past it."""
    return {"deleted": prune_old_log_entries(ctx.session_factory), "before": ctx.state["before"]}


def _half_request_retention(ctx: Context) -> None:
    patch = mock.patch.object(client_requests, "CLIENT_REQUEST_RETENTION", dt.timedelta(hours=12))
    patch.start()
    ctx.on_close(patch.stop)
    _count_and_pin_now(ctx, "client_requests", "created_at")


@scenario(
    "uptime.client_request_retention",
    area="uptime",
    dataset="install",
    writes=True,
    setup=_half_request_retention,
)
def client_request_retention(ctx: Context):
    """Client request retention with half the rows past it."""
    return {
        "deleted": client_requests.prune_old_client_requests(ctx.session_factory),
        "before": ctx.state["before"],
    }


@scenario("uptime.traffic_add_full_table", area="uptime", dataset="install", writes=True)
async def traffic_add_full_table(ctx: Context):
    """200 packets logged one after another on a device whose traffic table
    is full, yielding to the loop between them as a served device does.
    Returns how many of them reached the table and whether the flush finished."""
    device_id = ctx.ids.big_device_id
    query = "SELECT COALESCE(MAX(id), 0) FROM history.traffic_log_entries WHERE device_id = ?"
    with ctx.engine.connect() as connection:
        newest = connection.exec_driver_sql(query, (device_id,)).scalar()
    writer = HistoryWriter(ctx.session_factory, device_id=device_id)
    writer.start()
    try:
        log = TrafficLog(writer=writer, device_id=device_id)
        for n in range(TRAFFIC_BURST):
            log.add("read-property", f"192.168.1.60 -> analog-value:{n} presentValue")
            await asyncio.sleep(0)
        flushed = await asyncio.to_thread(writer.flush, 60.0)
    finally:
        writer.close()
    with ctx.engine.connect() as connection:
        added = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM history.traffic_log_entries WHERE device_id = ? AND id > ?", (device_id, newest)
        ).scalar()
    return {"added": added, "flushed": flushed}
