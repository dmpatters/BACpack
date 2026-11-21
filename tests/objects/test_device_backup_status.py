"""System_Status during a backup/restore and a writable, saved
Backup_Failure_Timeout.

A client that predates Backup_And_Restore_State sees a backup or restore
only through System_Status.
"""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.basetypes import DeviceStatus, PropertyIdentifier
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Unsigned

from bacpack.backup_restore import BACKUP_FAILURE_TIMEOUT
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application


async def _build(session_factory, **device_fields):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999, **device_fields)
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return app


def _saved_timeout(session_factory):
    with session_factory() as session:
        return session.get(Device, 599001).backup_failure_timeout


async def _status(app):
    return await app.device_object.read_property("systemStatus")


@pytest.mark.asyncio
async def test_system_status_follows_a_backup_session(session_factory):
    app = await _build(session_factory)
    assert await _status(app) == DeviceStatus.operational
    app.backup_restore.start_backup(b"x")
    assert await _status(app) == DeviceStatus.backupInProgress
    app.backup_restore.end_backup()
    assert await _status(app) == DeviceStatus.operational


@pytest.mark.asyncio
async def test_system_status_follows_a_restore_session(session_factory):
    app = await _build(session_factory)
    app.backup_restore.start_restore()
    assert await _status(app) == DeviceStatus.downloadInProgress
    app.backup_restore.abort_restore()
    assert await _status(app) == DeviceStatus.operational


@pytest.mark.asyncio
async def test_system_status_is_operational_again_after_a_timeout(session_factory):
    app = await _build(session_factory)
    app.backup_restore.timeout = 0.01
    app.backup_restore.start_backup(b"x")
    await asyncio.sleep(0.05)
    assert await _status(app) == DeviceStatus.operational


def test_the_default_timeout_is_60_seconds():
    assert BACKUP_FAILURE_TIMEOUT == 60


@pytest.mark.asyncio
async def test_a_new_device_reports_60_seconds(session_factory):
    app = await _build(session_factory)
    assert await app.device_object.read_property("backupFailureTimeout") == 60
    assert app.backup_restore.timeout == 60
    assert _saved_timeout(session_factory) == 60


@pytest.mark.asyncio
async def test_a_saved_timeout_is_used_at_start(session_factory):
    app = await _build(session_factory, backup_failure_timeout=120)
    assert await app.device_object.read_property("backupFailureTimeout") == 120
    assert app.backup_restore.timeout == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_a_timeout_write_applies_and_saves(session_factory, as_identifier):
    # Off the wire the property is a PropertyIdentifier, not a name.
    app = await _build(session_factory)
    target = PropertyIdentifier("backupFailureTimeout") if as_identifier else "backupFailureTimeout"
    await app.device_object.write_property(target, Unsigned(15))
    assert await app.device_object.read_property("backupFailureTimeout") == 15
    assert app.backup_restore.timeout == 15
    assert _saved_timeout(session_factory) == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 65536])
async def test_a_timeout_outside_1_to_65535_is_refused(session_factory, value):
    # 0 would end every session at once; the standard's type is Unsigned16.
    app = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("backupFailureTimeout", Unsigned(value))
    assert excinfo.value.errorCode == "value-out-of-range"
    assert await app.device_object.read_property("backupFailureTimeout") == 60
    assert app.backup_restore.timeout == 60
    assert _saved_timeout(session_factory) == 60


@pytest.mark.asyncio
async def test_a_written_timeout_ends_a_stalled_session(session_factory):
    app = await _build(session_factory)
    await app.device_object.write_property("backupFailureTimeout", Unsigned(1))
    app.backup_restore.start_backup(b"x")
    await asyncio.sleep(1.2)
    assert app.backup_restore.state == "idle"
    assert app.backup_restore.timed_out == "backup"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_system_status_stays_refused(session_factory, as_identifier):
    app = await _build(session_factory)
    target = PropertyIdentifier("systemStatus") if as_identifier else "systemStatus"
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property(target, DeviceStatus.operational)
    assert excinfo.value.errorCode == "writeAccessDenied"
