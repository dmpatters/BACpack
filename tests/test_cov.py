"""Covers COV subscription support. This comes almost entirely from bacpypes3's
ChangeOfValueServices mixin (already in Application) plus each object's
_cov_criteria (set when the objects are built) -- there's very little BACpack-side
code to test here, just that the wiring holds together end to end.

Subscribing and writing are exercised directly at the Application/object
level rather than over a real socket, since do_SubscribeCOVRequest and
write_property don't need the transport layer to do their job.
"""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.apdu import SubscribeCOVRequest
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Null, Unsigned

from bacpack.object_builder import build_application
from bacpack.bacnet import write_feed_value
from bacpack.models import BacnetObject, Device


@pytest.mark.asyncio
async def test_cov_subscriber_notified_on_value_change():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    obj_row = BacnetObject(
        object_type="analog-value", instance_number=1, name="AV-1", units="percent"
    )
    app = build_application(device, [obj_row])
    av = app.get_object_name("AV-1")

    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)

    subscribe = SubscribeCOVRequest(
        subscriberProcessIdentifier=1,
        monitoredObjectIdentifier=("analog-value", 1),
        issueConfirmedNotifications=False,
        lifetime=60,
    )
    subscribe.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(subscribe)
    # Per spec, SubscribeCOV itself triggers an immediate notification with
    # the current value; like the change notification below, it's scheduled
    # via call_soon rather than sent synchronously, so yield once.
    await asyncio.sleep(0)
    assert len(notifications) == 1

    await write_feed_value(av, 42.0)
    # COV notification dispatch is scheduled via call_soon, not sent
    # synchronously from the write -- yield once to let it run.
    await asyncio.sleep(0)

    assert len(notifications) == 2
    assert notifications[-1].monitoredObjectIdentifier == av.objectIdentifier


@pytest.mark.asyncio
async def test_no_notification_without_a_subscription():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    obj_row = BacnetObject(
        object_type="analog-value", instance_number=1, name="AV-1", units="percent"
    )
    app = build_application(device, [obj_row])
    av = app.get_object_name("AV-1")

    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)

    await write_feed_value(av, 42.0)
    await _let_the_loop_run()

    assert notifications == []


# --- Multi-state objects ----------------------------------------------------
# bacpypes3 gives Multi-state Input/Output/Value COVIncrementCriteria, which
# reads a covIncrement they don't have: every SubscribeCOV failed, and the
# half-done bind left monitors behind that then broke MSO priority writes.

_ALARMING_MSV = dict(
    alarming_enabled=True, notification_class_instance=1, alarm_values=[3]
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, extra",
    [
        ("multi-state-input", {}),
        ("multi-state-output", {}),
        ("multi-state-value", {}),
        ("multi-state-input", _ALARMING_MSV),
        ("multi-state-value", _ALARMING_MSV),
    ],
)
async def test_multistate_cov_subscription_notifies_on_change(object_type, extra):
    from bacpypes3.primitivedata import Unsigned

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    rows = [
        BacnetObject(
            object_type=object_type, instance_number=1, name="MS-1",
            state_text=["Off", "Low", "High"], **extra,
        )
    ]
    if extra:
        rows.append(BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"))
    app = build_application(device, rows)
    ms = app.get_object_name("MS-1")

    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)

    subscribe = SubscribeCOVRequest(
        subscriberProcessIdentifier=1,
        monitoredObjectIdentifier=(object_type, 1),
        issueConfirmedNotifications=False,
        lifetime=60,
    )
    subscribe.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(subscribe)
    await asyncio.sleep(0)
    assert len(notifications) == 1

    await write_feed_value(ms, 2)
    await asyncio.sleep(0)
    assert len(notifications) == 2

    if object_type in ("multi-state-output", "multi-state-value"):
        # Raised AttributeError (previously_reported_value) before the fix
        # (MSO); MSV is commandable. Exactly 3: one per change.
        await ms.write_property("presentValue", Unsigned(3), priority=8)
        await asyncio.sleep(0)
        assert ms.presentValue == 3
        assert len(notifications) == 3


# --- Present_Value monitors on objects without a priority array -------------
# bacpypes3's write_property sets the value past local Object.__setattr__,
# the code that tells _property_monitors -- so on a multi-state value (feed
# or client write) or an out-of-service input written by a client, COV,
# alarm detection and COV-mode trending never saw the change. A multi-state
# value is commandable and no longer needs this; the MSV tests below stay as
# regression checks.


async def _subscribe(app, object_type):
    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)
    subscribe = SubscribeCOVRequest(
        subscriberProcessIdentifier=1,
        monitoredObjectIdentifier=(object_type, 1),
        issueConfirmedNotifications=False,
        lifetime=60,
    )
    subscribe.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(subscribe)
    await asyncio.sleep(0)
    return notifications


@pytest.mark.asyncio
async def test_client_write_to_out_of_service_input_notifies_cov():
    from bacpypes3.primitivedata import Real

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [BacnetObject(
        object_type="analog-input", instance_number=1, name="AI-1", units="percent",
        out_of_service_default=True,
    )])
    ai = app.get_object_name("AI-1")
    notifications = await _subscribe(app, "analog-input")

    await ai.write_property("presentValue", Real(55.0))
    await asyncio.sleep(0)

    assert ai.presentValue == 55.0
    assert len(notifications) == 2


@pytest.mark.asyncio
async def test_unchanged_client_write_sends_no_extra_notification():
    from bacpypes3.primitivedata import Unsigned

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [BacnetObject(
        object_type="multi-state-value", instance_number=1, name="MS-1", state_text=["a", "b"],
    )])
    ms = app.get_object_name("MS-1")
    notifications = await _subscribe(app, "multi-state-value")

    await ms.write_property("presentValue", Unsigned(1))
    await asyncio.sleep(0)
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_multistate_value_feed_write_raises_its_alarm():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MS-1",
            state_text=["Off", "Low", "High"], alarming_enabled=True,
            notification_class_instance=1, alarm_values=[3],
        ),
        BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"),
    ])
    ms = app.get_object_name("MS-1")

    await write_feed_value(ms, 3)
    for _ in range(5):
        await asyncio.sleep(0)

    assert str(ms.eventState) == "offnormal"


@pytest.mark.asyncio
async def test_commanded_multistate_value_goes_offnormal_and_back():
    # Verified by hand in a live check. A BMS
    # write at priority 8 into an alarm state, then its relinquish back to
    # Relinquish_Default.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MS-1",
            state_text=["Off", "Low", "High"], relinquish_default="1", bacnet_writable=True,
            alarming_enabled=True, notification_class_instance=1, alarm_values=[3],
        ),
        BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"),
    ])
    ms = app.get_object_name("MS-1")

    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    await ms.write_property("presentValue", Unsigned(3), priority=8)
    await settle()
    assert str(ms.eventState) == "offnormal"

    await ms.write_property("presentValue", Null(()), priority=8)
    await settle()
    assert ms.presentValue == 1
    assert str(ms.eventState) == "normal"


@pytest.mark.asyncio
async def test_binary_value_cov_subscription_notifies_on_change():
    # The plain BV class (built around bacpypes3's BinaryValueObjectCmd MRO
    # bug) had no _cov_criteria at all: covSubscriptionFailed.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [BacnetObject(object_type="binary-value", instance_number=1, name="BV-1")])
    bv = app.get_object_name("BV-1")
    notifications = await _subscribe(app, "binary-value")
    assert len(notifications) == 1

    await write_feed_value(bv, "active")
    await asyncio.sleep(0)
    assert len(notifications) == 2


# --- Status_Flags-only changes (135 13.1) -----------------------------------
# bacpypes3 computes Status_Flags on read and never sets it, so the COV
# detection's statusFlags monitor never fired.


def _reported_status_flags(apdu):
    from bacpypes3.basetypes import StatusFlags

    for property_value in apdu.listOfValues:
        if property_value.propertyIdentifier.attr == "statusFlags":
            return list(property_value.value.cast_out(StatusFlags))
    raise AssertionError("no statusFlags in the notification")


_OOS_BIT, _FAULT_BIT, _ALARM_BIT = 3, 1, 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, extra",
    [
        ("analog-value", {"units": "percent"}),
        ("binary-input", {}),
        ("multi-state-output", {"state_text": ["a", "b"]}),
    ],
)
async def test_client_out_of_service_write_notifies_cov(object_type, extra):
    from bacpypes3.primitivedata import Boolean

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [BacnetObject(object_type=object_type, instance_number=1, name="OBJ-1", **extra)])
    obj = app.get_object_name("OBJ-1")
    notifications = await _subscribe(app, object_type)
    assert _reported_status_flags(notifications[-1])[_OOS_BIT] == 0

    await obj.write_property("outOfService", Boolean(True))
    await asyncio.sleep(0)

    assert len(notifications) == 2
    assert _reported_status_flags(notifications[-1])[_OOS_BIT] == 1


@pytest.mark.asyncio
async def test_reliability_change_notifies_cov():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [BacnetObject(
        object_type="analog-value", instance_number=1, name="AV-1", units="percent",
    )])
    av = app.get_object_name("AV-1")
    notifications = await _subscribe(app, "analog-value")

    av.reliability = "communicationFailure"  # what a proxy fault sets
    await asyncio.sleep(0)

    assert len(notifications) == 2
    assert _reported_status_flags(notifications[-1])[_FAULT_BIT] == 1


@pytest.mark.asyncio
async def test_going_into_alarm_reports_the_in_alarm_flag():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [
        BacnetObject(object_type="notification-class", instance_number=1, name="NC-1"),
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", units="percent",
            alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0,
            alarm_low_limit=0.0, alarm_notify_type="alarm",
        ),
    ])
    av = app.get_object_name("AV-1")
    notifications = await _subscribe(app, "analog-value")

    await write_feed_value(av, 150.0)
    for _ in range(5):
        await asyncio.sleep(0)

    assert av.eventState.attr == "highLimit"
    assert _reported_status_flags(notifications[-1])[_ALARM_BIT] == 1


# --- SubscribeCOV without a Lifetime -----------------------------------------
# Lifetime is optional; with Issue_Confirmed_Notifications present it means
# an indefinite subscription (ASHRAE 135 13.14.1). bacpypes3 compared None to
# 0 and the client got operationalProblem.

from bacpack import bacpypes3_compat


async def _subscribe_with(app, process_id=1, **fields):
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=process_id,
        monitoredObjectIdentifier=("analog-value", 1),
        **fields,
    )
    request.pduSource = Address("192.168.1.99")
    responses = []

    async def _response(apdu):
        responses.append(apdu)

    app.response = _response
    await app.do_SubscribeCOVRequest(request)
    await asyncio.sleep(0)
    return responses


async def _let_the_loop_run():
    # A COV notification is scheduled with call_soon, so an empty list right
    # after the write proves nothing until the loop has turned.
    for _ in range(5):
        await asyncio.sleep(0)


def _av_app():
    app = build_application(
        Device(id=599001, name="Test Device", vendor_identifier=999),
        [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")],
    )
    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)
    return app, app.get_object_name("AV-1"), notifications


@pytest.mark.asyncio
async def test_subscribe_without_lifetime_is_a_permanent_subscription():
    app, av, notifications = _av_app()

    responses = await _subscribe_with(app, issueConfirmedNotifications=False)

    assert [type(r).__name__ for r in responses] == ["SimpleAckPDU"]
    assert len(notifications) == 1  # the initial notification
    await write_feed_value(av, 42.0)
    await asyncio.sleep(0)
    assert len(notifications) == 2
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.lifetime == 0


@pytest.mark.asyncio
async def test_a_cancellation_without_both_fields_still_cancels():
    app, av, notifications = _av_app()
    await _subscribe_with(app, issueConfirmedNotifications=False)

    responses = await _subscribe_with(app)  # confirmed and lifetime both absent

    assert [type(r).__name__ for r in responses] == ["SimpleAckPDU"]
    notifications.clear()
    await write_feed_value(av, 42.0)
    await asyncio.sleep(0)
    assert notifications == []
    assert bacpypes3_compat.take_cov_subscriptions(app, av) == []


@pytest.mark.asyncio
async def test_renewing_without_lifetime_makes_the_subscription_permanent():
    app, av, notifications = _av_app()
    await _subscribe_with(app, issueConfirmedNotifications=False, lifetime=60)

    responses = await _subscribe_with(app, issueConfirmedNotifications=False)

    assert [type(r).__name__ for r in responses] == ["SimpleAckPDU"]
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.lifetime == 0


# --- SubscribeCOV: Lifetime needs Issue_Confirmed_Notifications; renewals ---------


@pytest.mark.asyncio
async def test_lifetime_without_the_confirmed_flag_is_refused_and_creates_nothing():
    from bacpypes3.errors import ExecutionError

    app, av, notifications = _av_app()

    with pytest.raises(ExecutionError) as caught:
        await _subscribe_with(app, lifetime=60)
    await _let_the_loop_run()

    assert caught.value.errorClass == "services"
    assert caught.value.errorCode == "missingRequiredParameter"
    assert notifications == []
    assert bacpypes3_compat.take_cov_subscriptions(app, av) == []


@pytest.mark.asyncio
async def test_a_refused_renewal_leaves_the_existing_subscription_alone():
    from bacpypes3.errors import ExecutionError

    app, av, notifications = _av_app()
    await _subscribe_with(app, issueConfirmedNotifications=True, lifetime=600)
    notifications.clear()

    with pytest.raises(ExecutionError):
        await _subscribe_with(app, lifetime=5)
    await _let_the_loop_run()

    assert notifications == []
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.confirmed is True
    assert state.lifetime > 5


@pytest.mark.asyncio
@pytest.mark.parametrize("first, second", [(False, True), (True, False)])
async def test_a_renewal_switches_between_confirmed_and_unconfirmed(first, second):
    app, av, notifications = _av_app()
    await _subscribe_with(app, issueConfirmedNotifications=first, lifetime=60)
    await _subscribe_with(app, issueConfirmedNotifications=second, lifetime=120)
    notifications.clear()

    await write_feed_value(av, 42.0)
    await asyncio.sleep(0)

    expected = "ConfirmedCOVNotificationRequest" if second else "UnconfirmedCOVNotificationRequest"
    assert [type(n).__name__ for n in notifications] == [expected]
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.confirmed is second
    assert state.lifetime > 60


@pytest.mark.asyncio
async def test_a_renewal_changes_only_its_own_subscription():
    app, av, notifications = _av_app()
    await _subscribe_with(app, process_id=1, issueConfirmedNotifications=False, lifetime=60)
    await _subscribe_with(app, process_id=2, issueConfirmedNotifications=False, lifetime=60)

    await _subscribe_with(app, process_id=1, issueConfirmedNotifications=True, lifetime=60)

    states = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert sorted((s.proc_id, s.confirmed) for s in states) == [(1, True), (2, False)]


@pytest.mark.asyncio
async def test_a_renewal_with_the_same_mode_still_renews_the_lifetime():
    app, av, notifications = _av_app()
    await _subscribe_with(app, issueConfirmedNotifications=True, lifetime=60)
    await _subscribe_with(app, issueConfirmedNotifications=True, lifetime=300)

    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.confirmed is True
    assert state.lifetime > 60
