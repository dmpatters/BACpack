"""Writable alarm configuration over BACnet. Objects are built from saved rows with a
real session factory, the way a running device builds them, so every write
can be checked against both the live object and the DB."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.apdu import (
    GetEventInformationRequest,
    ReadPropertyACK,
    ReadPropertyRequest,
    SimpleAckPDU,
    WritePropertyMultipleError,
    WritePropertyMultipleRequest,
    WritePropertyRequest,
)
from bacpypes3.basetypes import PropertyValue, WriteAccessSpecification
from bacpypes3.constructeddata import Any
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import BitString

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked


def _typed(obj, attr, value):
    element = obj._elements[attr]
    return element(element.cast(value))


def _analog_input(**overrides):
    fields = dict(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
    )
    fields.update(overrides)
    return BacnetObject(**fields)


async def _build(session_factory, *rows):
    """Save a device with two notification classes (NC-2 has distinct
    priorities) plus `rows`, then build the device the way a running
    device process does."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        session.add(BacnetObject(device=device, object_type="notification-class", instance_number=1, name="NC-1"))
        session.add(
            BacnetObject(
                device=device,
                object_type="notification-class",
                instance_number=2,
                name="NC-2",
                notification_priority=[10, 20, 30],
            )
        )
        for row in rows:
            row.device = device
            session.add(row)
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)  # Object._post_init links each object to its notification class
    return app


def _column(session_factory, name, column):
    with session_factory() as session:
        return getattr(session.query(BacnetObject).filter_by(name=name).one(), column)


async def _settle():
    # Parameter changes schedule the event algorithm with call_soon; a
    # transition then fires monitors of its own.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value, column, saved",
    [
        ("highLimit", 90.0, "alarm_high_limit", 90.0),
        ("lowLimit", 10.0, "alarm_low_limit", 10.0),
        ("deadband", 2.5, "alarm_deadband", 2.5),
        ("limitEnable", [False, True], "alarm_limit_enable", [False, True]),
        ("timeDelay", 30, "alarm_time_delay", 30),
        ("timeDelayNormal", 15, "alarm_time_delay_normal", 15),
        ("eventEnable", [True, False, True], "alarm_event_enable", [True, False, True]),
        ("notifyType", "event", "alarm_notify_type", "event"),
    ],
)
async def test_alarm_property_write_is_applied_and_saved(session_factory, attr, value, column, saved):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    await ai.write_property(attr, _typed(ai, attr, value))

    assert getattr(ai, attr) == _typed(ai, attr, value)
    assert _column(session_factory, "AI-1", column) == saved


@pytest.mark.asyncio
async def test_the_save_waits_for_a_held_lock_off_the_loop(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    run = await run_while_locked(session_factory, lambda: ai.write_property("highLimit", _typed(ai, "highLimit", 90.0)))

    assert_waited_off_the_loop(run)
    assert _column(session_factory, "AI-1", "alarm_high_limit") == 90.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value, column",
    [
        ("deadband", -1.0, "alarm_deadband"),
        ("limitEnable", [True, True, True], "alarm_limit_enable"),
        ("eventEnable", [True, True, True, True], "alarm_event_enable"),
    ],
)
async def test_bad_alarm_value_is_rejected_and_nothing_changes(session_factory, attr, value, column):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    before_live = getattr(ai, attr)
    before_saved = _column(session_factory, "AI-1", column)

    with pytest.raises(PropertyError) as exc:
        await ai.write_property(attr, _typed(ai, attr, value))

    assert exc.value.errorCode == "value-out-of-range"
    assert getattr(ai, attr) == before_live
    assert _column(session_factory, "AI-1", column) == before_saved


@pytest.mark.asyncio
async def test_enabling_a_side_with_no_limit_is_rejected(session_factory):
    # OutOfRangeEventAlgorithm compares against the limit, and a None limit
    # raises TypeError inside the event loop (alarming.md's Finding 3).
    app = await _build(session_factory, _analog_input(alarm_low_limit=None))
    ai = app.get_object_name("AI-1")

    with pytest.raises(PropertyError) as exc:
        await ai.write_property("limitEnable", _typed(ai, "limitEnable", [True, True]))

    assert exc.value.errorCode == "value-out-of-range"
    assert _column(session_factory, "AI-1", "alarm_limit_enable") is None


@pytest.mark.asyncio
async def test_indexed_alarm_write_is_rejected(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    with pytest.raises(PropertyError) as exc:
        await ai.write_property("limitEnable", _typed(ai, "limitEnable", [True, True]), index=1)

    assert exc.value.errorCode == "propertyIsNotAnArray"


@pytest.mark.asyncio
async def test_lowering_the_high_limit_below_the_value_alarms(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    ai.presentValue = 95.0
    await _settle()
    assert ai.eventState.attr == "normal"

    await ai.write_property("highLimit", _typed(ai, "highLimit", 90.0))
    await _settle()

    assert ai.eventState.attr == "highLimit"


@pytest.mark.asyncio
async def test_inverted_limits_are_accepted_without_crashing(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    ai.presentValue = 110.0
    await _settle()

    await ai.write_property("lowLimit", _typed(ai, "lowLimit", 120.0))
    await _settle()

    assert _column(session_factory, "AI-1", "alarm_low_limit") == 120.0
    assert ai.eventState.attr in ("highLimit", "lowLimit")


@pytest.mark.asyncio
async def test_limit_written_on_a_disabled_side_is_stored_but_stays_disabled(session_factory):
    app = await _build(session_factory, _analog_input(alarm_limit_enable=[False, True]))
    ai = app.get_object_name("AI-1")
    ai.presentValue = 50.0
    await _settle()

    await ai.write_property("lowLimit", _typed(ai, "lowLimit", 60.0))
    await _settle()

    assert _column(session_factory, "AI-1", "alarm_low_limit") == 60.0
    assert list(ai.limitEnable) == [False, True]
    assert ai.eventState.attr == "normal"


@pytest.mark.asyncio
async def test_written_value_survives_a_rebuild_from_the_db(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    await ai.write_property("highLimit", _typed(ai, "highLimit", 75.0))

    with session_factory() as session:
        device = session.get(Device, 599001)
        rebuilt = build_application(device, list(device.objects))
    assert rebuilt.get_object_name("AI-1").highLimit == 75.0


@pytest.mark.asyncio
async def test_write_with_a_deleted_row_is_still_acknowledged(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    with session_factory() as session:
        session.delete(session.query(BacnetObject).filter_by(name="AI-1").one())
        session.commit()

    await ai.write_property("highLimit", _typed(ai, "highLimit", 80.0))

    assert ai.highLimit == 80.0


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, value", [("objectName", "Renamed"), ("units", "degreesCelsius")])
async def test_non_alarm_properties_stay_denied_on_an_alarming_object(session_factory, attr, value):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    with pytest.raises(PropertyError) as exc:
        await ai.write_property(attr, _typed(ai, attr, value))

    assert exc.value.errorCode == "writeAccessDenied"


@pytest.mark.asyncio
async def test_alarm_write_ignores_bacnet_writable(session_factory):
    row = _analog_input(object_type="analog-value", name="AV-1", bacnet_writable=False)
    app = await _build(session_factory, row)
    av = app.get_object_name("AV-1")

    await av.write_property("highLimit", _typed(av, "highLimit", 70.0))

    assert _column(session_factory, "AV-1", "alarm_high_limit") == 70.0


@pytest.mark.asyncio
async def test_non_alarming_object_still_denies_alarm_properties(session_factory):
    row = BacnetObject(object_type="analog-value", instance_number=2, name="AV-2")
    app = await _build(session_factory, row)
    av = app.get_object_name("AV-2")

    with pytest.raises(PropertyError) as exc:
        await av.write_property("highLimit", 70.0)

    assert exc.value.errorCode == "writeAccessDenied"


@pytest.mark.asyncio
async def test_write_property_multiple_writes_two_alarm_properties(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyMultipleRequest(
        WritePropertyMultipleRequest(
            listOfWriteAccessSpecs=[
                WriteAccessSpecification(
                    objectIdentifier=ai.objectIdentifier,
                    listOfProperties=[
                        PropertyValue(propertyIdentifier="highLimit", value=_typed(ai, "highLimit", 85.0)),
                        PropertyValue(propertyIdentifier="deadband", value=_typed(ai, "deadband", 1.5)),
                    ],
                )
            ]
        )
    )

    assert isinstance(captured[0], SimpleAckPDU)
    assert _column(session_factory, "AI-1", "alarm_high_limit") == 85.0
    assert _column(session_factory, "AI-1", "alarm_deadband") == 1.5


def _binary_value(**overrides):
    fields = dict(
        object_type="binary-value",
        instance_number=1,
        name="BV-1",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_value=True,
    )
    fields.update(overrides)
    return BacnetObject(**fields)


def _multi_state_input(**overrides):
    fields = dict(
        object_type="multi-state-input",
        instance_number=1,
        name="MSI-1",
        state_text=["Off", "Low", "High"],
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_values=[3],
    )
    fields.update(overrides)
    return BacnetObject(**fields)


@pytest.mark.asyncio
async def test_alarm_value_write_changes_what_alarms(session_factory):
    app = await _build(session_factory, _binary_value())
    bv = app.get_object_name("BV-1")
    await _settle()
    assert bv.eventState.attr == "normal"  # presentValue starts inactive, alarm value is active

    await bv.write_property("alarmValue", _typed(bv, "alarmValue", "inactive"))
    await _settle()

    assert bv.eventState.attr == "offnormal"
    assert _column(session_factory, "BV-1", "alarm_value") is False


@pytest.mark.asyncio
async def test_alarm_values_write_changes_what_alarms(session_factory):
    app = await _build(session_factory, _multi_state_input())
    msi = app.get_object_name("MSI-1")
    await _settle()
    assert msi.eventState.attr == "normal"  # presentValue starts at 1, alarm state is 3

    await msi.write_property("alarmValues", _typed(msi, "alarmValues", [1, 2]))
    await _settle()

    assert msi.eventState.attr == "offnormal"
    assert _column(session_factory, "MSI-1", "alarm_values") == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[0], [4], [2, 9]])
async def test_alarm_values_outside_the_states_are_rejected(session_factory, bad):
    app = await _build(session_factory, _multi_state_input())
    msi = app.get_object_name("MSI-1")

    with pytest.raises(PropertyError) as exc:
        await msi.write_property("alarmValues", _typed(msi, "alarmValues", bad))

    assert exc.value.errorCode == "value-out-of-range"
    assert _column(session_factory, "MSI-1", "alarm_values") == [3]


@pytest.mark.asyncio
async def test_notification_class_write_relinks_the_object(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    await ai.write_property("notificationClass", _typed(ai, "notificationClass", 2))
    ai.presentValue = 150.0
    await _settle()

    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_GetEventInformationRequest(GetEventInformationRequest())
    summary = list(captured[0].listOfEventSummaries)[0]
    assert list(summary.eventPriorities) == [10, 20, 30]  # NC-2's, not NC-1's defaults
    assert _column(session_factory, "AI-1", "notification_class_instance") == 2


@pytest.mark.asyncio
async def test_notification_class_that_does_not_exist_is_rejected(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    with pytest.raises(PropertyError) as exc:
        await ai.write_property("notificationClass", _typed(ai, "notificationClass", 99))

    assert exc.value.errorCode == "value-out-of-range"
    assert int(ai.notificationClass) == 1
    assert _column(session_factory, "AI-1", "notification_class_instance") == 1


@pytest.mark.asyncio
async def test_event_detection_disable_resets_and_enable_resumes(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0
    await _settle()
    assert ai.eventState.attr == "highLimit"

    await ai.write_property("eventDetectionEnable", _typed(ai, "eventDetectionEnable", False))
    await _settle()

    # Spec: Event_State Normal, Acked_Transitions and Event_Time_Stamps back
    # to their initial values. bacpypes3 only does the first.
    assert ai.eventState.attr == "normal"
    assert list(ai.ackedTransitions) == [True, True, True]
    assert all(str(stamp.dateTime) == "*-*-* * *:*:*.*" for stamp in ai.eventTimeStamps)
    assert _column(session_factory, "AI-1", "alarm_event_detection_enable") is False

    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_GetEventInformationRequest(GetEventInformationRequest())
    assert list(captured[0].listOfEventSummaries) == []

    ai.presentValue = 160.0
    await _settle()
    assert ai.eventState.attr == "normal"  # detection is off

    await ai.write_property("eventDetectionEnable", _typed(ai, "eventDetectionEnable", True))
    await _settle()
    assert ai.eventState.attr == "highLimit"  # still out of range, so it alarms again
    assert _column(session_factory, "AI-1", "alarm_event_detection_enable") is True


@pytest.mark.asyncio
async def test_event_detection_disable_cancels_a_pending_delayed_transition(session_factory):
    # bacpypes3's disabled branch never cancels a transition that's waiting
    # out Time_Delay, so without this the disabled object alarms anyway.
    app = await _build(session_factory, _analog_input(alarm_time_delay=1))
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0
    await _settle()
    assert ai.eventState.attr == "normal"  # still waiting out the delay

    await ai.write_property("eventDetectionEnable", _typed(ai, "eventDetectionEnable", False))
    await asyncio.sleep(1.3)

    assert ai.eventState.attr == "normal"
    assert list(ai.ackedTransitions) == [True, True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value, column",
    [
        ("highLimit", float("nan"), "alarm_high_limit"),
        ("highLimit", float("inf"), "alarm_high_limit"),
        ("lowLimit", float("-inf"), "alarm_low_limit"),
        ("deadband", float("nan"), "alarm_deadband"),
    ],
)
async def test_non_finite_limit_is_rejected(session_factory, attr, value, column):
    # NaN saves as NULL (the device then can't start); infinity breaks the
    # objects API's JSON.
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    before = _column(session_factory, "AI-1", column)

    with pytest.raises(PropertyError) as exc:
        await ai.write_property(attr, _typed(ai, attr, value))

    assert exc.value.errorCode == "value-out-of-range"
    assert _column(session_factory, "AI-1", column) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, column", [("timeDelay", "alarm_time_delay"), ("timeDelayNormal", "alarm_time_delay_normal")])
async def test_time_delays_above_32_bits_are_refused(session_factory, attr, column):
    # The Unsigned cast accepts 2**32 and up, which then
    # can't be read back and makes the API refuse the row.
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    before = _column(session_factory, "AI-1", column)

    with pytest.raises(PropertyError) as excinfo:
        await ai.write_property(attr, _typed(ai, attr, 2**32))

    assert excinfo.value.errorCode == "value-out-of-range"
    assert _column(session_factory, "AI-1", column) == before
    await ai.write_property(attr, _typed(ai, attr, 2**32 - 1))
    assert _column(session_factory, "AI-1", column) == 2**32 - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ackNotification", 7])
async def test_notify_type_other_than_alarm_or_event_is_rejected(session_factory, value):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    with pytest.raises(PropertyError) as exc:
        await ai.write_property("notifyType", _typed(ai, "notifyType", value))

    assert exc.value.errorCode == "value-out-of-range"
    assert _column(session_factory, "AI-1", "alarm_notify_type") == "alarm"


# A bit string decoded as EventTransitionBits/LimitEnable is padded out to
# its named bits, so a short one reached write_property already the right
# length and was accepted: [1, 1] for eventEnable became [1, 1, 0], silently
# turning off to-normal notifications. These go through the real request
# decode, which a direct write_property call with a typed value skips.


def _write_request(obj, attr, value):
    return WritePropertyRequest(
        objectIdentifier=obj.objectIdentifier,
        propertyIdentifier=attr,
        propertyValue=Any(value),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, bits, column",
    [
        ("eventEnable", [1, 1], "alarm_event_enable"),
        ("limitEnable", [1], "alarm_limit_enable"),
    ],
)
async def test_short_bit_string_on_the_wire_is_rejected(session_factory, attr, bits, column):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    before_live = list(getattr(ai, attr))

    with pytest.raises(PropertyError) as exc:
        await app.do_WritePropertyRequest(_write_request(ai, attr, BitString(bits)))

    assert exc.value.errorCode == "value-out-of-range"
    assert list(getattr(ai, attr)) == before_live
    assert _column(session_factory, "AI-1", column) is None


@pytest.mark.asyncio
async def test_short_bit_string_in_write_property_multiple_is_rejected(session_factory):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyMultipleRequest(
        WritePropertyMultipleRequest(
            listOfWriteAccessSpecs=[
                WriteAccessSpecification(
                    objectIdentifier=ai.objectIdentifier,
                    listOfProperties=[PropertyValue(propertyIdentifier="eventEnable", value=BitString([1, 1]))],
                )
            ]
        )
    )

    assert isinstance(captured[0], WritePropertyMultipleError)
    assert str(captured[0].errorType.errorCode) == "value-out-of-range"
    assert _column(session_factory, "AI-1", "alarm_event_enable") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, bits, column, saved",
    [
        ("eventEnable", [1, 0, 1], "alarm_event_enable", [True, False, True]),
        ("limitEnable", [0, 1], "alarm_limit_enable", [False, True]),
    ],
)
async def test_full_length_bit_string_on_the_wire_is_applied_and_saved(session_factory, attr, bits, column, saved):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyRequest(_write_request(ai, attr, BitString(bits)))

    assert isinstance(captured[0], SimpleAckPDU)
    assert list(getattr(ai, attr)) == bits
    assert _column(session_factory, "AI-1", column) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, bits", [("eventEnable", [1, 1, 1]), ("limitEnable", [1, 1])])
async def test_bit_string_alarm_properties_still_read_over_the_wire(session_factory, attr, bits):
    app = await _build(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_ReadPropertyRequest(ReadPropertyRequest(objectIdentifier=ai.objectIdentifier, propertyIdentifier=attr))

    assert isinstance(captured[0], ReadPropertyACK)
    assert list(captured[0].propertyValue.cast_out(BitString)) == bits


# --- Event_Enable gates the send; acks follow ackRequired ------------------
# bacpypes3 never consults Event_Enable, and BACpack used to mark every
# transition unacknowledged whatever the Notification Class asked for.

_RECIPIENT = {
    "recipient": {"kind": "address", "address": "192.168.1.77"},
    "valid_days": [True] * 7,
    "from_time": "00:00",
    "to_time": "23:59",
    "transitions": [True, True, True],
    "confirmed": False,
    "process_identifier": 5,
}


async def _build_reporting(session_factory, row, *, ack_required=True, nc_fields=None):
    """A device with NC-1 (one unconfirmed recipient, `nc_fields` merged
    into its row) and `row`. Returns (app, sent) where `sent` collects every
    outbound notification."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        nc = dict(
            device=device,
            object_type="notification-class",
            instance_number=1,
            name="NC-1",
            notification_ack_required=[ack_required] * 3,
            notification_recipient_list=[_RECIPIENT],
        )
        nc.update(nc_fields or {})
        session.add(BacnetObject(**nc))
        row.device = device
        session.add(row)
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)
    sent = []

    def _capture(apdu):
        sent.append(apdu)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    app.request = _capture
    return app, sent


async def _settle_sends():
    for _ in range(5):
        await asyncio.sleep(0)


async def _event_summaries(app):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_GetEventInformationRequest(GetEventInformationRequest())
    return [s.objectIdentifier for s in captured[0].listOfEventSummaries]


@pytest.mark.asyncio
async def test_to_offnormal_off_withholds_the_alarm_but_still_transitions(session_factory):
    app, sent = await _build_reporting(session_factory, _analog_input(alarm_event_enable=[False, True, True]))
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()

    assert ai.eventState.attr == "highLimit"
    assert sent == []
    assert list(ai.ackedTransitions) == [True, True, True]


@pytest.mark.asyncio
async def test_to_normal_off_withholds_only_the_return(session_factory):
    app, sent = await _build_reporting(session_factory, _analog_input(alarm_event_enable=[True, True, False]))
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()
    assert [str(apdu.toState) for apdu in sent] == ["high-limit"]
    assert list(ai.ackedTransitions) == [False, True, True]

    ai.presentValue = 50.0
    await _settle_sends()
    assert ai.eventState.attr == "normal"
    assert len(sent) == 1
    assert list(ai.ackedTransitions) == [False, True, True]  # to-normal stays acked


@pytest.mark.asyncio
async def test_event_enable_write_takes_effect_on_the_next_transition(session_factory):
    app, sent = await _build_reporting(session_factory, _analog_input())
    ai = app.get_object_name("AI-1")

    await ai.write_property("eventEnable", _typed(ai, "eventEnable", [False, True, True]))
    ai.presentValue = 150.0
    await _settle_sends()
    assert sent == []

    ai.presentValue = 50.0
    await _settle_sends()
    await ai.write_property("eventEnable", _typed(ai, "eventEnable", [True, True, True]))
    sent.clear()
    ai.presentValue = 150.0
    await _settle_sends()
    assert [str(apdu.toState) for apdu in sent] == ["high-limit"]


@pytest.mark.asyncio
async def test_binary_to_offnormal_off_withholds_the_alarm(session_factory):
    app, sent = await _build_reporting(session_factory, _binary_value(alarm_event_enable=[False, True, True]))
    bv = app.get_object_name("BV-1")

    await bv.write_property("alarmValue", _typed(bv, "alarmValue", "inactive"))
    await _settle_sends()

    assert bv.eventState.attr == "offnormal"
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bit", [0, 1, 2])
async def test_withhold_disabled_notifications_checks_the_target_state_bit(bit):
    from types import SimpleNamespace

    from bacpypes3.basetypes import EventState

    from bacpack.objects.alarm_config import withhold_disabled_notifications

    calls = []

    async def _send(parameters):
        calls.append(parameters["toState"])

    enable = [True, True, True]
    enable[bit] = False
    algorithm = SimpleNamespace(
        send_notifications=_send, monitoring_object=None, monitored_object=SimpleNamespace(eventEnable=enable)
    )
    withhold_disabled_notifications(algorithm)

    states = [EventState.highLimit, EventState.fault, EventState.normal]
    for state in states:
        await algorithm.send_notifications({"toState": state})

    assert calls == [state for i, state in enumerate(states) if i != bit]


@pytest.mark.asyncio
async def test_notifications_take_priority_and_ack_required_from_their_transition_slot(session_factory):
    # bacpypes3 indexed the NC arrays by EventState number
    # (normal 0, fault 1, offnormal 2), not BACnet's transition order
    # (to-offnormal, to-fault, to-normal), so these two were swapped.
    app, sent = await _build_reporting(
        session_factory,
        _analog_input(),
        nc_fields={"notification_priority": [10, 20, 30], "notification_ack_required": [True, False, False]},
    )
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()
    ai.presentValue = 50.0
    await _settle_sends()

    assert [n.toState.attr for n in sent] == ["highLimit", "normal"]
    assert [int(n.priority) for n in sent] == [10, 30]
    assert [bool(n.ackRequired) for n in sent] == [True, False]


@pytest.mark.asyncio
async def test_no_ack_required_leaves_the_transition_acked(session_factory):
    app, sent = await _build_reporting(session_factory, _analog_input(), ack_required=False)
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()
    assert len(sent) == 1
    assert not sent[0].ackRequired
    assert list(ai.ackedTransitions) == [True, True, True]
    assert ai.objectIdentifier in await _event_summaries(app)  # still in alarm

    ai.presentValue = 50.0
    await _settle_sends()
    assert list(ai.ackedTransitions) == [True, True, True]
    assert ai.objectIdentifier not in await _event_summaries(app)


@pytest.mark.asyncio
async def test_ack_required_transition_is_listed_until_acknowledged(session_factory):
    app, sent = await _build_reporting(session_factory, _analog_input(), ack_required=True)
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()
    ai.presentValue = 50.0
    await _settle_sends()

    assert [bool(apdu.ackRequired) for apdu in sent] == [True, True]
    assert list(ai.ackedTransitions) == [False, True, False]
    assert ai.objectIdentifier in await _event_summaries(app)


# --- AcknowledgeAlarm sends an acknowledgement notification ------------------
# The Notification Class's recipients never learned an alarm was
# acknowledged.

from bacpypes3.apdu import AcknowledgeAlarmRequest
from bacpypes3.basetypes import TimeStamp

from bacpack.objects.alarm_config import state_group_index


async def _acknowledge(app, ai, *, at=7):
    async def _ignore(resp):
        pass

    app.response = _ignore
    await app.do_AcknowledgeAlarmRequest(
        AcknowledgeAlarmRequest(
            acknowledgingProcessIdentifier=1,
            eventObjectIdentifier=ai.objectIdentifier,
            eventStateAcknowledged=ai.eventState.attr,
            timeStamp=ai.eventTimeStamps[state_group_index(ai.eventState)],
            acknowledgmentSource="test",
            timeOfAcknowledgment=TimeStamp(sequenceNumber=at),
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_acknowledge_alarm_sends_one_ack_notification(session_factory):
    app, sent = await _build_reporting(
        session_factory, _analog_input(), nc_fields={"notification_priority": [10, 20, 30]}
    )
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0
    await _settle_sends()
    sent.clear()

    await _acknowledge(app, ai)
    assert len(sent) == 1
    note = sent[0]
    assert note.notifyType.attr == "ackNotification"
    assert note.toState.attr == "highLimit"
    assert int(note.priority) == 10
    assert note.eventType.attr == "outOfRange"
    assert int(note.timeStamp.sequenceNumber) == 7
    assert note.eventObjectIdentifier == ai.objectIdentifier
    assert note.fromState is None and note.ackRequired is None and note.eventValues is None

    sent.clear()
    await _acknowledge(app, ai)  # already acknowledged: nothing to announce
    assert sent == []


@pytest.mark.asyncio
async def test_no_ack_notification_to_a_recipient_without_that_transition(session_factory):
    recipient = dict(_RECIPIENT, transitions=[False, True, True])
    app, sent = await _build_reporting(
        session_factory, _analog_input(), nc_fields={"notification_recipient_list": [recipient]}
    )
    ai = app.get_object_name("AI-1")
    ai.presentValue = 150.0
    await _settle_sends()
    await _acknowledge(app, ai)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [2, 7])
async def test_binary_alarm_value_other_than_active_or_inactive_is_rejected(session_factory, bad):
    app = await _build(session_factory, _binary_value())
    bv = app.get_object_name("BV-1")
    before = bv.alarmValue

    with pytest.raises(PropertyError) as exc:
        await bv.write_property("alarmValue", _typed(bv, "alarmValue", bad))

    assert exc.value.errorCode == "value-out-of-range"
    assert bv.alarmValue == before
    assert _column(session_factory, "BV-1", "alarm_value") is True


# --- Valid_Days follows the device clock's weekday --------------------------
# bacpypes3 checked validDays[3] (Thursday) whatever the day.

from bacpack import device_clock

# (device-clock date, Valid_Days index of that weekday): a Monday, a
# Thursday and a Sunday, so neither the host's weekday nor Thursday decides.
_WEEKDAYS = [((2026, 11, 9), 0), ((2026, 11, 12), 3), ((2026, 11, 15), 6)]


def _days(*, only=None, all_but=None):
    return [day == only if only is not None else day != all_but for day in range(7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("date, weekday", _WEEKDAYS)
@pytest.mark.parametrize("valid_only_today", [True, False])
async def test_valid_days_is_checked_against_the_device_clocks_weekday(session_factory, date, weekday, valid_only_today):
    device_clock.install()
    device_clock.set_offset(device_clock.offset_for_local(*date, 12, 0, 0))
    valid_days = _days(only=weekday) if valid_only_today else _days(all_but=weekday)
    app, sent = await _build_reporting(
        session_factory,
        _analog_input(),
        nc_fields={"notification_recipient_list": [dict(_RECIPIENT, valid_days=valid_days)]},
    )
    ai = app.get_object_name("AI-1")

    ai.presentValue = 150.0
    await _settle_sends()
    assert len(sent) == (1 if valid_only_today else 0)

    sent.clear()
    await _acknowledge(app, ai)
    assert len(sent) == (1 if valid_only_today else 0)


# --- time_stamp_matches ------------------------------------------------------

from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time

from bacpack.objects.alarm_config import time_stamp_matches, unset_time_stamp


def _stamp(date=(126, 11, 17, 2), time=(9, 16, 6, 91)):
    return TimeStamp(dateTime=DateTime(date=Date(date), time=Time(time)))


def test_time_stamp_matches():
    stored = _stamp()
    assert time_stamp_matches(stored, _stamp())
    assert time_stamp_matches(stored, _stamp(date=(126, 11, 17, 255)))  # weekday is derived
    assert not time_stamp_matches(stored, _stamp(date=(126, 11, 16, 1)))
    assert not time_stamp_matches(stored, _stamp(time=(9, 16, 6, 92)))
    assert not time_stamp_matches(stored, _stamp(time=(9, 16, 6, 255)))
    assert not time_stamp_matches(stored, TimeStamp(sequenceNumber=0))
    assert not time_stamp_matches(stored, TimeStamp(time=Time((9, 16, 6, 91))))
    assert not time_stamp_matches(unset_time_stamp(), unset_time_stamp())
    assert not time_stamp_matches(TimeStamp(sequenceNumber=3), TimeStamp(sequenceNumber=3))
