"""Tests for LoggingApplication -- the bacpypes3 service handlers
(WritePropertyMultiple, Alarming ack/GetEventInformation,
ReinitializeDevice, DeviceCommunicationControl, indication/request
enforcement). See tests/test_object_builder.py for the object-
construction dispatcher's own tests."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
import sqlalchemy.exc
from bacpypes3.apdu import (
    APDU,
    AcknowledgeAlarmRequest,
    APCISequence,
    GetEventInformationACK,
    GetEventInformationRequest,
    ReadPropertyACK,
    ReadPropertyMultipleACK,
    ReadPropertyMultipleRequest,
    ReadPropertyRequest,
    SimpleAckPDU,
    WritePropertyMultipleError,
    WritePropertyMultipleRequest,
    WritePropertyRequest,
)
from bacpypes3.basetypes import (
    PropertyReference,
    PropertyValue,
    ReadAccessSpecification,
    TimeStamp,
    WriteAccessSpecification,
)
from bacpypes3.constructeddata import Any
from bacpypes3.errors import ExecutionError
from bacpypes3.pdu import PDU
from bacpypes3.primitivedata import CharacterString, Null, ObjectIdentifier, Real, Unsigned

from bacpack import application
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application

from tests.fakes import _device_and_objects
from tests.local_time import local_target


# --- WritePropertyMultiple (DS-WPM-B) ---------------------------------------
# bacpypes3 has no do_WritePropertyMultipleRequest at all -- confirmed absent
# from service/object.py (RP/WP/RPM/ReadRange all have handlers, WPM does
# not), and the older bacpypes package marks it "Not implemented" too. Same
# from-scratch-protocol-code shape as do_ReadRangeRequest for Trend Log.


def _typed(obj, value):
    element_class = obj._elements["presentValue"]
    return element_class(element_class.cast(value))


async def _write_property_multiple(app, specs, *, wire=False):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    req = WritePropertyMultipleRequest(listOfWriteAccessSpecs=specs)
    if wire:
        req = _off_the_wire(req)
    await app.do_WritePropertyMultipleRequest(req)
    return captured[0]


@pytest.mark.asyncio
async def test_write_property_multiple_writes_two_objects_in_one_request():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ai = app.get_object_name("AI-1")
    ao = app.get_object_name("AO-1")
    ai.outOfService = True  # AI needs OOS to accept a direct presentValue write

    specs = [
        WriteAccessSpecification(
            objectIdentifier=ai.objectIdentifier,
            listOfProperties=[
                PropertyValue(propertyIdentifier="presentValue", value=_typed(ai, 42.0))
            ],
        ),
        WriteAccessSpecification(
            objectIdentifier=ao.objectIdentifier,
            listOfProperties=[
                PropertyValue(propertyIdentifier="presentValue", value=_typed(ao, 55.0), priority=8)
            ],
        ),
    ]
    resp = await _write_property_multiple(app, specs)

    assert isinstance(resp, SimpleAckPDU)
    assert ai.presentValue == 42.0
    assert ao.presentValue == 55.0


@pytest.mark.asyncio
async def test_write_property_multiple_partial_failure_does_not_roll_back_earlier_writes():
    # Confirms the one spec ambiguity worth verifying rather than assuming:
    # bacpypes3's real do_WritePropertyMultipleRequest processes specs in
    # order and stops at the first failure, but does NOT undo writes that
    # already succeeded earlier in the same request.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ai = app.get_object_name("AI-1")
    bv = app.get_object_name("BV-1")
    ai.outOfService = True

    bv._bacnet_writable = False  # BacnetWriteLocked rejects presentValue writes

    specs = [
        WriteAccessSpecification(
            objectIdentifier=ai.objectIdentifier,
            listOfProperties=[
                PropertyValue(propertyIdentifier="presentValue", value=_typed(ai, 42.0))
            ],
        ),
        WriteAccessSpecification(
            objectIdentifier=bv.objectIdentifier,
            listOfProperties=[
                PropertyValue(
                    propertyIdentifier="presentValue",
                    value=bv._elements["presentValue"](bv._elements["presentValue"].cast("active")),
                    priority=8,
                )
            ],
        ),
    ]
    resp = await _write_property_multiple(app, specs)

    assert isinstance(resp, WritePropertyMultipleError)
    assert resp.firstFailedWriteAttempt.objectIdentifier == bv.objectIdentifier
    assert ai.presentValue == 42.0  # earlier successful write stands, not rolled back
    assert str(bv.presentValue) == "inactive"  # the rejected write never applied


def _pv_spec(oid, value):
    return WriteAccessSpecification(
        objectIdentifier=oid,
        listOfProperties=[PropertyValue(propertyIdentifier="presentValue", value=value, priority=8)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [False, True], ids=["built", "wire"])
async def test_wpm_to_only_unknown_objects_gets_an_encodable_wpm_error(wire):
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    resp = await _write_property_multiple(app, [_pv_spec(("analog-value", 999), Real(1.0))], wire=wire)

    assert isinstance(resp, WritePropertyMultipleError)
    assert (resp.errorType.errorClass.attr, resp.errorType.errorCode.attr) == ("object", "unknownObject")
    assert resp.firstFailedWriteAttempt.objectIdentifier == ObjectIdentifier(("analog-value", 999))
    assert resp.firstFailedWriteAttempt.propertyIdentifier.attr == "presentValue"
    assert resp.encode() is not None


@pytest.mark.asyncio
async def test_wpm_unknown_object_after_a_good_write_gets_an_encodable_wpm_error():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")

    resp = await _write_property_multiple(
        app, [_pv_spec(ao.objectIdentifier, _typed(ao, 55.0)), _pv_spec(("analog-value", 999), Real(1.0))]
    )

    assert isinstance(resp, WritePropertyMultipleError)
    assert resp.firstFailedWriteAttempt.propertyIdentifier.attr == "presentValue"
    assert resp.encode() is not None
    assert ao.presentValue == 55.0


@pytest.mark.asyncio
async def test_wpm_unexpected_exception_is_an_operational_problem_at_that_property(monkeypatch):
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(ao), "write_property", _boom)
    resp = await _write_property_multiple(app, [_pv_spec(ao.objectIdentifier, _typed(ao, 55.0))])

    assert isinstance(resp, WritePropertyMultipleError)
    assert (resp.errorType.errorClass.attr, resp.errorType.errorCode.attr) == ("device", "operationalProblem")
    assert resp.firstFailedWriteAttempt.objectIdentifier == ao.objectIdentifier
    assert resp.firstFailedWriteAttempt.propertyIdentifier.attr == "presentValue"


# --- device,4194303 wildcard -------------------------------------------------
# bacpypes3 compares an ObjectIdentifier to a plain tuple, which is never
# equal, so its own substitution never happened.


@pytest.mark.asyncio
async def test_read_property_to_the_device_wildcard_names_the_real_device():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _send(app, ReadPropertyRequest(objectIdentifier=("device", 4194303), propertyIdentifier="objectName"))
    assert isinstance(resp, ReadPropertyACK)
    assert resp.objectIdentifier == app.device_object.objectIdentifier
    assert resp.propertyValue.cast_out(CharacterString) == "Test Device"


@pytest.mark.asyncio
async def test_rpm_to_the_device_wildcard_names_the_real_device():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _send(
        app,
        ReadPropertyMultipleRequest(
            listOfReadAccessSpecs=[
                ReadAccessSpecification(
                    objectIdentifier=("device", 4194303),
                    listOfPropertyReferences=[PropertyReference(propertyIdentifier="objectName")],
                )
            ]
        ),
    )
    assert isinstance(resp, ReadPropertyMultipleACK)
    result = resp.listOfReadAccessResults[0]
    assert result.objectIdentifier == app.device_object.objectIdentifier
    assert result.listOfResults[0].readResult.propertyValue.cast_out(CharacterString) == "Test Device"


def _rpm(*specs):
    return ReadPropertyMultipleRequest(
        listOfReadAccessSpecs=[
            ReadAccessSpecification(
                objectIdentifier=oid,
                listOfPropertyReferences=[PropertyReference(propertyIdentifier=prop)],
            )
            for oid, prop in specs
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prop", ["all", "required", "optional"])
async def test_rpm_names_a_missing_object_beside_one_that_exists(prop):
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _send(app, _rpm((("analog-input", 1), prop), (("analog-value", 99), prop)))
    assert isinstance(resp, ReadPropertyMultipleACK)
    present, missing = resp.listOfReadAccessResults
    assert present.listOfResults
    assert missing.objectIdentifier == ObjectIdentifier("analog-value,99")
    [element] = missing.listOfResults
    assert element.propertyIdentifier.attr == prop
    assert element.readResult.propertyAccessError.errorCode.attr == "unknownObject"


@pytest.mark.asyncio
async def test_rpm_of_only_missing_objects_is_unknown_object():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_ReadPropertyMultipleRequest(_rpm((("analog-value", 99), "all")))
    assert excinfo.value.errorCode == "unknown-object"


async def _rpm_counting_turns(app, request):
    """The reply, and how many turns another task got while it was built."""
    turns = 0
    stop = False

    async def other_task():
        nonlocal turns
        while not stop:
            turns += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(other_task())
    await asyncio.sleep(0)
    before = turns
    resp = await _send(app, request)
    during = turns - before
    stop = True
    await ticker
    return resp, during


_EVERY_OBJECT_ALL = [(("analog-input", 1), "all"), (("analog-output", 1), "all"), (("binary-value", 1), "all")]


@pytest.mark.asyncio
async def test_a_long_rpm_reply_gives_the_loop_turns(monkeypatch):
    monkeypatch.setattr(application, "RPM_TURN_SECONDS", 0.0)
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp, turns = await _rpm_counting_turns(app, _rpm(*_EVERY_OBJECT_ALL))
    assert isinstance(resp, ReadPropertyMultipleACK)
    assert len(resp.listOfReadAccessResults) == 3
    assert turns > 0


@pytest.mark.asyncio
async def test_a_short_rpm_reply_is_built_in_one_go(monkeypatch):
    monkeypatch.setattr(application, "RPM_TURN_SECONDS", 3600.0)
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp, turns = await _rpm_counting_turns(app, _rpm(*_EVERY_OBJECT_ALL))
    assert isinstance(resp, ReadPropertyMultipleACK)
    assert turns == 0


@pytest.mark.asyncio
async def test_an_object_deleted_during_an_rpm_reply_is_named_unknown(monkeypatch):
    monkeypatch.setattr(application, "RPM_TURN_SECONDS", 0.0)
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    doomed = app.get_object_id(ObjectIdentifier("binary-value,1"))
    asyncio.get_running_loop().call_soon(app.delete_object, doomed)
    resp = await _send(app, _rpm(*_EVERY_OBJECT_ALL))
    assert isinstance(resp, ReadPropertyMultipleACK)
    [element] = resp.listOfReadAccessResults[2].listOfResults
    assert element.readResult.propertyAccessError.errorCode.attr == "unknownObject"


@pytest.mark.asyncio
async def test_write_property_to_the_device_wildcard_reaches_the_device():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _send(
        app,
        WritePropertyRequest(
            objectIdentifier=("device", 4194303),
            propertyIdentifier="description",
            propertyValue=Any(CharacterString("via wildcard")),
        ),
    )
    assert isinstance(resp, SimpleAckPDU)
    assert app.device_object.description == "via wildcard"


# BACpack's own WPM handler replaces bacpypes3's; these check it
# still resolves the wildcard, on a request decoded off the wire.


def _wildcard_wpm(prop, value):
    return [
        WriteAccessSpecification(
            objectIdentifier=("device", 4194303),
            listOfProperties=[PropertyValue(propertyIdentifier=prop, value=Any(value))],
        )
    ]


@pytest.mark.asyncio
async def test_wpm_to_the_device_wildcard_reaches_the_device():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _write_property_multiple(app, _wildcard_wpm("description", CharacterString("via wildcard")), wire=True)
    assert isinstance(resp, SimpleAckPDU)
    assert app.device_object.description == "via wildcard"


@pytest.mark.asyncio
async def test_a_failed_wpm_to_the_device_wildcard_names_the_real_device():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _write_property_multiple(app, _wildcard_wpm("description", Real(1.0)), wire=True)
    assert isinstance(resp, WritePropertyMultipleError)
    assert resp.errorType.errorCode.attr == "invalidDataType"
    assert resp.firstFailedWriteAttempt.objectIdentifier == app.device_object.objectIdentifier


@pytest.mark.asyncio
async def test_another_device_instance_is_still_unknown():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_ReadPropertyRequest(ReadPropertyRequest(objectIdentifier=("device", 12), propertyIdentifier="objectName"))
    assert excinfo.value.errorCode == "unknownObject"


def _alarming_app():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
        alarm_notify_type="alarm",  # the column default, which an unsaved row lacks
    )
    return build_application(device, [nc, ai])


async def _saves_settled():
    """Returns once every config save started so far has committed: the DCC
    save runs as a task, and run_in_order queues behind it."""
    from bacpack import config_save

    await asyncio.sleep(0)
    await config_save.run_in_order(lambda: None)


async def _send(app, req):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    handler_name = f"do_{type(req).__name__}"
    await getattr(app, handler_name)(req)
    return captured[0] if captured else None


@pytest.mark.asyncio
async def test_acknowledge_alarm_request_unknown_object_is_rejected():
    app = _alarming_app()
    req = AcknowledgeAlarmRequest(
        acknowledgingProcessIdentifier=1,
        eventObjectIdentifier=("analog-input", 99),
        eventStateAcknowledged="offnormal",
        timeStamp=TimeStamp(sequenceNumber=0),
        acknowledgmentSource="test",
        timeOfAcknowledgment=TimeStamp(sequenceNumber=0),
    )
    with pytest.raises(ExecutionError):
        await app.do_AcknowledgeAlarmRequest(req)


@pytest.mark.asyncio
async def test_acknowledge_alarm_request_non_alarming_object_is_rejected():
    device, objects = _device_and_objects()  # AI-1 has no alarming fields set
    app = build_application(device, objects)
    req = AcknowledgeAlarmRequest(
        acknowledgingProcessIdentifier=1,
        eventObjectIdentifier=("analog-input", 1),
        eventStateAcknowledged="offnormal",
        timeStamp=TimeStamp(sequenceNumber=0),
        acknowledgmentSource="test",
        timeOfAcknowledgment=TimeStamp(sequenceNumber=0),
    )
    with pytest.raises(ExecutionError):
        await app.do_AcknowledgeAlarmRequest(req)


@pytest.mark.asyncio
async def test_acknowledge_alarm_request_sets_acked_transition():
    import asyncio

    app = _alarming_app()
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0  # above highLimit -- real transition, resets ackedTransitions[0] to False
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert list(ai.ackedTransitions)[0] is False  # confirms there's something real to acknowledge
    # OutOfRangeEventAlgorithm transitions eventState to the specific
    # highLimit/lowLimit state, never the generic "offnormal" -- use the
    # object's real post-transition state so this test can't coincidentally
    # pass against a handler that only recognizes the generic name.
    assert ai.eventState.attr == "highLimit"

    req = AcknowledgeAlarmRequest(
        acknowledgingProcessIdentifier=1,
        eventObjectIdentifier=ai.objectIdentifier,
        eventStateAcknowledged=ai.eventState.attr,
        timeStamp=ai.eventTimeStamps[0],
        acknowledgmentSource="test",
        timeOfAcknowledgment=TimeStamp(sequenceNumber=0),
    )
    resp = await _send(app, req)
    from bacpypes3.apdu import SimpleAckPDU as _SimpleAckPDU

    assert isinstance(resp, _SimpleAckPDU)
    assert list(ai.ackedTransitions)[0] is True


@pytest.mark.asyncio
async def test_get_event_information_returns_empty_when_all_normal():
    app = _alarming_app()
    req = GetEventInformationRequest()
    resp = await _send(app, req)
    assert isinstance(resp, GetEventInformationACK)
    assert list(resp.listOfEventSummaries) == []
    # Not `is False`: bacpypes3's Boolean element is an int subclass, so
    # Boolean(False) is never identical to the singleton False -- confirmed
    # directly (Boolean(False) == False is True, `is False` is always False
    # for any Boolean instance regardless of value).
    assert resp.moreEvents == False  # noqa: E712


@pytest.mark.asyncio
async def test_get_event_information_lists_offnormal_object():
    app = _alarming_app()
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0  # above highLimit
    await __import__("asyncio").sleep(0)
    await __import__("asyncio").sleep(0)

    req = GetEventInformationRequest()
    resp = await _send(app, req)
    summaries = list(resp.listOfEventSummaries)
    assert len(summaries) == 1
    assert summaries[0].objectIdentifier == ai.objectIdentifier
    assert summaries[0].eventState.attr == "highLimit"


def _many_alarms_app(count):
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    rows = [BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")]
    rows += [
        BacnetObject(
            object_type="analog-input", instance_number=i, name=f"AI-{i}", units="degreesFahrenheit",
            alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=0.0,
            alarm_notify_type="alarm",  # the column default, which an unsaved row lacks
        )
        for i in range(1, count + 1)
    ]
    return build_application(device, rows)


@pytest.mark.asyncio
async def test_get_event_information_pages_events_that_do_not_fit_one_reply():
    # About 61 octets a summary: 30 events don't fit a 480-octet,
    # non-segmenting client (what a locally built request reads as), and
    # bacpypes3 would Abort the whole reply.
    app = _many_alarms_app(30)
    for i in range(1, 31):
        app.get_object_name(f"AI-{i}").presentValue = 150.0
    for _ in range(3):
        await asyncio.sleep(0)

    seen, last, pages = [], None, 0
    while True:
        req = GetEventInformationRequest()
        if last is not None:
            req.lastReceivedObjectIdentifier = last
        resp = await _send(app, req)
        pages += 1
        summaries = list(resp.listOfEventSummaries)
        assert summaries
        apdu = resp.encode()
        apdu.apduSeg = apdu.apduMor = False
        apdu.apduInvokeID = 1
        assert len(apdu.encode().pduData) <= 480
        seen += [s.objectIdentifier for s in summaries]
        last = summaries[-1].objectIdentifier
        if not resp.moreEvents:
            break
        assert pages < 30

    assert pages > 1
    assert seen == [ObjectIdentifier(("analog-input", i)) for i in range(1, 31)]


@pytest.mark.asyncio
async def test_get_event_information_resumes_after_an_object_that_went_back_to_normal():
    app = _many_alarms_app(3)
    for i in (1, 3):
        app.get_object_name(f"AI-{i}").presentValue = 150.0
    for _ in range(3):
        await asyncio.sleep(0)

    req = GetEventInformationRequest(lastReceivedObjectIdentifier=("analog-input", 2))
    resp = await _send(app, req)

    assert [s.objectIdentifier for s in resp.listOfEventSummaries] == [ObjectIdentifier(("analog-input", 3))]
    assert resp.moreEvents == False  # noqa: E712


@pytest.mark.asyncio
async def test_reinitialize_device_coldstart_acks_and_marks_reinitializing(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()

    app = build_application(device, objects, trend_session_factory=session_factory)
    req = ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart")
    resp = await _send(app, req)

    assert isinstance(resp, SimpleAckPDU)
    with session_factory() as session:
        assert session.get(Device, device.id).runtime.process_status == "reinitializing"


@pytest.mark.asyncio
async def test_reinitialize_device_warmstart_acks_and_marks_reinitializing(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()

    app = build_application(device, objects, trend_session_factory=session_factory)
    req = ReinitializeDeviceRequest(reinitializedStateOfDevice="warmstart")
    resp = await _send(app, req)

    assert isinstance(resp, SimpleAckPDU)
    with session_factory() as session:
        assert session.get(Device, device.id).runtime.process_status == "reinitializing"


@pytest.mark.asyncio
async def test_reinitialize_device_unsupported_state_is_rejected():
    from bacpypes3.apdu import ReinitializeDeviceRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)  # no session_factory needed -- rejected before any DB touch
    req = ReinitializeDeviceRequest(reinitializedStateOfDevice="activateChanges")

    with pytest.raises(ExecutionError):
        await app.do_ReinitializeDeviceRequest(req)


@pytest.mark.asyncio
async def test_reinitialize_device_without_session_factory_still_acks():
    from bacpypes3.apdu import ReinitializeDeviceRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)  # session_factory is None
    req = ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart")

    resp = await _send(app, req)
    assert isinstance(resp, SimpleAckPDU)  # no AttributeError from a None session_factory


@pytest.mark.asyncio
async def test_dcc_disable_then_enable_round_trip(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()

    app = build_application(device, objects, trend_session_factory=session_factory)

    resp = await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    assert isinstance(resp, SimpleAckPDU)
    assert app._dcc_state == "disable"
    await _saves_settled()
    with session_factory() as session:
        assert session.get(Device, device.id).dcc_state == "disable"

    resp = await _send(app, DeviceCommunicationControlRequest(enableDisable="enable"))
    assert isinstance(resp, SimpleAckPDU)
    assert app._dcc_state == "enable"
    await _saves_settled()
    with session_factory() as session:
        assert session.get(Device, device.id).dcc_state == "enable"


@pytest.mark.asyncio
async def test_dcc_missing_enable_disable_is_rejected():
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)
    with pytest.raises(ExecutionError):
        await app.do_DeviceCommunicationControlRequest(DeviceCommunicationControlRequest())


@pytest.mark.asyncio
async def test_dcc_time_duration_schedules_auto_reenable(monkeypatch):
    import asyncio as _asyncio
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)

    scheduled = {}
    loop = _asyncio.get_event_loop()

    def fake_call_later(delay, callback, *args):
        scheduled["delay"] = delay
        scheduled["callback"] = callback
        scheduled["args"] = args
        return object()  # stand-in TimerHandle -- never actually fires

    # Patches this one test's own running loop instance, not the module-level
    # asyncio.get_event_loop function -- the handler calls
    # asyncio.get_event_loop() internally, which returns this exact loop
    # object under pytest-asyncio, so this is enough without risking
    # affecting any other test's event-loop machinery.
    monkeypatch.setattr(loop, "call_later", fake_call_later)

    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", timeDuration=5))
    assert scheduled["delay"] == 300  # 5 minutes
    scheduled["callback"](*scheduled["args"])
    assert app._dcc_state == "enable"


@pytest.mark.asyncio
async def test_dcc_enable_handle_is_cleared_once_its_own_timer_fires(monkeypatch):
    """_dcc_enable_handle was only ever cleared
    by the *next* DCC request (do_DeviceCommunicationControlRequest's own
    cancel-and-clear), never by the timer firing naturally on its own --
    leaving a stale reference to an already-fired TimerHandle."""
    import asyncio as _asyncio
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)

    scheduled = {}
    loop = _asyncio.get_event_loop()

    def fake_call_later(delay, callback, *args):
        scheduled["callback"] = callback
        scheduled["args"] = args
        return object()

    monkeypatch.setattr(loop, "call_later", fake_call_later)

    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", timeDuration=5))
    assert app._dcc_enable_handle is not None
    scheduled["callback"](*scheduled["args"])
    assert app._dcc_enable_handle is None


@pytest.mark.asyncio
async def test_set_dcc_state_db_write_failure_does_not_raise(monkeypatch, caplog):
    """_set_dcc_state's DB write is a best-effort mirror: a failure there is
    logged and mustn't crash the timeDuration auto-reenable callback (a plain
    call_later callback, not a coroutine bacpypes3 could report an error
    for)."""
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    def _raise(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(app, "session_factory", _raise)

    with caplog.at_level(logging.ERROR, logger="bacpack"):
        app._set_dcc_state("enable")  # must not raise
        await _saves_settled()

    assert app._dcc_state == "enable"
    assert "could not save dcc_state=enable" in caplog.text


@pytest.mark.asyncio
async def test_indication_drops_everything_while_disabled_except_dcc_itself():
    from bacpypes3.apdu import DeviceCommunicationControlRequest, WhoIsRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))

    # Spy on app.request: do_WhoIsRequest reaches this method to send an I-Am
    # reply via i_am(). If indication() correctly drops the WhoIsRequest early,
    # do_WhoIsRequest is never called, so request() is never reached.
    request_calls = []
    original_request = app.request
    async def capture_request(*args, **kwargs):
        request_calls.append((args, kwargs))
        return await original_request(*args, **kwargs)
    app.request = capture_request

    await app.indication(WhoIsRequest())
    assert request_calls == []  # dropped -- do_WhoIsRequest never reached request()

    dcc_req = DeviceCommunicationControlRequest(enableDisable="enable")
    await app.indication(dcc_req)
    assert app._dcc_state == "enable"  # DCC-Request itself still got through


@pytest.mark.asyncio
async def test_indication_allows_reinitialize_device_while_disabled(session_factory):
    """ReinitializeDeviceRequest is DM-DCC-B/DM-RD-B's paired recovery escape
    hatch alongside DeviceCommunicationControlRequest itself -- ASHRAE 135
    requires both to stay reachable while disabled, matching bacpypes3's own
    gate (appservice.py:1599-1610, both service 17 and service 20 pass
    through)."""
    from bacpypes3.apdu import DeviceCommunicationControlRequest, ReinitializeDeviceRequest

    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()

    app = build_application(device, objects, trend_session_factory=session_factory)
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    assert app._dcc_state == "disable"

    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture

    req = ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart")
    await app.indication(req)  # exercise the actual enforcement point, not do_ directly

    assert len(captured) == 1
    assert isinstance(captured[0], SimpleAckPDU)
    with session_factory() as session:
        assert session.get(Device, device.id).runtime.process_status == "reinitializing"


@pytest.mark.asyncio
async def test_indication_disable_initiation_does_not_block_incoming_requests():
    from bacpypes3.apdu import DeviceCommunicationControlRequest, IAmRequest

    device, objects = _device_and_objects()
    app = build_application(device, objects)
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disableInitiation"))

    # do_IAmRequest exists on LoggingApplication and logs an "i-am-received"
    # traffic entry before delegating to the base class. If indication()
    # correctly allows this request through (does not gate it under
    # disableInitiation), the traffic_log will contain the "i-am-received"
    # entry, confirming delegation to super().indication(apdu) happened.
    initial_entries_count = len(app.traffic_log._entries)
    await app.indication(IAmRequest(iAmDeviceIdentifier=("device", 599002), maxAPDULengthAccepted=1024, segmentationSupported="noSegmentation", vendorID=999))

    # Assert that a new traffic log entry was added (proving do_IAmRequest was called)
    assert len(app.traffic_log._entries) > initial_entries_count
    latest_entry = list(app.traffic_log._entries)[-1]
    assert latest_entry["event_type"] == "i-am-received"
    assert latest_entry["detail"].endswith(" is device 599002")


@pytest.mark.asyncio
async def test_disable_initiation_suppresses_alarm_notification_delivery():
    import asyncio as _asyncio
    from bacpypes3.apdu import ConfirmedEventNotificationRequest, DeviceCommunicationControlRequest

    app = _alarming_app()
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disableInitiation"))

    sent = []
    real_request = app.request

    def spying_request(apdu):
        sent.append(apdu)
        return real_request(apdu)

    app.request = spying_request

    fake_notification = ConfirmedEventNotificationRequest(
        processIdentifier=1,
        initiatingDeviceIdentifier=("device", 599001),
        eventObjectIdentifier=("analog-input", 1),
        timeStamp=TimeStamp(sequenceNumber=0),
        notificationClass=1,
        priority=100,
        eventType="outOfRange",
        messageText="test",
        notifyType="alarm",
        ackRequired=False,
        fromState="normal",
        toState="highLimit",
    )
    future = app.request(fake_notification)
    assert sent == [fake_notification]
    result = await _asyncio.wait_for(future, timeout=1.0)
    assert result is None  # suppressed -- resolved without actually being sent on the wire


@pytest.mark.asyncio
async def test_disable_suppresses_alarm_notification_delivery():
    """disable is strictly stronger than disableInitiation -- it must ALSO
    suppress outbound Alarming notifications, matching bacpypes3's own DCC
    gate (appservice.py:1738-1757), which drops all outbound under
    "disable" and only exempts I-Am under "disableInitiation"."""
    import asyncio as _asyncio
    from bacpypes3.apdu import ConfirmedEventNotificationRequest, DeviceCommunicationControlRequest

    app = _alarming_app()
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))

    sent = []
    real_request = app.request

    def spying_request(apdu):
        sent.append(apdu)
        return real_request(apdu)

    app.request = spying_request

    fake_notification = ConfirmedEventNotificationRequest(
        processIdentifier=1,
        initiatingDeviceIdentifier=("device", 599001),
        eventObjectIdentifier=("analog-input", 1),
        timeStamp=TimeStamp(sequenceNumber=0),
        notificationClass=1,
        priority=100,
        eventType="outOfRange",
        messageText="test",
        notifyType="alarm",
        ackRequired=False,
        fromState="normal",
        toState="highLimit",
    )
    future = app.request(fake_notification)
    assert sent == [fake_notification]
    result = await _asyncio.wait_for(future, timeout=1.0)
    assert result is None  # suppressed -- resolved without actually being sent on the wire


@pytest.mark.asyncio
async def test_disable_initiation_suppresses_unconfirmed_alarm_notification_delivery():
    """Same suppression as test_disable_initiation_suppresses_alarm_notification_delivery,
    but for the unconfirmed notification class -- both notification classes
    must be caught by the isinstance() check in request()."""
    import asyncio as _asyncio
    from bacpypes3.apdu import DeviceCommunicationControlRequest, UnconfirmedEventNotificationRequest

    app = _alarming_app()
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disableInitiation"))

    sent = []
    real_request = app.request

    def spying_request(apdu):
        sent.append(apdu)
        return real_request(apdu)

    app.request = spying_request

    fake_notification = UnconfirmedEventNotificationRequest(
        processIdentifier=1,
        initiatingDeviceIdentifier=("device", 599001),
        eventObjectIdentifier=("analog-input", 1),
        timeStamp=TimeStamp(sequenceNumber=0),
        notificationClass=1,
        priority=100,
        eventType="outOfRange",
        messageText="test",
        notifyType="alarm",
        ackRequired=False,
        fromState="normal",
        toState="highLimit",
    )
    future = app.request(fake_notification)
    assert sent == [fake_notification]
    result = await _asyncio.wait_for(future, timeout=1.0)
    assert result is None  # suppressed -- resolved without actually being sent on the wire


@pytest.mark.asyncio
async def test_enable_does_not_suppress_alarm_notification_delivery():
    from bacpypes3.apdu import ConfirmedEventNotificationRequest
    from bacpypes3.pdu import LocalBroadcast

    app = _alarming_app()
    assert app._dcc_state == "enable"  # default, nothing disabled this session

    fake_notification = ConfirmedEventNotificationRequest(
        processIdentifier=1,
        initiatingDeviceIdentifier=("device", 599001),
        eventObjectIdentifier=("analog-input", 1),
        timeStamp=TimeStamp(sequenceNumber=0),
        notificationClass=1,
        priority=100,
        eventType="outOfRange",
        messageText="test",
        notifyType="alarm",
        ackRequired=False,
        fromState="normal",
        toState="highLimit",
    )
    # Application.request()'s real (base) implementation for a confirmed PDU
    # asserts pduDestination is set (see app.py:1052-1074) before assigning
    # an invoke ID and queuing it -- set it the same way EventAlgorithm's own
    # notification-sending code does (bacpypes3/local/event.py) right before
    # this exact call, so this test proves the call actually reached the
    # real implementation instead of being short-circuited, rather than
    # failing on an unrelated missing field.
    fake_notification.pduDestination = LocalBroadcast()
    future = app.request(fake_notification)
    assert fake_notification.apduInvokeID is not None


@pytest.mark.asyncio
async def test_device_object_advertises_corrected_services_and_object_types():
    # Read through the device object's properties -- the path a real
    # client's ReadProperty takes -- not just the capabilities helpers.
    # bacpypes3 set the wrong Protocol_Services_Supported bits for the
    # unconfirmed services (Who-Is read as add-list-element); this pins the fix
    # and that the object types match what BACpack serves.
    from bacpack.capabilities import object_types_supported

    device, objects = _device_and_objects()
    app = build_application(device, objects)

    services = await app.device_object.read_property("protocolServicesSupported")
    service_bits = {n for n in str(services).split(";") if n}
    assert {"who-is", "who-has", "i-am", "i-have", "read-property"} <= service_bits
    # add-list-element (Who-Is's wrong bit) is now legitimately set by
    # AE-CRL-B, like atomic-write-file (Who-Has's).
    assert "confirmed-event-notification" not in service_bits
    assert {"add-list-element", "remove-list-element"} <= service_bits
    assert {"atomic-read-file", "atomic-write-file"} <= service_bits

    types = await app.device_object.read_property("protocolObjectTypesSupported")
    assert {n for n in str(types).split(";") if n} == set(object_types_supported())


# --- Time sync (DM-TS-B / DM-UTC-B) --------------------------------------

import logging
import time

from bacpypes3.apdu import TimeSynchronizationRequest, UTCTimeSynchronizationRequest
from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time

from bacpack import device_clock


def _bacnet_datetime(struct, hundredths=0):
    return DateTime(
        date=Date((struct.tm_year - 1900, struct.tm_mon, struct.tm_mday, struct.tm_wday + 1)),
        time=Time((struct.tm_hour, struct.tm_min, struct.tm_sec, hundredths)),
    )


def _clock_app(session_factory):
    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()
    device_clock.install()
    return device, build_application(device, objects, trend_session_factory=session_factory)


@pytest.mark.asyncio
async def test_time_synchronization_moves_local_time_and_saves_the_clock(session_factory):
    device, app = _clock_app(session_factory)
    target, ahead = local_target(3 * 3600)

    resp = await _send(app, TimeSynchronizationRequest(time=_bacnet_datetime(target)))

    assert resp is None  # unconfirmed: nothing goes back
    assert abs(device_clock.offset() - ahead) < 2
    local_time = await app.device_object.read_property("localTime")
    got, want = local_time[0] * 60 + local_time[1], target.tm_hour * 60 + target.tm_min
    assert min((got - want) % 1440, (want - got) % 1440) <= 1
    with session_factory() as session:
        assert abs(session.get(Device, device.id).clock_offset_seconds - ahead) < 2


@pytest.mark.asyncio
async def test_utc_time_synchronization_moves_the_clock(session_factory):
    _device, app = _clock_app(session_factory)
    target = time.gmtime(time.time() - 2 * 3600)

    await _send(app, UTCTimeSynchronizationRequest(time=_bacnet_datetime(target)))

    assert abs(device_clock.offset() + 2 * 3600) < 2


@pytest.mark.asyncio
async def test_time_synchronization_with_a_wildcard_is_ignored(session_factory, caplog):
    _device, app = _clock_app(session_factory)
    wildcard = DateTime(date=Date((255, 9, 25, 5)), time=Time((14, 0, 0, 0)))

    with caplog.at_level(logging.WARNING, logger="bacpack.application"):
        await _send(app, TimeSynchronizationRequest(time=wildcard))

    assert device_clock.offset() == 0.0
    assert "ignoring TimeSynchronization" in caplog.text


@pytest.mark.asyncio
async def test_time_synchronization_to_an_unrepresentable_year_is_ignored(session_factory, caplog):
    _device, app = _clock_app(session_factory)
    far = DateTime(date=Date((254, 1, 1, 255)), time=Time((0, 0, 0, 0)))  # year 2154 is fine...
    await _send(app, UTCTimeSynchronizationRequest(time=far))
    assert device_clock.offset() != 0.0
    device_clock.set_offset(0.0)

    too_old = DateTime(date=Date((0, 1, 1, 255)), time=Time((0, 0, 0, 0)))  # ...year 1900 is not
    with caplog.at_level(logging.WARNING, logger="bacpack.application"):
        await _send(app, UTCTimeSynchronizationRequest(time=too_old))
    assert device_clock.offset() == 0.0
    assert "ignoring UTCTimeSynchronization" in caplog.text


@pytest.mark.asyncio
async def test_time_synchronization_is_dropped_while_communication_is_disabled(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _device, app = _clock_app(session_factory)
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    target = time.localtime(time.time() + 3600)

    await app.indication(TimeSynchronizationRequest(time=_bacnet_datetime(target)))

    assert device_clock.offset() == 0.0


# --- Backup/Restore (DM-BR-B) ----------------------------------------------

import json

from bacpypes3.apdu import (
    AtomicReadFileACK,
    AtomicReadFileRequest,
    AtomicWriteFileACK,
    AtomicWriteFileRequest,
    ReinitializeDeviceRequest,
)
from bacpypes3.basetypes import (
    AtomicReadFileRequestAccessMethodChoice,
    AtomicReadFileRequestAccessMethodChoiceRecordAccess,
    AtomicReadFileRequestAccessMethodChoiceStreamAccess,
    AtomicWriteFileRequestAccessMethodChoice,
    AtomicWriteFileRequestAccessMethodChoiceStreamAccess,
)

from bacpack.config_io import export_device_backup


def _backup_app(session_factory):
    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name, bind_address="10.0.0.5/24"))
        for obj in objects:
            session.add(
                BacnetObject(
                    device_id=device.id,
                    object_type=obj.object_type,
                    instance_number=obj.instance_number,
                    name=obj.name,
                    units=obj.units,
                    state_text=obj.state_text,
                )
            )
        session.commit()
    return build_application(device, objects, trend_session_factory=session_factory)


async def _reinit(app, state):
    return await _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice=state))


def _read_request(start, count, segmented=True, max_resp=3, max_segs=0):
    req = AtomicReadFileRequest(
        fileIdentifier=("file", 1),
        accessMethod=AtomicReadFileRequestAccessMethodChoice(
            streamAccess=AtomicReadFileRequestAccessMethodChoiceStreamAccess(
                fileStartPosition=start, requestedOctetCount=count
            )
        ),
    )
    # As bacpypes3's APCI decode leaves them: max_resp 3 = 480 octets,
    # max_segs 0 = unspecified.
    req.apduSA = segmented
    req.apduMaxResp = max_resp
    req.apduMaxSegs = max_segs
    return req


def _write_request(start, data, file_id=("file", 1)):
    return AtomicWriteFileRequest(
        fileIdentifier=file_id,
        accessMethod=AtomicWriteFileRequestAccessMethodChoice(
            streamAccess=AtomicWriteFileRequestAccessMethodChoiceStreamAccess(
                fileStartPosition=start, fileData=data
            )
        ),
    )


async def _read_all(app, segmented=True):
    data, start = b"", 0
    while True:
        ack = await _send(app, _read_request(start, 100000, segmented, max_resp=5))
        assert isinstance(ack, AtomicReadFileACK)
        chunk = bytes(ack.accessMethod.streamAccess.fileData)
        data += chunk
        start += len(chunk)
        if ack.endOfFile:
            return data


@pytest.mark.asyncio
async def test_backup_reads_the_export_of_this_device(session_factory):
    app = _backup_app(session_factory)

    assert isinstance(await _reinit(app, "startBackup"), SimpleAckPDU)
    data = await _read_all(app)
    with session_factory() as session:
        assert data == export_device_backup(session.get(Device, 599001))
    assert isinstance(await _reinit(app, "endBackup"), SimpleAckPDU)
    assert app.backup_restore.state == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segmented, max_resp, max_segs, fits",
    [
        (False, 3, 0, 480),  # 480-octet APDU, no segmentation
        (False, 2, 0, 206),  # e.g. a client routed from MS/TP
        (False, 1, 0, 128),
        (True, 3, 1, 2 * 480),  # segmented, but at most 2 segments
    ],
)
async def test_atomic_read_caps_the_chunk_to_what_the_client_accepts(
    session_factory, segmented, max_resp, max_segs, fits
):
    # bacpypes3's ServerSSM aborts a reply that needs segmentation the
    # client didn't accept, or more segments than it allows -- the chunk
    # must shrink instead, with endOfFile=false so the client keeps reading.
    from bacpack.application import READ_ACK_OVERHEAD

    app = _backup_app(session_factory)
    await _reinit(app, "startBackup")
    assert app.backup_restore.size > fits  # else the test proves nothing

    ack = await _send(app, _read_request(0, 100000, segmented, max_resp, max_segs))
    assert len(ack.accessMethod.streamAccess.fileData) == fits - READ_ACK_OVERHEAD
    assert not ack.endOfFile


@pytest.mark.asyncio
async def test_atomic_read_without_segmentation_reads_the_whole_file_in_chunks(session_factory):
    app = _backup_app(session_factory)
    await _reinit(app, "startBackup")
    assert await _read_all(app, segmented=False) == await _read_all(app)


@pytest.mark.asyncio
async def test_restore_applies_and_marks_reinitializing(session_factory):
    app = _backup_app(session_factory)
    with session_factory() as session:
        doc = json.loads(export_device_backup(session.get(Device, 599001)))
    doc["devices"][0]["objects"] = [o for o in doc["devices"][0]["objects"] if o["name"] != "BV-1"]
    payload = json.dumps(doc).encode()

    assert isinstance(await _reinit(app, "startRestore"), SimpleAckPDU)
    ack = await _send(app, _write_request(-1, payload[:50]))
    assert isinstance(ack, AtomicWriteFileACK) and ack.fileStartPosition == 0
    ack = await _send(app, _write_request(-1, payload[50:]))
    assert ack.fileStartPosition == 50
    assert isinstance(await _reinit(app, "endRestore"), SimpleAckPDU)

    with session_factory() as session:
        device = session.get(Device, 599001)
        assert device.runtime.process_status == "reinitializing"
        assert "BV-1" not in [o.name for o in device.objects]
        assert device.last_restore_time is not None
    assert app.backup_restore.state == "idle"


@pytest.mark.asyncio
async def test_restore_logs_when_marking_reinitializing_fails(session_factory, caplog):
    # The restore commits, then the reinitializing write fails: the device
    # keeps serving its old config, so that has to reach the Logs page (the
    # bacpack logger), and the client must get only the one SimpleAck.
    app = _backup_app(session_factory)
    with session_factory() as session:
        payload = export_device_backup(session.get(Device, 599001))
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, payload))

    calls = []

    def _failing_after_first(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise sqlalchemy.exc.OperationalError("UPDATE devices", {}, Exception("database is locked"))
        return session_factory(*args, **kwargs)

    app.session_factory = _failing_after_first
    app._stored_bacnet_password_hash = lambda: None  # this test counts the saves' sessions
    responses = []

    async def _capture(resp):
        responses.append(resp)

    app.response = _capture
    with caplog.at_level(logging.ERROR, logger="bacpack"):
        await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="endRestore"))

    assert len(responses) == 1 and isinstance(responses[0], SimpleAckPDU)
    assert any(
        r.name.startswith("bacpack") and r.levelno == logging.ERROR and "restart" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_bad_restore_file_is_rejected_and_changes_nothing(session_factory):
    app = _backup_app(session_factory)
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, b"{not json"))

    with pytest.raises(ExecutionError) as excinfo:
        await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="endRestore"))
    assert str(excinfo.value.errorCode) == "invalidConfigurationData"
    assert app.backup_restore.state == "idle"
    with session_factory() as session:
        device = session.get(Device, 599001)
        assert device.runtime.process_status != "reinitializing"
        assert len(device.objects) == 4
        assert device.last_restore_time is None


@pytest.mark.asyncio
async def test_abort_restore_returns_to_idle(session_factory):
    app = _backup_app(session_factory)
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, b"partial"))
    assert isinstance(await _reinit(app, "abortRestore"), SimpleAckPDU)
    assert app.backup_restore.state == "idle"
    assert app.backup_restore.size == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup, state",
    [([], "endBackup"), ([], "endRestore"), ([], "abortRestore"), (["startRestore"], "startBackup"), (["startBackup"], "startRestore")],
)
async def test_wrong_session_state_is_configuration_in_progress(session_factory, setup, state):
    app = _backup_app(session_factory)
    for step in setup:
        await _reinit(app, step)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice=state))
    assert str(excinfo.value.errorCode) == "configurationInProgress"


@pytest.mark.asyncio
async def test_warmstart_ends_an_open_session(session_factory):
    app = _backup_app(session_factory)
    await _reinit(app, "startBackup")
    assert isinstance(await _reinit(app, "warmstart"), SimpleAckPDU)
    assert app.backup_restore.state == "idle"


# --- the config read and the restore run off the device's loop ---------------

def _thread_spy(monkeypatch, name, seen):
    real = getattr(application, name)

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(application, name, spy)


@pytest.mark.asyncio
async def test_a_backup_exports_off_the_loop(session_factory, monkeypatch):
    # On a 5,000-object device the export blocks the loop 0.2-0.4 s.
    app = _backup_app(session_factory)
    seen = []
    _thread_spy(monkeypatch, "export_device_backup", seen)

    assert isinstance(await _reinit(app, "startBackup"), SimpleAckPDU)
    assert len(seen) == 1 and seen[0] != threading.get_ident()
    with session_factory() as session:
        assert await _read_all(app) == export_device_backup(session.get(Device, 599001))
    assert app.backup_restore._timer is not None  # Backup_Failure_Timeout runs on the loop


@pytest.mark.asyncio
async def test_a_restore_applies_off_the_loop(session_factory, monkeypatch):
    # On a 5,000-object device the apply blocks the loop 2.4 s.
    app = _backup_app(session_factory)
    with session_factory() as session:
        payload = export_device_backup(session.get(Device, 599001))
    seen = []
    _thread_spy(monkeypatch, "apply_device_restore", seen)
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, payload))

    assert isinstance(await _reinit(app, "endRestore"), SimpleAckPDU)
    assert len(seen) == 1 and seen[0] != threading.get_ident()
    with session_factory() as session:
        assert session.get(Device, 599001).runtime.process_status == "reinitializing"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_ok", [True, False], ids=["applied", "rejected"])
async def test_reloads_are_held_only_while_a_restore_applies(session_factory, monkeypatch, payload_ok):
    app = _backup_app(session_factory)
    with session_factory() as session:
        payload = export_device_backup(session.get(Device, 599001)) if payload_ok else b"{not json"
    held = []
    real = application.apply_device_restore

    def spy(*args, **kwargs):
        held.append(app.applying_restore)
        return real(*args, **kwargs)

    monkeypatch.setattr(application, "apply_device_restore", spy)
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, payload))
    assert app.applying_restore is False

    if payload_ok:
        await _reinit(app, "endRestore")
    else:
        with pytest.raises(ExecutionError):
            await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="endRestore"))
    assert held == [True]
    assert app.applying_restore is False


@pytest.mark.asyncio
async def test_a_reinitialize_waits_for_one_still_in_progress(session_factory, monkeypatch):
    # The loop is free while startBackup reads, so a second request can
    # arrive meanwhile: it waits, then sees the backup the first one started.
    app = _backup_app(session_factory)
    started, release = threading.Event(), threading.Event()
    real = application.export_device_backup
    exports = []

    def slow_export(device):
        exports.append(1)
        started.set()
        release.wait(5)
        return real(device)

    monkeypatch.setattr(application, "export_device_backup", slow_export)
    responses = []

    async def _capture(resp):
        responses.append(resp)

    app.response = _capture
    first = asyncio.ensure_future(
        app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="startBackup"))
    )
    await asyncio.to_thread(started.wait, 5)
    second = asyncio.ensure_future(
        app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="startBackup"))
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await first
    with pytest.raises(ExecutionError) as excinfo:
        await second
    assert str(excinfo.value.errorCode) == "configurationInProgress"
    assert exports == [1]  # refused before reading
    assert len(responses) == 1 and isinstance(responses[0], SimpleAckPDU)
    assert app.backup_restore.state == "performingABackup"


@pytest.mark.asyncio
async def test_atomic_write_outside_a_restore_is_denied(session_factory):
    app = _backup_app(session_factory)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_AtomicWriteFileRequest(_write_request(0, b"x"))
    assert str(excinfo.value.errorCode) == "fileAccessDenied"


@pytest.mark.asyncio
async def test_atomic_file_services_reject_other_files(session_factory):
    app = _backup_app(session_factory)
    await _reinit(app, "startRestore")
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_AtomicWriteFileRequest(_write_request(0, b"x", file_id=("file", 2)))
    assert str(excinfo.value.errorCode) == "unknownObject"


@pytest.mark.asyncio
async def test_atomic_read_record_access_is_rejected(session_factory):
    app = _backup_app(session_factory)
    req = AtomicReadFileRequest(
        fileIdentifier=("file", 1),
        accessMethod=AtomicReadFileRequestAccessMethodChoice(
            recordAccess=AtomicReadFileRequestAccessMethodChoiceRecordAccess(
                fileStartRecord=0, requestedRecordCount=1
            )
        ),
    )
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_AtomicReadFileRequest(req)
    assert str(excinfo.value.errorCode) == "invalidFileAccessMethod"


@pytest.mark.asyncio
async def test_atomic_services_are_dropped_while_communication_is_disabled(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    app = _backup_app(session_factory)
    await _reinit(app, "startRestore")
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    sent = []

    async def _capture(resp):
        sent.append(resp)

    app.response = _capture
    await app.indication(_write_request(-1, b"x"))
    assert sent == []
    assert app.backup_restore.size == 0


# --- AddListElement / RemoveListElement (AE-CRL-B) ---------------------------
# A failure must come back
# as a ChangeListError (errorType + firstFailedElementNumber), which the
# generic ExecutionError path can't build.

from bacpypes3.apdu import AddListElementRequest, ChangeListError, RemoveListElementRequest
from bacpypes3.basetypes import Destination, DeviceAddress, Recipient
from bacpypes3.constructeddata import ListOf
from bacpypes3.primitivedata import Time, Unsigned


def _dest(address="192.168.1.77", pid=5, confirmed=False, to_time="23:59:59.99"):
    return Destination(
        validDays=[1] * 7,
        fromTime=Time("00:00:00.00"),
        toTime=Time(to_time),
        recipient=Recipient(address=DeviceAddress(address)),
        processIdentifier=pid,
        issueConfirmedNotifications=confirmed,
        transitions=[1, 1, 1],
    )


def _list_request(cls, elements, obj=("notification-class", 1), prop="recipientList", index=None, element_type=Destination):
    req = cls(objectIdentifier=obj, propertyIdentifier=prop, listOfElements=ListOf(element_type)(elements))
    if index is not None:
        req.propertyArrayIndex = index
    return req


def _change_list_error(resp):
    assert isinstance(resp, ChangeListError), resp
    return resp.errorType.errorClass.attr, resp.errorType.errorCode.attr, int(resp.firstFailedElementNumber)


@pytest.mark.asyncio
async def test_add_list_element_adds_a_recipient():
    app = _alarming_app()
    resp = await _send(app, _list_request(AddListElementRequest, [_dest()]))
    assert isinstance(resp, SimpleAckPDU)
    nc = app.get_object_name("NC-1")
    assert [int(d.processIdentifier) for d in nc.recipientList] == [5]


@pytest.mark.asyncio
async def test_remove_list_element_removes_a_recipient():
    app = _alarming_app()
    await _send(app, _list_request(AddListElementRequest, [_dest(), _dest(pid=6)]))
    resp = await _send(app, _list_request(RemoveListElementRequest, [_dest(pid=5)]))
    assert isinstance(resp, SimpleAckPDU)
    assert [int(d.processIdentifier) for d in app.get_object_name("NC-1").recipientList] == [6]


@pytest.mark.asyncio
async def test_add_list_element_with_no_elements_changes_nothing():
    app = _alarming_app()
    resp = await _send(app, _list_request(AddListElementRequest, []))
    assert isinstance(resp, SimpleAckPDU)
    assert list(app.get_object_name("NC-1").recipientList) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "req, expected",
    [
        (lambda: _list_request(AddListElementRequest, [_dest()], obj=("notification-class", 99)), ("object", "unknownObject", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest()], obj=("analog-input", 1), prop="recipientList"), ("property", "unknownProperty", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest()], obj=("analog-input", 1), prop="presentValue"), ("services", "propertyIsNotAList", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest()], prop="priority"), ("services", "propertyIsNotAList", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest()], obj=("device", 599001), prop="deviceAddressBinding"), ("property", "writeAccessDenied", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest()], index=1), ("property", "propertyIsNotAnArray", 0)),
        (lambda: _list_request(AddListElementRequest, [1, 2], element_type=Unsigned), ("property", "invalidDataType", 0)),
        (lambda: _list_request(AddListElementRequest, [_dest(), _dest(pid=6, to_time="*:*:*.*")]), ("property", "valueOutOfRange", 2)),
        (lambda: _list_request(RemoveListElementRequest, [_dest(pid=9)]), ("services", "listElementNotFound", 1)),
    ],
)
async def test_list_element_failures_send_a_change_list_error(req, expected):
    app = _alarming_app()
    resp = await _send(app, req())
    assert _change_list_error(resp) == expected
    assert list(app.get_object_name("NC-1").recipientList) == []


@pytest.mark.asyncio
async def test_change_list_error_encodes():
    app = _alarming_app()
    resp = await _send(app, _list_request(RemoveListElementRequest, [_dest(pid=9)]))
    assert resp.encode() is not None


@pytest.mark.asyncio
async def test_list_element_requests_are_traffic_logged():
    app = _alarming_app()
    await _send(app, _list_request(AddListElementRequest, [_dest()]))
    await _send(app, _list_request(RemoveListElementRequest, [_dest()]))
    kinds = [entry["event_type"] for entry in app.traffic_log.recent()]
    assert "add-list-element" in kinds and "remove-list-element" in kinds


@pytest.mark.asyncio
async def test_alarm_after_add_list_element_reaches_the_new_recipient():
    import asyncio

    app = _alarming_app()
    sent = []

    def _capture(apdu):
        sent.append(apdu)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    app.request = _capture
    resp = await _send(app, _list_request(AddListElementRequest, [_dest("192.168.1.77", confirmed=False)]))
    assert isinstance(resp, SimpleAckPDU)

    app.get_object_name("AI-1").presentValue = 150.0  # above highLimit
    for _ in range(50):
        await asyncio.sleep(0)
        if sent:
            break
    assert len(sent) == 1
    assert type(sent[0]).__name__ == "UnconfirmedEventNotificationRequest"
    assert bytes(sent[0].pduDestination.addrAddr) == bytes(DeviceAddress("192.168.1.77").macAddress)
    assert int(sent[0].processIdentifier) == 5


@pytest.mark.asyncio
async def test_global_broadcast_recipient_is_refused_as_a_change_list_error():
    # Network 65535 with no MAC is a legal recipient, but BACpack's stored
    # address form can't hold it (DeviceAddress won't parse it back), so it
    # must be refused at its position -- not escape as a plain Error.
    global_broadcast = Destination(
        validDays=[1] * 7,
        fromTime=Time("00:00:00.00"),
        toTime=Time("23:59:59.99"),
        recipient=Recipient(address=DeviceAddress(networkNumber=65535, macAddress=b"")),
        processIdentifier=1,
        issueConfirmedNotifications=False,
        transitions=[1, 1, 1],
    )
    app = _alarming_app()
    resp = await _send(app, _list_request(AddListElementRequest, [_dest(), global_broadcast]))
    assert _change_list_error(resp) == ("property", "valueOutOfRange", 2)
    assert list(app.get_object_name("NC-1").recipientList) == []


@pytest.mark.asyncio
async def test_unexpected_list_element_failure_is_still_a_change_list_error(caplog, monkeypatch):
    app = _alarming_app()
    nc = app.get_object_name("NC-1")

    def _boom(self, destinations):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(nc), "add_recipients", _boom)
    with caplog.at_level(logging.ERROR, logger="bacpack"):
        resp = await _send(app, _list_request(AddListElementRequest, [_dest()]))
    assert _change_list_error(resp) == ("device", "operationalProblem", 0)
    assert any(r.exc_info for r in caplog.records if r.name.startswith("bacpack"))


# --- WriteProperty / WPM error codes ---------------------------------------
# An array index on Present_Value was
# acked, priority 0/17 got write-access-denied/invalid-array-index, an
# unknown property was operational-problem, and a wrong datatype (or a Null
# with no priority) a Reject invalid-tag.

_AI = ("analog-input", 1)
_AO = ("analog-output", 1)
_BV = ("binary-value", 1)
_MSV = ("multi-state-value", 1)


def _wp(object_identifier, prop, value, *, index=None, priority=None):
    req = WritePropertyRequest(
        objectIdentifier=object_identifier, propertyIdentifier=prop, propertyValue=Any(value)
    )
    if index is not None:
        req.propertyArrayIndex = index
    if priority is not None:
        req.priority = priority
    return req


_BAD_WRITES = [
    (_AO, "presentValue", Real(5.0), 1, None, ("property", "propertyIsNotAnArray")),
    (_AO, "presentValue", Real(5.0), 0, None, ("property", "propertyIsNotAnArray")),
    (_AO, "presentValue", Real(5.0), None, 0, ("services", "parameterOutOfRange")),
    (_AO, "presentValue", Real(5.0), None, 17, ("services", "parameterOutOfRange")),
    (_AO, "vendorName", CharacterString("x"), None, None, ("property", "unknownProperty")),
    (_AO, "presentValue", CharacterString("hi"), None, None, ("property", "invalidDataType")),
    (_AO, "presentValue", Unsigned(3), None, 8, ("property", "invalidDataType")),
    (_BV, "presentValue", Real(1.0), None, None, ("property", "invalidDataType")),
    (_AO, "outOfService", CharacterString("x"), None, None, ("property", "invalidDataType")),
    # A Null with a priority to a property with no priority array: it reached
    # write_property and became a TypeError / RuntimeError, so device /
    # operational-problem. An analog input: a multi-state value has a
    # priority array.
    (_AI, "presentValue", Null(()), None, 8, ("property", "invalidDataType")),
    (_AO, "outOfService", Null(()), None, 8, ("property", "invalidDataType")),
    (_MSV, "stateText", Null(()), 2, 8, ("property", "invalidDataType")),
]
# Explicit ids: pytest can't build one from a CharacterString (a str
# subclass whose encode() takes no arguments).
_BAD_WRITE_IDS = [
    f"{oid[0]}-{prop}-{type(value).__name__}-index{index}-priority{priority}"
    for oid, prop, value, index, priority, _ in _BAD_WRITES
]


def _off_the_wire(req):
    """`req` encoded to bytes and decoded back, as the handler gets it from a
    real client: identifiers as enums, the value an encoded Any rather than
    the Python object it was built from."""
    apdu = req.encode()
    apdu.apduSeg = apdu.apduMor = apdu.apduSA = False
    apdu.apduMaxSegs, apdu.apduMaxResp, apdu.apduInvokeID = 0, 5, 1
    return APCISequence.decode(APDU.decode(PDU(apdu.encode().pduData)))


def _maybe_wire(req, wire):
    return _off_the_wire(req) if wire else req


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [False, True], ids=["built", "wire"])
@pytest.mark.parametrize("oid, prop, value, index, priority, expected", _BAD_WRITES, ids=_BAD_WRITE_IDS)
async def test_write_property_edge_cases_get_the_spec_error(oid, prop, value, index, priority, expected, wire):
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_WritePropertyRequest(_maybe_wire(_wp(oid, prop, value, index=index, priority=priority), wire))
    assert (excinfo.value.errorClass, excinfo.value.errorCode) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [False, True], ids=["built", "wire"])
@pytest.mark.parametrize("oid, prop, value, index, priority, expected", _BAD_WRITES, ids=_BAD_WRITE_IDS)
async def test_wpm_edge_cases_get_the_spec_error(oid, prop, value, index, priority, expected, wire):
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    prop_value = PropertyValue(propertyIdentifier=prop, value=Any(value))
    if index is not None:
        prop_value.propertyArrayIndex = index
    if priority is not None:
        prop_value.priority = priority
    resp = await _write_property_multiple(
        app, [WriteAccessSpecification(objectIdentifier=oid, listOfProperties=[prop_value])], wire=wire
    )
    assert isinstance(resp, WritePropertyMultipleError)
    assert (resp.errorType.errorClass.attr, resp.errorType.errorCode.attr) == expected
    assert resp.firstFailedWriteAttempt.propertyIdentifier.attr == prop


def _slot(obj, priority):
    pv = obj.priorityArray[priority - 1]
    return None if pv.null is not None else getattr(pv, pv._choice)


@pytest.mark.asyncio
async def test_null_without_priority_relinquishes_priority_16():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")
    await _send(app, _wp(_AO, "presentValue", Real(30.0), priority=16))
    await _send(app, _wp(_AO, "presentValue", Real(55.0), priority=8))

    resp = await _send(app, _wp(_AO, "presentValue", Null(())))

    assert isinstance(resp, SimpleAckPDU)
    assert _slot(ao, 16) is None
    assert _slot(ao, 8) == 55.0


@pytest.mark.asyncio
async def test_null_without_priority_off_the_wire_relinquishes_priority_16():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")
    await _send(app, _off_the_wire(_wp(_AO, "presentValue", Real(30.0), priority=16)))

    resp = await _send(app, _off_the_wire(_wp(_AO, "presentValue", Null(()))))

    assert isinstance(resp, SimpleAckPDU)
    assert _slot(ao, 16) is None


@pytest.mark.asyncio
async def test_wpm_null_without_priority_relinquishes_priority_16():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")
    await _send(app, _wp(_AO, "presentValue", Real(30.0), priority=16))

    resp = await _write_property_multiple(
        app,
        [
            WriteAccessSpecification(
                objectIdentifier=_AO,
                listOfProperties=[PropertyValue(propertyIdentifier="presentValue", value=Any(Null(())))],
            )
        ],
    )

    assert isinstance(resp, SimpleAckPDU)
    assert _slot(ao, 16) is None


@pytest.mark.asyncio
async def test_wpm_bad_datatype_keeps_earlier_writes():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    ao = app.get_object_name("AO-1")
    resp = await _write_property_multiple(
        app,
        [
            WriteAccessSpecification(
                objectIdentifier=_AO,
                listOfProperties=[
                    PropertyValue(propertyIdentifier="presentValue", value=Any(Real(20.0)), priority=8),
                    PropertyValue(propertyIdentifier="presentValue", value=Any(CharacterString("hi")), priority=9),
                ],
            )
        ],
    )
    assert isinstance(resp, WritePropertyMultipleError)
    assert resp.errorType.errorCode.attr == "invalidDataType"
    assert ao.presentValue == 20.0  # the write before the failure stands


@pytest.mark.asyncio
async def test_indexed_write_to_a_real_array_still_works():
    # State_Text is a real BACnetARRAY: an indexed write must not be refused
    # as property-is-not-an-array.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    msv = app.get_object_name("MSV-1")
    resp = await _send(app, _wp(("multi-state-value", 1), "stateText", CharacterString("Medium"), index=2))
    assert isinstance(resp, SimpleAckPDU)
    assert str(msv.stateText[1]) == "Medium"

    resp = await _send(app, _off_the_wire(_wp(("multi-state-value", 1), "stateText", CharacterString("High"), index=3)))
    assert isinstance(resp, SimpleAckPDU)
    assert str(msv.stateText[2]) == "High"


@pytest.mark.asyncio
async def test_multistate_value_takes_a_priority_write_and_a_null_relinquish_off_the_wire():
    # MSV is commandable, so a Null at a priority is a relinquish.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    msv = app.get_object_name("MSV-1")
    resp = await _send(app, _off_the_wire(_wp(_MSV, "presentValue", Unsigned(3), priority=8)))
    assert isinstance(resp, SimpleAckPDU)
    assert msv.presentValue == 3
    resp = await _send(app, _off_the_wire(_wp(_MSV, "presentValue", Null(()), priority=8)))
    assert isinstance(resp, SimpleAckPDU)
    assert msv.presentValue == 1


@pytest.mark.asyncio
async def test_a_null_relinquish_default_write_to_a_multistate_value_is_refused_off_the_wire():
    # Verified by hand in a live check.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    msv = app.get_object_name("MSV-1")
    before = msv.relinquishDefault
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_WritePropertyRequest(_off_the_wire(_wp(_MSV, "relinquishDefault", Null(()))))
    # Off the wire a Null doesn't decode as the Unsigned Relinquish_Default
    # takes (in-process it's value-out-of-range: test_relinquish_default.py).
    assert (str(excinfo.value.errorClass), str(excinfo.value.errorCode)) == ("property", "invalidDataType")
    assert msv.relinquishDefault == before and msv.presentValue == 1


@pytest.mark.asyncio
async def test_a_value_that_decodes_to_an_incomplete_sequence_is_invalid_data_type():
    # A Real written to a Schedule's Effective_Period
    # made cast_out raise AttributeError (a required DateRange element
    # missing), which became device / operational-problem.
    device, objects = _device_and_objects()
    objects.append(
        BacnetObject(
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            schedule_priority=8,
        )
    )
    app = build_application(device, objects)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_WritePropertyRequest(_wp(("schedule", 1), "effectivePeriod", Real(1.0)))
    assert (excinfo.value.errorClass, excinfo.value.errorCode) == ("property", "invalidDataType")


# --- YABE backup/restore pass follow-ups ------------------------------------


async def _restore(app, payload):
    await _reinit(app, "startRestore")
    await _send(app, _write_request(-1, payload))
    return await _reinit(app, "endRestore")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [False, True], ids=["identical", "changed"])
async def test_a_restore_bumps_database_revision_by_exactly_one(session_factory, change):
    # 135 says Database_Revision goes up whenever a restore is performed;
    # the flush listener alone missed an identical restore.
    app = _backup_app(session_factory)
    with session_factory() as session:
        device = session.get(Device, 599001)
        before = device.database_revision
        doc = json.loads(export_device_backup(device))
    if change:
        objects = doc["devices"][0]["objects"]
        doc["devices"][0]["objects"] = [o for o in objects if o["name"] != "BV-1"]
        doc["devices"][0]["objects"][0]["name"] = "Renamed"
    assert isinstance(await _restore(app, json.dumps(doc).encode()), SimpleAckPDU)
    with session_factory() as session:
        assert session.get(Device, 599001).database_revision == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup, state, detail",
    [
        ([], "endBackup", "endBackup refused: no backup is in progress"),
        ([], "endRestore", "endRestore refused: no restore is in progress"),
        (["startBackup"], "startRestore", "startRestore refused: a backup is still in progress"),
        (["startBackup"], "startBackup", "startBackup refused: a backup is still in progress"),
        (["startRestore"], "endBackup", "endBackup refused: a restore is still in progress"),
        ([], "activateChanges", "activateChanges refused: this device doesn't support that reinitialize state"),
    ],
)
async def test_a_refused_reinitialize_is_logged_with_its_reason(session_factory, caplog, setup, state, detail):
    app = _backup_app(session_factory)
    for step in setup:
        await _reinit(app, step)
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        with pytest.raises(ExecutionError):
            await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice=state))
    entry = app.traffic_log.recent()[0]
    assert entry["event_type"] == "reinitialize-refused"
    assert entry["detail"].endswith(detail)
    assert any(r.name.startswith("bacpack") and detail in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_fn, detail",
    [
        (lambda: _write_request(0, b"x"), "AtomicWriteFile refused: no restore is in progress"),
        (
            lambda: _write_request(0, b"x", file_id=("file", 2)),
            "AtomicWriteFile refused: file,2 is not the config file (only file,1 is)",
        ),
        (lambda: _read_request(5, 10), "AtomicReadFile refused: start position 5 is outside the file (0 octets)"),
    ],
)
async def test_a_refused_atomic_file_request_is_logged_with_its_reason(session_factory, request_fn, detail):
    app = _backup_app(session_factory)
    with pytest.raises(ExecutionError):
        req = request_fn()
        handler = app.do_AtomicWriteFileRequest if isinstance(req, AtomicWriteFileRequest) else app.do_AtomicReadFileRequest
        await handler(req)
    entry = app.traffic_log.recent()[0]
    assert entry["event_type"] == "atomic-file-refused"
    assert entry["detail"].endswith(detail)


@pytest.mark.asyncio
async def test_a_rejected_restore_file_is_not_also_logged_as_a_refusal(session_factory):
    # endRestore with a bad file already logs restore-rejected with the
    # parse error; one entry is enough.
    app = _backup_app(session_factory)
    with pytest.raises(ExecutionError):
        await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="startRestore"))
        await _send(app, _write_request(-1, b"{not json"))
        await app.do_ReinitializeDeviceRequest(ReinitializeDeviceRequest(reinitializedStateOfDevice="endRestore"))
    kinds = [entry["event_type"] for entry in app.traffic_log.recent()]
    assert "restore-rejected" in kinds
    assert "reinitialize-refused" not in kinds


# --- Property_List leaves out the four required properties -----------------
# ASHRAE 135: Property_List doesn't list Object_Identifier, Object_Name,
# Object_Type or Property_List itself. RPM ALL still returns them.

_NOT_LISTED = {"object-identifier", "object-name", "object-type", "property-list"}


@pytest.mark.asyncio
async def test_property_list_leaves_out_the_four_required_properties():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    checked = 0
    for obj in list(app.objectIdentifier.values()):
        resp = await _send(
            app, ReadPropertyRequest(objectIdentifier=obj.objectIdentifier, propertyIdentifier="propertyList")
        )
        assert isinstance(resp, ReadPropertyACK), obj.objectIdentifier
        listed = {str(p) for p in await obj.read_property("propertyList")}
        assert listed and not (listed & _NOT_LISTED), (obj.objectIdentifier, sorted(listed & _NOT_LISTED))
        checked += 1
    assert checked >= 5  # device, network port and the four fake objects at least


@pytest.mark.asyncio
async def test_read_property_multiple_all_still_returns_the_required_properties():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    resp = await _send(
        app,
        ReadPropertyMultipleRequest(
            listOfReadAccessSpecs=[
                ReadAccessSpecification(
                    objectIdentifier=("analog-input", 1),
                    listOfPropertyReferences=[PropertyReference(propertyIdentifier="all")],
                )
            ]
        ),
    )
    assert isinstance(resp, ReadPropertyMultipleACK)
    returned = {str(r.propertyIdentifier) for r in resp.listOfReadAccessResults[0].listOfResults}
    assert {"object-identifier", "object-name", "object-type", "present-value"} <= returned


@pytest.mark.asyncio
async def test_a_restore_that_lowers_a_sample_cap_trims_the_samples(session_factory):
    from bacpack.models import TrendSample, get_or_create_history_counters
    from bacpack.trending import record_sample

    app = _backup_app(session_factory)
    with session_factory() as session:
        av = session.query(BacnetObject).filter_by(device_id=599001, object_type="analog-output").first()
        av.trended = True
        session.commit()
        object_id = av.id
        for value in range(5):
            record_sample(session_factory, object_id, float(value))
        doc = json.loads(export_device_backup(session.get(Device, 599001)))
    entry = next(o for o in doc["devices"][0]["objects"] if o["name"] == av.name)
    entry["trend_sample_cap"] = 2

    assert isinstance(await _restore(app, json.dumps(doc).encode()), SimpleAckPDU)
    with session_factory() as session:
        values = [s.value for s in session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id)]
        assert values == [3.0, 4.0]
        assert get_or_create_history_counters(session).trend_total_sample_count == 2


def _saved_device(session_factory):
    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name))
        session.commit()
    return build_application(device, objects, trend_session_factory=session_factory), device


@pytest.mark.asyncio
async def test_reinitialize_saves_off_the_loop(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    app, device = _saved_device(session_factory)

    run = await run_while_locked(
        session_factory,
        lambda: _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart")),
    )

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(Device, device.id).runtime.process_status == "reinitializing"


@pytest.mark.asyncio
async def test_dcc_saves_off_the_loop(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    app, device = _saved_device(session_factory)

    async def disable_and_wait_for_the_save():
        await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
        assert app._dcc_state == "disable"  # enforced before the save lands
        await _saves_settled()

    run = await run_while_locked(session_factory, disable_and_wait_for_the_save)

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(Device, device.id).dcc_state == "disable"


@pytest.mark.asyncio
async def test_dcc_disable_then_enable_saves_enable(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    app, device = _saved_device(session_factory)
    opened = []
    first_committed = threading.Event()

    def slow_first_save():
        # Saves that weren't queued would let the quick second one commit
        # before this one, leaving "disable" in the row.
        session = session_factory()
        if not opened:
            opened.append(True)
            time.sleep(0.2)
            sqlalchemy.event.listen(session, "after_commit", lambda _session: first_committed.set())
        return session

    app.session_factory = slow_first_save
    app._stored_bacnet_password_hash = lambda: None  # this test counts the saves' sessions
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    await _send(app, DeviceCommunicationControlRequest(enableDisable="enable"))
    assert app._dcc_state == "enable"
    await _saves_settled()
    # Unqueued saves don't wait on each other: wait for the slow first one.
    assert await asyncio.to_thread(first_committed.wait, 5)

    with session_factory() as session:
        assert session.get(Device, device.id).dcc_state == "enable"


@pytest.mark.asyncio
async def test_a_time_sync_request_saves_off_the_loop(session_factory):
    from bacpypes3.apdu import UTCTimeSynchronizationRequest
    from bacpypes3.basetypes import DateTime
    from bacpypes3.primitivedata import Date, Time
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    app, device = _saved_device(session_factory)
    request = UTCTimeSynchronizationRequest(time=DateTime(date=Date((126, 9, 25, 5)), time=Time((14, 3, 12, 0))))

    run = await run_while_locked(session_factory, lambda: _send(app, request))

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(Device, device.id).clock_offset_seconds != 0


# --- The optional DCC / ReinitializeDevice password --------------------------


def _protected_app(session_factory, password="right-pw"):
    from bacpack.auth import hash_password

    device, objects = _device_and_objects()
    with session_factory() as session:
        session.add(Device(id=device.id, name=device.name, bacnet_password_hash=hash_password(password) if password else None))
        session.commit()
    return device, build_application(device, objects, trend_session_factory=session_factory)


def _password_failure(excinfo):
    return (str(excinfo.value.errorClass), str(excinfo.value.errorCode))


@pytest.mark.asyncio
async def test_dcc_without_a_device_password_ignores_a_supplied_one(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _, app = _protected_app(session_factory, password=None)
    resp = await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("whatever")))
    assert isinstance(resp, SimpleAckPDU)


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "wrong-pw", ""])
async def test_dcc_with_a_device_password_refuses_a_missing_or_wrong_one(session_factory, password):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _, app = _protected_app(session_factory)
    kwargs = {} if password is None else {"password": CharacterString(password)}
    with pytest.raises(ExecutionError) as excinfo:
        await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", **kwargs))
    assert _password_failure(excinfo) == ("security", "passwordFailure")
    assert app._dcc_state == "enable"


@pytest.mark.asyncio
async def test_dcc_with_the_right_password_acks_and_changes_state(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _, app = _protected_app(session_factory)
    resp = await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("right-pw")))
    assert isinstance(resp, SimpleAckPDU)
    assert app._dcc_state == "disable"


@pytest.mark.asyncio
async def test_reinitialize_refuses_a_wrong_password_and_does_not_mark_reinitializing(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    device, app = _protected_app(session_factory)
    with pytest.raises(ExecutionError) as excinfo:
        await _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart", password=CharacterString("wrong-pw")))
    assert _password_failure(excinfo) == ("security", "passwordFailure")
    with session_factory() as session:
        assert session.get(Device, device.id).runtime.process_status != "reinitializing"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["startBackup", "startRestore", "endBackup", "endRestore", "abortRestore", "warmstart"])
async def test_every_reinitialize_state_needs_the_password(session_factory, state):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    _, app = _protected_app(session_factory)
    with pytest.raises(ExecutionError) as excinfo:
        await _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice=state, password=CharacterString("wrong-pw")))
    assert _password_failure(excinfo) == ("security", "passwordFailure")
    assert app.backup_restore.state == "idle"


@pytest.mark.asyncio
async def test_reinitialize_with_the_right_password_acks(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    _, app = _protected_app(session_factory)
    resp = await _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart", password=CharacterString("right-pw")))
    assert isinstance(resp, SimpleAckPDU)


@pytest.mark.asyncio
async def test_a_password_changed_in_the_database_applies_to_the_next_request(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest
    from bacpack.auth import hash_password

    device, app = _protected_app(session_factory, password=None)
    assert isinstance(await _send(app, DeviceCommunicationControlRequest(enableDisable="enable")), SimpleAckPDU)
    with session_factory() as session:
        session.get(Device, device.id).bacnet_password_hash = hash_password("now-set")
        session.commit()
    with pytest.raises(ExecutionError):
        await _send(app, DeviceCommunicationControlRequest(enableDisable="enable"))


@pytest.mark.asyncio
async def test_a_refused_password_is_logged_without_the_password(session_factory, caplog):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _, app = _protected_app(session_factory)
    with caplog.at_level(logging.WARNING, logger="bacpack.security"):
        with pytest.raises(ExecutionError):
            await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("wrong-pw")))
    assert "DeviceCommunicationControl refused" in caplog.text
    assert "wrong-pw" not in caplog.text and "right-pw" not in caplog.text
    entries = [str(entry) for entry in app.traffic_log._entries]
    assert any("bacnet-password-failed" in entry or "DeviceCommunicationControl" in entry for entry in entries)
    assert not any("wrong-pw" in entry for entry in entries)


@pytest.mark.asyncio
async def test_password_failures_never_lock_the_device_out(session_factory):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    _, app = _protected_app(session_factory)
    for _ in range(6):
        with pytest.raises(ExecutionError):
            await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("wrong-pw")))
    resp = await _send(app, DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("right-pw")))
    assert isinstance(resp, SimpleAckPDU)


@pytest.mark.asyncio
async def test_password_checks_run_one_at_a_time_and_requests_keep_their_order(session_factory, monkeypatch):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    from bacpack import application as application_module

    _, app = _protected_app(session_factory)
    running = peak = calls = 0
    guard = threading.Lock()
    real = application_module.verify_password

    def slow_first(password, stored):
        nonlocal running, peak, calls
        with guard:
            calls += 1
            first = calls == 1
            running += 1
            peak = max(peak, running)
        time.sleep(0.2 if first else 0.0)
        try:
            return real(password, stored)
        finally:
            with guard:
                running -= 1

    monkeypatch.setattr(application_module, "verify_password", slow_first)
    disable = DeviceCommunicationControlRequest(enableDisable="disable", password=CharacterString("right-pw"))
    enable = DeviceCommunicationControlRequest(enableDisable="enable", password=CharacterString("right-pw"))

    first = asyncio.create_task(_send(app, disable))
    await asyncio.sleep(0.05)  # the first request is in its (slow) check
    second = asyncio.create_task(_send(app, enable))
    await asyncio.gather(first, second)

    assert peak == 1
    assert app._dcc_state == "enable"


@pytest.mark.asyncio
async def test_a_refused_password_leaves_the_reinitialize_lock_free(session_factory):
    from bacpypes3.apdu import ReinitializeDeviceRequest

    _, app = _protected_app(session_factory)
    with pytest.raises(ExecutionError):
        await _send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice="coldstart", password=CharacterString("wrong-pw")))
    assert app._reinitialize_lock is None or not app._reinitialize_lock.locked()


@pytest.mark.asyncio
async def test_an_i_am_is_recorded_in_the_device_info_cache():
    from bacpypes3.apdu import IAmRequest
    from bacpypes3.pdu import Address

    app = _alarming_app()
    i_am = IAmRequest(
        iAmDeviceIdentifier=("device", 599002),
        maxAPDULengthAccepted=1024,
        segmentationSupported="noSegmentation",
        vendorID=999,
    )
    i_am.pduSource = Address("127.0.0.2")
    await app.do_IAmRequest(i_am)
    assert (await app.device_info_cache.get_device_info(599002)).address == Address("127.0.0.2")


@pytest.mark.asyncio
async def test_an_i_am_without_a_source_is_not_recorded():
    from bacpypes3.apdu import IAmRequest

    app = _alarming_app()
    await app.do_IAmRequest(
        IAmRequest(
            iAmDeviceIdentifier=("device", 599002),
            maxAPDULengthAccepted=1024,
            segmentationSupported="noSegmentation",
            vendorID=999,
        )
    )
    assert await app.device_info_cache.get_device_info(599002) is None


# --- DCC suppresses every initiated request, COV notifications included -----


def _cov_notification(confirmed: bool):
    from bacpypes3.apdu import ConfirmedCOVNotificationRequest, UnconfirmedCOVNotificationRequest
    from bacpypes3.pdu import Address

    cls = ConfirmedCOVNotificationRequest if confirmed else UnconfirmedCOVNotificationRequest
    apdu = cls(
        subscriberProcessIdentifier=1,
        initiatingDeviceIdentifier=("device", 599001),
        monitoredObjectIdentifier=("analog-input", 1),
        timeRemaining=0,
        listOfValues=[],
    )
    apdu.pduDestination = Address("192.168.1.77")
    return apdu


@pytest.fixture
def base_requests(monkeypatch):
    """What reaches bacpypes3's own Application.request."""
    import bacpypes3.app

    seen = []

    def recorder(self, apdu):
        seen.append(apdu)
        future = asyncio.get_running_loop().create_future()
        future.set_result("sent")
        return future

    monkeypatch.setattr(bacpypes3.app.Application, "request", recorder)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["disable", "disableInitiation"])
@pytest.mark.parametrize("confirmed", [True, False])
async def test_dcc_suppresses_cov_notifications(base_requests, state, confirmed):
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    app = _alarming_app()
    await _send(app, DeviceCommunicationControlRequest(enableDisable=state))
    future = app.request(_cov_notification(confirmed))
    assert future.done() and future.result() is None
    assert base_requests == []


@pytest.mark.asyncio
async def test_dcc_disable_initiation_still_sends_an_i_am(base_requests):
    from bacpypes3.apdu import DeviceCommunicationControlRequest, IAmRequest

    def i_am():
        return IAmRequest(
            iAmDeviceIdentifier=("device", 599001), maxAPDULengthAccepted=1024,
            segmentationSupported="noSegmentation", vendorID=999,
        )

    app = _alarming_app()
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disableInitiation"))
    app.request(i_am())
    assert len(base_requests) == 1
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disable"))
    app.request(i_am())
    assert len(base_requests) == 1


@pytest.mark.asyncio
async def test_enable_passes_cov_notifications_through(base_requests):
    app = _alarming_app()
    app.request(_cov_notification(False))
    assert len(base_requests) == 1


@pytest.mark.asyncio
async def test_a_value_change_sends_no_cov_notification_under_dcc(base_requests):
    from bacpypes3.apdu import DeviceCommunicationControlRequest, SubscribeCOVRequest
    from bacpypes3.pdu import Address

    app = _alarming_app()
    subscribe = SubscribeCOVRequest(
        subscriberProcessIdentifier=1,
        monitoredObjectIdentifier=("analog-input", 1),
        issueConfirmedNotifications=False,
        lifetime=60,
    )
    subscribe.pduSource = Address("192.168.1.99")
    await _send(app, subscribe)
    await asyncio.sleep(0)
    base_requests.clear()
    ai = app.get_object_name("AI-1")

    def cov_entries():
        return [e for e in app.traffic_log.recent() if e["event_type"] == "cov-notification"]

    logged = len(cov_entries())
    await _send(app, DeviceCommunicationControlRequest(enableDisable="disableInitiation"))
    ai.presentValue = 50.0
    for _ in range(10):
        await asyncio.sleep(0)
    assert [a for a in base_requests if "COV" in type(a).__name__] == []
    assert len(cov_entries()) == logged

    await _send(app, DeviceCommunicationControlRequest(enableDisable="enable"))
    ai.presentValue = 51.0
    for _ in range(10):
        await asyncio.sleep(0)
    assert [a for a in base_requests if "COV" in type(a).__name__]


@pytest.mark.asyncio
async def test_initiation_suppressed_follows_the_dcc_state():
    from bacpypes3.apdu import DeviceCommunicationControlRequest

    app = _alarming_app()
    seen = [app.initiation_suppressed]
    for state in ("disable", "disableInitiation", "enable"):
        await _send(app, DeviceCommunicationControlRequest(enableDisable=state))
        seen.append(app.initiation_suppressed)
    assert seen == [False, True, True, False]


# --- AcknowledgeAlarm is checked against the transition ---------------------


async def _alarm(app, value=150.0):
    ai = app.get_object_name("AI-1")
    ai.presentValue = value
    for _ in range(5):
        await asyncio.sleep(0)
    return ai


def _ack(ai, *, time_stamp=None, state=None, group=0):
    return AcknowledgeAlarmRequest(
        acknowledgingProcessIdentifier=1,
        eventObjectIdentifier=ai.objectIdentifier,
        eventStateAcknowledged=state or ai.eventState.attr,
        timeStamp=time_stamp if time_stamp is not None else ai.eventTimeStamps[group],
        acknowledgmentSource="test",
        timeOfAcknowledgment=TimeStamp(sequenceNumber=0),
    )


def _refusal(exc_info):
    return exc_info.value.errorClass, exc_info.value.errorCode


def _stamp_a_day_earlier(stamp):
    from bacpypes3.basetypes import DateTime
    from bacpypes3.primitivedata import Date, Time

    year, month, day, _dow = tuple(stamp.dateTime.date)
    return TimeStamp(dateTime=DateTime(date=Date((year, month, max(day - 1, 1), 255)), time=Time(tuple(stamp.dateTime.time))))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sequence", "wildcard", "other-day"])
async def test_an_ack_with_a_time_stamp_that_does_not_match_is_refused(kind):
    from bacpack.objects.alarm_config import unset_time_stamp

    app = _alarming_app()
    ai = await _alarm(app)
    given = {
        "sequence": TimeStamp(sequenceNumber=0),
        "wildcard": unset_time_stamp(),
        "other-day": _stamp_a_day_earlier(ai.eventTimeStamps[0]),
    }[kind]
    started = []
    app._send_ack_notification = lambda *a: started.append(a)
    with pytest.raises(ExecutionError) as raised:
        await _send(app, _ack(ai, time_stamp=given))
    assert _refusal(raised) == ("services", "invalidTimeStamp")
    assert list(ai.ackedTransitions) == [False, True, True]
    assert started == []
    assert "acknowledge-alarm-refused" in [e["event_type"] for e in app.traffic_log.recent()]


@pytest.mark.asyncio
async def test_an_ack_whose_time_stamp_differs_only_in_day_of_week_is_accepted():
    from bacpypes3.basetypes import DateTime
    from bacpypes3.primitivedata import Date, Time

    app = _alarming_app()
    ai = await _alarm(app)
    stored = ai.eventTimeStamps[0].dateTime
    year, month, day, _dow = tuple(stored.date)
    given = TimeStamp(dateTime=DateTime(date=Date((year, month, day, 255)), time=Time(tuple(stored.time))))
    resp = await _send(app, _ack(ai, time_stamp=given))
    assert type(resp).__name__ == "SimpleAckPDU"
    assert list(ai.ackedTransitions)[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["unset", "real"])
async def test_an_ack_for_a_transition_that_never_happened_is_refused(given):
    from bacpack.objects.alarm_config import unset_time_stamp

    app = _alarming_app()
    ai = app.get_object_name("AI-1")
    stamp = unset_time_stamp() if given == "unset" else TimeStamp.as_dateTime()
    with pytest.raises(ExecutionError) as raised:
        await _send(app, _ack(ai, time_stamp=stamp, state="highLimit"))
    assert _refusal(raised) == ("services", "invalidTimeStamp")
    assert list(ai.ackedTransitions) == [True, True, True]


@pytest.mark.asyncio
async def test_a_stale_ack_cannot_silence_a_recurrence():
    from bacpypes3.basetypes import TimeStamp as TS
    from bacpack import device_clock

    device_clock.install()
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(
        device,
        [
            BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"),
            BacnetObject(
                object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
                alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0,
                alarm_low_limit=0.0, alarm_deadband=1.0, alarm_notify_type="alarm",
            ),
        ],
    )
    ai = await _alarm(app)
    stale = TS(dateTime=ai.eventTimeStamps[0].dateTime)
    await _alarm(app, 50.0)
    device_clock.set_offset(device_clock.offset() + 60)
    await _alarm(app, 150.0)
    assert ai.eventState.attr == "highLimit"
    with pytest.raises(ExecutionError) as raised:
        await _send(app, _ack(ai, time_stamp=stale))
    assert _refusal(raised) == ("services", "invalidTimeStamp")
    assert list(ai.ackedTransitions)[0] is False
    resp = await _send(app, _ack(ai))
    assert type(resp).__name__ == "SimpleAckPDU"


@pytest.mark.asyncio
async def test_an_ack_naming_the_wrong_event_state_is_refused():
    app = _alarming_app()
    ai = await _alarm(app)
    assert ai.eventState.attr == "highLimit"
    with pytest.raises(ExecutionError) as raised:
        await _send(app, _ack(ai, state="lowLimit"))
    assert _refusal(raised) == ("services", "invalidEventState")
    assert list(ai.ackedTransitions)[0] is False
    resp = await _send(app, _ack(ai, state="highLimit"))
    assert type(resp).__name__ == "SimpleAckPDU"


@pytest.mark.asyncio
async def test_the_time_stamp_is_checked_before_the_event_state():
    app = _alarming_app()
    ai = await _alarm(app)
    with pytest.raises(ExecutionError) as raised:
        await _send(app, _ack(ai, time_stamp=TimeStamp(sequenceNumber=0), state="lowLimit"))
    assert _refusal(raised) == ("services", "invalidTimeStamp")
