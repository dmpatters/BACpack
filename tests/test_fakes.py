"""Direct tests for the test-only process fakes in tests/fakes.py --
verifies both behave as EngineManager (Tasks 9-10) and the web_client
fixture will rely on, before either consumes them.
"""
from __future__ import annotations

import asyncio

import pytest

from bacpack.models import Device, get_or_create_settings
from tests.fakes import FakeDeviceProcess, FakeProcess


def test_fake_process_starts_alive_and_can_be_terminated():
    process = FakeProcess("db.sqlite", 1)
    process.start()
    assert process.is_alive()
    process.terminate()
    assert not process.is_alive()


@pytest.mark.asyncio
async def test_fake_device_process_really_runs_serve_device(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    process = FakeDeviceProcess(session_factory, 1)
    process.start()
    await asyncio.sleep(0.05)
    assert process.is_alive()

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()

    await asyncio.sleep(0.1)
    assert not process.is_alive()
