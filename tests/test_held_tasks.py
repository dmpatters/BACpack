import asyncio
import gc
import weakref

from bacpack import bacpypes3_compat, held_tasks


async def _wait_forever():
    # A future only this coroutine's frame refers to: once suspended, the
    # task is reachable from nothing but the loop's weak references.
    await asyncio.get_running_loop().create_future()


async def test_a_started_task_survives_a_collection():
    task_ref = weakref.ref(held_tasks.start(_wait_forever()))
    await asyncio.sleep(0)
    gc.collect()

    task = task_ref()
    assert task is not None and not task.done()
    task.cancel()


async def test_a_finished_task_is_let_go():
    task = held_tasks.start(asyncio.sleep(0))
    await task
    await asyncio.sleep(0)

    assert task not in held_tasks._tasks


async def test_a_task_started_during_a_deferred_build_is_held_after_replay():
    work = bacpypes3_compat.DeferredLoopWork()
    handle = work.build(held_tasks.start, _wait_forever())
    work.replay()
    task_ref = weakref.ref(handle.real)
    del handle  # a caller drops it, as a bare start() does
    await asyncio.sleep(0)
    gc.collect()

    task = task_ref()
    assert task is not None and not task.done()
    task.cancel()
