"""Relinquish_Default over BACnet: checked, applied to Present_Value at once when no
slot is set, and saved to the object's row."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.basetypes import BinaryPV, PropertyIdentifier
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Null, Real, Unsigned

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked


async def _build(session_factory, **fields):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        row = BacnetObject(device=device, instance_number=1, name="OBJ-1", **fields)
        session.add(row)
        session.commit()
        object_id = row.id
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return app.get_object_name("OBJ-1"), object_id


def _saved(session_factory, object_id):
    with session_factory() as session:
        return session.get(BacnetObject, object_id).relinquish_default


# (fields, a good value, its column text, the Present_Value it gives, bad values)
CASES = [
    (dict(object_type="analog-output", units="percent"), Real(42.5), "42.5", 42.5, [Real(float("nan")), Real(float("inf"))]),
    (dict(object_type="analog-value", units="percent"), Real(42.5), "42.5", 42.5, [Real(float("nan"))]),
    (dict(object_type="binary-output"), BinaryPV("active"), "active", "active", [BinaryPV(7)]),
    (dict(object_type="binary-value"), BinaryPV("active"), "active", "active", [BinaryPV(2)]),
    (dict(object_type="multi-state-output", state_text=["Off", "Low", "High"]), Unsigned(3), "3", 3, [Unsigned(4), Unsigned(0)]),
    (dict(object_type="multi-state-value", state_text=["Off", "Low", "High"]), Unsigned(3), "3", 3, [Unsigned(4), Unsigned(0)]),
]


def _pv(obj):
    value = obj.presentValue
    return str(value) if isinstance(value, BinaryPV) else value


@pytest.mark.asyncio
@pytest.mark.parametrize("fields, good, text, pv, bad", CASES)
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_valid_write_moves_present_value_and_saves(session_factory, fields, good, text, pv, bad, as_identifier):
    obj, object_id = await _build(session_factory, **fields)
    target = PropertyIdentifier("relinquishDefault") if as_identifier else "relinquishDefault"
    await obj.write_property(target, good)
    assert _pv(obj) == pv
    assert _saved(session_factory, object_id) == text


@pytest.mark.asyncio
async def test_the_save_waits_for_a_held_lock_off_the_loop(session_factory):
    obj, object_id = await _build(session_factory, object_type="analog-value", units="percent")

    run = await run_while_locked(session_factory, lambda: obj.write_property("relinquishDefault", Real(42.5)))

    assert_waited_off_the_loop(run)
    assert _saved(session_factory, object_id) == "42.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("fields, good, text, pv, bad", CASES)
async def test_invalid_write_is_refused_and_changes_nothing(session_factory, fields, good, text, pv, bad):
    obj, object_id = await _build(session_factory, **fields)
    before_rd, before_pv = obj.relinquishDefault, _pv(obj)
    for value in bad + [Null(())]:
        with pytest.raises(PropertyError) as excinfo:
            await obj.write_property("relinquishDefault", value)
        assert excinfo.value.errorCode == "value-out-of-range"
    assert obj.relinquishDefault == before_rd
    assert _pv(obj) == before_pv
    assert _saved(session_factory, object_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fields, good, text, pv, bad", CASES)
async def test_present_value_starts_at_the_configured_relinquish_default(session_factory, fields, good, text, pv, bad):
    # Found by a live check: Present_Value was seeded with the
    # built-in 0.0/inactive/1 at start, so with an empty priority array a
    # configured Relinquish_Default didn't show until the first write.
    obj, object_id = await _build(session_factory, relinquish_default=text, **fields)
    assert _pv(obj) == pv


@pytest.mark.asyncio
async def test_write_with_a_slot_set_leaves_present_value_alone(session_factory):
    obj, object_id = await _build(session_factory, object_type="analog-output", units="percent", bacnet_writable=True)
    await obj.write_property("presentValue", Real(10.0), priority=8)
    await obj.write_property("relinquishDefault", Real(42.5))
    assert obj.presentValue == 10.0
    assert _saved(session_factory, object_id) == "42.5"
