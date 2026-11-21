"""Tests for bacpack.objects.multistate -- see test_write_paths.py for
the generic cross-type write-path tests kept separate from these."""
from __future__ import annotations

import pytest
from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.errors import PropertyError

from bacpack.object_builder import build_bacnet_object
from bacpack.models import BacnetObject, Device
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked
from tests.session_fakes import CommitFailingSessionFactory


def _typed(obj, value):
    element_class = obj._elements["presentValue"]
    return element_class(element_class.cast(value))


@pytest.mark.asyncio
async def test_multistate_value_rejects_out_of_range_write():
    """Same upstream gap as binary-value, for multi-state Present_Value's
    legal range of [1, Number_Of_States] -- verified live with YABE writing
    50 to a 3-state multi-state-value. See MultiStateRangeValidated in
    bacnet.py.
    """
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Unoccupied", "Occupied", "Standby"],
        )
    )

    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 2), priority=8)
    assert obj.presentValue == 2

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 50), priority=8)
    assert obj.presentValue == 2  # unchanged by the rejected write


@pytest.mark.asyncio
async def test_gated_multistate_input_object_ir_has_working_change_of_state_algorithm():
    from bacpack.objects.multistate import GatedMultiStateInputObjectIR
    from bacpack.objects.notification_class import (
        LocalNotificationClassObject,
        build_recipient_list,
    )
    from bacpack.models import Device
    from bacpack.objects.device import build_device_object
    from bacpypes3.app import Application
    import asyncio

    device_obj = build_device_object(Device(id=599001, name="Test Device", vendor_identifier=999))
    nc = LocalNotificationClassObject(
        objectIdentifier=("notification-class", 1),
        objectName="NC-1",
        notificationClass=1,
        priority=[100, 100, 100],
        ackRequired=[True, True, True],
        recipientList=build_recipient_list(None),
    )
    msi = GatedMultiStateInputObjectIR(
        objectIdentifier=("multi-state-input", 1),
        objectName="MSI-1",
        presentValue=1,
        numberOfStates=3,
        stateText=["Off", "Low", "High"],
        notificationClass=1,
        alarmValues=[3],
        timeDelay=0,
        timeDelayNormal=None,
        eventEnable=[True, True, True],
        ackedTransitions=[True, True, True],  # nothing pending ack on a fresh/still-normal object
        notifyType="alarm",
        eventTimeStamps=[None, None, None],
        eventDetectionEnable=True,
        eventState="normal",  # required -- the algorithm only transitions out of "normal"
    )
    Application.from_object_list([device_obj, nc, msi])
    await asyncio.sleep(0)

    msi.presentValue = 3  # matches an alarmValues entry
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert msi.eventState.attr == "offnormal"


# --- Commandable Multi-state Value ------------------------------------------
# MSV matches Binary Value.


def _msv(**extra):
    return build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1",
            state_text=["Off", "Low", "High"], **extra,
        )
    )


def _msv_slot(obj, priority):
    value = obj.priorityArray[priority - 1]
    return None if value._choice in (None, "null") else int(getattr(value, value._choice))


@pytest.mark.asyncio
async def test_msv_has_a_priority_array_and_lowest_slot_wins():
    from bacpypes3.primitivedata import Null

    obj = _msv(relinquish_default="2")
    assert len(obj.priorityArray) == 16
    assert obj.relinquishDefault == 2
    assert obj.presentValue == 2

    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 3), priority=10)
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 1), priority=8)
    assert obj.presentValue == 1
    assert obj.currentCommandPriority.unsigned == 8
    assert (_msv_slot(obj, 8), _msv_slot(obj, 10)) == (1, 3)

    await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=8)
    assert obj.presentValue == 3
    await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=10)
    assert obj.presentValue == 2  # back to Relinquish_Default
    assert obj.currentCommandPriority._choice == "null"


@pytest.mark.asyncio
async def test_msv_write_without_priority_lands_in_slot_16():
    obj = _msv()
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 3))
    assert _msv_slot(obj, 16) == 3
    assert obj.presentValue == 3


@pytest.mark.asyncio
async def test_msv_without_a_relinquish_default_starts_at_state_1():
    # Upgrade behaviour: an existing MSV with relinquish_default null looks
    # exactly as before. Commandable takes the initial Present_Value as the default.
    obj = _msv()
    assert obj.presentValue == 1
    assert obj.relinquishDefault == 1


@pytest.mark.asyncio
async def test_msv_out_of_range_priority_write_still_refused():
    obj = _msv()
    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 4), priority=8)
    assert _msv_slot(obj, 8) is None


@pytest.mark.asyncio
async def test_alarming_msv_is_commandable_too():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1",
            state_text=["Off", "Low", "High"], relinquish_default="1",
            alarming_enabled=True, notification_class_instance=1, alarm_values=[3],
        )
    )
    assert len(obj.priorityArray) == 16
    await obj.write_property("relinquishDefault", _typed(obj, 2))
    assert obj.presentValue == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("alarming", [False, True])
@pytest.mark.parametrize("bad", [0, 50])
async def test_multistate_input_out_of_service_rejects_out_of_range_write(alarming, bad):
    extra = dict(alarming_enabled=True, notification_class_instance=1, alarm_values=[3]) if alarming else {}
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-input", instance_number=1, name="MSI-1",
            state_text=["Off", "Low", "High"], out_of_service_default=True, **extra,
        )
    )

    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 2))
    assert obj.presentValue == 2

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, bad))
    assert obj.presentValue == 2


# --- State_Text saves ---------------------------------------------------------


def _saved_msv(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device, object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "On"]
        )
        session.add_all([device, row])
        session.commit()
        object_id = row.id
    with session_factory() as session:
        return object_id, build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)


@pytest.mark.asyncio
async def test_state_text_save_waits_for_a_held_lock_off_the_loop(session_factory):
    object_id, obj = _saved_msv(session_factory)

    run = await run_while_locked(session_factory, lambda: obj.write_property("stateText", ["Off", "Low", "High"]))

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Low", "High"]


@pytest.mark.asyncio
async def test_state_text_failed_save_is_logged_and_the_write_still_succeeds(session_factory, caplog):
    object_id, obj = _saved_msv(session_factory)
    obj._state_text_session_factory = CommitFailingSessionFactory(session_factory)

    await obj.write_property("stateText", ["Off", "Low", "High"])

    assert obj.numberOfStates == 3
    assert "could not save" in caplog.text
