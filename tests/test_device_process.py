"""device_process._serve_device -- the whole lifetime of one device's child
process, from initial build through graceful shutdown. Exercised directly as
a background asyncio task here (no real subprocess/multiprocessing); the real
subprocess boundary is covered in test_device_process_subprocess.py.
"""
from __future__ import annotations

import asyncio
import logging as _logging

import pytest

from bacpack import history_writer
from bacpack.device_process import STOP_CHECK_TICK_SECONDS, _serve_device
from bacpack.models import BacnetObject, Device, get_or_create_settings


@pytest.fixture(autouse=True)
def _reset_bacpack_logger():
    yield
    logger = _logging.getLogger("bacpack")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(_logging.NOTSET)


@pytest.mark.asyncio
async def test_serve_device_marks_itself_running_then_stopped(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        )
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running"
        assert device.runtime.process_pid is not None
        assert device.runtime.process_started_at is not None

        device.runtime.process_status = "stopping"
        session.commit()

    await asyncio.wait_for(task, timeout=2.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "stopped"
        assert device.runtime.process_pid is None


@pytest.mark.asyncio
async def test_serve_device_uses_the_devices_own_bind_address_override(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D", bind_address="127.0.0.1/8")
        session.add(device)
        # Deliberately leave the global bind_address unset -- proves the
        # per-device override alone is sufficient.
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


class _BuildStopped(Exception):
    pass


async def _network_port_arguments(session_factory, monkeypatch, *devices):
    """What _serve_device(1) hands build_application for the Network Port."""
    import bacpack.device_process as device_process

    with session_factory() as session:
        session.add_all(devices)
        session.commit()
    seen = {}

    def build(*args, **kwargs):
        seen.update({k: kwargs[k] for k in ("bind_address", "foreign_bbmd", "foreign_ttl", "bbmd_peers")})
        raise _BuildStopped()

    monkeypatch.setattr(device_process, "build_application", build)
    await asyncio.wait_for(_serve_device(session_factory, 1), timeout=2.0)
    return seen


@pytest.mark.asyncio
async def test_a_bbmds_network_port_gets_itself_and_its_linked_devices(session_factory, monkeypatch):
    from bacpack.models import DeviceLink

    bbmd = Device(id=1, name="D", bind_address="127.0.0.1/8:47810", network_role="bbmd", bbmd_peers=["10.0.0.9"])
    bbmd.bbmd_links = [DeviceLink(linked_device_id=2)]
    seen = await _network_port_arguments(
        session_factory, monkeypatch, bbmd, Device(id=2, name="E", bind_address="127.0.0.1/8:47811")
    )
    assert seen == {
        "bind_address": "127.0.0.1/8:47810",
        "foreign_bbmd": None,
        "foreign_ttl": 60,
        "bbmd_peers": ["127.0.0.1:47810", "127.0.0.1:47811", "10.0.0.9:47808"],
    }


@pytest.mark.asyncio
async def test_a_foreign_devices_network_port_gets_its_bbmd_and_ttl(session_factory, monkeypatch):
    device = Device(id=1, name="D", bind_address="127.0.0.1/8:47810", network_role="foreign",
                    foreign_bbmd="10.0.0.5:47808", foreign_ttl=120)
    seen = await _network_port_arguments(session_factory, monkeypatch, device)
    assert (seen["foreign_bbmd"], seen["foreign_ttl"], seen["bbmd_peers"]) == ("10.0.0.5:47808", 120, None)


@pytest.mark.asyncio
async def test_serve_device_marks_crashed_on_a_startup_failure(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        # No bind_address anywhere (device or global settings) -- build
        # should fail before ever reaching "running".
        session.commit()

    await _serve_device(session_factory, 1)  # returns immediately on failure

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error
        assert device.runtime.process_pid is None


@pytest.mark.asyncio
async def test_serve_device_returns_cleanly_for_an_unknown_device(session_factory):
    await _serve_device(session_factory, 999)  # must not raise


@pytest.mark.asyncio
async def test_serve_device_exits_when_its_device_row_is_deleted(session_factory, monkeypatch):
    """A running child whose Device row gets deleted out from under it (e.g.
    DELETE /api/devices/{id} reaching a device the parent has lost track of)
    used to loop forever in the stop-check loop -- _current_state returned
    "stopped" for a missing row, which never matches the loop's own
    "stopping" break condition. See device_process.py's _current_state
    docstring for the fix.
    """
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"
        session.delete(session.get(Device, 1))
        session.commit()

    # Must finish on its own within a reasonable timeout, not loop forever
    # holding its bind socket.
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_serve_device_marks_crashed_when_a_post_build_step_fails(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    def _boom(self):
        raise RuntimeError("trend sampler blew up")

    monkeypatch.setattr(device_process.TrendSampler, "start", _boom)

    # Should not raise -- build succeeded, but the post-build startup step
    # (trend_sampler.start()) failed, and that whole window must be covered
    # by the same crashed-handling as a build failure.
    await _serve_device(session_factory, 1)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error
        assert device.runtime.process_pid is None


@pytest.mark.asyncio
async def test_serve_device_installs_log_capture_tagged_with_its_own_device_id(
    session_factory, monkeypatch
):
    import logging

    import bacpack.device_process as device_process
    from bacpack.models import LogEntry

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    logging.getLogger("bacpack.device_process").warning("child process wiring test")

    with session_factory() as session:
        device = session.get(Device, 1)
        device.runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)
    history_writer.flush_current()

    with session_factory() as session:
        rows = session.query(LogEntry).filter_by(device_id=1).all()
    assert any(r.message == "child process wiring test" for r in rows)


def test_mark_running_resets_dcc_state_to_enable(session_factory):
    from bacpack.device_process import _mark_running

    with session_factory() as session:
        session.add(Device(id=1, name="D", dcc_state="disable"))
        session.commit()

    _mark_running(session_factory, 1)

    with session_factory() as session:
        assert session.get(Device, 1).dcc_state == "enable"


def test_mark_stopped_resets_dcc_state_to_enable(session_factory):
    """A device disabled via DCC then stopped
    kept reporting a stale "disable" dcc_state, even though nothing is
    running to be disabled -- _mark_running already resets this on start
    (see the test above), but _mark_stopped never did."""
    from bacpack.device_process import _mark_stopped

    with session_factory() as session:
        session.add(Device(id=1, name="D", dcc_state="disable"))
        session.commit()

    _mark_stopped(session_factory, 1)

    with session_factory() as session:
        assert session.get(Device, 1).dcc_state == "enable"


def test_mark_crashed_resets_dcc_state_to_enable(session_factory):
    """Same gap as _mark_stopped above, for the crashed path."""
    from bacpack.device_process import _mark_crashed

    with session_factory() as session:
        session.add(Device(id=1, name="D", dcc_state="disable"))
        session.commit()

    _mark_crashed(session_factory, 1, "boom")

    with session_factory() as session:
        assert session.get(Device, 1).dcc_state == "enable"


@pytest.mark.asyncio
async def test_serve_device_preserves_reinitializing_status_through_shutdown(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(
            BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        )
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running"
        device.runtime.process_status = "reinitializing"
        session.commit()

    await asyncio.wait_for(task, timeout=2.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        # NOT overwritten to "stopped" -- the parent's health-check loop
        # (engine.py) needs to see "reinitializing" survive to the moment
        # it detects the process has actually died.
        assert device.runtime.process_status == "reinitializing"


def test_mark_running_records_the_os_process_creation_time(session_factory):
    """Duplicate-device-process fix: process_started_at used to be
    datetime.now() at the end of startup, which a slow start (~3.6 s on a
    cold Linux box) pushed past engine._is_same_process's 2 s tolerance
    against the OS's own creation time -- so a parent restart judged every
    live child dead and a later start spawned a second copy on the same
    port. Recording the OS's creation time makes that comparison like-for-like.
    """
    import datetime as dt
    import os

    import psutil

    from bacpack.device_process import _mark_running

    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    _mark_running(session_factory, 1)

    with session_factory() as session:
        started_at = session.get(Device, 1).runtime.process_started_at
    started_at = started_at.replace(tzinfo=dt.timezone.utc)
    assert abs(started_at.timestamp() - psutil.Process(os.getpid()).create_time()) < 0.01


def test_mark_running_records_the_boot_id_and_since_boot_time(session_factory, monkeypatch):
    """The parent compares these when a clock step has moved
    the absolute creation time (see engine._is_same_process)."""
    import bacpack.process_identity as process_identity
    from bacpack.device_process import _mark_running

    monkeypatch.setattr(process_identity, "read_boot_id", lambda: "boot-A")
    monkeypatch.setattr(process_identity, "started_since_boot", lambda pid: 42.0)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()

    _mark_running(session_factory, 1)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_boot_id == "boot-A"
        assert device.runtime.process_started_since_boot == 42.0


async def _start_serving_device_1(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "running"
    return task


@pytest.mark.asyncio
async def test_serve_device_exits_when_another_process_takes_over_its_row(session_factory, monkeypatch):
    """A child whose row now names a different pid has been superseded --
    it must stop serving (not keep answering on the same port as the new
    copy) and must not overwrite the new child's "running" row on exit."""
    import os

    task = await _start_serving_device_1(session_factory, monkeypatch)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_pid = os.getpid() + 1
        session.commit()

    await asyncio.wait_for(task, timeout=2.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running"
        assert device.runtime.process_pid == os.getpid() + 1


@pytest.mark.asyncio
async def test_serve_device_exits_when_the_parent_marks_it_crashed(session_factory, monkeypatch):
    """engine.reattach_running_devices marks a row it can't match to a live
    process "crashed" with no pid. If that judgment was wrong, the child
    must still converge to it rather than live on untracked."""
    task = await _start_serving_device_1(session_factory, monkeypatch)
    with session_factory() as session:
        device = session.get(Device, 1)
        device.runtime.process_status = "crashed"
        device.runtime.process_error = "process was not found on parent restart"
        device.runtime.process_pid = None
        session.commit()

    await asyncio.wait_for(task, timeout=2.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error == "process was not found on parent restart"


# --- Time sync -----------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_device_seeds_the_saved_clock_before_building(session_factory, monkeypatch):
    import time

    from bacpypes3 import primitivedata

    import bacpack.device_process as device_process
    from bacpack import device_clock

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D", clock_offset_seconds=3600.0)
        session.add(device)
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    seen = {}
    real_build = device_process.build_application

    def _spy(*args, **kwargs):
        seen["offset"] = device_clock.offset()
        seen["installed"] = primitivedata.time is not time
        return real_build(*args, **kwargs)

    monkeypatch.setattr(device_process, "build_application", _spy)

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)

    assert seen == {"offset": 3600.0, "installed": True}
    assert primitivedata.time is time  # uninstalled on shutdown
    assert device_clock.offset() == 0.0
    with session_factory() as session:
        assert session.get(Device, 1).clock_offset_seconds == 3600.0  # kept, like an RTC


@pytest.mark.asyncio
async def test_serve_device_uninstalls_the_clock_after_a_startup_crash(session_factory, monkeypatch):
    import time

    from bacpypes3 import primitivedata

    import bacpack.device_process as device_process

    with session_factory() as session:
        session.add(Device(id=1, name="D", clock_offset_seconds=60.0))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    def _boom(self):
        raise RuntimeError("trend sampler blew up")

    monkeypatch.setattr(device_process.TrendSampler, "start", _boom)

    await _serve_device(session_factory, 1)

    assert primitivedata.time is time


@pytest.mark.asyncio
async def test_serve_device_keeps_the_clock_through_reinitialize(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        session.add(Device(id=1, name="D", clock_offset_seconds=-900.0))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "reinitializing"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "reinitializing"
        assert device.clock_offset_seconds == -900.0


# --- BACnet client role ----------------------------------------------------


def _running_device(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()


@pytest.mark.asyncio
async def test_serve_device_answers_client_requests(session_factory, monkeypatch):
    import bacpack.client_requests as client_requests
    import bacpack.device_process as device_process
    from bacpack.models import ClientRequest

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    monkeypatch.setattr(client_requests, "CLIENT_REQUEST_TICK_SECONDS", 0.01)

    async def fake_execute(self, kind, params, request_id=None):
        return {"kind": kind}

    monkeypatch.setattr(client_requests.ClientRequestPoller, "execute", fake_execute)
    _running_device(session_factory)
    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        row = ClientRequest(device_id=1, kind="who-is", params={})
        session.add(row)
        session.commit()
        request_id = row.id

    await asyncio.sleep(0.1)
    with session_factory() as session:
        row = session.get(ClientRequest, request_id)
        assert (row.status, row.result) == ("done", {"kind": "who-is"})
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_status, relinquishes", [("stopping", True), ("reinitializing", False)])
async def test_serve_device_relinquishes_only_on_a_clean_stop(
    session_factory, monkeypatch, exit_status, relinquishes
):
    import bacpack.bacnet_connectors as bacnet_connectors
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    calls = []

    async def fake_shutdown(self, timeout=bacnet_connectors.SHUTDOWN_RELINQUISH_SECONDS):
        calls.append("shutdown")
        self.stop()

    monkeypatch.setattr(bacnet_connectors.BacnetConnectorRunner, "shutdown", fake_shutdown)
    _running_device(session_factory)
    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = exit_status
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)

    assert calls == (["shutdown"] if relinquishes else [])


@pytest.mark.asyncio
async def test_serve_device_starts_and_stops_the_external_schedule_writer(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    calls = []
    monkeypatch.setattr(device_process.ExternalScheduleWriter, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(device_process.ExternalScheduleWriter, "stop", lambda self: calls.append("stop"))
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        settings = get_or_create_settings(session)
        settings.bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    assert calls == ["start"]
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)
    assert calls == ["start", "stop"]


# --- A UDP port that's already taken -----------------------------------------
# bacpypes3 retries a failed bind forever without saying so, so a device
# whose port was taken showed "running" and served nothing.

import socket


def _hold_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


def _add_device_on(session_factory, port):
    with session_factory() as session:
        session.add(Device(id=1, name="D", bind_address=f"127.0.0.1/8:{port}"))
        session.commit()


@pytest.mark.asyncio
async def test_serve_device_crashes_when_its_udp_port_is_taken(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "PORT_FREE_WAIT_SECONDS", 0.2)
    held, port = _hold_udp_port()
    try:
        _add_device_on(session_factory, port)
        await asyncio.wait_for(_serve_device(session_factory, 1), timeout=5.0)
    finally:
        held.close()

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error == (
            f"UDP port 127.0.0.1:{port} is already in use by another program or device"
        )


@pytest.mark.asyncio
async def test_serve_device_reports_a_bind_address_this_machine_does_not_have(
    session_factory, monkeypatch
):
    # 192.0.2.1 is TEST-NET-1, never assigned to a real interface: the bind
    # fails with "Cannot assign requested address" (errno 99 on Linux,
    # WSAEADDRNOTAVAIL on Windows), not "in use" -- e.g. a VM losing its address.
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "PORT_FREE_WAIT_SECONDS", 0.2)
    with session_factory() as session:
        session.add(Device(id=1, name="D", bind_address="192.0.2.1/24:47808"))
        session.commit()
    await asyncio.wait_for(_serve_device(session_factory, 1), timeout=5.0)

    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "crashed"
        assert device.runtime.process_error == (
            "Address 192.0.2.1 is not assigned to any network interface on this machine"
        )


@pytest.mark.asyncio
async def test_serve_device_starts_once_its_udp_port_is_freed(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    monkeypatch.setattr(device_process, "PORT_FREE_WAIT_SECONDS", 3.0)
    held, port = _hold_udp_port()
    _add_device_on(session_factory, port)
    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.3)
    held.close()  # e.g. the previous process of a restarting device exiting

    for _ in range(100):
        await asyncio.sleep(0.05)
        with session_factory() as session:
            if session.get(Device, 1).runtime.process_status == "running":
                break
    with session_factory() as session:
        device = session.get(Device, 1)
        assert device.runtime.process_status == "running", device.runtime.process_error
        device.runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_serve_device_runs_the_simulation_runner(session_factory, monkeypatch):
    import bacpack.device_process as device_process
    import bacpack.simulation_runner as simulation_runner

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    monkeypatch.setattr(simulation_runner, "RUNNER_TICK_SECONDS", 0.01)
    written = []
    real = simulation_runner.write_feed_value

    async def recording(obj, value, forward=True):
        written.append(value)
        return await real(obj, value, forward)

    monkeypatch.setattr(simulation_runner, "write_feed_value", recording)
    with session_factory() as session:
        device = Device(id=1, name="D", bind_address="127.0.0.1/8")
        session.add(device)
        session.add(
            BacnetObject(
                device=device,
                object_type="analog-value",
                instance_number=1,
                name="AV-1",
                simulation={"kind": "noise", "center": 5, "amplitude": 1, "interval_s": 1},
            )
        )
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    # Until the first write, not a fixed time: a slow CI runner can take
    # longer than a short sleep just to start the device.
    for _ in range(500):
        if written or task.done():
            break
        await asyncio.sleep(0.01)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)
    assert written and all(4 <= v <= 6 for v in written)


# --- DeviceWatch end to end -------------------------------------------------


async def _start_serving_device_1_with_av(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    for _ in range(250):
        with session_factory() as session:
            if session.get(Device, 1).runtime.process_status == "running":
                return task
        await asyncio.sleep(0.02)
    task.cancel()
    pytest.fail("the device never reported running")


async def _stop(session_factory, task):
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_serve_device_applies_a_write_queued_while_running(session_factory, monkeypatch):
    task = await _start_serving_device_1_with_av(session_factory, monkeypatch)
    try:
        with session_factory() as session:
            obj = session.query(BacnetObject).filter_by(device_id=1).one()
            obj.queue_manual_write(55.0)
            session.commit()
        # The device polls for queued writes on a tick of about a second.
        for _ in range(500):
            await asyncio.sleep(0.02)
            with session_factory() as session:
                if not session.query(BacnetObject).filter_by(device_id=1).one().runtime.pending_manual_queued:
                    break
        else:
            pytest.fail("the queued write was never applied")
    finally:
        await _stop(session_factory, task)


@pytest.mark.asyncio
async def test_serve_device_picks_up_a_simulator_added_while_running(session_factory, monkeypatch):
    """Trigger -> DeviceWatch -> SimulationRunner, end to end."""
    import bacpack.simulation_runner as simulation_runner

    monkeypatch.setattr(simulation_runner, "RUNNER_TICK_SECONDS", 0.01)
    seen = []
    real = simulation_runner.write_feed_value

    async def recording(obj, value, forward=True):
        seen.append(value)
        return await real(obj, value, forward)

    monkeypatch.setattr(simulation_runner, "write_feed_value", recording)
    task = await _start_serving_device_1_with_av(session_factory, monkeypatch)
    try:
        await asyncio.sleep(0.1)
        assert seen == []
        with session_factory() as session:
            obj = session.query(BacnetObject).filter_by(device_id=1).one()
            obj.simulation = {"kind": "square", "low": 10, "high": 20, "period_s": 100, "interval_s": 1}
            session.commit()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if seen:
                break
        assert seen, "the running device never picked up the new simulator"
    finally:
        await _stop(session_factory, task)


@pytest.mark.asyncio
async def test_serve_device_resets_the_live_apply_status_at_start(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.flush()
        device.runtime.apply_error = "old"
        device.runtime.restart_pending = True
        device.runtime.applied_seq = 99
        session.commit()

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        runtime = session.get(Device, 1).runtime
        assert runtime.apply_error is None
        assert runtime.restart_pending is False
        runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_an_object_added_while_running_is_served_without_a_restart(session_factory, monkeypatch):
    import bacpack.device_process as device_process
    from bacpack.live_config import ObjectReloader

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    made = []

    class _Recording(ObjectReloader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(device_process, "ObjectReloader", _Recording)
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()

    def runtime_state():
        with session_factory() as session:
            runtime = session.get(Device, 1).runtime
            return runtime.process_status, runtime.process_pid, runtime.applied_seq, runtime.change_seq

    async def until(condition, what):
        # The device writes applied_seq after it has served the object, so a
        # fixed sleep can read the row between the two on a loaded machine.
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"timed out waiting for {what}")

    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await until(lambda: runtime_state()[0] == "running", "the device to run")
    pid = runtime_state()[1]
    with session_factory() as session:
        session.add(BacnetObject(device_id=1, object_type="analog-value", instance_number=2, name="AV-2"))
        session.commit()
    await until(lambda: runtime_state()[2] == runtime_state()[3] != 0, "the device to apply the new object")
    [reloader] = made
    assert reloader.app.get_object_name("AV-2") is not None
    with session_factory() as session:
        runtime = session.get(Device, 1).runtime
        assert runtime.process_pid == pid
        assert runtime.applied_seq == runtime.change_seq
        runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


# --- freezing the startup objects out of GC ----------------------------------

def _one_object_device(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()


def _spy_freeze_and_reloader(session_factory, monkeypatch):
    """Record when the startup freeze is called (the device's status then, and
    whether the build had returned) and the refreezer the ObjectReloader is
    given; nothing is really frozen."""
    import bacpack.device_process as device_process

    seen = {"freezes": [], "refreezers": [], "built": False}
    build = device_process.build_application

    def build_application(*args, **kwargs):
        app = build(*args, **kwargs)
        seen["built"] = True
        return app

    def freeze_startup():
        with session_factory() as session:
            device = session.get(Device, 1)
            seen["freezes"].append((device.runtime.process_status if device.runtime else None, seen["built"]))

    class Recording(device_process.ObjectReloader):
        def __init__(self, *args, **kwargs):
            seen["refreezers"].append(kwargs.get("refreezer"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(device_process, "build_application", build_application)
    monkeypatch.setattr(device_process.gc_freeze, "freeze_startup", freeze_startup)
    monkeypatch.setattr(device_process, "ObjectReloader", Recording)
    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    return seen


async def _serve_and_stop(session_factory, **kwargs):
    task = asyncio.ensure_future(_serve_device(session_factory, 1, **kwargs))
    await asyncio.sleep(0.05)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_serve_device_freezes_after_the_build_before_running(session_factory, monkeypatch):
    from bacpack.gc_freeze import Refreezer

    _one_object_device(session_factory)
    seen = _spy_freeze_and_reloader(session_factory, monkeypatch)
    await _serve_and_stop(session_factory, freeze_gc=True)
    assert len(seen["freezes"]) == 1
    status, built = seen["freezes"][0]
    assert built and status != "running"
    assert len(seen["refreezers"]) == 1 and isinstance(seen["refreezers"][0], Refreezer)


@pytest.mark.asyncio
async def test_serve_device_does_not_freeze_by_default(session_factory, monkeypatch):
    _one_object_device(session_factory)
    seen = _spy_freeze_and_reloader(session_factory, monkeypatch)
    await _serve_and_stop(session_factory)
    assert seen["freezes"] == []
    assert seen["refreezers"] == [None]


@pytest.mark.asyncio
async def test_a_failed_build_does_not_freeze(session_factory, monkeypatch):
    import bacpack.device_process as device_process

    _one_object_device(session_factory)
    seen = _spy_freeze_and_reloader(session_factory, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("build failed")

    monkeypatch.setattr(device_process, "build_application", boom)
    await _serve_device(session_factory, 1, freeze_gc=True)
    assert seen["freezes"] == []
    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "crashed"


def test_run_device_process_asks_for_the_freeze(tmp_path, monkeypatch):
    import bacpack.device_process as device_process

    asked = []

    async def serve(session_factory, device_id, *, freeze_gc=False):
        asked.append((device_id, freeze_gc))

    monkeypatch.setattr(device_process, "prepare_device_process", lambda: None)
    monkeypatch.setattr(device_process, "_serve_device", serve)
    device_process.run_device_process(str(tmp_path / "x.db"), 1)
    assert asked == [(1, True)]


# --- The history writer is flushed before a process leaves ----------------


def _log_messages(session_factory):
    from bacpack.models import LogEntry

    with session_factory() as session:
        return [r.message for r in session.query(LogEntry).order_by(LogEntry.id)]


def _run_with_recorded_order(session_factory, monkeypatch):
    """Runs run_device_process in this thread against the test database,
    recording when the writer is closed and when the engine is disposed."""
    import bacpack.device_process as device_process
    from bacpack import history_writer
    from bacpack.db import make_engine

    order = []
    real_close = history_writer.close_current

    def recording_close(timeout=None):
        order.append("close writer")
        real_close(timeout)

    def recording_make_engine(db_path):
        engine = make_engine(db_path)
        real_dispose = engine.dispose

        def dispose(*args, **kwargs):
            order.append("dispose engine")
            real_dispose(*args, **kwargs)

        engine.dispose = dispose
        return engine

    monkeypatch.setattr(history_writer, "close_current", recording_close)
    monkeypatch.setattr(device_process, "make_engine", recording_make_engine)
    monkeypatch.setattr(device_process, "prepare_device_process", lambda: None)
    return order, session_factory.kw["bind"].url.database


def test_run_device_process_flushes_before_disposing_the_engine(session_factory, monkeypatch):
    from bacpack.device_process import run_device_process

    with session_factory() as session:
        session.add(Device(id=1, name="D"))  # no bind address anywhere: the start fails
        session.commit()
    order, db_path = _run_with_recorded_order(session_factory, monkeypatch)

    run_device_process(db_path, 1)

    assert order == ["close writer", "dispose engine"]
    assert any("device 1 failed to start" in m for m in _log_messages(session_factory))


def test_an_unhandled_error_leaves_its_log_line(session_factory, monkeypatch):
    import bacpack.device_process as device_process
    from bacpack.device_process import run_device_process

    async def dies(factory, device_id, **kwargs):
        device_process.install_log_capture(factory, device_id=device_id)
        raise RuntimeError("kaboom")

    monkeypatch.setattr(device_process, "_serve_device", dies)
    order, db_path = _run_with_recorded_order(session_factory, monkeypatch)

    with pytest.raises(RuntimeError, match="kaboom"):
        run_device_process(db_path, 1)

    assert order == ["close writer", "dispose engine"]
    assert any("exited via an unhandled error" in m for m in _log_messages(session_factory))


@pytest.mark.asyncio
async def test_serve_device_lands_queued_saves_before_the_stopped_mark(session_factory, monkeypatch):
    import time

    import bacpack.device_process as device_process
    from bacpack import config_save

    monkeypatch.setattr(device_process, "STOP_CHECK_TICK_SECONDS", 0.01)
    _running_device(session_factory)
    task = asyncio.ensure_future(_serve_device(session_factory, 1))
    await asyncio.sleep(0.05)

    def slow_late_save(session) -> None:
        time.sleep(0.3)
        session.get(Device, 1).runtime.process_status = "zz-late"

    late = asyncio.ensure_future(config_save.save(session_factory, slow_late_save, "a late save"))
    await asyncio.sleep(0)
    with session_factory() as session:
        session.get(Device, 1).runtime.process_status = "stopping"
        session.commit()
    await asyncio.wait_for(task, timeout=5.0)
    await late

    with session_factory() as session:
        assert session.get(Device, 1).runtime.process_status == "stopped"
