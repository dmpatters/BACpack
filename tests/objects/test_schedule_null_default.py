"""A Schedule_Default of Null: outside the scheduled times the schedule's
Present_Value is Null and its write targets are released."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import Null, ObjectIdentifier, Real

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application, build_bacnet_object
from bacpack.objects.schedule_config import build_daily_schedule, validate_schedule_config


def _weekly(time="00:00", value="60.0"):
    return [[{"time": time, "value": value}] for _ in range(7)]


@pytest.mark.parametrize(
    "value_type, weekly",
    [("analog", "50.0"), ("binary", "active"), ("multi-state", "2")],
)
def test_validation_accepts_a_null_default(value_type, weekly):
    validate_schedule_config(value_type, None, _weekly(value=weekly), None, None, None, 8, None)
    validate_schedule_config(value_type, None, None, None, None, None, 8, None)


def test_validation_still_checks_a_default_that_is_given():
    with pytest.raises(ValueError, match="not a valid analog schedule value"):
        validate_schedule_config("analog", "nan", None, None, None, None, 8, None)


def _sched_row(**fields):
    base = dict(
        object_type="schedule", instance_number=1, name="Sched-1", schedule_value_type="analog",
        schedule_default=None, weekly_schedule=_weekly(), schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    return BacnetObject(**{**base, **fields})


def _app(*rows):
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")
    return build_application(Device(id=599001, name="Test Device", vendor_identifier=999), [target, *rows])


async def _turns():
    for _ in range(10):
        await asyncio.sleep(0.02)


def _slot(obj, priority):
    pv = obj.priorityArray[priority - 1]
    return None if pv.null is not None else getattr(pv, pv._choice)


def _empty_week(value_type="analog"):
    return [build_daily_schedule([], value_type) for _ in range(7)]


@pytest.mark.asyncio
async def test_a_null_default_builds_a_working_schedule():
    app = _app(_sched_row())
    await _turns()
    sched = app.get_object_name("Sched-1")
    assert isinstance(sched.scheduleDefault.get_value(), Null)
    assert str(sched.reliability) == "no-fault-detected"
    assert sched.presentValue.get_value() == 60.0
    assert _slot(app.get_object_name("Target-AV"), 8) == 60.0


@pytest.mark.asyncio
async def test_outside_the_scheduled_times_the_value_is_null_and_the_target_is_released():
    app = _app(_sched_row())
    await _turns()
    target = app.get_object_name("Target-AV")
    assert _slot(target, 8) == 60.0

    app.get_object_name("Sched-1").weeklySchedule = _empty_week()
    await _turns()

    assert isinstance(app.get_object_name("Sched-1").presentValue.get_value(), Null)
    assert _slot(target, 8) is None


@pytest.mark.asyncio
async def test_a_schedule_that_starts_outside_its_times_leaves_the_target_alone():
    app = _app(_sched_row(weekly_schedule=None))
    await _turns()
    assert isinstance(app.get_object_name("Sched-1").presentValue.get_value(), Null)
    assert _slot(app.get_object_name("Target-AV"), 8) is None


@pytest.mark.asyncio
async def test_a_bacnet_write_of_null_to_the_default_is_saved_as_none(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = _sched_row(device=device, schedule_default="10.0", bacnet_writable=True, schedule_write_targets=None)
        session.add_all([device, row])
        session.commit()
        object_id = row.id
    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)

    await obj.write_property("scheduleDefault", Null(()))

    assert isinstance(obj.scheduleDefault.get_value(), Null)
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).schedule_default is None
    await obj.write_property("scheduleDefault", Real(7.0))
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).schedule_default == "7.0"
