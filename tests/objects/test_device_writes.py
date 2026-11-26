"""Device object writes over BACnet: Object_Name, Description and Location are
writable and saved to the device row; every other property is refused."""
from __future__ import annotations

import asyncio
import socket

import pytest
import pytest_asyncio
from bacpypes3.apdu import WritePropertyMultipleError, WritePropertyMultipleRequest
from bacpypes3.basetypes import PropertyIdentifier, PropertyValue, WriteAccessSpecification
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import CharacterString

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

_built_apps: list = []


@pytest_asyncio.fixture(autouse=True)
async def _close_built_apps():
    # Each app binds 127.0.0.1:47808. Left open, its socket is freed only
    # when the cyclic collector happens to run, and the next UDP test on this
    # worker fails to bind. Async, so it closes them before the test's loop
    # does.
    yield
    while _built_apps:
        _built_apps.pop().close()
    # A transport closes its socket a few loop turns later; the next test's
    # bind would otherwise go into bacpypes3's retry loop.
    for _ in range(100):
        if _port_is_free():
            return
        await asyncio.sleep(0.01)
    pytest.fail("a closed app's socket on 127.0.0.1:47808 was not freed within a second")


def _port_is_free() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 47808))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def _build(session_factory, *extra_objects, location=None):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", description="old", vendor_identifier=999, location=location)
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        for row in extra_objects:
            row.device = device
            session.add(row)
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    _built_apps.append(app)
    await _wait_bound(app)
    return app


async def _wait_bound(app) -> None:
    # The bind runs in a task: closing the app before it lands closes
    # nothing, and the socket then binds anyway and leaks.
    await asyncio.sleep(0)
    for _ in range(200):
        if all(link.server.local_transport is not None for link in app.link_layers.values()):
            return
        await asyncio.sleep(0.01)
    pytest.fail("the app's link layer did not bind within two seconds")


def _saved(session_factory, column):
    with session_factory() as session:
        return getattr(session.get(Device, 599001), column)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr",
    ["objectIdentifier", "vendorIdentifier", "systemStatus", "apduTimeout", "numberOfApduRetries", "modelName", "databaseRevision"],
)
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_device_refuses_writes_outside_the_allowlist(session_factory, attr, as_identifier):
    # Writing back the current value rules out a type error as the cause.
    app = await _build(session_factory)
    dev = app.device_object
    current = await dev.read_property(attr)
    target = PropertyIdentifier(attr) if as_identifier else attr
    with pytest.raises(PropertyError) as excinfo:
        await dev.write_property(target, current)
    assert excinfo.value.errorCode == "writeAccessDenied"
    assert dev.objectIdentifier[1] == 599001


@pytest.mark.asyncio
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_object_name_write_applies_and_saves(session_factory, as_identifier):
    app = await _build(session_factory)
    target = PropertyIdentifier("objectName") if as_identifier else "objectName"
    await app.device_object.write_property(target, CharacterString("Renamed"))
    assert app.device_object.objectName == "Renamed"
    assert _saved(session_factory, "name") == "Renamed"


@pytest.mark.asyncio
async def test_object_name_write_is_trimmed(session_factory):
    # Names never start or end with whitespace, on any save path.
    app = await _build(session_factory)
    await app.device_object.write_property("objectName", CharacterString(" Renamed\t"))
    assert app.device_object.objectName == "Renamed"
    assert _saved(session_factory, "name") == "Renamed"


@pytest.mark.asyncio
async def test_object_name_write_differing_only_in_edge_spaces_is_a_duplicate(session_factory):
    app = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("objectName", CharacterString("AV-1 "))
    assert excinfo.value.errorCode == "duplicate-name"


@pytest.mark.asyncio
async def test_description_write_applies_and_saves(session_factory):
    app = await _build(session_factory)
    await app.device_object.write_property("description", CharacterString(""))
    assert app.device_object.description == ""
    assert _saved(session_factory, "description") == ""


@pytest.mark.asyncio
async def test_the_save_waits_for_a_held_lock_off_the_loop(session_factory):
    app = await _build(session_factory)

    run = await run_while_locked(
        session_factory, lambda: app.device_object.write_property("description", CharacterString("new"))
    )

    assert_waited_off_the_loop(run)
    assert _saved(session_factory, "description") == "new"


@pytest.mark.asyncio
async def test_location_reads_empty_when_unset(session_factory):
    # Always present, so a client reading a device header gets no error.
    app = await _build(session_factory)
    assert await app.device_object.read_property("location") == ""


@pytest.mark.asyncio
async def test_location_reads_the_saved_value(session_factory):
    app = await _build(session_factory, location="Plant room 2")
    assert await app.device_object.read_property("location") == "Plant room 2"


@pytest.mark.asyncio
async def test_location_is_in_property_list(session_factory):
    app = await _build(session_factory)
    listed = await app.device_object.read_property("propertyList")
    assert "location" in {str(p) for p in listed}


@pytest.mark.asyncio
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_location_write_applies_and_saves(session_factory, as_identifier):
    app = await _build(session_factory)
    target = PropertyIdentifier("location") if as_identifier else "location"
    await app.device_object.write_property(target, CharacterString("Roof AHU-3"))
    assert app.device_object.location == "Roof AHU-3"
    assert _saved(session_factory, "location") == "Roof AHU-3"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   "])
async def test_blank_object_name_is_refused(session_factory, name):
    app = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("objectName", CharacterString(name))
    assert excinfo.value.errorCode == "value-out-of-range"
    assert app.device_object.objectName == "Test Device"
    assert _saved(session_factory, "name") == "Test Device"


@pytest.mark.asyncio
async def test_object_name_taken_by_an_object_is_refused(session_factory):
    app = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("objectName", CharacterString("AV-1"))
    assert excinfo.value.errorCode == "duplicate-name"
    assert _saved(session_factory, "name") == "Test Device"


@pytest.mark.asyncio
async def test_object_name_whose_backup_file_name_is_taken_is_refused(session_factory):
    # The device couldn't start with it: build_application refuses an object
    # named "<device name>-config.json" (file,1's name).
    app = await _build(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=2, name="Y-config.json"),
    )
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("objectName", CharacterString("Y"))
    assert excinfo.value.errorCode == "duplicate-name"
    assert _saved(session_factory, "name") == "Test Device"


@pytest.mark.asyncio
async def test_wpm_follows_the_same_rules(session_factory):
    # Whitespace, not "": bacpypes3's WPM handler casts the value to
    # Object_Name's type before write_property, and that cast already
    # refuses an empty string.
    app = await _build(session_factory)
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyMultipleRequest(
        WritePropertyMultipleRequest(
            listOfWriteAccessSpecs=[
                WriteAccessSpecification(
                    objectIdentifier=app.device_object.objectIdentifier,
                    listOfProperties=[PropertyValue(propertyIdentifier="objectName", value=CharacterString("   "))],
                )
            ]
        )
    )
    assert isinstance(captured[0], WritePropertyMultipleError)
    assert _saved(session_factory, "name") == "Test Device"


async def _build_with_network_port(session_factory, *extra_objects):
    from bacpack.objects.network_port import build_network_port_object

    app = await _build(session_factory, *extra_objects)
    app.add_object(build_network_port_object("127.0.0.1/8"))
    await _wait_bound(app)
    return app


@pytest.mark.asyncio
async def test_object_name_write_with_a_network_port_and_trend_log_applies(session_factory):
    # The live network port and trend logs aren't user objects, so
    # their reserved names mustn't refuse every rename.
    app = await _build_with_network_port(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=2, name="AV-2", trended=True),
    )
    await app.device_object.write_property("objectName", CharacterString("Renamed"))
    assert _saved(session_factory, "name") == "Renamed"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["NetworkPort-1", "AV-2-Trend"])
async def test_object_name_of_the_network_port_or_a_trend_log_is_refused(session_factory, name):
    app = await _build_with_network_port(
        session_factory,
        BacnetObject(object_type="analog-value", instance_number=2, name="AV-2", trended=True),
    )
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property("objectName", CharacterString(name))
    assert excinfo.value.errorCode == "duplicate-name"
    assert _saved(session_factory, "name") == "Test Device"
