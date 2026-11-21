"""Tests for bacpack.objects.analog."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_gated_analog_input_object_ir_has_working_out_of_range_algorithm():
    from bacpack.objects.analog import GatedAnalogInputObjectIR
    from bacpack.objects.notification_class import (
        LocalNotificationClassObject,
        build_recipient_list,
    )
    from bacpack.models import Device
    from bacpack.objects.device import build_device_object
    from bacpypes3.app import Application

    # Application.from_object_list requires a real device object in the
    # list (confirmed by reading app.py -- it raises RuntimeError("missing
    # device object") otherwise), so every direct from_object_list call in
    # this plan's lower-level object tests includes one, same as every
    # build_application call already does internally.
    device_obj = build_device_object(Device(id=599001, name="Test Device", vendor_identifier=999))
    nc = LocalNotificationClassObject(
        objectIdentifier=("notification-class", 1),
        objectName="NC-1",
        notificationClass=1,
        priority=[100, 100, 100],
        ackRequired=[True, True, True],
        recipientList=build_recipient_list(None),
    )
    ai = GatedAnalogInputObjectIR(
        objectIdentifier=("analog-input", 1),
        objectName="AI-1",
        presentValue=0.0,
        units="degreesFahrenheit",
        covIncrement=1.0,
        notificationClass=1,
        highLimit=100.0,
        lowLimit=0.0,
        deadband=1.0,
        limitEnable=[True, True],
        timeDelay=0,
        timeDelayNormal=None,
        eventEnable=[True, True, True],
        ackedTransitions=[True, True, True],  # nothing pending ack on a fresh/still-normal object
        notifyType="alarm",
        eventTimeStamps=[None, None, None],
        eventDetectionEnable=True,
        eventState="normal",  # required -- the algorithm only transitions out of "normal"
    )
    import asyncio

    Application.from_object_list([device_obj, nc, ai])
    await asyncio.sleep(0)  # let _post_init's ensure_future run

    ai.presentValue = 150.0  # above highLimit
    await asyncio.sleep(0)  # let the property-monitor callback run
    assert ai.eventState.attr == "highLimit"
