"""The child-process entry point for one device, coordinated with the parent
only through SQLite.

run_device_process takes plain, picklable arguments only (a path string and
an int): 'spawn' re-imports this module fresh in the child and can't carry a
live engine/sessionmaker across. Everything else is built here from scratch.
It does NOT call db.init_db(): the parent makes the schema current before
spawning, and concurrent migrations from several children would race.

_serve_device is the async lifetime, separate so tests can run it as an
in-process task (tests/test_device_process.py).
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import socket
from typing import Optional, Tuple

from bacpypes3.pdu import IPv4Address

from . import clock_sync, config_save, device_clock, gc_freeze, history_writer, lifecycle
from .bacnet_client import BacnetClient
from .bacnet_connectors import BacnetConnectorRunner
from .client_requests import ClientRequestPoller
from .schedule_external import ExternalScheduleWriter
from .object_builder import build_application
from .db import make_engine, make_session_factory
from .device_network import resolve_network
from .device_watch import DeviceWatch
from .live_config import ObjectReloader
from .log_capture import install_log_capture
from .models import Device, get_or_create_settings
from .pending_writes import PendingWritePoller
from .process_identity import record_identity
from .process_title import DEVICE_SHORT_NAME, device_title, set_process_title
from .simulation_runner import SimulationRunner
from .traffic import TrafficLog
from .trending import TrendSampler

logger = logging.getLogger(__name__)

# How often DeviceWatch reads this device's runtime row, where a stop request,
# queued writes and config changes show up.
STOP_CHECK_TICK_SECONDS = 1.0

# A restarted or reinitialized device may start before its previous process
# has let go of the UDP port.
PORT_FREE_WAIT_SECONDS = 5.0
PORT_FREE_RETRY_SECONDS = 0.25


class DeviceStartupError(Exception):
    """The device can't be built (no bind address, unknown device id, ...);
    _serve_device records it as "crashed" with process_error."""


def _load_device_config(session_factory, device_id: int):
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise DeviceStartupError(f"no device with id {device_id}")
        objects = list(device.objects)

        settings = get_or_create_settings(session)
        bind_address = device.bind_address or settings.bind_address
        if not bind_address:
            raise DeviceStartupError(
                "no network bind address configured for this device or globally"
            )
        network = resolve_network(session, device)

        session.expunge_all()

    return device, objects, bind_address, network


def _mark_running(session_factory, device_id: int) -> None:
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is not None:
            record_identity(device.runtime, os.getpid())
            device.runtime.process_status = "running"
            device.runtime.process_error = None
            device.runtime.applied_seq = None
            device.runtime.apply_error = None
            device.runtime.restart_pending = False
            device.dcc_state = "enable"
            session.commit()


def _mark_crashed(session_factory, device_id: int, error: str) -> None:
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is not None:
            device.runtime.process_status = "crashed"
            device.runtime.process_error = error
            device.runtime.process_pid = None
            # A crashed device has nothing left to be DCC-disabled.
            device.dcc_state = "enable"
            session.commit()


def _mark_stopped(session_factory, device_id: int) -> None:
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is not None:
            device.runtime.process_status = "stopped"
            device.runtime.process_pid = None
            device.runtime.process_error = None
            device.dcc_state = "enable"
            session.commit()


def _current_state(session_factory, device_id: int) -> Optional[Tuple[str, Optional[int]]]:
    """(process_status, process_pid) of this device's row, or None if the
    Device row is gone (deleted while the parent had lost track of it)."""
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is None:
            return None
        return device.runtime.process_status, device.runtime.process_pid


async def _wait_for_udp_port(bind_address: str) -> None:
    """Raises DeviceStartupError if the device's UDP address and port stay
    taken for PORT_FREE_WAIT_SECONDS. bacpypes3 binds in a background task
    and retries a failed bind forever silently, so a taken port (YABE on the
    same PC, or on Linux another device sharing it via SO_REUSEPORT) would
    show "running" and serve nothing. A plain socket without SO_REUSEADDR/
    SO_REUSEPORT fails to bind on Windows and Linux while anything holds the
    port. Unicast only: on Linux devices share the broadcast address on
    purpose."""
    try:
        host, port = IPv4Address(bind_address).addrTuple
    except Exception:
        return  # not an address bacpypes3 can bind either; build_application reports it
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PORT_FREE_WAIT_SECONDS
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((host, port))
            return
        except OSError as exc:
            if loop.time() >= deadline:
                raise DeviceStartupError(_bind_error_message(exc, host, port)) from None
        finally:
            probe.close()
        await asyncio.sleep(PORT_FREE_RETRY_SECONDS)


_PORT_IN_USE = "UDP port {host}:{port} is already in use by another program or device"
_PORT_IN_USE_PARTS = _PORT_IN_USE.split("{host}:{port}")


def is_port_in_use_error(message: Optional[str]) -> bool:
    """Whether a device's process_error is _bind_error_message's "in use"
    case, the one a free port fixes."""
    if not message:
        return False
    return message.startswith(_PORT_IN_USE_PARTS[0]) and message.endswith(_PORT_IN_USE_PARTS[1])


def _bind_error_message(exc: OSError, host: str, port: int) -> str:
    """Names what stopped the bind: "in use" is wrong for an address the
    machine no longer has. errno's constants are the WSA values on Windows."""
    if exc.errno == errno.EADDRINUSE:
        return _PORT_IN_USE.format(host=host, port=port)
    if exc.errno == errno.EADDRNOTAVAIL:
        return f"Address {host} is not assigned to any network interface on this machine"
    return f"Cannot bind UDP port {host}:{port}: {exc.strerror or exc}"


async def _serve_device(session_factory, device_id: int, *, freeze_gc: bool = False) -> None:
    """One device's lifetime: build, serve until the row says "stopping",
    shut down. A startup error is recorded as "crashed". freeze_gc is for
    the real child only: freezing the heap of a test process would hide it
    from every later collection."""
    # In a real child this is the process's only writer. A test running
    # _serve_device inside the web process replaces the web app's writer here.
    install_log_capture(session_factory, device_id=device_id)
    app = None
    trend_sampler = None
    pending_writes = None
    connector_runner = None
    client_requests = None
    schedule_writer = None
    simulation_runner = None
    clean_stop = False
    try:
        device, objects, bind_address, network = _load_device_config(session_factory, device_id)
        # The saved clock must be in place before
        # build_application, so the first Schedule evaluation already sees it.
        device_clock.install()
        device_clock.set_offset(device.clock_offset_seconds or 0.0)
        traffic_log = TrafficLog(writer=history_writer.current(), device_id=device_id)
        if bind_address:
            await _wait_for_udp_port(bind_address)
        app = build_application(
            device,
            objects,
            bind_address=bind_address,
            foreign_bbmd=network.foreign_bbmd,
            foreign_ttl=network.foreign_ttl,
            bbmd_peers=list(network.bdt) if network.bdt else None,
            traffic_log=traffic_log,
            trend_session_factory=session_factory,
        )
        if freeze_gc:
            # Keeps the startup objects out of every later full collection.
            gc_freeze.freeze_startup()
        # Object edits apply live.
        trend_sampler = TrendSampler(session_factory, app, device_id)
        trend_sampler.start()
        reloader = ObjectReloader(
            session_factory, app, device, objects, trend_sampler=trend_sampler, network=network,
            refreezer=gc_freeze.Refreezer() if freeze_gc else None,
        )
        pending_writes = PendingWritePoller(session_factory, app, device_id)
        # One client, so the connectors, the web UI's client tool and external
        # schedule writes share device bindings.
        client = BacnetClient(app)
        connector_runner = BacnetConnectorRunner(session_factory, app, device_id, client=client)
        connector_runner.start()
        client_requests = ClientRequestPoller(session_factory, device_id, client=client, app=app)
        client_requests.start()
        schedule_writer = ExternalScheduleWriter(session_factory, app, device_id, client=client)
        schedule_writer.start()
        simulation_runner = SimulationRunner(session_factory, app, device_id)
        simulation_runner.start()
        _mark_running(session_factory, device_id)
    except Exception as exc:
        logger.exception("device %d failed to start", device_id)
        if trend_sampler is not None:
            trend_sampler.stop()
        if connector_runner is not None:
            connector_runner.stop()
        if client_requests is not None:
            client_requests.stop()
        if schedule_writer is not None:
            schedule_writer.stop()
        if simulation_runner is not None:
            simulation_runner.stop()
        if app is not None:
            app.close()
        device_clock.uninstall()
        _mark_crashed(session_factory, device_id, str(exc))
        return

    try:
        watch = DeviceWatch(
            session_factory,
            device_id,
            pending_writes=pending_writes,
            runners=(connector_runner, simulation_runner),
            reloader=reloader,
            tick_seconds=STOP_CHECK_TICK_SECONDS,
            reloads_held=lambda: app.applying_restore,
        )
        clean_stop = await watch.run()
    finally:
        trend_sampler.stop()
        client_requests.stop()
        schedule_writer.stop()
        simulation_runner.stop()
        if clean_stop:
            await connector_runner.shutdown()
        else:
            connector_runner.stop()
        app.close()
        device_clock.uninstall()
        # Saves queued by the handlers and runners above must land before the
        # stopped mark, not after it.
        await clock_sync.settle()
        await config_save.run_in_order(lambda: None)
        # Don't overwrite "reinitializing" (written by the ReinitializeDevice
        # handler): the parent's health check reads it only after seeing this
        # process dead, then respawns. Nor a row with another pid: it may
        # already describe a replacement child.
        state = _current_state(session_factory, device_id)
        if state is not None and state[0] != "reinitializing" and state[1] == os.getpid():
            _mark_stopped(session_factory, device_id)


def prepare_device_process() -> None:
    """A Ctrl+C meant for the parent (shared console or process group) must
    not kill a device: the parent stops devices in order at shutdown. Under
    a service, inherited stdio is dropped so a surviving device never holds
    the wrapper's log pipes (a quick WinSW restart crashes on them)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if lifecycle.service_mode():
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.close(devnull)


def run_device_process(db_path: str, device_id: int) -> None:
    """multiprocessing.Process target (plain arguments: see the module docstring)."""
    prepare_device_process()
    set_process_title(device_title(device_id), DEVICE_SHORT_NAME)
    engine = make_engine(db_path)
    session_factory = make_session_factory(engine)
    try:
        asyncio.run(_serve_device(session_factory, device_id, freeze_gc=True))
    except Exception:
        # Anything escaping _serve_device leaves the DB state unknown, so the
        # row is deliberately left alone: the parent's health check marks a
        # dead process still "running" as crashed.
        logger.exception("device %d process exited via an unhandled error", device_id)
        raise
    finally:
        # Before the engine goes: the writer commits through it.
        history_writer.close_current()
        engine.dispose()
