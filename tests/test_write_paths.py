"""Covers the object behaviour rules: OOS-gated writes
for Input objects, and fixed-priority writes for Output/Value objects.
"""
from __future__ import annotations

import pytest
from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import CharacterString, Null

from bacpack.object_builder import build_bacnet_object
from bacpack.bacnet import (
    FEED_PRIORITY,
    MANUAL_PRIORITY,
    write_feed_value,
    write_manual_value,
)
from bacpack.models import BacnetObject, Device
from bacpack.point_config import validate_relinquish_default


def _typed(obj, value):
    element_class = obj._elements["presentValue"]
    return element_class(element_class.cast(value))


@pytest.mark.asyncio
async def test_input_object_rejects_direct_write_unless_out_of_service():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", units="percent"
        )
    )

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 42.0))

    obj.outOfService = True
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 42.0))
    assert obj.presentValue == 42.0


@pytest.mark.asyncio
async def test_feed_writes_input_only_while_not_out_of_service():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", units="percent"
        )
    )

    assert await write_feed_value(obj, 10.0) is True
    assert obj.presentValue == 10.0

    obj.outOfService = True
    assert await write_feed_value(obj, 99.0) is False
    assert obj.presentValue == 10.0  # unchanged -- feed paused


@pytest.mark.asyncio
async def test_feed_writes_output_regardless_of_out_of_service():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-output", instance_number=1, name="AO-1", units="percent"
        )
    )

    obj.outOfService = True
    assert await write_feed_value(obj, 55.0) is True
    assert obj.presentValue == 55.0


@pytest.mark.asyncio
async def test_manual_write_outranks_feed_write():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", units="percent"
        )
    )

    await write_feed_value(obj, 5.0)
    assert obj.presentValue == 5.0

    await write_manual_value(obj, 7.0)
    assert obj.presentValue == 7.0  # MANUAL_PRIORITY (8) outranks FEED_PRIORITY (10)

    # a later feed write doesn't override the still-active manual value
    await write_feed_value(obj, 6.0)
    assert obj.presentValue == 7.0

    assert MANUAL_PRIORITY < FEED_PRIORITY


@pytest.mark.asyncio
async def test_manual_write_rejected_on_input_object():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", units="percent"
        )
    )

    with pytest.raises(TypeError):
        await write_manual_value(obj, 1.0)


@pytest.mark.asyncio
async def test_manual_write_relinquish_falls_back_to_feed_value():
    """value=None on the manual write path clears the manual (priority 8)
    slot without hitting bacpypes3's PriorityValue(None) bug -- see
    _relinquish() in bacnet.py. Once relinquished, the still-active feed
    write at FEED_PRIORITY (10) takes back over.
    """
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", units="percent"
        )
    )

    await write_feed_value(obj, 5.0)
    await write_manual_value(obj, 7.0)
    assert obj.presentValue == 7.0

    await write_manual_value(obj, None)
    assert obj.presentValue == 5.0  # back to the feed's value


@pytest.mark.asyncio
async def test_manual_write_relinquish_falls_back_to_relinquish_default():
    """With no feed write active either, relinquishing the manual slot
    drops all the way to relinquish-default.
    """
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", units="percent"
        )
    )
    relinquish_default = obj.relinquishDefault

    await write_manual_value(obj, 7.0)
    assert obj.presentValue == 7.0

    await write_manual_value(obj, None)
    assert obj.presentValue == relinquish_default


@pytest.mark.asyncio
async def test_feed_write_relinquish():
    """value=None on the feed write path (e.g. a gateway poller that's lost
    its source) clears FEED_PRIORITY without disturbing a lower-priority
    manual override.
    """
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value", instance_number=1, name="AV-1", units="percent"
        )
    )
    relinquish_default = obj.relinquishDefault

    await write_feed_value(obj, 5.0)
    assert obj.presentValue == 5.0

    assert await write_feed_value(obj, None) is True
    assert obj.presentValue == relinquish_default

    # a manual override still outranks a subsequent feed write
    await write_manual_value(obj, 9.0)
    await write_feed_value(obj, 5.0)
    assert obj.presentValue == 9.0


# --- relinquish_default wiring (found live: stored but never consulted) ----
# See validate_relinquish_default/relinquish_default_for and the
# COMMANDABLE_TYPES comment in bacnet.py.


@pytest.mark.asyncio
async def test_configured_relinquish_default_applied_for_analog_value():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            relinquish_default="42.5",
        )
    )
    assert obj.relinquishDefault == 42.5


@pytest.mark.asyncio
async def test_configured_relinquish_default_applied_for_binary_output():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="binary-output",
            instance_number=1,
            name="BO-1",
            relinquish_default="active",
        )
    )
    assert str(obj.relinquishDefault) == "active"


@pytest.mark.asyncio
async def test_configured_relinquish_default_applied_for_multistate_output():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-output",
            instance_number=1,
            name="MSO-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="2",
        )
    )
    assert obj.relinquishDefault == 2


@pytest.mark.asyncio
async def test_configured_relinquish_default_applied_for_multistate_value():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="3",
        )
    )
    assert obj.relinquishDefault == 3
    assert obj.presentValue == 3


def test_validate_relinquish_default_rejects_out_of_range_multistate_value():
    for bad in ("0", "4", "foo"):
        with pytest.raises(ValueError, match="not a valid relinquish-default"):
            validate_relinquish_default("multi-state-value", ["Off", "Low", "High"], bad)
    validate_relinquish_default("multi-state-value", ["Off", "Low", "High"], "3")


@pytest.mark.asyncio
async def test_msv_state_text_shrink_refused_below_a_commanded_slot():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1",
            state_text=["Off", "Low", "High"],
        )
    )
    await write_manual_value(obj, 3)  # slot 8 holds state 3
    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "Low"])
    assert list(obj.stateText) == ["Off", "Low", "High"]
    assert obj.numberOfStates == 3


@pytest.mark.asyncio
async def test_relinquish_default_ignored_for_non_commandable_type():
    # analog-input has no priority array -- a configured relinquish_default
    # is silently ignored rather than erroring, same as units validation
    # being skipped for non-analog types.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-input",
            instance_number=1,
            name="AI-1",
            units="percent",
            relinquish_default="42.5",
        )
    )
    assert not hasattr(obj, "priorityArray")


@pytest.mark.asyncio
async def test_relinquishing_falls_back_to_configured_default():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            relinquish_default="42.5",
        )
    )
    await write_feed_value(obj, 5.0)
    assert obj.presentValue == 5.0

    await write_feed_value(obj, None)
    assert obj.presentValue == 42.5


def test_validate_relinquish_default_rejects_bad_analog_value():
    with pytest.raises(ValueError, match="not a valid relinquish-default"):
        validate_relinquish_default("analog-value", None, "not-a-number")


def test_validate_relinquish_default_rejects_bad_binary_value():
    with pytest.raises(ValueError, match="not a valid relinquish-default"):
        validate_relinquish_default("binary-value", None, "on")


def test_validate_relinquish_default_rejects_out_of_range_multistate():
    with pytest.raises(ValueError, match="not a valid relinquish-default"):
        validate_relinquish_default("multi-state-output", ["Off", "On"], "5")


def test_validate_relinquish_default_ignores_non_commandable_type():
    # No priority array to hold a relinquish-default on an input object --
    # nothing to validate, so this must not raise even for a nonsense value.
    validate_relinquish_default("analog-input", None, "not-a-number")


# --- write_feed_value(obj, None) on a non-commandable object ---------------
# Found in testing: `not hasattr(obj,
# "priorityArray")` (Input types, and multi-state-value -- see
# COMMANDABLE_TYPES) used to cast `value` straight into presentValue with
# no None special-case, and every `_elements["presentValue"]` caster
# (Real/Unsigned/BinaryPV) raises TypeError on None. This is exactly the
# path a connector poller's `_relinquish()` takes when a mapping targets an
# Input object -- ordinary config, not exotic -- and the exception used to
# escape uncaught and kill the whole connector scheduler task (see
# test_connectors.py's test_transport_failure_against_input_mapping_*).


@pytest.mark.asyncio
async def test_write_feed_value_none_on_analog_input_reverts_to_class_default():
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", units="percent")
    )
    await write_feed_value(obj, 42.0)
    assert obj.presentValue == 42.0

    assert await write_feed_value(obj, None) is True
    assert obj.presentValue == 0.0  # the same class default build_bacnet_object seeds


@pytest.mark.asyncio
async def test_write_feed_value_none_on_binary_input_reverts_to_class_default():
    obj = build_bacnet_object(
        BacnetObject(object_type="binary-input", instance_number=1, name="BI-1")
    )
    await write_feed_value(obj, "active")
    assert str(obj.presentValue) == "active"

    assert await write_feed_value(obj, None) is True
    assert str(obj.presentValue) == "inactive"


@pytest.mark.asyncio
async def test_write_feed_value_none_on_multistate_value_releases_slot_10():
    # MSV is commandable. Before that it had no priority array and
    # this relinquish reverted it to the class default, like an Input.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="2",
        )
    )
    await write_feed_value(obj, 3)
    assert obj.presentValue == 3

    assert await write_feed_value(obj, None) is True
    assert obj.presentValue == 2
    assert obj.priorityArray[9]._choice == "null"


@pytest.mark.asyncio
async def test_write_manual_value_none_on_multistate_value_releases_slot_8():
    # write_manual_value's None-relinquish path once crashed on an MSV
    # (priorityArray declared but None). MSV is commandable now and
    # "clear override" releases slot 8 to the relinquish default.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="2",
        )
    )
    await write_manual_value(obj, 3)
    assert obj.presentValue == 3

    await write_manual_value(obj, None)
    assert obj.presentValue == 2


@pytest.mark.asyncio
async def test_write_feed_value_none_on_out_of_service_input_is_still_skipped():
    # The existing OOS gate takes priority over the None-relinquish path --
    # an out-of-service Input ignores the feed entirely, same as any other
    # feed write.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-input",
            instance_number=1,
            name="AI-1",
            units="percent",
            out_of_service_default=True,
        )
    )
    assert await write_feed_value(obj, None) is False


# --- External-client relinquish via write_property -------------------------
# A *real* WriteProperty relinquish decodes off the wire as an actual `Null`
# instance (service.object.ReadWritePropertyServices.do_WritePropertyRequest's
# `cast_out(..., null=True)` -> Null.decode(), not plain None) -- distinct
# from write_feed_value/write_manual_value's own internal convention of
# passing plain Python `None`, which _relinquish() already handles (see the
# tests above). BinaryRangeValidated/MultiStateRangeValidated only ever
# skipped their own range check for `value is None`, so a real Null slipped
# through unrecognized into `int(Null())` -- an unconditional TypeError,
# surfacing to a real client as Error(class=device, code=operational-
# problem). Found live via a real second BACnet client relinquishing a
# commanded priority on a binary/multi-state-output during manual poking.
# Analog types have no range-validation mixin, so a real Null already sailed
# through untouched into bacpypes3's own Commandable.write_property(), which
# handles a genuine Null instance correctly on its own (probe-confirmed) --
# they only *appeared* immune to the bug, not evidence it was analog-vs-
# binary/multistate. Fixed via is_relinquish(), which recognizes both forms.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type,instance,name,on_value,default_value,extra_kwargs",
    [
        ("binary-output", 1, "BO-1", "active", "inactive", {}),
        ("binary-value", 1, "BV-1", "active", "inactive", {}),
        (
            "multi-state-output",
            1,
            "MSO-1",
            2,
            1,
            {"state_text": ["Off", "On", "Auto"]},
        ),
        ("analog-output", 1, "AO-1", 55.0, 0.0, {}),
        ("analog-value", 1, "AV-1", 42.0, 0.0, {}),
    ],
)
async def test_external_relinquish_via_write_property_does_not_crash(
    object_type, instance, name, on_value, default_value, extra_kwargs
):
    obj = build_bacnet_object(
        BacnetObject(
            object_type=object_type,
            instance_number=instance,
            name=name,
            **extra_kwargs,
        )
    )
    # Simulate a real client commanding then relinquishing priority 8 --
    # nothing else in the array holds a value, so recalculating() reaches
    # this exact slot when scanning for the highest-priority set value
    # (the case that crashed before the fix). A real client's relinquish is
    # a Null instance, not plain None -- see the module comment above.
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, on_value), priority=8)
    assert str(obj.presentValue) == str(on_value)

    await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=8)
    assert str(obj.presentValue) == str(default_value)


@pytest.mark.asyncio
async def test_external_relinquish_falls_back_to_a_lower_priority_still_set():
    obj = build_bacnet_object(
        BacnetObject(object_type="binary-output", instance_number=1, name="BO-1")
    )
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, "active"), priority=5)
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, "inactive"), priority=8)
    assert str(obj.presentValue) == "active"  # priority 5 outranks 8 (lower number wins)

    await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=5)
    assert str(obj.presentValue) == "inactive"  # falls back to priority 8, still set


@pytest.mark.asyncio
async def test_external_relinquish_respects_configured_relinquish_default():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-output",
            instance_number=1,
            name="AO-1",
            relinquish_default="67",
        )
    )
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 55.0), priority=8)
    await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=8)
    assert obj.presentValue == 67.0


# --- Editing State_Text via BACnet (feature request) ------------------------
# See StateTextEditable in bacnet.py. bacpypes3 accepts a WriteProperty to
# stateText at any length with no validation and no Number_Of_States sync --
# confirmed live the same way as the presentValue range gaps above.


@pytest.mark.asyncio
async def test_state_text_write_syncs_number_of_states():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "On"]
        )
    )
    assert obj.numberOfStates == 2

    await obj.write_property("stateText", ["Off", "Low", "High"])
    assert list(obj.stateText) == ["Off", "Low", "High"]
    assert obj.numberOfStates == 3


@pytest.mark.asyncio
async def test_state_text_write_clamps_present_value_if_now_out_of_range():
    # A Multi-state Input: a Multi-state Value is commandable,
    # and a shrink below its commanded value is refused instead (see
    # test_msv_state_text_shrink_refused_below_a_commanded_slot).
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-input",
            instance_number=1,
            name="MSI-1",
            state_text=["Off", "Low", "High"],
        )
    )
    obj.presentValue = 3

    await obj.write_property("stateText", ["Off", "On"])
    assert obj.numberOfStates == 2
    assert obj.presentValue == 2  # was 3, now out of range -- clamped down


@pytest.mark.asyncio
async def test_state_text_write_rejects_empty_list():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "On"]
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("stateText", [])
    assert list(obj.stateText) == ["Off", "On"]  # unchanged by the rejected write


@pytest.mark.asyncio
async def test_state_text_editable_on_input_output_and_value_types():
    # Descriptive metadata, not a controlled process value -- editable
    # regardless of Input/Output/Value, unlike presentValue's OOS-gating.
    for object_type in ("multi-state-input", "multi-state-output", "multi-state-value"):
        obj = build_bacnet_object(
            BacnetObject(
                object_type=object_type, instance_number=1, name="MSx-1", state_text=["Off", "On"]
            )
        )
        await obj.write_property("stateText", ["Off", "Low", "High"])
        assert obj.numberOfStates == 3


@pytest.mark.asyncio
async def test_state_text_write_persists_to_db(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "On"],
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    await obj.write_property("stateText", ["Off", "Low", "High"])

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.state_text == ["Off", "Low", "High"]


@pytest.mark.asyncio
async def test_state_text_write_without_session_factory_still_works_live_only():
    # config_session_factory is opt-in (omitted in most tests, and for any
    # build_bacnet_object() caller that doesn't have one) -- the live object
    # write must still succeed, it just won't be persisted anywhere.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "On"]
        )
    )
    await obj.write_property("stateText", ["Off", "Low", "High"])
    assert obj.numberOfStates == 3


# A shrink that would drop a state something still uses is refused:
# it used to be saved, and the next start then failed validation (a
# Relinquish_Default or alarm state past the new Number_Of_States), or a
# priority slot was left holding a state that no longer exists.


@pytest.mark.asyncio
async def test_state_text_shrink_refused_below_relinquish_default(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="multi-state-output",
            instance_number=1,
            name="MSO-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="3",
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)

    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "Low"])
    assert list(obj.stateText) == ["Off", "Low", "High"]
    assert obj.numberOfStates == 3
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Low", "High"]


@pytest.mark.asyncio
async def test_state_text_shrink_refused_below_a_commanded_priority_slot():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-output",
            instance_number=1,
            name="MSO-1",
            state_text=["Off", "Low", "High"],
        )
    )
    await write_manual_value(obj, 3)

    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "Low"])
    assert obj.numberOfStates == 3
    assert obj.presentValue == 3


@pytest.mark.asyncio
async def test_state_text_shrink_refused_below_an_alarm_state(session_factory):
    from bacpack.object_builder import build_application

    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        nc = BacnetObject(device=device, object_type="notification-class", instance_number=1, name="NC-1")
        row = BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            alarming_enabled=True,
            notification_class_instance=1,
            alarm_values=[3],
        )
        session.add_all([device, nc, row])
        session.commit()
        object_id = row.id
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)

    obj = app.get_object_name("MSV-1")
    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "Low"])
    assert obj.numberOfStates == 3
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Low", "High"]


def _simulated_msv(session_factory, simulation):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
            simulation=simulation,
        )
        session.add_all([device, row])
        session.commit()
        object_id = row.id
    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)
    return obj, object_id


@pytest.mark.asyncio
async def test_state_text_shrink_refused_below_a_simulators_states(session_factory):
    # The simulator sits on state 1, so nothing live uses
    # state 3, but the saved `states` does -- the shrink used to save a row
    # import validation then refused (export/restore broken).
    obj, object_id = _simulated_msv(session_factory, {"kind": "cycle", "dwell_s": 60.0, "states": [1, 2, 3]})

    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "Low"])
    assert obj.numberOfStates == 3
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Low", "High"]


@pytest.mark.asyncio
async def test_state_text_shrink_allowed_with_a_simulator_on_every_state(session_factory):
    # No explicit `states`: the simulator uses whatever states there are.
    obj, object_id = _simulated_msv(session_factory, {"kind": "cycle", "dwell_s": 60.0})

    await obj.write_property("stateText", ["Off", "Low"])
    assert obj.numberOfStates == 2
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Low"]


@pytest.mark.asyncio
async def test_state_text_shrink_still_allowed_when_nothing_uses_the_dropped_state():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-output",
            instance_number=1,
            name="MSO-1",
            state_text=["Off", "Low", "High"],
            relinquish_default="2",
        )
    )
    await write_manual_value(obj, 1)

    await obj.write_property("stateText", ["Off", "Low"])
    assert obj.numberOfStates == 2
    assert obj.presentValue == 1


# --- bacnet_writable / BacnetWriteLocked ------------------------------------
# See BacnetWriteLocked in bacnet.py: bacnet_writable=False rejects an
# external BACnet WriteProperty to presentValue, but leaves BACpack's own
# feed/manual writes working.


@pytest.mark.asyncio
async def test_bacnet_writable_true_by_default_allows_external_write():
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")
    )
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 42.0), priority=8)
    assert obj.presentValue == 42.0


@pytest.mark.asyncio
async def test_bacnet_writable_false_rejects_external_write():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            bacnet_writable=False,
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 42.0), priority=8)


@pytest.mark.asyncio
async def test_bacnet_writable_false_still_allows_internal_feed_and_manual_writes():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            bacnet_writable=False,
        )
    )
    assert await write_feed_value(obj, 10.0) is True
    assert obj.presentValue == 10.0

    await write_manual_value(obj, 20.0)
    assert obj.presentValue == 20.0

    # a real client is still locked out even after internal writes landed
    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 99.0), priority=8)
    assert obj.presentValue == 20.0


@pytest.mark.asyncio
async def test_bacnet_writable_false_rejects_external_relinquish():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            bacnet_writable=False,
        )
    )
    await write_manual_value(obj, 20.0)

    with pytest.raises(PropertyError) as exc:
        await obj.write_property(PropertyIdentifier.presentValue, Null(()), priority=8)
    assert exc.value.errorCode == "writeAccessDenied"
    assert obj.presentValue == 20.0


@pytest.mark.asyncio
async def test_bacnet_writable_false_on_multistate_value_rejects_external_write_but_not_feed():
    # Multi-state value once had no priority array and took its own feed
    # path. Now commandable: the feed writes slot 10
    # through write_property() under internal_write, like the types above.
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "On"],
            bacnet_writable=False,
        )
    )
    assert await write_feed_value(obj, 2) is True
    assert obj.presentValue == 2

    with pytest.raises(PropertyError):
        await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 1), priority=8)


# --- WritePropertyAllowlisted ----------------------------------------------
# Confirmed live via a real second bacpypes3 client over real BACnet/IP wire
# traffic: nothing gated WriteProperty to any property other than
# presentValue/stateText -- objectName, description, units, and reliability
# were all silently rewritable on every object, regardless of
# bacnet_writable/out_of_service/object type. See WritePropertyAllowlisted
# in bacnet.py.

_ALL_OBJECT_TYPES_AND_KWARGS = [
    ("analog-input", {"units": "percent"}),
    ("analog-output", {"units": "percent"}),
    ("analog-value", {"units": "percent"}),
    ("binary-input", {}),
    ("binary-output", {}),
    ("binary-value", {}),
    ("multi-state-input", {"state_text": ["Off", "On"]}),
    ("multi-state-output", {"state_text": ["Off", "On"]}),
    ("multi-state-value", {"state_text": ["Off", "On"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type,extra_kwargs", _ALL_OBJECT_TYPES_AND_KWARGS)
async def test_external_write_to_object_name_rejected_on_every_type(object_type, extra_kwargs):
    obj = build_bacnet_object(
        BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra_kwargs)
    )
    with pytest.raises(PropertyError):
        await obj.write_property("objectName", "Hacked")
    assert obj.objectName == "X-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type,extra_kwargs", _ALL_OBJECT_TYPES_AND_KWARGS)
async def test_external_write_to_description_rejected_on_every_type(object_type, extra_kwargs):
    obj = build_bacnet_object(
        BacnetObject(
            object_type=object_type,
            instance_number=1,
            name="X-1",
            description="original",
            **extra_kwargs,
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("description", "Hacked")
    assert obj.description == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type,extra_kwargs", _ALL_OBJECT_TYPES_AND_KWARGS)
async def test_external_write_to_reliability_rejected_on_every_type(object_type, extra_kwargs):
    obj = build_bacnet_object(
        BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra_kwargs)
    )
    with pytest.raises(PropertyError):
        await obj.write_property("reliability", "communicationFailure")


@pytest.mark.asyncio
async def test_external_write_to_units_rejected_on_analog_object():
    # units only exists on analog types -- covered separately rather than
    # parametrized across every type.
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")
    )
    with pytest.raises(PropertyError):
        await obj.write_property("units", "degreesFahrenheit")
    assert str(obj.units) == "percent"


@pytest.mark.asyncio
async def test_allowlist_still_permits_present_value_write():
    # The allowlist must not regress any of the writes covered elsewhere in
    # this file -- a quick end-to-end sanity check alongside the targeted
    # rejections above.
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")
    )
    await obj.write_property(PropertyIdentifier.presentValue, _typed(obj, 5.0), priority=8)
    assert obj.presentValue == 5.0


@pytest.mark.asyncio
async def test_allowlist_still_permits_out_of_service_write_on_input():
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", units="percent")
    )
    await obj.write_property("outOfService", True)
    assert obj.outOfService  # bacpypes3's Boolean reads back int-like (1), not Python True


@pytest.mark.asyncio
async def test_allowlist_still_permits_state_text_write_on_multistate():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "On"]
        )
    )
    await obj.write_property("stateText", ["Off", "Low", "High"])
    assert list(obj.stateText) == ["Off", "Low", "High"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type,extra_kwargs",
    [
        ("analog-output", {}),
        ("analog-value", {}),
        ("binary-output", {}),
        ("binary-value", {}),
        ("multi-state-output", {"state_text": ["Off", "On"]}),
        ("multi-state-value", {"state_text": ["Off", "On"]}),
    ],
)
async def test_allowlist_permits_out_of_service_write_on_output_and_value(object_type, extra_kwargs):
    # Widened by an explicit decision, in response to a real
    # writeAccessDenied a live client hit against a bacnet_writable=True
    # object): Out_Of_Service is an ordinary testing/commissioning flag on
    # real devices, not an Input-only concept -- every Output/Value type
    # gets a real out_of_service property too (build_bacnet_object's shared
    # kwargs). It's still purely informational for these types (no
    # OutOfService-style gate applies to Output/Value presentValue writes),
    # but a real client must be able to set it without writeAccessDenied.
    obj = build_bacnet_object(
        BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra_kwargs)
    )
    await obj.write_property("outOfService", True)
    assert obj.outOfService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type,extra_kwargs",
    [
        ("analog-output", {}),
        ("analog-value", {}),
        ("binary-output", {}),
        ("binary-value", {}),
        ("multi-state-output", {"state_text": ["Off", "On"]}),
        ("multi-state-value", {"state_text": ["Off", "On"]}),
    ],
)
async def test_allowlist_permits_relinquish_default_write_on_commandable_types(object_type, extra_kwargs):
    # Widened alongside outOfService above, same explicit user decision --
    # Relinquish_Default is optional-but-writable on a commandable object
    # per spec, unlike units/objectName/description/reliability which stay
    # blocked. Multi-state Value is commandable now.
    obj = build_bacnet_object(
        BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra_kwargs)
    )
    new_default = _typed(obj, "active" if "binary" in object_type else (2 if "multi-state" in object_type else 5.0))
    await obj.write_property("relinquishDefault", new_default)
    assert str(obj.relinquishDefault) == str(new_default)


@pytest.mark.asyncio
async def test_allowlist_rejects_state_text_write_on_non_multistate():
    # analog-value isn't in a stateText-allowed class -- and has no such
    # property at all -- so the allowlist itself must be what rejects this,
    # before bacpypes3 ever gets a chance to raise its own "no such
    # property" error over a different attr.
    obj = build_bacnet_object(
        BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", units="percent")
    )
    with pytest.raises(PropertyError):
        await obj.write_property("stateText", ["Off", "On"])


# --- Whole-number floats into binary/multi-state ---------------------------
# A connector's coerce_mapped_value always returns a float, and JSON feed
# bodies can carry 1.0 too -- BinaryPV/Unsigned casts reject any float, so a
# connector mapped to a binary or multi-state object never applied a value.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, state_text, value, expected",
    [
        ("binary-value", None, 1.0, "active"),
        ("binary-input", None, 0.0, "inactive"),
        ("multi-state-value", ["a", "b", "c"], 2.0, 2),
        ("multi-state-output", ["a", "b", "c"], 3.0, 3),
    ],
)
async def test_write_feed_value_accepts_whole_number_float_for_enumerated_types(
    object_type, state_text, value, expected
):
    obj = build_bacnet_object(
        BacnetObject(object_type=object_type, instance_number=1, name="X", state_text=state_text)
    )
    assert await write_feed_value(obj, value) is True
    if isinstance(expected, str):
        assert str(obj.presentValue) == expected
    else:
        assert int(obj.presentValue) == expected


@pytest.mark.asyncio
async def test_write_feed_value_still_rejects_fractional_float_for_multistate():
    obj = build_bacnet_object(
        BacnetObject(object_type="multi-state-value", instance_number=1, name="X", state_text=["a", "b"])
    )
    with pytest.raises(TypeError):
        await write_feed_value(obj, 1.5)


# --- Indexed State_Text writes ----------------------------------------------
# stateText is a BACnetARRAY, so a client can write one label by index. The
# old code iterated the single label's characters as if it were the whole
# list: Number_Of_States became the label's length and the DB got one state
# per letter.


@pytest.mark.asyncio
async def test_indexed_state_text_write_replaces_one_label_and_persists(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=1,
            name="MSV-1",
            state_text=["Off", "Low", "High"],
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    await obj.write_property("stateText", CharacterString("Medium"), index=2)

    assert list(obj.stateText) == ["Off", "Medium", "High"]
    assert obj.numberOfStates == 3
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).state_text == ["Off", "Medium", "High"]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 4])
async def test_indexed_state_text_write_rejects_resize_and_out_of_range(index):
    obj = build_bacnet_object(
        BacnetObject(
            object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["Off", "Low", "High"]
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("stateText", CharacterString("X"), index=index)
    assert list(obj.stateText) == ["Off", "Low", "High"]
    assert obj.numberOfStates == 3


@pytest.mark.parametrize(
    "object_type, extra",
    [
        ("analog-output", {"units": "percent"}),
        ("analog-value", {"units": "percent"}),
        ("binary-output", {}),
        ("binary-value", {}),
        ("multi-state-output", {"state_text": ["Off", "On"]}),
        ("multi-state-value", {"state_text": ["Off", "On"]}),
    ],
)
@pytest.mark.asyncio
async def test_every_commandable_type_builds_a_real_priority_array(object_type, extra):
    # write_feed_value/write_manual_value rely on it: the old "priorityArray is
    # None" fallback existed only for MSV, which wasn't commandable then.
    from bacpack.models import COMMANDABLE_TYPES

    assert object_type in COMMANDABLE_TYPES
    obj = build_bacnet_object(BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra))
    assert obj.priorityArray is not None and len(obj.priorityArray) == 16


@pytest.mark.parametrize(
    "object_type, extra",
    [
        ("analog-input", {"units": "percent"}),
        ("binary-input", {}),
        ("multi-state-input", {"state_text": ["Off", "On"]}),
    ],
)
@pytest.mark.asyncio
async def test_an_input_has_no_priority_array_attribute(object_type, extra):
    # The other half of the same assumption: the types write_feed_value
    # feeds without a priority array have no such attribute at all, never a
    # declared-but-None one (what MSV once was).
    obj = build_bacnet_object(BacnetObject(object_type=object_type, instance_number=1, name="X-1", **extra))
    assert not hasattr(obj, "priorityArray")
