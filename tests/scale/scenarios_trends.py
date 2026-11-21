"""Area 1, trend logs: ReadRange pulls of a full log, the trend API, pruning,
retention, deleting a full log and one sampler pass over 500 trended
objects."""
from __future__ import annotations

import asyncio
import datetime as dt
from unittest import mock

from bacpypes3.apdu import ReadRangeRequest
from bacpypes3.basetypes import LogRecord, Range, RangeByPosition, RangeBySequenceNumber, RangeByTime
from bacpypes3.constructeddata import ListOf

from bacpack import timeutil, trending
from bacpack.db import lock_both_files

from .bacnet_requests import send
from .scenarios import Context, scenario
from .support import application_for, iso_z, sample_span, web_client

PAGE_COUNT = 1000
KINDS = ("position", "sequence", "time")
_MAX_PAGES = 5000


async def read_range_page(app, object_id: int, kind: str, reference):
    """One ReadRange the way YABE sends it: unsegmented, a 1476-octet reply."""
    choice = {
        "position": lambda: Range(byPosition=RangeByPosition(referenceIndex=reference, count=PAGE_COUNT)),
        "sequence": lambda: Range(
            bySequenceNumber=RangeBySequenceNumber(referenceSequenceNumber=reference, count=PAGE_COUNT)
        ),
        "time": lambda: Range(byTime=RangeByTime(referenceTime=reference, count=PAGE_COUNT)),
    }[kind]()
    request = ReadRangeRequest(
        objectIdentifier=("trend-log", object_id), propertyIdentifier="logBuffer", range=choice
    )
    request.apduMaxResp = 5
    request.apduSA = False
    request.apduMaxSegs = 0
    return await send(app, request)


def _records(resp) -> list:
    return list(resp.itemData.cast_out(ListOf(LogRecord)))


async def pull_log(app, object_id: int, kind: str) -> dict:
    """Pages through a whole log forward until a reply isn't trimmed."""
    reference = {"position": 1, "sequence": 1, "time": dt.datetime(2000, 1, 1)}[kind]
    pages = records = 0
    while pages < _MAX_PAGES:
        resp = await read_range_page(app, object_id, kind, reference)
        pages += 1
        count = int(resp.itemCount)
        records += count
        if not int(resp.resultFlags[2]) or not count:
            break
        if kind == "time":
            reference = timeutil.python_datetime(_records(resp)[-1].timestamp)
        else:
            reference += count
    return {"pages": pages, "records": records}


def _sample_count(ctx: Context, object_id: int) -> int:
    with ctx.engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT COUNT(*) FROM history.trend_samples WHERE object_id = ?", (object_id,)
        ).scalar()


def middle_reference(ctx: Context, object_id: int, kind: str):
    """A reference in the middle of the log's rows."""
    middle = _sample_count(ctx, object_id) // 2
    if kind != "time":
        return middle
    with ctx.engine.connect() as connection:
        stored = connection.exec_driver_sql(
            "SELECT timestamp FROM history.trend_samples WHERE object_id = ? ORDER BY id LIMIT 1 OFFSET ?",
            (object_id, middle),
        ).scalar()
    return timeutil.host_local(timeutil.from_db(dt.datetime.fromisoformat(stored)))


async def middle_page(app, ctx: Context, object_id: int, kind: str):
    return await read_range_page(app, object_id, kind, middle_reference(ctx, object_id, kind))


def log_app(ctx: Context) -> None:
    ctx.state["app"] = application_for(ctx, [ctx.ids.full_log_ids[0], ctx.ids.small_twin_id])


def _register_reads(kind: str) -> None:
    @scenario(f"trends.readrange_pull_by_{kind}", area="trends", dataset="install", setup=log_app)
    async def pull(ctx: Context):
        return await pull_log(ctx.state["app"], ctx.ids.full_log_ids[0], kind)

    @scenario(f"trends.readrange_page_middle_by_{kind}", area="trends", dataset="install", setup=log_app)
    async def page(ctx: Context):
        return await middle_page(ctx.state["app"], ctx, ctx.ids.full_log_ids[0], kind)


for _kind in KINDS:
    _register_reads(_kind)


def trend_url(ctx: Context, object_id: int, tail: str = "") -> str:
    return f"/api/devices/{ctx.ids.big_device_id}/objects/{object_id}/trend{tail}"


def _client(ctx: Context) -> None:
    web_client(ctx)


@scenario("trends.range_api_full_span", area="trends", dataset="install", setup=_client)
def range_api_full_span(ctx: Context):
    first, last = sample_span(ctx, ctx.ids.full_log_ids[0])
    response = ctx.state["client"].get(
        trend_url(ctx, ctx.ids.full_log_ids[0], "/range"),
        params={"start": iso_z(first - dt.timedelta(minutes=1)), "end": iso_z(last + dt.timedelta(minutes=1))},
    )
    return response


def narrow_params(ctx: Context, object_id: int) -> dict:
    """The last hour of a log."""
    _, last = sample_span(ctx, object_id)
    return {"start": iso_z(last - dt.timedelta(hours=1)), "end": iso_z(last + dt.timedelta(minutes=1))}


def get_range(ctx: Context, object_id: int, params: dict):
    return ctx.state["client"].get(trend_url(ctx, object_id, "/range"), params=params)


@scenario("trends.range_api_narrow", area="trends", dataset="install", setup=_client)
def range_api_narrow(ctx: Context):
    object_id = ctx.ids.full_log_ids[0]
    return get_range(ctx, object_id, narrow_params(ctx, object_id))


def get_trend(ctx: Context, object_id: int):
    return ctx.state["client"].get(trend_url(ctx, object_id))


@scenario("trends.trend_api_default", area="trends", dataset="install", setup=_client)
def trend_api_default(ctx: Context):
    return get_trend(ctx, ctx.ids.full_log_ids[0])


@scenario("trends.trend_api_full", area="trends", dataset="install", setup=_client)
def trend_api_full(ctx: Context):
    return ctx.state["client"].get(trend_url(ctx, ctx.ids.full_log_ids[0]), params={"limit": 32767})


@scenario("trends.export_csv_full", area="trends", dataset="install", setup=_client)
def export_csv_full(ctx: Context):
    return ctx.state["client"].get(trend_url(ctx, ctx.ids.full_log_ids[0], "/export.csv"))


@scenario("trends.prune_to_cap", area="trends", dataset="install", writes=True)
def prune_to_cap(ctx: Context):
    with ctx.session_factory() as session:
        lock_both_files(session)
        removed = trending.prune_object_to_cap(session, ctx.ids.full_log_ids[0], 100)
        trending._adjust_total_sample_count(session, -removed)
        session.commit()
    return removed


def _one_day_retention(ctx: Context) -> None:
    with ctx.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE server_settings SET trend_retention_days = 1")
        before, newest = connection.exec_driver_sql(
            "SELECT COUNT(*), MAX(timestamp) FROM history.trend_samples"
        ).one()
    ctx.state["before"] = before
    ctx.state["newest"] = dt.datetime.fromisoformat(newest)


@scenario("trends.retention_one_day", area="trends", dataset="install", writes=True, setup=_one_day_retention)
def retention_one_day(ctx: Context):
    """prune_old_samples with about a third of the samples past retention.
    "Now" is the dataset's newest sample: from the real now, a cached
    dataset a day old had 75% past retention, and the share kept growing."""
    with mock.patch.object(timeutil, "db_utc_now", return_value=ctx.state["newest"]):
        deleted = trending.prune_old_samples(ctx.session_factory)
    return {"deleted": deleted, "before": ctx.state["before"]}


@scenario("trends.delete_trended_object", area="trends", dataset="install", writes=True, setup=_client)
def delete_trended_object(ctx: Context):
    return ctx.state["client"].delete(
        f"/api/devices/{ctx.ids.big_device_id}/objects/{ctx.ids.full_log_ids[0]}"
    )


async def _started_sampler(ctx: Context) -> None:
    """The app and a started TrendSampler, outside the timed pass: start()
    loads every row, which is startup cost, not a sampling pass. Its own
    scheduler loop is stopped, so the scenario runs the one pass it times."""
    app = application_for(ctx, ctx.ids.trended_ids)
    sampler = trending.TrendSampler(ctx.session_factory, app, ctx.ids.big_device_id)
    sampler.start()
    ctx.on_close(sampler.stop)
    if sampler._scheduler_task is not None:
        sampler._scheduler_task.cancel()
    ctx.state["sampler"] = sampler


@scenario("trends.sampler_tick_500", area="trends", dataset="install", writes=True, setup=_started_sampler)
async def sampler_tick_500(ctx: Context):
    """One pass of the real TrendSampler, its writes included, with every one
    of its 500 interval objects due. Waiting for the due times to move on
    isn't enough: the pass moves them before it writes."""
    sampler = ctx.state["sampler"]
    now = asyncio.get_running_loop().time()
    for object_id in list(sampler._due):
        sampler._due[object_id] = now
    await sampler._sample_due()
    return len(sampler._due)


