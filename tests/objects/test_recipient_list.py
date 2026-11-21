"""Editing a Notification Class's Recipient_List over BACnet (AE-CRL-B). Built
from saved rows with a real session factory, like a running device, so each
change is checked against both the live object and the DB."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.apdu import SimpleAckPDU, WritePropertyRequest
from bacpypes3.basetypes import Destination, DeviceAddress, EventState, EventTransitionBits, Recipient
from bacpypes3.constructeddata import Any, ArrayOf, ListOf
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import PropertyIdentifier, Time, Unsigned

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from bacpack.objects.alarm_config import with_notification_class_slots
from bacpack.objects.notification_class import ListElementError
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked


def _entry(address="192.168.1.200", pid=1, **overrides):
    entry = {
        "recipient": {"kind": "address", "address": address},
        "valid_days": [True] * 7,
        "from_time": "00:00:00.00",
        "to_time": "23:59:59.99",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": pid,
    }
    entry.update(overrides)
    return entry


def _destination(address="192.168.1.200", pid=1, confirmed=True, to_time="23:59:59.99"):
    return Destination(
        validDays=[1] * 7,
        fromTime=Time("00:00:00.00"),
        toTime=Time(to_time),
        recipient=Recipient(address=DeviceAddress(address)),
        processIdentifier=pid,
        issueConfirmedNotifications=confirmed,
        transitions=[1, 1, 1],
    )


async def _build(session_factory, recipients, extra=()):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        session.add(
            BacnetObject(
                device=device,
                object_type="notification-class",
                instance_number=1,
                name="NC-1",
                notification_recipient_list=recipients,
            )
        )
        for row in extra:
            row.device = device
            session.add(row)
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return app, app.get_object_name("NC-1")


def _saved(session_factory):
    with session_factory() as session:
        return session.query(BacnetObject).filter_by(name="NC-1").one().notification_recipient_list


def _live_pids(nc):
    return [int(d.processIdentifier) for d in nc.recipientList]


@pytest.mark.asyncio
async def test_add_recipients_applies_and_saves(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    await nc.add_recipients([_destination("192.168.1.77", pid=5)])
    assert _live_pids(nc) == [1, 5]
    assert _saved(session_factory) == [_entry(), _entry("192.168.1.77", pid=5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["add", "write"])
async def test_the_save_waits_for_a_held_lock_off_the_loop(session_factory, change):
    app, nc = await _build(session_factory, [_entry()])
    destination = _destination("192.168.1.77", pid=5)
    if change == "add":
        make = lambda: nc.add_recipients([destination])  # noqa: E731
    else:
        make = lambda: nc.write_property("recipientList", ListOf(Destination)([_destination(), destination]))  # noqa: E731

    run = await run_while_locked(session_factory, make)

    assert_waited_off_the_loop(run)
    assert _saved(session_factory) == [_entry(), _entry("192.168.1.77", pid=5)]


@pytest.mark.asyncio
async def test_add_recipients_with_a_matching_key_overwrites(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    await nc.add_recipients([_destination(confirmed=False)])
    assert [bool(d.issueConfirmedNotifications) for d in nc.recipientList] == [False]
    assert _saved(session_factory) == [_entry(confirmed=False)]


@pytest.mark.asyncio
async def test_remove_recipients_applies_and_saves(session_factory):
    app, nc = await _build(session_factory, [_entry(), _entry("192.168.1.77", pid=5)])
    await nc.remove_recipients([_destination("192.168.1.77", pid=5)])
    assert _live_pids(nc) == [1]
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_removing_the_last_entry_saves_an_empty_list(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    await nc.remove_recipients([_destination()])
    assert list(nc.recipientList) == []
    assert _saved(session_factory) == []
    with session_factory() as session:
        rebuilt = build_application(session.get(Device, 599001), list(session.get(Device, 599001).objects))
    assert list(rebuilt.get_object_name("NC-1").recipientList) == []


@pytest.mark.asyncio
async def test_failed_remove_changes_nothing(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(ListElementError) as exc:
        await nc.remove_recipients([_destination(), _destination("10.0.0.9")])
    assert (exc.value.error_code, exc.value.position) == ("listElementNotFound", 2)
    assert _live_pids(nc) == [1]
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_bad_element_fails_at_its_position_and_changes_nothing(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(ListElementError) as exc:
        await nc.add_recipients([_destination("192.168.1.77"), _destination("192.168.1.78", to_time="*:*:*.*")])
    assert (exc.value.error_class, exc.value.error_code, exc.value.position) == (
        "property",
        "valueOutOfRange",
        2,
    )
    assert _live_pids(nc) == [1]
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_process_identifier_above_32_bits_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(ListElementError) as exc:
        await nc.add_recipients([_destination("192.168.1.77", pid=2**32)])
    assert (exc.value.error_code, exc.value.position) == ("valueOutOfRange", 1)
    assert _live_pids(nc) == [1]
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_recipient_list_write_with_a_process_identifier_above_32_bits_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("recipientList", ListOf(Destination)([_destination("192.168.1.77", pid=2**32)]))
    assert exc.value.errorCode == "value-out-of-range"
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["recipientList", PropertyIdentifier("recipientList")])
async def test_whole_list_write_property_applies_and_saves(session_factory, attr):
    app, nc = await _build(session_factory, [_entry()])
    await nc.write_property(attr, ListOf(Destination)([_destination("192.168.1.77", pid=5), _destination(pid=2)]))
    assert _live_pids(nc) == [5, 2]
    assert _saved(session_factory) == [_entry("192.168.1.77", pid=5), _entry(pid=2)]


@pytest.mark.asyncio
async def test_whole_list_write_with_an_index_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError):
        await nc.write_property("recipientList", ListOf(Destination)([_destination(pid=2)]), index=1)
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_whole_list_write_with_a_bad_element_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("recipientList", ListOf(Destination)([_destination(to_time="*:*:*.*")]))
    assert exc.value.errorCode == "value-out-of-range"
    assert _live_pids(nc) == [1]
    assert _saved(session_factory) == [_entry()]


@pytest.mark.asyncio
async def test_recipient_list_write_through_the_request_decode(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyRequest(
        WritePropertyRequest(
            objectIdentifier=nc.objectIdentifier,
            propertyIdentifier="recipientList",
            propertyValue=Any(ListOf(Destination)([_destination("192.168.1.77", pid=5)])),
        )
    )
    assert isinstance(captured[0], SimpleAckPDU)
    assert _saved(session_factory) == [_entry("192.168.1.77", pid=5)]


@pytest.mark.asyncio
async def test_other_notification_class_properties_stay_read_only(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError):
        await nc.write_property("notificationClass", Unsigned(9))


@pytest.mark.asyncio
async def test_change_with_a_deleted_row_is_still_applied(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with session_factory() as session:
        session.delete(session.query(BacnetObject).filter_by(name="NC-1").one())
        session.commit()
    await nc.add_recipients([_destination("192.168.1.77", pid=5)])
    assert _live_pids(nc) == [1, 5]


@pytest.mark.asyncio
async def test_change_does_not_bump_database_revision(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with session_factory() as session:
        before = session.get(Device, 599001).database_revision
    await nc.add_recipients([_destination("192.168.1.77", pid=5)])
    with session_factory() as session:
        assert session.get(Device, 599001).database_revision == before


@pytest.mark.asyncio
async def test_whole_list_write_with_a_global_broadcast_recipient_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    global_broadcast = _destination()
    global_broadcast.recipient = Recipient(address=DeviceAddress(networkNumber=65535, macAddress=b""))
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("recipientList", ListOf(Destination)([global_broadcast]))
    assert exc.value.errorCode == "value-out-of-range"
    assert _saved(session_factory) == [_entry()]


def _saved_column(session_factory, column):
    with session_factory() as session:
        return getattr(session.query(BacnetObject).filter_by(name="NC-1").one(), column)


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["priority", PropertyIdentifier("priority")])
async def test_priority_write_applies_and_saves(session_factory, attr):
    app, nc = await _build(session_factory, [_entry()])
    await nc.write_property(attr, ArrayOf(Unsigned)([3, 4, 5]))
    assert [int(p) for p in nc.priority] == [3, 4, 5]
    assert _saved_column(session_factory, "notification_priority") == [3, 4, 5]


@pytest.mark.asyncio
async def test_priority_index_write_changes_one_slot(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    await nc.write_property("priority", Unsigned(7), index=2)
    assert [int(p) for p in nc.priority] == [100, 7, 100]
    assert _saved_column(session_factory, "notification_priority") == [100, 7, 100]


@pytest.mark.asyncio
@pytest.mark.parametrize("index, code", [(0, "writeAccessDenied"), (4, "invalidArrayIndex")])
async def test_priority_bad_index_is_refused(session_factory, index, code):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("priority", Unsigned(7), index=index)
    assert exc.value.errorCode == code
    assert [int(p) for p in nc.priority] == [100, 100, 100]
    assert _saved_column(session_factory, "notification_priority") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [[1, 2], [1, 2, 256]])
async def test_bad_priority_is_refused_and_nothing_changes(session_factory, value):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("priority", ArrayOf(Unsigned)(value))
    assert exc.value.errorCode == "value-out-of-range"
    assert [int(p) for p in nc.priority] == [100, 100, 100]
    assert _saved_column(session_factory, "notification_priority") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["ackRequired", PropertyIdentifier("ackRequired")])
async def test_ack_required_write_applies_and_saves(session_factory, attr):
    app, nc = await _build(session_factory, [_entry()])
    await nc.write_property(attr, EventTransitionBits([1, 0, 1]))
    assert list(nc.ackRequired) == [1, 0, 1]
    assert _saved_column(session_factory, "notification_ack_required") == [True, False, True]


@pytest.mark.asyncio
async def test_ack_required_index_write_is_refused(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    with pytest.raises(PropertyError) as exc:
        await nc.write_property("ackRequired", EventTransitionBits([1, 0, 1]), index=1)
    assert exc.value.errorCode == "propertyIsNotAnArray"
    assert _saved_column(session_factory, "notification_ack_required") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prop, value",
    [("priority", ArrayOf(Unsigned)([3, 4, 5])), ("ackRequired", EventTransitionBits([1, 0, 1]))],
)
async def test_priority_and_ack_saves_wait_off_the_loop(session_factory, prop, value):
    app, nc = await _build(session_factory, [_entry()])
    run = await run_while_locked(session_factory, lambda: nc.write_property(prop, value))
    assert_waited_off_the_loop(run)


@pytest.mark.asyncio
async def test_priority_write_through_the_request_decode(session_factory):
    app, nc = await _build(session_factory, [_entry()])
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_WritePropertyRequest(
        WritePropertyRequest(
            objectIdentifier=nc.objectIdentifier,
            propertyIdentifier="priority",
            propertyValue=Any(ArrayOf(Unsigned)([9, 9, 9])),
        )
    )
    assert isinstance(captured[0], SimpleAckPDU)
    assert _saved_column(session_factory, "notification_priority") == [9, 9, 9]


@pytest.mark.asyncio
async def test_a_notification_after_a_priority_write_uses_the_new_priority(session_factory):
    alarming = BacnetObject(
        object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=0.0,
    )
    app, nc = await _build(session_factory, [_entry()], extra=[alarming])
    await nc.write_property("priority", ArrayOf(Unsigned)([11, 22, 33]))
    await nc.write_property("ackRequired", EventTransitionBits([0, 1, 1]))
    event_object = app.get_object_name("AI-1")
    params = with_notification_class_slots(
        {"toState": EventState.offnormal, "priority": 0, "ackRequired": True}, event_object
    )
    assert params["priority"] == 11
    assert not params["ackRequired"]
