"""A BACnet write's config save, off the device's event loop."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from bacpack import config_save
from bacpack.models import Device
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked
from tests.session_fakes import CommitFailingSessionFactory


def _device(session_factory, description="before"):
    with session_factory() as session:
        session.add(Device(id=599001, name="Test Device", description=description))
        session.commit()


def _description(session_factory):
    with session_factory() as session:
        return session.get(Device, 599001).description


@pytest.mark.asyncio
async def test_a_save_waits_for_a_held_lock_off_the_loop(session_factory):
    _device(session_factory)

    run = await run_while_locked(
        session_factory,
        lambda: config_save.save_columns(session_factory, Device, 599001, {"description": "after"}, "description"),
    )

    assert_waited_off_the_loop(run)
    assert _description(session_factory) == "after"


@pytest.mark.asyncio
async def test_a_failed_save_is_logged_not_raised(session_factory, caplog):
    _device(session_factory)

    with caplog.at_level(logging.ERROR, logger="bacpack"):
        await config_save.save_columns(
            CommitFailingSessionFactory(session_factory), Device, 599001, {"description": "after"}, "the description"
        )

    assert _description(session_factory) == "before"
    assert "could not save the description" in caplog.text


@pytest.mark.asyncio
async def test_saves_commit_in_the_order_they_were_made(session_factory):
    # Without the ordering, the slow first save would commit last and the
    # row would keep the older value while the live object has the newer.
    _device(session_factory)

    def slow_first(session):
        time.sleep(0.2)
        session.get(Device, 599001).description = "first"

    first = asyncio.ensure_future(config_save.save(session_factory, slow_first, "first"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(
        config_save.save_columns(session_factory, Device, 599001, {"description": "second"}, "second")
    )
    await asyncio.gather(first, second)

    assert _description(session_factory) == "second"


@pytest.mark.asyncio
async def test_a_missing_row_saves_nothing(session_factory):
    await config_save.save_columns(session_factory, Device, 599001, {"description": "after"}, "description")

    with session_factory() as session:
        assert session.get(Device, 599001) is None


@pytest.mark.asyncio
async def test_run_in_order_returns_and_raises():
    assert await config_save.run_in_order(lambda a: a * 2, 21) == 42

    def fails():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError, match="x"):
        await config_save.run_in_order(fails)


@pytest.mark.asyncio
async def test_run_in_order_keeps_order():
    done: list[int] = []

    def save(index: int, delay: float) -> None:
        time.sleep(delay)
        done.append(index)

    await asyncio.gather(
        config_save.run_in_order(save, 0, 0.1),
        config_save.run_in_order(save, 1, 0.05),
        config_save.run_in_order(save, 2, 0.0),
    )

    assert done == [0, 1, 2]
