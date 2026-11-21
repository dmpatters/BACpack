"""Every path that saves or builds an object goes through object_config. Each
test replaces check_object/check_device with ones that always refuse, drives
one path, and expects the refusal: a path that skips the checks fails here."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.basetypes import CalendarEntry, Destination, DeviceAddress, Recipient
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import CharacterString, Date, Real, Time

from bacpack import object_config
from bacpack.models import BacnetObject, Device
from tests.web._helpers import _create_device, _create_object

OBJECT_REFUSAL = "WIRED-OBJECT"
DEVICE_REFUSAL = "WIRED-DEVICE"


@pytest.fixture
def wired(monkeypatch):
    def refuse_object(row):
        raise ValueError(OBJECT_REFUSAL)

    monkeypatch.setattr(object_config, "check_object", refuse_object)
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)


@pytest.fixture
def device_only(monkeypatch):
    """Only check_device refuses, for paths whose object check would stop first."""
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)


def _av():
    return BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")


async def test_build_bacnet_object_checks_the_object(wired):
    from bacpack.object_builder import build_bacnet_object

    with pytest.raises(ValueError, match=OBJECT_REFUSAL):
        build_bacnet_object(_av())


async def test_build_application_checks_the_device(device_only):
    from bacpack.object_builder import build_application

    with pytest.raises(ValueError, match=DEVICE_REFUSAL):
        build_application(Device(id=599001, name="Dev", vendor_identifier=999), [_av()])


def _config(objects):
    return {"devices": [{"id": 599001, "name": "Dev", "objects": objects}]}


_AV_ROW = {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent"}


def test_json_import_checks_each_object(wired):
    from bacpack.config_io import parse_config
    from bacpack.config_schemas import ConfigImportError

    with pytest.raises(ConfigImportError, match=OBJECT_REFUSAL):
        parse_config(_config([_AV_ROW]))


def test_json_import_checks_the_device(device_only):
    from bacpack.config_io import parse_config
    from bacpack.config_schemas import ConfigImportError

    with pytest.raises(ConfigImportError, match=DEVICE_REFUSAL):
        parse_config(_config([_AV_ROW]))


def test_csv_import_checks_each_object(wired):
    from bacpack.csv_io import CsvImportError, parse_objects_csv, validate_objects_csv

    parsed = parse_objects_csv("object_type,instance_number,name,units\nanalog-value,1,AV-1,percent\n")
    with pytest.raises(CsvImportError, match=OBJECT_REFUSAL):
        validate_objects_csv(parsed, {})


def _refuse_objects(monkeypatch):
    def refuse_object(row):
        raise ValueError(OBJECT_REFUSAL)

    monkeypatch.setattr(object_config, "check_object", refuse_object)


_AV_BODY = {"object_type": "analog-value", "instance_number": 1, "name": "AV-1", "units": "percent"}


def test_api_create_checks_the_object(web_client, monkeypatch):
    _create_device(web_client)
    _refuse_objects(monkeypatch)
    r = web_client.post("/api/devices/599001/objects", json=_AV_BODY)
    assert r.status_code == 400 and OBJECT_REFUSAL in r.text


def test_api_create_checks_the_device(web_client, device_only):
    _create_device(web_client)
    r = web_client.post("/api/devices/599001/objects", json=_AV_BODY)
    assert r.status_code == 409 and DEVICE_REFUSAL in r.text


def test_api_update_checks_the_object(web_client, monkeypatch):
    _create_device(web_client)
    obj = _create_object(web_client)
    _refuse_objects(monkeypatch)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"description": "x"})
    assert r.status_code == 400 and OBJECT_REFUSAL in r.text


def test_api_update_checks_the_device(web_client, monkeypatch):
    _create_device(web_client)
    obj = _create_object(web_client)
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)
    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"description": "x"})
    assert r.status_code == 409 and DEVICE_REFUSAL in r.text


def test_batch_edit_checks_each_object(web_client, monkeypatch):
    _create_device(web_client)
    obj = _create_object(web_client)
    _refuse_objects(monkeypatch)
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": [obj["id"]], "patch": {"description": "x"}}
    )
    assert r.status_code == 400 and OBJECT_REFUSAL in r.text


def test_batch_edit_checks_the_device(web_client, monkeypatch):
    _create_device(web_client)
    obj = _create_object(web_client)
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": [obj["id"]], "patch": {"description": "x"}}
    )
    assert r.status_code == 409 and DEVICE_REFUSAL in r.text


def test_device_rename_checks_the_device(web_client, monkeypatch):
    _create_device(web_client)
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)
    r = web_client.put("/api/devices/599001", json={"name": "Renamed"})
    assert r.status_code == 409 and DEVICE_REFUSAL in r.text


@pytest.mark.parametrize("mode", ["merge", "sync"])
def test_csv_route_checks_the_device(web_client, monkeypatch, mode):
    _create_device(web_client)
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)
    r = web_client.post(
        f"/api/devices/599001/objects/import?mode={mode}",
        files={"file": ("objects.csv", "object_type,instance_number,name,units\nanalog-value,1,AV-1,percent\n", "text/csv")},
    )
    assert r.status_code == 409 and DEVICE_REFUSAL in r.text


# --- BACnet writes that save config ----------------------------------------


_RECIPIENT_ENTRY = {
    "recipient": {"kind": "address", "address": "192.168.1.200"},
    "valid_days": [True] * 7,
    "from_time": "00:00:00.00",
    "to_time": "23:59:59.99",
    "transitions": [True, True, True],
    "confirmed": True,
    "process_identifier": 1,
}


async def _running(session_factory, *rows):
    """Save `rows` on device 599001 and build the device the way a device
    process does, with config write-back wired."""
    from bacpack.object_builder import build_application

    with session_factory() as session:
        device = Device(id=599001, name="Dev", vendor_identifier=999)
        session.add(device)
        for row in rows:
            row.device = device
            session.add(row)
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return app


def _msv():
    return BacnetObject(object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["A", "B"])


def _nc():
    return BacnetObject(
        object_type="notification-class", instance_number=1, name="NC-1", notification_recipient_list=[_RECIPIENT_ENTRY]
    )


def _destination():
    return Destination(
        validDays=[1] * 7,
        fromTime=Time("00:00:00.00"),
        toTime=Time("23:59:59.99"),
        recipient=Recipient(address=DeviceAddress("192.168.1.221")),
        processIdentifier=2,
        issueConfirmedNotifications=True,
        transitions=[1, 1, 1],
    )


async def test_state_text_write_checks_the_saved_row(session_factory, monkeypatch):
    app = await _running(session_factory, _msv())
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("MSV-1").write_property("stateText", ["A", "B", "C"])


async def test_relinquish_default_write_checks_the_saved_row(session_factory, monkeypatch):
    app = await _running(session_factory, _av())
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("AV-1").write_property("relinquishDefault", Real(5.0))


async def test_alarm_config_write_checks_the_saved_row(session_factory, monkeypatch):
    av = _av()
    av.alarming_enabled, av.notification_class_instance, av.alarm_high_limit = True, 1, 80.0
    app = await _running(session_factory, _nc(), av)
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("AV-1").write_property("highLimit", Real(90.0))


async def test_recipient_list_write_checks_the_saved_row(session_factory, monkeypatch):
    app = await _running(session_factory, _nc())
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("NC-1").write_property("recipientList", [_destination()])


async def test_add_list_element_checks_the_saved_row(session_factory, monkeypatch):
    from bacpack.objects.notification_class import ListElementError

    app = await _running(session_factory, _nc())
    _refuse_objects(monkeypatch)
    with pytest.raises(ListElementError):
        await app.get_object_name("NC-1").add_recipients([_destination()])


async def test_schedule_write_checks_the_saved_row(session_factory, monkeypatch):
    schedule = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="SCH-1",
        schedule_value_type="analog",
        schedule_default="1.0",
        schedule_priority=14,
    )
    app = await _running(session_factory, schedule)
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("SCH-1").write_property("scheduleDefault", Real(99.0))


async def test_calendar_write_checks_the_saved_row(session_factory, monkeypatch):
    calendar = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="CAL-1",
        date_list=[{"kind": "date", "year": "*", "month": 1, "day": 1}],
    )
    app = await _running(session_factory, calendar)
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name("CAL-1").write_property("dateList", [CalendarEntry(date=Date((126, 12, 25, 255)))])


async def test_device_name_write_checks_the_saved_objects(session_factory, monkeypatch):
    app = await _running(session_factory, _av())
    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: DEVICE_REFUSAL)
    with pytest.raises(PropertyError):
        await app.device_object.write_property("objectName", CharacterString("Renamed"))


async def test_a_write_after_the_row_was_deleted_still_applies(session_factory):
    app = await _running(session_factory, _msv())
    with session_factory() as session:
        session.delete(session.query(BacnetObject).filter_by(name="MSV-1").one())
        session.commit()
    msv = app.get_object_name("MSV-1")
    await msv.write_property("stateText", ["A", "B", "C"])
    assert msv.numberOfStates == 3


# --- Database errors in the pre-save check ----------------------------------

import logging
import sqlite3

from sqlalchemy.exc import OperationalError


def _locked_factory():
    raise OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked"))


def _alarming_av():
    av = _av()
    av.alarming_enabled, av.notification_class_instance, av.alarm_high_limit = True, 1, 80.0
    return av


async def test_alarm_config_refusal_keeps_the_checks_message(session_factory, monkeypatch):
    app = await _running(session_factory, _nc(), _alarming_av())
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError) as exc:
        await app.get_object_name("AV-1").write_property("highLimit", Real(90.0))
    assert OBJECT_REFUSAL in str(exc.value.__cause__)


async def test_an_alarm_write_applies_when_the_check_cannot_read_the_database(session_factory, caplog):
    app = await _running(session_factory, _nc(), _alarming_av())
    live = app.get_object_name("AV-1")
    live._alarm_config_session_factory = _locked_factory
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        await live.write_property("highLimit", Real(90.0))
    assert float(live.highLimit) == 90.0
    assert "applying it live" in caplog.text


async def test_a_device_rename_applies_when_the_check_cannot_read_the_database(session_factory, caplog):
    app = await _running(session_factory, _av())
    app.session_factory = _locked_factory
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        await app.device_object.write_property("objectName", CharacterString("Renamed"))
    assert str(app.device_object.objectName) == "Renamed"
    assert "applying it live" in caplog.text


# --- Save paths covered only indirectly elsewhere --------------------------


def _saved_device(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Dev", vendor_identifier=999)
        device.objects = [_av()]
        session.add(device)
        session.commit()


def _restore_doc(session_factory):
    from bacpack.config_io import export_device_backup

    with session_factory() as session:
        return export_device_backup(session.get(Device, 599001))


def test_restore_checks_each_object(session_factory, wired):
    from bacpack.config_io import apply_device_restore
    from bacpack.config_schemas import ConfigImportError

    _saved_device(session_factory)
    data = _restore_doc(session_factory)
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match=OBJECT_REFUSAL):
            apply_device_restore(session, session.get(Device, 599001), data)


def test_restore_checks_the_device(session_factory, device_only):
    from bacpack.config_io import apply_device_restore
    from bacpack.config_schemas import ConfigImportError

    _saved_device(session_factory)
    data = _restore_doc(session_factory)
    with session_factory() as session:
        with pytest.raises(ConfigImportError, match=DEVICE_REFUSAL):
            apply_device_restore(session, session.get(Device, 599001), data)


async def test_trend_log_reporting_write_checks_the_saved_row(session_factory, monkeypatch):
    from bacpypes3.primitivedata import Unsigned

    from bacpack.objects.reserved_names import trend_log_name

    av = _av()
    av.trended, av.trend_reporting_enabled, av.trend_notification_class_instance = True, True, 1
    app = await _running(session_factory, _nc(), av)
    _refuse_objects(monkeypatch)
    with pytest.raises(PropertyError):
        await app.get_object_name(trend_log_name("AV-1")).write_property("notificationThreshold", Unsigned(7))
