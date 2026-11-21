"""Fire-and-forget tasks, held until they finish.

The event loop keeps only a weak reference to a task, so a task nothing
else refers to can be garbage-collected mid-run and its work silently lost.
"""

import asyncio

_tasks: set[asyncio.Future] = set()


def hold(task: asyncio.Future) -> asyncio.Future:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def start(coro):
    """Run `coro` as a held task. Inside bacpypes3_compat.DeferredLoopWork's
    build this returns the recorder's handle, and replay holds the task."""
    task = asyncio.ensure_future(coro)
    if isinstance(task, asyncio.Future):
        hold(task)
    return task
