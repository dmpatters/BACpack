"""Tests for bacpack.objects.binary -- see test_write_paths.py for the
generic cross-type write-path tests (allowlisting, write-locking,
relinquish, feed/manual writes) that these are deliberately kept
separate from.
"""
from __future__ import annotations

import pytest
from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.errors import PropertyError

from bacpack.object_builder import build_bacnet_object
from bacpack.models import BacnetObject


def _typed(obj, value):
    element_class = obj._elements["presentValue"]
    return element_class(element_class.cast(value))


@pytest.mark.asyncio
async def test_binary_value_rejects_out_of_range_write():
    """bacpypes3's Commandable.write_property() decodes and stores whatever
    value it's given with no check that it's a legal BinaryPV (0/1) --
    verified against a real client (YABE) WriteProperty-ing a binary-value
    to 15. See BinaryRangeValidated / _validate_binary_value in bacnet.py.
    """
    obj = build_bacnet_object(
        BacnetObject(object_type="binary-value", instance_number=1, name="BV-1")
    )

    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 1), priority=8)
    assert str(obj.presentValue) == "active"

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 15), priority=8)
    assert str(obj.presentValue) == "active"  # unchanged by the rejected write


@pytest.mark.asyncio
async def test_gated_binary_input_object_ir_has_working_change_of_state_algorithm():
    from bacpack.objects.binary import GatedBinaryInputObjectIR
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
    bi = GatedBinaryInputObjectIR(
        objectIdentifier=("binary-input", 1),
        objectName="BI-1",
        presentValue="inactive",
        polarity="normal",
        notificationClass=1,
        alarmValue="active",
        timeDelay=0,
        timeDelayNormal=None,
        eventEnable=[True, True, True],
        ackedTransitions=[True, True, True],  # nothing pending ack on a fresh/still-normal object
        notifyType="alarm",
        eventTimeStamps=[None, None, None],
        eventDetectionEnable=True,
        eventState="normal",  # required -- the algorithm only transitions out of "normal"
    )
    Application.from_object_list([device_obj, nc, bi])
    await asyncio.sleep(0)

    bi.presentValue = "active"  # matches alarmValue
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bi.eventState.attr == "offnormal"


@pytest.mark.asyncio
async def test_validated_binary_value_object_cmd_ir_has_working_change_of_state_algorithm():
    """bacpypes3's own upstream
    BinaryValueObjectIR (bacpypes3/local/binary.py) is unusable as shipped
    -- its base, BinaryValueObjectCmd(Commandable, _BinaryValueObject),
    omits bacpypes3.local.object.Object (unlike every sibling class in
    that file), so EventAlgorithm.__init__ crashes on a missing
    _fault_algorithm attribute the moment an alarming-enabled binary-value
    object is constructed. Confirmed directly against the installed
    0.0.106:

        from bacpypes3.local.binary import BinaryValueObjectIR
        from bacpypes3.local.object import Object as LocalObject
        print(LocalObject in BinaryValueObjectIR.__mro__)  # False

    This test constructs a real ValidatedBinaryValueObjectCmdIR inside a
    real Application.from_object_list (same pattern as the
    GatedBinaryInputObjectIR test above) and drives a real presentValue
    transition -- would raise AttributeError against the old code (which
    composed directly on upstream's broken BinaryValueObjectIR).
    """
    from bacpack.objects.binary import ValidatedBinaryValueObjectCmdIR
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
    bv = ValidatedBinaryValueObjectCmdIR(
        objectIdentifier=("binary-value", 1),
        objectName="BV-1",
        presentValue="inactive",
        notificationClass=1,
        alarmValue="active",
        timeDelay=0,
        timeDelayNormal=None,
        eventEnable=[True, True, True],
        ackedTransitions=[True, True, True],  # nothing pending ack on a fresh/still-normal object
        notifyType="alarm",
        eventTimeStamps=[None, None, None],
        eventDetectionEnable=True,
        eventState="normal",  # required -- the algorithm only transitions out of "normal"
    )
    Application.from_object_list([device_obj, nc, bv])
    await asyncio.sleep(0)

    bv.presentValue = "active"  # matches alarmValue
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bv.eventState.attr == "offnormal"


@pytest.mark.asyncio
@pytest.mark.parametrize("alarming", [False, True])
async def test_binary_input_out_of_service_rejects_out_of_range_write(alarming):
    extra = dict(alarming_enabled=True, notification_class_instance=1, alarm_value=True) if alarming else {}
    obj = build_bacnet_object(
        BacnetObject(object_type="binary-input", instance_number=1, name="BI-1", out_of_service_default=True, **extra)
    )

    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 1))
    assert str(obj.presentValue) == "active"

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 7))
    assert str(obj.presentValue) == "active"
