"""live_swap: replacing one live object with a rebuilt one -- runtime state
carried, COV subscriptions moved, timers on the retired object cancelled."""
from __future__ import annotations

import asyncio
import logging

import pytest
from bacpypes3.apdu import SubscribeCOVRequest
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Null, Real, Unsigned

from bacpack import bacpypes3_compat, live_swap, object_config
from bacpack.bacnet import write_manual_value
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application, build_bacnet_object

pytestmark = pytest.mark.asyncio


def _app(*rows):
    return build_application(Device(id=599001, name="D", vendor_identifier=999), list(rows))


def _edited(row: BacnetObject, **changes) -> BacnetObject:
    return BacnetObject(**{**object_config.row_from_object(row), **changes})


async def _swap(app, row, **changes):
    old = app.get_object_name(row.name)
    new = build_bacnet_object(_edited(row, **changes))
    taken = live_swap.retire(app, old)
    live_swap.carry_runtime(old, new, frozenset(changes))
    live_swap.attach(app, new, taken)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return new


def _av(**fields):
    return BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", relinquish_default="0", **fields)


async def test_priority_slots_carry_and_present_value_follows_them():
    row = _av()
    app = _app(row)
    await app.get_object_name("AV-1").write_property("presentValue", Real(42.0), priority=8)
    new = await _swap(app, row, description="edited")
    assert new.priorityArray[7].real == 42.0
    assert new.presentValue == 42.0
    assert app.get_object_name("AV-1") is new


async def test_an_edited_relinquish_default_moves_an_unwritten_present_value():
    row = _av()
    app = _app(row)
    new = await _swap(app, row, relinquish_default="5")
    assert new.presentValue == 5.0


async def test_an_edited_relinquish_default_does_not_beat_a_written_slot():
    row = _av()
    app = _app(row)
    await app.get_object_name("AV-1").write_property("presentValue", Real(42.0), priority=8)
    new = await _swap(app, row, relinquish_default="5")
    assert new.presentValue == 42.0


async def test_a_slot_naming_a_state_that_no_longer_exists_is_dropped(caplog):
    row = BacnetObject(
        object_type="multi-state-value", instance_number=1, name="MSV-1",
        state_text=["Off", "Low", "High"], relinquish_default="1",
    )
    app = _app(row)
    await app.get_object_name("MSV-1").write_property("presentValue", Unsigned(3), priority=8)
    new = await _swap(app, row, state_text=["Off", "Low"])
    assert new.priorityArray[7].null is not None
    assert new.presentValue == 1
    assert "priority 8" in caplog.text


async def test_an_inputs_present_value_carries():
    row = BacnetObject(object_type="analog-input", instance_number=1, name="AI-1")
    app = _app(row)
    app.get_object_name("AI-1").presentValue = 12.5
    new = await _swap(app, row, description="edited")
    assert new.presentValue == 12.5


async def test_out_of_service_carries_unless_its_default_was_edited():
    row = _av()
    app = _app(row)
    app.get_object_name("AV-1").outOfService = True
    kept = await _swap(app, row, description="edited")
    assert bool(kept.outOfService) is True

    row2 = _av()
    app2 = _app(row2)
    edited = await _swap(app2, row2, out_of_service_default=True)
    assert bool(edited.outOfService) is True  # the edited default wins over the live False


async def test_reliability_and_the_proxy_hook_carry():
    row = _av()
    app = _app(row)
    old = app.get_object_name("AV-1")
    old.reliability = "communicationFailure"

    async def forward(value, priority):
        return None

    old._proxy_forward = forward
    new = await _swap(app, row, description="edited")
    assert new.reliability.attr == "communicationFailure"
    assert new._proxy_forward is forward


def _alarming_ai(**fields):
    defaults = dict(
        object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=0.0,
        alarm_deadband=1.0, alarm_time_delay=0,
    )
    return BacnetObject(**{**defaults, **fields})


def _nc():
    return BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")


_RECIPIENT = {
    "recipient": {"kind": "address", "address": "192.168.1.200"},
    "valid_days": [True] * 7,
    "from_time": "00:00:00.00",
    "to_time": "23:59:59.99",
    "transitions": [True, True, True],
    "confirmed": False,
    "process_identifier": 1,
}


def _nc_with_recipient():
    return BacnetObject(
        object_type="notification-class", instance_number=1, name="NC-1", notification_recipient_list=[_RECIPIENT]
    )


def _capture_notifications(app) -> list:
    """The event notifications the app sends, in order."""
    sent = []

    def capture(apdu):
        sent.append(apdu)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    app.request = capture
    return sent


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def _in_high_alarm(app):
    ai = app.get_object_name("AI-1")
    await asyncio.sleep(0)  # _post_init links the notification class
    ai.presentValue = 150.0
    for _ in range(3):
        await asyncio.sleep(0)
    assert ai.eventState.attr == "highLimit"
    return ai


async def test_alarm_state_carries_and_still_out_of_range_does_not_refire():
    row = _alarming_ai()
    app = _app(_nc_with_recipient(), row)
    sent = _capture_notifications(app)
    old = await _in_high_alarm(app)
    await _settle()
    assert [str(n.toState) for n in sent] == ["high-limit"]
    stamp = old.eventTimeStamps[0]
    acked = list(old.ackedTransitions)
    new = await _swap(app, row, alarm_high_limit=120.0)
    await _settle()
    assert new.eventState.attr == "highLimit"
    assert new.eventTimeStamps[0] == stamp
    assert list(new.ackedTransitions) == acked
    # A rebuilt object that started normal would go to highLimit again and notify a second time.
    assert [str(n.toState) for n in sent] == ["high-limit"]


async def test_raising_the_limit_past_the_value_returns_to_normal():
    row = _alarming_ai()
    app = _app(_nc_with_recipient(), row)
    sent = _capture_notifications(app)
    old = await _in_high_alarm(app)
    offnormal_stamp, normal_stamp = old.eventTimeStamps[0], old.eventTimeStamps[2]
    new = await _swap(app, row, alarm_high_limit=200.0)
    await _settle()
    assert new.eventState.attr == "normal"
    assert [str(n.toState) for n in sent] == ["high-limit", "normal"]
    # Through a to-normal transition from the carried alarm, not a fresh start.
    assert new.eventTimeStamps[0] == offnormal_stamp
    assert new.eventTimeStamps[2] != normal_stamp


async def test_turning_detection_off_resets_the_event_state_instead_of_carrying_it():
    # ASHRAE 135: with Event_Detection_Enable FALSE, Acked_Transitions and
    # Event_Time_Stamps go back to their initial values. Carried, the
    # to-normal bit stayed unacknowledged with no time stamp to ack it by.
    from bacpack.objects.alarm_config import unset_time_stamp

    row = _alarming_ai()
    app = _app(_nc(), row)
    old = await _in_high_alarm(app)
    assert list(old.ackedTransitions) != [True, True, True]
    new = await _swap(app, row, alarm_event_detection_enable=False)
    for _ in range(3):
        await asyncio.sleep(0)
    assert new.eventState.attr == "normal"
    assert list(new.ackedTransitions) == [True, True, True]
    assert list(new.eventTimeStamps) == [unset_time_stamp() for _ in range(3)]


async def test_retire_cancels_a_pending_alarm_transition():
    row = _alarming_ai(alarm_time_delay=60)
    app = _app(_nc(), row)
    ai = app.get_object_name("AI-1")
    await asyncio.sleep(0)
    ai.presentValue = 150.0
    for _ in range(3):
        await asyncio.sleep(0)
    algorithm = bacpypes3_compat.event_algorithm(ai)
    assert bacpypes3_compat.has_pending_transition(algorithm)
    live_swap.retire(app, ai)
    assert not bacpypes3_compat.has_pending_transition(algorithm)


async def test_retire_cancels_a_queued_alarm_evaluation():
    app = _app(_nc(), _alarming_ai())
    ai = app.get_object_name("AI-1")
    for _ in range(3):
        await asyncio.sleep(0)
    algorithm = bacpypes3_compat.event_algorithm(ai)
    bacpypes3_compat.schedule_algorithm_run(algorithm)  # what a value change just before the swap queues
    handle = algorithm._execute_handle
    live_swap.retire(app, ai)
    assert handle.cancelled()


async def test_retire_cancels_a_schedules_pending_interpretation():
    row = BacnetObject(
        object_type="schedule", instance_number=1, name="SCH-1", schedule_value_type="analog",
        schedule_default="0", schedule_priority=16,
        weekly_schedule=[[{"time": "08:00", "value": "1"}] for _ in range(7)],
    )
    app = _app(row)
    await asyncio.sleep(0)  # interpret_schedule arms the next-transition timer
    schedule = app.get_object_name("SCH-1")
    handle = schedule._interpret_schedule_handle
    assert handle is not None
    live_swap.retire(app, schedule)
    assert handle.cancelled()


async def test_a_cov_subscription_moves_with_its_lifetime_and_notifies_once():
    row = _av()
    app = _app(row)
    sent = []
    app.cov_notification = lambda cov, apdu: sent.append(apdu)
    app.response = lambda apdu: asyncio.sleep(0)
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=3, monitoredObjectIdentifier=("analog-value", 1),
        issueConfirmedNotifications=False, lifetime=300,
    )
    request.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(request)
    await asyncio.sleep(0)
    sent.clear()
    new = await _swap(app, row, description="edited")
    assert len(sent) == 1
    await new.write_property("presentValue", Real(9.0), priority=8)
    await asyncio.sleep(0)
    assert len(sent) == 2


async def test_a_retired_object_without_attach_keeps_no_subscription():
    row = _av()
    app = _app(row)
    app.response = lambda apdu: asyncio.sleep(0)
    app.cov_notification = lambda cov, apdu: None
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=3, monitoredObjectIdentifier=("analog-value", 1),
        issueConfirmedNotifications=False, lifetime=300,
    )
    request.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(request)
    live_swap.retire(app, app.get_object_name("AV-1"))
    assert app.get_object_name("AV-1") is None
    assert len(app.device_object.activeCovSubscriptions) == 0


async def test_relink_notification_class_moves_linked_objects():
    row = _alarming_ai()
    nc_row = _nc()
    app = _app(nc_row, row)
    await asyncio.sleep(0)
    ai = app.get_object_name("AI-1")
    old_nc = app.get_object_name("NC-1")
    assert bacpypes3_compat.notification_class_object(ai) is old_nc
    new_nc = build_bacnet_object(_edited(nc_row, description="edited"))
    taken = live_swap.retire(app, old_nc)
    live_swap.attach(app, new_nc, taken)
    live_swap.relink_notification_class(app, old_nc, new_nc)
    assert bacpypes3_compat.notification_class_object(ai) is new_nc


# --- A proxy write that waits out a swap ------------------------------------

def _gated_proxy(obj):
    gate = asyncio.Event()
    sent = []

    async def forward(value, priority):
        sent.append((value, priority))
        await gate.wait()

    obj._proxy_forward = forward
    return gate, sent


async def test_a_proxy_write_waiting_on_its_remote_lands_on_the_swapped_in_object():
    row = _av()
    app = _app(row)
    old = app.get_object_name("AV-1")
    gate, sent = _gated_proxy(old)
    write = asyncio.ensure_future(old.write_property("presentValue", Real(42.0), priority=8))
    await asyncio.sleep(0)
    new = await _swap(app, row, description="edited")
    gate.set()
    await write
    assert len(sent) == 1  # forwarded once, not again from the new object
    assert new.priorityArray[7].real == 42.0
    assert new.presentValue == 42.0


async def test_a_proxy_write_whose_object_was_deleted_meanwhile_lands_nowhere():
    row = _av()
    app = _app(row)
    old = app.get_object_name("AV-1")
    gate, _ = _gated_proxy(old)
    write = asyncio.ensure_future(old.write_property("presentValue", Real(42.0), priority=8))
    await asyncio.sleep(0)
    live_swap.retire(app, old)
    gate.set()
    await write  # no exception
    assert app.get_object_name("AV-1") is None
    assert old.priorityArray[7].null is not None


async def test_a_manual_relinquish_waiting_on_its_remote_clears_the_swapped_in_object():
    row = _av()
    app = _app(row)
    old = app.get_object_name("AV-1")
    await old.write_property("presentValue", Real(42.0), priority=8)
    gate, _ = _gated_proxy(old)
    clear = asyncio.ensure_future(write_manual_value(old, None))
    await asyncio.sleep(0)
    new = await _swap(app, row, description="edited")
    assert new.priorityArray[7].real == 42.0
    gate.set()
    await clear
    assert new.priorityArray[7].null is not None


async def test_a_proxy_write_follows_its_renumbered_row_not_the_identifier():
    # AV-1 is renumbered 1 -> 3 while its forward waits, and
    # AV-2 (another proxy) takes instance 1 in the same apply. The write
    # belongs to AV-1's row, now at instance 3.
    row1 = _av(id=1)
    row2 = BacnetObject(id=2, object_type="analog-value", instance_number=2, name="AV-2", relinquish_default="0")
    app = _app(row1, row2)
    old1, old2 = app.get_object_name("AV-1"), app.get_object_name("AV-2")
    gate, sent = _gated_proxy(old1)
    write = asyncio.ensure_future(old1.write_property("presentValue", Real(42.0), priority=8))
    await asyncio.sleep(0)
    live_swap.retire(app, old1)
    live_swap.retire(app, old2)
    new1 = build_bacnet_object(_edited(row1, instance_number=3))
    new2 = build_bacnet_object(_edited(row2, instance_number=1))
    for new in (new1, new2):
        _gated_proxy(new)  # the runner attaches a hook to each live proxy every tick
        live_swap.attach(app, new, [])
    gate.set()
    await write
    assert len(sent) == 1
    assert new1.priorityArray[7].real == 42.0
    assert new2.priorityArray[7].null is not None


async def test_a_proxy_write_does_not_follow_its_row_to_another_object_type():
    # Only a direct DB edit re-types a row in place; the object now serving
    # that row id is a different object, so the write lands nowhere.
    row = _av(id=1)
    app = _app(row)
    old = app.get_object_name("AV-1")
    gate, sent = _gated_proxy(old)
    write = asyncio.ensure_future(old.write_property("presentValue", Real(42.0), priority=8))
    await asyncio.sleep(0)
    live_swap.retire(app, old)
    new = build_bacnet_object(_edited(row, object_type="analog-output"))
    _gated_proxy(new)
    live_swap.attach(app, new, [])
    gate.set()
    await write
    assert len(sent) == 1
    assert new.priorityArray[7].null is not None


def _msv(**fields):
    return BacnetObject(object_type="multi-state-value", instance_number=1, name="MSV-1",
                        state_text=["A", "B", "C"], relinquish_default="1", **fields)


async def test_a_proxy_write_past_a_swapped_in_objects_states_is_not_applied(caplog):
    # Number_Of_States narrowed 3 -> 2 while the remote answers.
    row = _msv()
    app = _app(row)
    old = app.get_object_name("MSV-1")
    gate, sent = _gated_proxy(old)
    write = asyncio.ensure_future(old.write_property("presentValue", Unsigned(3), priority=8))
    await asyncio.sleep(0)
    new = await _swap(app, row, state_text=["A", "B"])
    gate.set()
    with caplog.at_level(logging.WARNING, logger="bacpack.bacnet"):
        await write
    assert len(sent) == 1  # the remote took it
    assert new.priorityArray[7].null is not None
    assert int(new.presentValue) <= 2
    assert "not applied locally" in caplog.text


async def test_a_proxy_write_past_a_state_count_shrunk_meanwhile_is_not_applied(caplog):
    # Same gap without a swap: the states shrink on the same object (as a
    # State_Text write does) while the remote answers.
    app = _app(_msv())
    obj = app.get_object_name("MSV-1")
    gate, _ = _gated_proxy(obj)
    write = asyncio.ensure_future(obj.write_property("presentValue", Unsigned(3), priority=8))
    await asyncio.sleep(0)
    obj.numberOfStates = 2
    gate.set()
    with caplog.at_level(logging.WARNING, logger="bacpack.bacnet"):
        await write
    assert obj.priorityArray[7].null is not None
    assert "not applied locally" in caplog.text


async def test_a_proxied_relinquish_is_not_range_checked():
    app = _app(_msv())
    obj = app.get_object_name("MSV-1")
    await obj.write_property("presentValue", Unsigned(3), priority=8)
    gate, _ = _gated_proxy(obj)
    write = asyncio.ensure_future(obj.write_property("presentValue", Null(()), priority=8))
    await asyncio.sleep(0)
    obj.numberOfStates = 2
    gate.set()
    await write
    assert obj.priorityArray[7].null is not None


# --- A value-type change doesn't carry the old value ------------------------

async def test_an_out_of_season_schedule_does_not_keep_its_old_value_type():
    # Out of season it holds its value; carrying the analog Real 5.0 onto a
    # binary schedule would serve a Real until it next came into season.
    row = BacnetObject(
        object_type="schedule", instance_number=1, name="SCH-1", schedule_value_type="analog",
        schedule_default="5.0", schedule_priority=16,
        weekly_schedule=[[{"time": "00:00", "value": "10.0"}] for _ in range(7)],
        schedule_effective_start="2099-01-01", schedule_effective_end="2099-12-31",
    )
    app = _app(row)
    for _ in range(3):
        await asyncio.sleep(0)
    new = await _swap(
        app, row, schedule_value_type="binary", schedule_default="active",
        weekly_schedule=[[{"time": "00:00", "value": "active"}] for _ in range(7)],
    )
    for _ in range(3):
        await asyncio.sleep(0)
    assert not isinstance(new.presentValue.get_value(), float)


async def test_a_start_value_edit_on_a_running_input_keeps_the_live_value():
    """The new start value applies at the next start; the running input keeps
    the value it has (carry_runtime carries Present_Value)."""
    row = BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", start_value="5")
    app = _app(row)
    app.get_object_name("AI-1").presentValue = 17.0
    new = await _swap(app, row, start_value="9")
    assert new.presentValue == 17.0
