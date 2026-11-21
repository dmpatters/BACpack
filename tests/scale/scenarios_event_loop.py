"""Area 5, the device event loop: a ReadProperty probe answered while a
heavy operation runs on the same device. The
other areas' device-loop checks count toward this one too."""
from __future__ import annotations

import asyncio
import time

from bacpypes3.apdu import ReadPropertyRequest

from .bacnet_requests import read_config_file, reinitialize, send
from .harness import LoopLagMonitor
from .scenarios import Context, scenario
from .scenarios_devices import big_app, edit_rows
from .scenarios_trends import pull_log
from bacpack.live_config import ObjectReloader

PROBES = 20
PROBE_INTERVAL = 0.1
OPERATION_STARTS_AT = 0.3
LIVE_EDIT_OBJECTS = 100


async def probe_during(ctx: Context, operation) -> dict:
    """Runs `operation` (a coroutine function) on the device's loop while a
    client sends PROBES ReadProperty requests, one every PROBE_INTERVAL
    seconds. A probe's latency is how long after its intended send time its
    answer was ready, so a blocked loop shows up whole."""
    app = ctx.state["app"]
    latencies: list[float] = []
    loop = asyncio.get_running_loop()

    async def probes():
        start = time.perf_counter()
        for n in range(PROBES):
            intended = start + n * PROBE_INTERVAL
            await asyncio.sleep(max(0.0, intended - time.perf_counter()))
            resp = await send(
                app, ReadPropertyRequest(objectIdentifier=("analog-value", 1), propertyIdentifier="presentValue")
            )
            assert resp is not None
            latencies.append(time.perf_counter() - intended)

    async def heavy():
        await asyncio.sleep(OPERATION_STARTS_AT)
        return await operation()

    async with LoopLagMonitor() as lag:
        _, result = await asyncio.gather(probes(), heavy())
    return {"max_latency": max(latencies), "max_gap": lag.max_gap, "result": result, "loop": loop}


@scenario("event_loop.readproperty_during_readrange", area="event_loop", dataset="install", setup=big_app)
async def readproperty_during_readrange(ctx: Context):
    async def pull():
        return await pull_log(ctx.state["app"], ctx.ids.full_log_ids[0], "position")

    return await probe_during(ctx, pull)


def _live_edit_setup(ctx: Context) -> None:
    big_app(ctx)
    ctx.state["reloader"] = ObjectReloader(
        ctx.session_factory, ctx.state["app"], ctx.state["device"], ctx.state["objects"]
    )
    first = ctx.ids.big_object_ids[len(ctx.ids.big_object_ids) // 2]
    ctx.state["change_seq"] = edit_rows(ctx, range(first, first + LIVE_EDIT_OBJECTS))


@scenario(
    "event_loop.readproperty_during_live_edit",
    area="event_loop",
    dataset="install",
    writes=True,
    setup=_live_edit_setup,
)
async def readproperty_during_live_edit(ctx: Context):
    async def apply():
        return await ctx.state["reloader"].apply(ctx.state["change_seq"])

    return await probe_during(ctx, apply)


@scenario("event_loop.readproperty_during_backup", area="event_loop", dataset="install", setup=big_app)
async def readproperty_during_backup(ctx: Context):
    async def backup():
        app = ctx.state["app"]
        await reinitialize(app, "startBackup")
        data = await read_config_file(app)
        await reinitialize(app, "endBackup")
        return len(data)

    return await probe_during(ctx, backup)
