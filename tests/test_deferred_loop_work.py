"""bacpypes3_compat.DeferredLoopWork: the work bacpypes3 queues on the loop
while it constructs an object waits for replay()."""
from __future__ import annotations

import asyncio
import gc
import inspect
import warnings

import pytest

from bacpack.bacpypes3_compat import DeferredLoopWork
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import OBJECT_CLASSES, build_bacnet_object
from bacpack.objects.trend_log import build_trend_log_object
from bacpack.objects.trend_log_reporting import build_reporting_trend_log_object

_ROWS = {
    "multi-state-input": dict(state_text=["Off", "On"]),
    "multi-state-output": dict(state_text=["Off", "On"], relinquish_default="1"),
    "multi-state-value": dict(state_text=["Off", "On"], relinquish_default="1"),
    "schedule": dict(
        schedule_value_type="analog", schedule_default="10.0", schedule_priority=8,
        weekly_schedule=[[{"time": "00:00", "value": "10.0"}] for _ in range(7)],
    ),
    "calendar": dict(date_list=[{"kind": "date", "month": 12, "day": 25}]),
}


def _row(object_type, **fields):
    return BacnetObject(object_type=object_type, instance_number=1, name=f"{object_type}-1",
                        **{**_ROWS.get(object_type, {}), **fields})


def _queued(work):
    return [kind for kind, *_ in work._calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type", sorted(OBJECT_CLASSES))
async def test_building_any_object_type_queues_work_that_waits_for_replay(object_type):
    work = DeferredLoopWork()
    obj = work.build(build_bacnet_object, _row(object_type))
    assert asyncio.get_running_loop() is not None and "task" in _queued(work)
    post_inits = [fn for kind, fn, *_ in work._calls if kind == "task"]
    for _ in range(5):
        await asyncio.sleep(0)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CREATED for coro in post_inits)
    work.replay()
    for _ in range(5):
        await asyncio.sleep(0)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in post_inits)
    assert obj.objectName == f"{object_type}-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("reporting", [False, True], ids=["plain", "reporting"])
async def test_building_a_trend_log_queues_work_too(session_factory, reporting):
    with session_factory() as session:
        device = Device(id=1, name="D")
        av = _row("analog-value", device=device, trended=True, trend_reporting_enabled=reporting)
        session.add(av)
        session.commit()
        build = build_reporting_trend_log_object if reporting else build_trend_log_object
        work = DeferredLoopWork()
        work.build(build, device, av, session_factory)
    assert "task" in _queued(work)
    work.discard()


@pytest.mark.asyncio
async def test_replay_runs_the_work_in_the_order_it_was_queued():
    ran = []

    async def task(label):
        ran.append(label)

    def queue_things():
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(task("task-1"))
        loop.call_soon(ran.append, "soon-1")
        asyncio.get_event_loop().call_soon(ran.append, "soon-2")
        asyncio.ensure_future(task("task-2"))
        loop.call_later(0.01, ran.append, "later")

    work = DeferredLoopWork()
    work.build(queue_things)
    await asyncio.sleep(0.03)
    assert ran == []
    work.replay()
    await asyncio.sleep(0.03)
    assert ran == ["task-1", "soon-1", "soon-2", "task-2", "later"]


@pytest.mark.asyncio
async def test_a_handle_cancelled_before_replay_is_skipped_and_after_cancels_the_real_one():
    ran = []
    handles = {}

    def queue_things():
        loop = asyncio.get_running_loop()
        handles["early"] = loop.call_soon(ran.append, "early")
        handles["late"] = loop.call_later(0.01, ran.append, "late")

    work = DeferredLoopWork()
    work.build(queue_things)
    handles["early"].cancel()
    assert handles["early"].cancelled() and not handles["late"].cancelled()
    work.replay()
    assert handles["late"].when() is not None
    handles["late"].cancel()
    assert handles["late"].cancelled()
    await asyncio.sleep(0.03)
    assert ran == []


@pytest.mark.asyncio
async def test_discard_closes_the_queued_coroutines():
    async def never():
        pass  # pragma: no cover

    work = DeferredLoopWork()
    work.build(lambda: asyncio.ensure_future(never()))
    coro = work._calls[0][1]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        work.discard()
        gc.collect()
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    assert not [w for w in caught if "never awaited" in str(w.message)]
    assert work._calls == []


@pytest.mark.asyncio
async def test_a_loop_method_the_recorder_lacks_fails_the_build_and_restores_the_loop():
    real = asyncio.get_running_loop()
    work = DeferredLoopWork()
    with pytest.raises(NotImplementedError):
        work.build(lambda: asyncio.get_running_loop().run_in_executor(None, print))
    assert asyncio.get_running_loop() is real
    with pytest.raises(ValueError):
        work.build(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert asyncio.get_running_loop() is real
