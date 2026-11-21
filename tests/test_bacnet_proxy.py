"""Proxy mappings, the object side: mirroring a remote's state
into a local commandable object, Reliability, the error a failed forward
becomes, and the ProxyForwarding mixin. The runner side is in
test_bacnet_connectors.py."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.basetypes import BinaryPV, PriorityValue, Reliability
from bacpypes3.errors import ExecutionError, PropertyError
from bacpypes3.primitivedata import Boolean, CharacterString, Null, ObjectIdentifier, PropertyIdentifier, Real

from bacpack.bacnet import ProxyForwarding, write_feed_value, write_manual_value
from bacpack.bacnet_client import ClientError
from bacpack.bacnet_proxy import clear_mirror, forward_error, mirror, set_reliability
from bacpack.models import COMMANDABLE_TYPES, BacnetObject, Device
from bacpack.object_builder import OBJECT_CLASSES, OBJECT_CLASSES_IR, build_application


def _app(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add_all(
            [
                device,
                BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"),
                BacnetObject(device=device, object_type="binary-value", instance_number=1, name="BV-1"),
                BacnetObject(
                    device=device, object_type="multi-state-value", instance_number=1, name="MSV-1",
                    state_text=["Off", "Low", "High"],
                ),
                BacnetObject(
                    device=device, object_type="analog-value", instance_number=2, name="AV-locked",
                    bacnet_writable=False,
                ),
            ]
        )
        session.commit()
        device = session.get(Device, 1)
        return build_application(device, list(device.objects))


def _obj(app, object_type="analog-value", instance=1):
    return app.get_object_id(ObjectIdentifier((object_type, instance)))


def _slot(obj, priority):
    value = obj.priorityArray[priority - 1]
    return None if value._choice in (None, "null") else getattr(value, value._choice)


def _array(**slots):
    """16 remote slots; p8=Real(72.0) fills priority 8, the rest are empty."""
    return [
        PriorityValue(slots[f"p{i}"]) if f"p{i}" in slots else PriorityValue(null=())
        for i in range(1, 17)
    ]


def _fault(obj) -> bool:
    return list(obj.statusFlags)[1] == 1  # in-alarm, fault, overridden, out-of-service


# --- mirror ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_copies_the_array_and_present_value(session_factory):
    obj = _obj(_app(session_factory))

    mirror(obj, Real(72.0), _array(p8=Real(72.0), p16=Real(60.0)))

    assert (obj.presentValue, _slot(obj, 8), _slot(obj, 16), _slot(obj, 10)) == (72.0, 72.0, 60.0, None)


@pytest.mark.asyncio
async def test_mirror_takes_the_remotes_present_value_even_off_its_array(session_factory):
    # An out-of-service remote reports a Present_Value its array doesn't give.
    obj = _obj(_app(session_factory))

    mirror(obj, Real(50.0), _array(p8=Real(72.0)))

    assert (obj.presentValue, _slot(obj, 8)) == (50.0, 72.0)


@pytest.mark.asyncio
async def test_mirror_without_an_array_empties_every_slot(session_factory):
    obj = _obj(_app(session_factory))
    obj.priorityArray[7] = PriorityValue(Real(9.0))

    mirror(obj, Real(21.0), None)

    assert obj.presentValue == 21.0
    assert all(_slot(obj, p) is None for p in range(1, 17))


@pytest.mark.asyncio
async def test_mirror_binary(session_factory):
    obj = _obj(_app(session_factory), "binary-value")

    mirror(obj, BinaryPV("active"), _array(p8=BinaryPV("active"), p16=BinaryPV("inactive")))

    assert (obj.presentValue, _slot(obj, 8), _slot(obj, 16)) == (
        BinaryPV("active"), BinaryPV("active"), BinaryPV("inactive"),
    )


@pytest.mark.asyncio
async def test_mirror_multistate_value(session_factory):
    from bacpypes3.primitivedata import Unsigned

    obj = _obj(_app(session_factory), "multi-state-value")

    mirror(obj, Unsigned(3), _array(p8=Unsigned(3), p16=Unsigned(1)))

    assert (int(obj.presentValue), int(_slot(obj, 8)), int(_slot(obj, 16)), _slot(obj, 10)) == (3, 3, 1, None)


@pytest.mark.asyncio
async def test_mirror_that_wont_cast_changes_nothing(session_factory):
    obj = _obj(_app(session_factory))
    mirror(obj, Real(1.0), _array(p8=Real(1.0)))

    with pytest.raises((ValueError, TypeError)):
        mirror(obj, CharacterString("hot"), _array(p8=Real(2.0)))

    assert (obj.presentValue, _slot(obj, 8)) == (1.0, 1.0)


@pytest.mark.asyncio
async def test_mirror_fires_present_value_monitors(session_factory):
    obj = _obj(_app(session_factory))
    seen = []
    obj._property_monitors["presentValue"].append(lambda old, new: seen.append(new))

    mirror(obj, Real(5.0), _array(p8=Real(5.0)))

    assert seen == [5.0]


@pytest.mark.asyncio
async def test_reliability_sets_and_clears_the_fault_flag(session_factory):
    obj = _obj(_app(session_factory))
    mirror(obj, Real(3.0), _array(p8=Real(3.0)))

    set_reliability(obj, "communication-failure")
    assert obj.reliability == Reliability("communication-failure") and _fault(obj)

    clear_mirror(obj)
    assert all(_slot(obj, p) is None for p in range(1, 17))
    assert obj.presentValue == obj.relinquishDefault
    assert obj.reliability == Reliability("no-fault-detected") and not _fault(obj)


# --- forward_error -----------------------------------------------------------


@pytest.mark.parametrize(
    "failure, expected",
    [
        (
            ClientError("property: value-out-of-range", error_class="property", error_code="value-out-of-range", reply="error"),
            ("property", "value-out-of-range"),
        ),
        (ClientError("reject: invalid-tag", reply="reject"), ("communication", "other")),
        (ClientError("abort: other", kind="transport", reply="abort"), ("communication", "other")),
        (ClientError("no response within 2.5 s", kind="transport"), ("communication", "timeout")),
        (ClientError("device 2 did not answer Who-Is", kind="transport"), ("communication", "timeout")),
        (ClientError("suppressed by DCC (disableInitiation)", kind="dcc"), ("communication", "communication-disabled")),
        (ClientError("can't write 'x' to analog-value,5 present-value as Real"), ("property", "invalid-data-type")),
    ],
)
def test_forward_error(failure, expected):
    error = forward_error(failure)
    assert isinstance(error, ExecutionError)
    assert (error.errorClass, error.errorCode) == expected


# --- ProxyForwarding -------------------------------------------------------


@pytest.mark.parametrize("object_type", sorted(COMMANDABLE_TYPES))
def test_every_commandable_class_forwards(object_type):
    assert issubclass(OBJECT_CLASSES[object_type], ProxyForwarding)
    if object_type in OBJECT_CLASSES_IR:
        assert issubclass(OBJECT_CLASSES_IR[object_type], ProxyForwarding)


def _recorder(obj, error=None):
    calls = []

    async def forward(value, priority):
        calls.append((value, priority))
        if error is not None:
            raise error

    obj._proxy_forward = forward
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["presentValue", PropertyIdentifier.presentValue])
async def test_a_present_value_write_is_forwarded_then_applied(session_factory, attr):
    obj = _obj(_app(session_factory))
    calls = _recorder(obj)

    await obj.write_property(attr, Real(72.0), priority=8)

    assert calls == [(72.0, 8)]
    assert _slot(obj, 8) == 72.0


@pytest.mark.asyncio
async def test_no_priority_is_16_and_a_null_is_a_relinquish(session_factory):
    obj = _obj(_app(session_factory))
    calls = _recorder(obj)

    await obj.write_property("presentValue", Real(5.0))
    await obj.write_property("presentValue", Null(()), priority=16)

    assert calls == [(5.0, 16), (None, 16)]
    assert _slot(obj, 16) is None


@pytest.mark.asyncio
async def test_a_refused_forward_leaves_the_object_alone(session_factory):
    obj = _obj(_app(session_factory))
    _recorder(obj, ExecutionError(errorClass="property", errorCode="value-out-of-range"))

    with pytest.raises(ExecutionError):
        await obj.write_property("presentValue", Real(9.0), priority=8)

    assert _slot(obj, 8) is None


@pytest.mark.asyncio
async def test_bacnet_writable_false_refuses_before_forwarding(session_factory):
    obj = _obj(_app(session_factory), "analog-value", 2)
    calls = _recorder(obj)

    with pytest.raises(PropertyError):
        await obj.write_property("presentValue", Real(1.0), priority=8)

    assert calls == []


@pytest.mark.asyncio
async def test_bacnet_writable_false_refuses_a_relinquish_before_forwarding(session_factory):
    obj = _obj(_app(session_factory), "analog-value", 2)
    obj.priorityArray[7] = PriorityValue(Real(4.0))
    calls = _recorder(obj)

    with pytest.raises(PropertyError):
        await obj.write_property("presentValue", Null(()), priority=8)

    assert calls == [] and _slot(obj, 8) == 4.0


@pytest.mark.asyncio
async def test_bacnet_writable_false_without_a_proxy_refuses_a_relinquish(session_factory):
    obj = _obj(_app(session_factory), "analog-value", 2)
    obj.priorityArray[7] = PriorityValue(Real(4.0))

    with pytest.raises(PropertyError) as exc:
        await obj.write_property("presentValue", Null(()), priority=8)

    assert exc.value.errorCode == "writeAccessDenied"
    assert _slot(obj, 8) == 4.0


@pytest.mark.asyncio
async def test_a_bacnet_write_during_an_internal_forward_is_still_locked(session_factory):
    # The UI's write waits on the remote; a client's write to the same
    # locked object meanwhile must not ride on the UI's bypass.
    obj = _obj(_app(session_factory), "analog-value", 2)
    calls = []
    entered, gate = asyncio.Event(), asyncio.Event()

    async def forward(value, priority):
        calls.append((value, priority))
        if len(calls) == 1:  # only the UI's write waits on the remote
            entered.set()
            await gate.wait()

    obj._proxy_forward = forward
    ui = asyncio.create_task(write_manual_value(obj, 55.0))
    await entered.wait()

    client = asyncio.create_task(obj.write_property("presentValue", Real(99.0), priority=3))
    with pytest.raises(PropertyError):
        await client
    client_null = asyncio.create_task(obj.write_property("presentValue", Null(()), priority=3))
    with pytest.raises(PropertyError):
        await client_null
    gate.set()
    await ui

    assert calls == [(55.0, 8)]
    assert (_slot(obj, 8), _slot(obj, 3)) == (55.0, None)


@pytest.mark.asyncio
async def test_a_feed_value_and_its_relinquish_are_forwarded_at_10(session_factory):
    obj = _obj(_app(session_factory))
    calls = _recorder(obj)

    await write_feed_value(obj, 5.0)
    await write_feed_value(obj, None)

    assert calls == [(5.0, 10), (None, 10)]
    assert _slot(obj, 10) is None


@pytest.mark.asyncio
async def test_a_feed_relinquish_can_stay_local(session_factory):
    # The connector runner releasing its own read feed sends nothing.
    obj = _obj(_app(session_factory))
    obj.priorityArray[9] = PriorityValue(Real(5.0))
    calls = _recorder(obj)

    await write_feed_value(obj, None, forward=False)

    assert calls == [] and _slot(obj, 10) is None


@pytest.mark.asyncio
async def test_a_forward_finishing_after_the_proxy_is_gone_changes_nothing_locally(session_factory):
    obj = _obj(_app(session_factory))
    calls = []

    async def forward(value, priority):
        calls.append((value, priority))
        obj._proxy_forward = None  # the proxy was removed meanwhile

    obj._proxy_forward = forward
    await obj.write_property("presentValue", Real(5.0), priority=5)

    assert calls == [(5.0, 5)]
    assert _slot(obj, 5) is None


@pytest.mark.asyncio
async def test_a_forward_spanning_a_runner_tick_still_applies(session_factory):
    # The runner re-attaches a fresh hook every tick; the proxy is still live.
    obj = _obj(_app(session_factory))
    later = _recorder(obj)
    fresh = obj._proxy_forward

    async def forward(value, priority):
        obj._proxy_forward = fresh

    obj._proxy_forward = forward
    await obj.write_property("presentValue", Real(5.0), priority=5)

    assert later == [] and _slot(obj, 5) == 5.0


@pytest.mark.asyncio
async def test_other_properties_and_unhooked_objects_stay_local(session_factory):
    app = _app(session_factory)
    obj = _obj(app)
    calls = _recorder(obj)
    plain = _obj(app, "binary-value")

    await obj.write_property("outOfService", Boolean(True))
    await plain.write_property("presentValue", BinaryPV("active"), priority=8)

    assert calls == []
    assert _slot(plain, 8) == BinaryPV("active")


@pytest.mark.asyncio
async def test_ui_edits_and_clears_are_forwarded_at_8(session_factory):
    # bacnet_writable=False only stops BACnet clients; the UI still forwards.
    obj = _obj(_app(session_factory), "analog-value", 2)
    calls = _recorder(obj)

    await write_manual_value(obj, 55.0)
    await write_manual_value(obj, None)

    assert calls == [(55.0, 8), (None, 8)]
    assert _slot(obj, 8) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "present_value, array",
    [(Real(21.0), None), (Real(50.0), _array(p8=Real(72.0))), (Real(72.0), _array(p8=Real(72.0)))],
)
async def test_mirroring_an_unchanged_remote_fires_no_monitors(session_factory, present_value, array):
    obj = _obj(_app(session_factory))
    mirror(obj, present_value, array)
    seen = []
    obj._property_monitors["presentValue"].append(lambda old, new: seen.append(new))

    mirror(obj, present_value, array)

    assert seen == []


@pytest.mark.asyncio
async def test_mirror_fires_once_when_the_value_changes_off_its_array(session_factory):
    obj = _obj(_app(session_factory))
    mirror(obj, Real(1.0), _array(p8=Real(1.0)))
    seen = []
    obj._property_monitors["presentValue"].append(lambda old, new: seen.append(new))

    mirror(obj, Real(50.0), _array(p8=Real(72.0)))

    assert seen == [50.0]


@pytest.mark.asyncio
async def test_mirror_sets_current_command_priority(session_factory):
    obj = _obj(_app(session_factory))

    mirror(obj, Real(60.0), _array(p10=Real(70.0), p16=Real(60.0)))
    assert obj.currentCommandPriority.unsigned == 10

    mirror(obj, Real(3.0), None)
    assert obj.currentCommandPriority._choice == "null"
