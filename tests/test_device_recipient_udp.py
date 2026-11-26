"""End to end over real UDP on loopback: an alarm whose Notification Class
recipient is a device instance only (no address) reaches that device. The
global broadcast isn't carried reliably on loopback, so the Who-Is is
retargeted to a unicast to the receiver."""
from __future__ import annotations

import asyncio
import logging

import pytest
import pytest_asyncio
from bacpypes3.apdu import WhoIsRequest
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier

from bacpack.models import BacnetObject, Device
from tests.udp_bind import build_on_loopback

_RECIPIENT = {
    "recipient": {"kind": "device", "instance_number": 2},
    "valid_days": [True] * 7,
    "from_time": "00:00",
    "to_time": "23:59:59.99",
    "transitions": [True, True, True],
    "confirmed": False,
    "process_identifier": 5,
}


def _build(session_factory, device_id, bind_address):
    return build_on_loopback(session_factory, device_id, bind_address)


@pytest_asyncio.fixture
async def pair(session_factory):
    with session_factory() as session:
        sender = Device(id=1, name="Sender")
        receiver = Device(id=2, name="Receiver")
        session.add_all(
            [
                sender,
                receiver,
                BacnetObject(
                    device=sender, object_type="notification-class", instance_number=1, name="NC-1",
                    notification_recipient_list=[_RECIPIENT],
                ),
                BacnetObject(
                    device=sender, object_type="analog-input", instance_number=1, name="AI-1",
                    units="degreesFahrenheit", alarming_enabled=True, notification_class_instance=1,
                    alarm_high_limit=100.0, alarm_low_limit=0.0, alarm_notify_type="alarm",
                ),
            ]
        )
        session.commit()
    sender_app = _build(session_factory, 1, "127.0.0.1/8")
    try:
        receiver_app = _build(session_factory, 2, "127.0.0.2/8")
    except BaseException:
        sender_app.close()
        raise
    received = asyncio.get_running_loop().create_future()

    async def handler(apdu):
        if not received.done():
            received.set_result(apdu)

    receiver_app.do_UnconfirmedEventNotificationRequest = handler
    who_is_seen = []
    real_request = type(sender_app).request

    def retargeting_request(apdu):
        if isinstance(apdu, WhoIsRequest):
            who_is_seen.append(apdu)
            apdu.pduDestination = Address("127.0.0.2")
        return real_request(sender_app, apdu)

    sender_app.request = retargeting_request
    try:
        yield sender_app, receiver_app, received, who_is_seen
    finally:
        sender_app.close()
        receiver_app.close()


@pytest.mark.asyncio
async def test_an_alarm_reaches_a_device_recipient_found_by_who_is(pair):
    sender, _receiver, received, who_is_seen = pair
    sender.get_object_id(ObjectIdentifier("analog-input,1")).presentValue = 150.0
    apdu = await asyncio.wait_for(received, 10)
    assert len(who_is_seen) == 1
    assert (who_is_seen[0].deviceInstanceRangeLowLimit, who_is_seen[0].deviceInstanceRangeHighLimit) == (2, 2)
    assert apdu.eventObjectIdentifier == ObjectIdentifier("analog-input,1")
    assert apdu.toState.attr == "highLimit"
    assert int(apdu.processIdentifier) == 5


@pytest.mark.asyncio
async def test_a_recipient_that_rejects_a_confirmed_notification_is_logged(session_factory, caplog):
    # A BACpack device has no handler for ConfirmedEventNotification, so
    # bacpypes3 answers Reject unrecognized-service, as a front end that
    # expects another process ID does.
    recipient = dict(
        _RECIPIENT,
        recipient={"kind": "address", "address": "127.0.0.2"},
        confirmed=True,
    )
    with session_factory() as session:
        sender = Device(id=1, name="Sender")
        session.add_all(
            [
                sender,
                Device(id=2, name="Receiver"),
                BacnetObject(
                    device=sender, object_type="notification-class", instance_number=1, name="NC-1",
                    notification_recipient_list=[recipient],
                ),
                BacnetObject(
                    device=sender, object_type="analog-input", instance_number=1, name="AI-1",
                    units="degreesFahrenheit", alarming_enabled=True, notification_class_instance=1,
                    alarm_high_limit=100.0, alarm_low_limit=0.0, alarm_notify_type="alarm",
                ),
            ]
        )
        session.commit()
    sender_app = _build(session_factory, 1, "127.0.0.1/8")
    try:
        receiver_app = _build(session_factory, 2, "127.0.0.2/8")
    except BaseException:
        sender_app.close()
        raise
    try:
        with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
            sender_app.get_object_id(ObjectIdentifier("analog-input,1")).presentValue = 150.0
            for _ in range(100):
                lines = [r.getMessage() for r in caplog.records if r.name == "bacpack.bacpypes3_compat"]
                if lines:
                    break
                await asyncio.sleep(0.05)
        assert lines == [
            'event notification from analog-input:1 "AI-1" to 127.0.0.2, process ID 5, '
            "to-offnormal: refused, reject: unrecognized-service"
        ]
        traffic = [e["detail"] for e in sender_app.traffic_log.recent() if e["event_type"] == "event-notification"]
        assert traffic == ["-> 127.0.0.2 obj analog-input:1 to high-limit"]
    finally:
        sender_app.close()
        receiver_app.close()


@pytest.mark.asyncio
async def test_an_alarm_reaches_a_device_recipient_that_announced_itself(pair):
    sender, receiver, received, who_is_seen = pair
    receiver.i_am(address=Address("127.0.0.1"))
    for _ in range(100):
        if await sender.device_info_cache.get_device_info(2) is not None:
            break
        await asyncio.sleep(0.05)
    assert await sender.device_info_cache.get_device_info(2) is not None
    sender.get_object_id(ObjectIdentifier("analog-input,1")).presentValue = 150.0
    await asyncio.wait_for(received, 10)
    assert who_is_seen == []
