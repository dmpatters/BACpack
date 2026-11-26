"""Tests for bacpack.objects.schedule."""
from __future__ import annotations

import asyncio
import logging
import datetime as dt

import pytest
from bacpypes3.basetypes import (
    BinaryPV,
    CalendarEntry,
    DailySchedule,
    DateRange,
    DeviceObjectPropertyReference,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Date, PropertyIdentifier, Real, Time, Unsigned

from bacpack import bacpypes3_compat
from bacpack.object_builder import build_application, build_bacnet_object
from bacpack.bacpypes3_compat import _match_date_range_with_wildcards
from bacpack.objects.schedule_config import validate_schedule_config
from bacpack.models import BacnetObject, Device
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked
from tests.session_fakes import CommitFailingSessionFactory


# --- Schedule / Calendar objects --------------------------------------------
# See "Add BACnet Schedule and Calendar objects" plan
# (luminous-growing-pike.md). validate_schedule_config/validate_calendar_config
# are pure/DB-free structural validators, same division of labor as
# validate_units/validate_relinquish_default -- called from web/api/objects.py
# at create/update time and again inside build_bacnet_object for defense in
# depth against pre-existing bad data.


def _weekly_schedule(time="08:00", value="50.0"):
    """A full 7-day (Monday-Sunday) weekly_schedule with the same single
    time-value entry on every day -- valid shape for validate_schedule_config,
    which requires exactly 7 entries.
    """
    return [[{"time": time, "value": value}] for _ in range(7)]


def test_validate_schedule_config_accepts_valid_weekly_schedule():
    validate_schedule_config(
        "analog", "10.0", _weekly_schedule(), None, None, None, 8, None
    )


def test_validate_schedule_config_accepts_exception_schedule_only():
    exception_schedule = [
        {
            "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
            "priority": 1,
            "time_values": [{"time": "08:00", "value": "active"}],
        }
    ]
    validate_schedule_config(
        "binary", "inactive", None, exception_schedule, None, None, 8, None
    )


def test_validate_schedule_config_accepts_calendar_reference_period():
    exception_schedule = [
        {
            "period": {"kind": "calendar-reference", "instance_number": 1},
            "priority": 1,
            "time_values": [{"time": "08:00", "value": "1"}],
        }
    ]
    validate_schedule_config(
        "multi-state",
        "1",
        None,
        exception_schedule,
        None,
        None,
        8,
        None,
        state_text=["Off", "On"],
    )


def test_validate_schedule_config_rejects_bad_value_type():
    with pytest.raises(ValueError, match="schedule_value_type"):
        validate_schedule_config(
            "not-a-type", "10.0", _weekly_schedule(), None, None, None, 8, None
        )


@pytest.mark.parametrize("text", ["nan", "inf", "1e999"])
def test_validate_schedule_config_rejects_non_finite_analog_default(text):
    # "nan" parsed as a float and saved.
    with pytest.raises(ValueError, match="not a valid analog schedule value"):
        validate_schedule_config("analog", text, None, None, None, None, 8, None)




def test_validate_schedule_config_accepts_no_weekly_or_exception_schedule():
    # A schedule that always sits at Schedule_Default is legal BACnet:
    # an empty week used to pass the API but fail every wire write.
    validate_schedule_config("analog", "10.0", None, None, None, None, 8, None)
    validate_schedule_config("analog", "10.0", [[] for _ in range(7)], None, None, None, 8, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("weekly", [None, [[] for _ in range(7)]])
async def test_bacnet_write_to_a_schedule_with_an_empty_week_is_accepted(session_factory, weekly):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=weekly,
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add_all([device, row])
        session.commit()
        object_id = row.id

    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)

    await obj.write_property("scheduleDefault", Real(99.0))
    await obj.write_property("priorityForWriting", Unsigned(12))
    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert (row.schedule_default, row.schedule_priority) == ("99.0", 12)
        assert row.exception_schedule is None


def test_validate_schedule_config_rejects_bad_priority():
    with pytest.raises(ValueError, match="schedule_priority"):
        validate_schedule_config(
            "analog", "10.0", _weekly_schedule(), None, None, None, 17, None
        )


def test_validate_schedule_config_rejects_wrong_number_of_weekly_entries():
    with pytest.raises(ValueError, match="exactly 7 entries"):
        validate_schedule_config(
            "analog",
            "10.0",
            _weekly_schedule()[:6],
            None,
            None,
            None,
            8,
            None,
        )


def test_validate_schedule_config_rejects_duplicate_time_in_day():
    day = [{"time": "08:00", "value": "1.0"}, {"time": "08:00", "value": "2.0"}]
    with pytest.raises(ValueError, match="duplicate schedule time"):
        validate_schedule_config(
            "analog", "10.0", [day] + _weekly_schedule()[1:], None, None, None, 8, None
        )


def test_validate_schedule_config_rejects_multistate_value_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        validate_schedule_config(
            "multi-state",
            "5",
            _weekly_schedule(value="5"),
            None,
            None,
            None,
            8,
            None,
            state_text=["Off", "On"],
        )


def test_validate_schedule_config_rejects_bad_effective_date():
    with pytest.raises(ValueError):
        validate_schedule_config(
            "analog", "10.0", _weekly_schedule(), None, "not-a-date", None, 8, None
        )


def test_validate_schedule_config_rejects_bad_write_target_object_type():
    with pytest.raises(ValueError, match="not a valid"):
        validate_schedule_config(
            "analog",
            "10.0",
            _weekly_schedule(),
            None,
            None,
            None,
            8,
            [{"object_type": "not-a-real-type", "instance_number": 1}],
        )


def test_validate_schedule_config_rejects_write_target_missing_instance_number():
    with pytest.raises(ValueError, match="instance_number"):
        validate_schedule_config(
            "analog",
            "10.0",
            _weekly_schedule(),
            None,
            None,
            None,
            8,
            [{"object_type": "analog-value"}],
        )


def test_validate_schedule_config_rejects_schedule_or_calendar_as_write_target():
    """Schedule/calendar presentValue is calculated, never fed -- a write
    target pointing at either type passed this check before (both are
    valid OBJECT_TYPES) and would silently no-op forever at runtime
    (WritePropertyAllowlisted rejects the write, _apply_schedule_writes
    swallows it) -- the same shape connector-mapping targets already
    reject explicitly (see create_mapping in web/api/connectors.py)."""
    for bad_type in ("schedule", "calendar"):
        with pytest.raises(ValueError, match="not a valid"):
            validate_schedule_config(
                "analog",
                "10.0",
                _weekly_schedule(),
                None,
                None,
                None,
                8,
                [{"object_type": bad_type, "instance_number": 1}],
            )


# --- build_bacnet_object / live evaluation for schedule -------------------


@pytest.mark.asyncio
async def test_build_application_builds_schedule_object():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(),
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    assert obj is not None
    # Seeded from schedule_default at construction time -- interpret_schedule
    # (real bacpypes3 evaluation loop) hasn't necessarily run yet.
    assert (await obj.read_property("scheduleDefault")).get_value() == 10.0


@pytest.mark.asyncio
async def test_schedule_object_unconfigured_side_reads_as_empty_not_unknown_property():
    # Found live via a real second bacpypes3 client: omitting the
    # kwarg entirely for whichever of weeklySchedule/exceptionSchedule isn't
    # configured left that property genuinely unset, so a real ReadProperty
    # for it raised Error(unknown-property) instead of returning an empty
    # list -- confirmed to break YABE's scheduler view. Both a weekly-only
    # and an exception-only schedule must read the *other*, unconfigured
    # property back as an empty list instead of erroring.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    weekly_only = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-Weekly",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(),
        schedule_priority=8,
    )
    exception_only = BacnetObject(
        object_type="schedule",
        instance_number=2,
        name="Sched-Exception",
        schedule_value_type="binary",
        schedule_default="inactive",
        exception_schedule=[
            {
                "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
                "priority": 1,
                "time_values": [{"time": "08:00", "value": "active"}],
            }
        ],
        schedule_priority=8,
    )
    app = build_application(device, [weekly_only, exception_only])

    weekly_obj = app.get_object_name("Sched-Weekly")
    exception_schedule = await weekly_obj.read_property("exceptionSchedule")
    assert list(exception_schedule) == []

    exception_obj = app.get_object_name("Sched-Exception")
    weekly_schedule = await exception_obj.read_property("weeklySchedule")
    # weeklySchedule is a fixed Array[7] of DailySchedule -- the
    # "unconfigured" placeholder is still 7 entries, each with no
    # time-value entries for that day.
    assert len(weekly_schedule) == 7
    assert all(list(day.daySchedule) == [] for day in weekly_schedule)


@pytest.mark.asyncio
async def test_schedule_object_eval_returns_weekly_schedule_value_for_matching_time():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    # eval() is called directly with an explicit date/time (rather than
    # letting interpret_schedule read the real wall clock) so this test is
    # deterministic regardless of when it actually runs.
    current_value, _next_transition = obj.eval(Date.now(), Time((9, 0, 0, 0)))
    assert current_value.get_value() == 50.0


@pytest.mark.asyncio
async def test_schedule_object_eval_falls_back_to_default_before_first_time_value():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    current_value, _next_transition = obj.eval(Date.now(), Time((1, 0, 0, 0)))
    assert current_value.get_value() == 10.0


@pytest.mark.asyncio
async def test_schedule_object_eval_exception_schedule_overrides_weekly_schedule():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        exception_schedule=[
            {
                "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
                "priority": 1,
                "time_values": [{"time": "08:00", "value": "99.0"}],
            }
        ],
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    current_value, _next_transition = obj.eval(Date.now(), Time((9, 0, 0, 0)))
    assert current_value.get_value() == 99.0


# --- Exception_Schedule entries sharing an Event_Priority: the first wins -----

_EVERY_DAY = {"kind": "date", "year": "*", "month": "*", "day": "*"}
_OTHER_DAY = {"kind": "date", "year": 2001, "month": 1, "day": 1}


def _event(priority, *time_values, period=_EVERY_DAY):
    return {
        "period": period,
        "priority": priority,
        "time_values": [{"time": t, "value": v} for t, v in time_values],
    }


def _evaluated(events, at=(9, 0, 0, 0)):
    """(value, next transition) of a schedule whose weekly value is 50.0 from
    08:00 and whose Exception_Schedule is `events`, evaluated at `at`."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        exception_schedule=events,
        schedule_priority=8,
    )
    obj = build_application(device, [sched]).get_object_name("Sched-1")
    value, transition = obj.eval(Date.now(), Time(at))
    return value.get_value(), tuple(transition)


@pytest.mark.asyncio
async def test_on_equal_priority_the_first_exception_entry_wins():
    first, second = _event(1, ("08:00", "11.0")), _event(1, ("08:00", "22.0"))

    assert _evaluated([first, second])[0] == 11.0
    assert _evaluated([second, first])[0] == 22.0


@pytest.mark.asyncio
async def test_an_earlier_entry_not_yet_started_lets_the_later_one_hold_until_it_starts():
    events = [_event(1, ("10:00", "11.0")), _event(1, ("08:00", "22.0"))]

    assert _evaluated(events, at=(9, 0, 0, 0)) == (22.0, (10, 0, 0, 0))
    assert _evaluated(events, at=(10, 30, 0, 0))[0] == 11.0


@pytest.mark.asyncio
async def test_the_next_transition_is_the_earliest_pending_time_of_every_tied_entry():
    events = [_event(1, ("12:00", "11.0")), _event(1, ("08:00", "22.0"), ("11:00", None))]

    assert _evaluated(events, at=(9, 0, 0, 0)) == (22.0, (11, 0, 0, 0))


@pytest.mark.asyncio
async def test_an_entry_whose_value_in_effect_is_null_does_not_compete():
    null_first = [_event(1, ("08:00", None)), _event(1, ("08:00", "22.0"))]
    null_second = [_event(1, ("08:00", "11.0")), _event(1, ("08:00", None))]
    both_null = [_event(1, ("08:00", None)), _event(1, ("08:00", None))]

    assert _evaluated(null_first)[0] == 22.0
    assert _evaluated(null_second)[0] == 11.0
    assert _evaluated(both_null)[0] == 50.0


@pytest.mark.asyncio
async def test_different_event_priorities_are_unchanged():
    events = [_event(2, ("08:00", "11.0")), _event(1, ("08:00", "77.0"))]

    assert _evaluated(events)[0] == 77.0


@pytest.mark.asyncio
async def test_an_equal_priority_entry_for_another_day_does_not_compete():
    other_day, every_day = _event(1, ("08:00", "11.0"), period=_OTHER_DAY), _event(1, ("08:00", "22.0"))

    assert _evaluated([other_day, every_day])[0] == 22.0
    assert _evaluated([every_day, other_day])[0] == 22.0


@pytest.mark.asyncio
async def test_a_tie_does_not_disturb_an_entry_of_another_priority():
    tied = [_event(3, ("08:00", "11.0")), _event(3, ("08:00", "22.0"))]
    higher = _event(1, ("08:00", "77.0"))
    unmatched = _event(1, ("08:00", "77.0"), period=_OTHER_DAY)

    assert _evaluated([*tied, higher])[0] == 77.0
    assert _evaluated([*tied, unmatched])[0] == 11.0


@pytest.mark.asyncio
async def test_interpret_schedule_writes_the_first_tied_entry_to_present_value():
    from bacpack.objects.schedule import reinterpret_schedule

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="50.0"),
        exception_schedule=[_event(1, ("00:00", "11.0")), _event(1, ("00:00", "22.0"))],
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    await asyncio.sleep(0.05)
    obj = app.get_object_name("Sched-1")

    reinterpret_schedule(obj)

    assert obj.presentValue.get_value() == 11.0


# --- Write-target forwarding (the upstream present_value_changed no-`await`
# bug fix -- see WritingScheduleObject's docstring) -------------------------


@pytest.mark.asyncio
async def test_schedule_write_target_actually_updates_target_present_value():
    # Regression test for the upstream bacpypes3 bug: without
    # WritingScheduleObject's override, present_value_changed calls
    # write_property() with no `await`, silently discarding the coroutine --
    # this assertion (the target's real presentValue actually changed) is
    # exactly what would fail under the un-overridden upstream method.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    app = build_application(device, [target, sched])
    # interpret_schedule runs via asyncio.get_running_loop().call_soon at
    # construction time, and the write-forwarding it triggers is itself a
    # fire-and-forget asyncio.ensure_future task -- give the loop a few
    # turns to actually run both.
    for _ in range(10):
        await asyncio.sleep(0.02)
    tgt = app.get_object_name("Target-AV")
    assert await tgt.read_property("presentValue") == 60.0


@pytest.mark.asyncio
async def test_schedule_write_queued_before_the_schedule_is_retired_does_nothing():
    # The write runs as a task; a live edit can retire the schedule
    # (delete_object clears its app) before the task gets to run.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    app = build_application(device, [target, sched])
    for _ in range(10):
        await asyncio.sleep(0.02)
    sched_obj = app.get_object_name("Sched-1")
    app.delete_object(sched_obj)

    await sched_obj._apply_schedule_writes(70.0)

    assert await app.get_object_name("Target-AV").read_property("presentValue") == 60.0


@pytest.mark.asyncio
async def test_schedule_write_target_bypasses_bacnet_write_locked():
    # A schedule write-target is BACpack's own engine applying the
    # schedule's own evaluated value, not an external client -- it must
    # still succeed even when the target has bacnet_writable=False, via the
    # same internal_write() bypass write_feed_value/
    # write_manual_value already use.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(
        object_type="analog-value",
        instance_number=7,
        name="Locked-AV",
        bacnet_writable=False,
    )
    sched = BacnetObject(
        object_type="schedule",
        instance_number=2,
        name="Sched-2",
        schedule_value_type="analog",
        schedule_default="1.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="77.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 7}],
    )
    app = build_application(device, [target, sched])
    for _ in range(10):
        await asyncio.sleep(0.02)
    tgt = app.get_object_name("Locked-AV")
    assert await tgt.read_property("presentValue") == 77.0


class _TrafficLog:
    def __init__(self):
        self.entries = []

    def add(self, event, detail):
        self.entries.append((event, detail))


@pytest.mark.asyncio
async def test_schedule_write_traffic_entry_shows_the_value_as_written():
    # Found live: the Traffic tab showed a binary schedule's
    # write as "present-value = <Enumerated: 1>" (the repr of the demoted
    # AnyAtomic value) instead of the BinaryPV name the target received.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="binary-output", instance_number=1, name="BO-1")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-B",
        schedule_value_type="binary",
        schedule_default="inactive",
        weekly_schedule=_weekly_schedule(time="00:00", value="active"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "binary-output", "instance_number": 1}],
    )
    app = build_application(device, [target, sched])
    app.traffic_log = _TrafficLog()
    for _ in range(10):
        await asyncio.sleep(0.02)
    details = [detail for event, detail in app.traffic_log.entries if event == "schedule-write"]
    assert details and details[-1].endswith("= active")


# --- Clock jump reschedules a running Schedule ------------------------
# bacpypes3's own interpret_schedule() arms its next-transition timer with a
# delay computed against whatever the clock was when it last ran. A device
# clock jump moves Local_Time immediately, but without also cancelling and
# re-running that timer, Present_Value stays stale until the OLD timer
# eventually fires -- possibly hours later on the real (unshifted) event
# loop clock.


@pytest.mark.asyncio
async def test_clock_jump_reschedules_a_running_schedule_immediately():
    from bacpack import device_clock
    from bacpack.clock_sync import apply_clock_offset

    device_clock.install()
    try:
        # Pin the device's clock to a fixed, controlled local time (07:55)
        # before the app is even built, so interpret_schedule's own
        # construction-time evaluation is deterministic regardless of the
        # real wall clock.
        device_clock.set_offset(device_clock.offset_for_local(2030, 6, 15, 7, 55, 0))

        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        sched = BacnetObject(
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
            schedule_priority=8,
        )
        app = build_application(device, [sched])
        for _ in range(5):
            await asyncio.sleep(0.02)  # let the construction-time call_soon run

        obj = app.get_object_name("Sched-1")
        assert obj.presentValue.get_value() == 10.0  # 07:55 -- before the 08:00 transition

        old_handle = obj._interpret_schedule_handle
        assert old_handle is not None and not old_handle.cancelled()

        # Jump straight past the 08:00 transition (07:55 -> 08:05).
        new_offset = device_clock.offset_for_local(2030, 6, 15, 8, 5, 0)
        await apply_clock_offset(None, app, device.id, new_offset, "test")

        # Present_Value flips immediately -- it must not wait for the OLD
        # timer (armed for a real ~5-minute delay that never actually
        # elapses in this test) to fire.
        assert obj.presentValue.get_value() == 50.0
        assert old_handle.cancelled()
        assert obj._interpret_schedule_handle is not old_handle
    finally:
        device_clock.uninstall()


# --- One evaluation timer chain per Schedule ---------------------------------


def _record_timers(monkeypatch):
    """The loop's call_later calls as (callback, handle), through the public API."""
    loop = asyncio.get_running_loop()
    real = loop.call_later
    recorded = []

    def call_later(delay, callback, *args, **kwargs):
        handle = real(delay, callback, *args, **kwargs)
        recorded.append((callback, handle))
        return handle

    monkeypatch.setattr(loop, "call_later", call_later)
    return recorded


def _live_timers(obj, recorded):
    return [h for callback, h in recorded if callback == obj.interpret_schedule and not h.cancelled()]


def _timer_schedule_row():
    return BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        schedule_priority=8,
    )


@pytest.mark.asyncio
async def test_a_new_schedule_runs_one_evaluation_timer(monkeypatch):
    recorded = _record_timers(monkeypatch)
    app = build_application(Device(id=599001, name="Test Device", vendor_identifier=999), [_timer_schedule_row()])
    await asyncio.sleep(0.05)

    assert len(_live_timers(app.get_object_name("Sched-1"), recorded)) == 1


@pytest.mark.asyncio
async def test_re_evaluating_a_schedule_keeps_one_evaluation_timer(monkeypatch):
    from bacpack.objects.schedule import reinterpret_schedule

    recorded = _record_timers(monkeypatch)
    app = build_application(Device(id=599001, name="Test Device", vendor_identifier=999), [_timer_schedule_row()])
    await asyncio.sleep(0.05)
    obj = app.get_object_name("Sched-1")

    obj.interpret_schedule()
    reinterpret_schedule(obj)
    obj.interpret_schedule()

    assert len(_live_timers(obj, recorded)) == 1


@pytest.mark.asyncio
async def test_a_retired_schedule_leaves_no_timer(monkeypatch):
    from bacpack import live_swap

    recorded = _record_timers(monkeypatch)
    app = build_application(Device(id=599001, name="Test Device", vendor_identifier=999), [_timer_schedule_row()])
    await asyncio.sleep(0.05)
    obj = app.get_object_name("Sched-1")

    live_swap.retire(app, obj)

    assert _live_timers(obj, recorded) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_first", [True, False])
async def test_a_live_swapped_schedule_has_one_timer_and_the_old_one_none(monkeypatch, replay_first):
    from bacpack import live_swap, object_config

    recorded = _record_timers(monkeypatch)
    row = _timer_schedule_row()
    app = build_application(Device(id=599001, name="Test Device", vendor_identifier=999), [row])
    await asyncio.sleep(0.05)
    old = app.get_object_name("Sched-1")
    work = bacpypes3_compat.DeferredLoopWork()
    new = work.build(build_bacnet_object, BacnetObject(**object_config.row_from_object(row)))

    if replay_first:  # the order live_config applies a swap in
        work.replay()
    taken = live_swap.retire(app, old)
    live_swap.attach(app, new, taken)
    if not replay_first:
        work.replay()
    await asyncio.sleep(0.05)

    assert _live_timers(old, recorded) == []
    assert len(_live_timers(new, recorded)) == 1


# --- Effective Period wildcard support --------------------------------------
# See effective_period/_match_date_range_with_wildcards in bacpypes3_compat.py: the
# conventional "always in effect" representation (fully-wildcarded
# 255,255,255,255 on both ends) needed evaluation taught to recognize it,
# since bacpypes3's own match_date_range is a plain tuple comparison with no
# wildcard awareness at all.


def test_match_date_range_with_wildcards_matches_any_date():
    always = DateRange(startDate=Date((255, 255, 255, 255)), endDate=Date((255, 255, 255, 255)))
    assert _match_date_range_with_wildcards(Date((126, 8, 30, 0)), always) is True
    assert _match_date_range_with_wildcards(Date((0, 1, 1, 0)), always) is True
    assert _match_date_range_with_wildcards(Date((254, 12, 31, 0)), always) is True


def test_match_date_range_with_wildcards_still_bounds_a_concrete_range():
    bounded = DateRange(startDate=Date((126, 1, 1, 255)), endDate=Date((126, 6, 30, 255)))
    assert _match_date_range_with_wildcards(Date((126, 3, 15, 0)), bounded) is True
    assert _match_date_range_with_wildcards(Date((126, 8, 30, 0)), bounded) is False


@pytest.mark.asyncio
async def test_schedule_with_no_effective_period_configured_uses_wildcard_and_still_evaluates():
    # A schedule with no schedule_effective_start/end configured builds a
    # fully-wildcarded "always in effect" EffectivePeriod -- confirm eval()
    # still returns a real value (not None, which would mean "outside the
    # effective period") at dates far outside the old wide-concrete-range
    # implementation's own bounds (1900-2154), proving this isn't just
    # working by coincidence of still being in-range.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
        schedule_priority=8,
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    for edate in (Date((0, 1, 1, 0)), Date((254, 12, 31, 0)), Date.now()):
        current_value, _next_transition = obj.eval(edate, Time((1, 0, 0, 0)))
        assert current_value is not None
        assert current_value.get_value() == 10.0  # before the first weekly time-value


# --- BACnet-writable Schedule/Calendar config --------------------------------
# Widened per explicit user request: reuses the same bacnet_writable
# flag/checkbox every other writable type already has. Unchecked (the
# original v1 behavior) keeps schedule/calendar config BACpack-UI-only;
# checked lets a real client fully configure it over the wire, persisted
# back to this row's own columns (see ScheduleConfigPersisted/
# CalendarConfigPersisted in bacnet.py).


@pytest.mark.asyncio
async def test_schedule_config_write_rejected_when_not_bacnet_writable():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=False,
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("scheduleDefault", Real(99.0))
    assert obj.scheduleDefault.get_value() == 10.0


@pytest.mark.asyncio
async def test_schedule_config_write_persists_to_db_when_bacnet_writable(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    await obj.write_property("scheduleDefault", Real(99.0))
    assert obj.scheduleDefault.get_value() == 99.0

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.schedule_default == "99.0"


@pytest.mark.asyncio
async def test_schedule_config_write_rejects_nonexistent_write_target(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    with pytest.raises(PropertyError):
        await obj.write_property(
            "listOfObjectPropertyReferences",
            [DeviceObjectPropertyReference(objectIdentifier=("analog-value", 999))],
        )

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.schedule_write_targets is None  # rejected write never persisted


@pytest.mark.asyncio
async def test_schedule_config_single_day_array_write_persists_only_that_day(session_factory):
    """Confirmed live: a real client (YABE) writes weeklySchedule
    one day at a time via its BACnetARRAY index (1-7), not the whole
    7-element array at once. `value` for such a write is a lone
    DailySchedule, not the full week -- ScheduleConfigPersisted.write_property
    must splice it into the current week before validating/persisting, not
    treat it as the complete prospective weeklySchedule (which crashed with
    an unhandled exception, surfaced to the client as a bare
    "device: operational-problem")."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    new_monday = DailySchedule(daySchedule=[TimeValue(time=Time("09:00:00"), value=Real(77.0))])
    await obj.write_property("weeklySchedule", new_monday, index=1)

    # only Monday (array index 1 -> Python index 0) changed; every other day
    # keeps the _weekly_schedule() fixture's original 08:00/50.0 entry.
    week = list(obj.weeklySchedule)
    assert [tv.time for tv in week[0].daySchedule] == [Time("09:00:00")]
    assert week[0].daySchedule[0].value.get_value() == 77.0
    for day in week[1:]:
        assert [tv.time for tv in day.daySchedule] == [Time("08:00:00")]

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.weekly_schedule[0] == [{"time": "09:00", "value": "77.0"}]
        for day in row.weekly_schedule[1:]:
            assert day == [{"time": "08:00", "value": "50.0"}]


@pytest.mark.asyncio
async def test_schedule_config_write_normalizes_time_value_order_for_eval(session_factory):
    """Real root cause of a live bug report: a client (YABE)
    can send a day's time-value entries in any order over the wire --
    BACpack's own construction path (build_daily_schedule) always sorts
    entries ascending before building the live object, since bacpypes3's
    eval() assumes ascending order within a day (it iterates forward,
    overwrites `daily_value` on each match, and stops at the first entry
    whose time is still in the future) -- but the live WriteProperty path
    handed the client's raw value straight to bacpypes3's own
    write_property() completely unsorted. Live repro: a client wrote
    Sunday's day with entries in descending order (19:23, 18:00, 08:00,
    exactly what was observed on the wire); evaluating at 19:23 then
    returned the 08:00 value (72.0) instead of the correct, just-written
    19:23 value (69.0), because the unsorted array made the "keep
    overwriting until the first still-future entry" loop walk straight
    through every entry with no early stop."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="67.0",
            weekly_schedule=_weekly_schedule(time="08:00", value="72.0"),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    # array index 7 -> Sunday; entries sent in descending order.
    sunday = DailySchedule(
        daySchedule=[
            TimeValue(time=Time("19:23:00"), value=Real(69.0)),
            TimeValue(time=Time("18:00:00"), value=Real(65.0)),
            TimeValue(time=Time("08:00:00"), value=Real(72.0)),
        ]
    )
    await obj.write_property("weeklySchedule", sunday, index=7)

    # BACnet Date day-of-week: 1=Monday ... 7=Sunday.
    current_value, _next_transition = obj.eval(Date((126, 9, 6, 7)), Time((19, 23, 0, 0)))
    assert current_value.get_value() == 69.0


def _saved_schedule_object(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="67.0",
            weekly_schedule=_weekly_schedule(time="08:00", value="72.0"),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id
    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        return object_id, build_bacnet_object(row, config_session_factory=session_factory)


def _descending_time_values():
    return [
        TimeValue(time=Time("07:30:00"), value=Real(70.0)),
        TimeValue(time=Time("05:00:00"), value=Real(65.0)),
    ]


_ASCENDING_SAVED = [{"time": "05:00", "value": "65.0"}, {"time": "07:30", "value": "70.0"}]


@pytest.mark.asyncio
async def test_schedule_config_indexed_day_write_saves_time_values_sorted(session_factory):
    """The saved row matches the sorted day the live object runs."""
    object_id, obj = _saved_schedule_object(session_factory)

    await obj.write_property("weeklySchedule", DailySchedule(daySchedule=_descending_time_values()), index=2)

    with session_factory() as session:
        assert session.get(BacnetObject, object_id).weekly_schedule[1] == _ASCENDING_SAVED


@pytest.mark.asyncio
async def test_schedule_config_save_waits_for_a_held_lock_off_the_loop(session_factory):
    object_id, obj = _saved_schedule_object(session_factory)
    day = DailySchedule(daySchedule=_descending_time_values())

    run = await run_while_locked(session_factory, lambda: obj.write_property("weeklySchedule", day, index=2))

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).weekly_schedule[1] == _ASCENDING_SAVED


@pytest.mark.asyncio
async def test_schedule_config_failed_save_is_logged_and_the_write_still_succeeds(session_factory, caplog):
    # The live object already runs the new day; failing the write would tell
    # the client it didn't.
    object_id, obj = _saved_schedule_object(session_factory)
    obj._schedule_session_factory = CommitFailingSessionFactory(session_factory)

    await obj.write_property("weeklySchedule", DailySchedule(daySchedule=_descending_time_values()), index=2)

    assert [str(tv.time) for tv in obj.weeklySchedule[1].daySchedule] == ["05:00:00.00", "07:30:00.00"]
    assert "could not save" in caplog.text


@pytest.mark.asyncio
async def test_schedule_config_whole_week_write_saves_time_values_sorted(session_factory):
    object_id, obj = _saved_schedule_object(session_factory)

    week = [DailySchedule(daySchedule=_descending_time_values()) for _ in range(7)]
    await obj.write_property("weeklySchedule", week)

    with session_factory() as session:
        assert session.get(BacnetObject, object_id).weekly_schedule == [_ASCENDING_SAVED] * 7


@pytest.mark.asyncio
async def test_schedule_config_exception_write_saves_time_values_sorted(session_factory):
    object_id, obj = _saved_schedule_object(session_factory)

    event = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(date=Date((126, 12, 25, 255)))),
        listOfTimeValues=_descending_time_values(),
        eventPriority=1,
    )
    await obj.write_property("exceptionSchedule", [event])

    with session_factory() as session:
        saved = session.get(BacnetObject, object_id).exception_schedule
    assert saved[0]["time_values"] == _ASCENDING_SAVED


@pytest.mark.asyncio
async def test_schedule_config_write_clears_last_weekly_day_when_exception_schedule_covers_validation(
    session_factory,
):
    """Edge case guarded while fixing the ordering bug above: clearing
    weeklySchedule's last remaining configured day (via an indexed write)
    is a legal write as long as exception_schedule alone satisfies
    validate_schedule_config's "at least one of weekly_schedule/
    exception_schedule" rule. The unbuilt weekly_schedule then collapses
    to None (build_bacnet_object's own "not configured" placeholder shape)
    -- computing the live write_value for this specific indexed day must
    not crash indexing into that None."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
            exception_schedule=[
                {
                    "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
                    "priority": 1,
                    "time_values": [{"time": "08:00", "value": "99.0"}],
                }
            ],
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    # Clear every day one at a time -- once ALL seven are empty,
    # weekly_schedule collapses to None; exception_schedule alone still
    # satisfies validation, so this must not crash on the last write.
    for day_index in range(1, 8):
        await obj.write_property("weeklySchedule", DailySchedule(daySchedule=[]), index=day_index)

    week = list(obj.weeklySchedule)
    assert all(day.daySchedule == [] for day in week)


@pytest.mark.asyncio
async def test_schedule_config_single_day_array_write_rejects_bad_index(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    new_day = DailySchedule(daySchedule=[TimeValue(time=Time("09:00:00"), value=Real(77.0))])
    with pytest.raises(PropertyError):
        await obj.write_property("weeklySchedule", new_day, index=8)


@pytest.mark.asyncio
async def test_schedule_config_write_recomputes_present_value_immediately():
    """Confirmed live: a real external weeklySchedule write
    succeeded and persisted correctly, but presentValue kept serving its
    previous value instead of picking up a newly-added time-value already
    in the past -- bacpypes3's own property-monitor-driven re-evaluation
    never fires for a write arriving through write_property() (see the
    comment in ScheduleConfigPersisted.write_property). A schedule's
    presentValue must reflect a config change immediately, not just after
    the object's own periodic timer or an engine restart happens to catch
    up."""
    obj = build_bacnet_object(
        BacnetObject(
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(time="08:00", value="50.0"),
            schedule_priority=8,
            bacnet_writable=True,
        )
    )
    assert obj.presentValue.get_value() != 77.0  # not the fixture's value, and not today's write yet

    past = "00:00:00"  # always already started today
    week = [
        DailySchedule(daySchedule=[TimeValue(time=Time(past), value=Real(77.0))]) for _ in range(7)
    ]
    await obj.write_property("weeklySchedule", week)

    assert obj.presentValue.get_value() == 77.0


@pytest.mark.asyncio
async def test_schedule_config_write_accepts_binary_weekly_schedule_value():
    """Confirmed live: ANY external weeklySchedule/exceptionSchedule
    write to a binary-typed schedule was unconditionally rejected with
    value-out-of-range. Root cause: a BinaryPV value arriving inside a
    TimeValue's AnyAtomic-typed slot gets demoted by `.get_value()` to
    bacpypes3's generic Enumerated base, which loses BinaryPV's name
    mapping -- `str(value)` returned the raw int ("0"/"1") instead of
    "inactive"/"active", failing validate_schedule_config's own check on
    the string it had just been handed. See atomic_value_to_schedule_string."""
    obj = build_bacnet_object(
        BacnetObject(
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="binary",
            schedule_default="inactive",
            weekly_schedule=_weekly_schedule(time="08:00", value="active"),
            schedule_priority=8,
            bacnet_writable=True,
        )
    )
    week = [
        DailySchedule(daySchedule=[TimeValue(time=Time("09:00:00"), value=BinaryPV("inactive"))])
        for _ in range(7)
    ]
    await obj.write_property("weeklySchedule", week)  # must not raise
    assert list(obj.weeklySchedule)[0].daySchedule[0].value.get_value() == BinaryPV("inactive")


@pytest.mark.asyncio
async def test_schedule_config_write_accepts_partially_wildcarded_effective_period(session_factory):
    """("Partially": one whole side wildcarded, which stays accepted; a partly
    wildcarded *date* is refused.) A real client may wildcard only one side of
    effectivePeriod (e.g. "starts on this date, no end") --
    date_range_to_effective_start_end previously only recognized a FULLY
    wildcarded DateRange (both sides 255,255,255,255), so a partial wildcard
    fell through to dt.date(..., 255, 255) and crashed with an uncaught
    ValueError instead of persisting cleanly."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    partial = DateRange(startDate=Date((126, 1, 1, 255)), endDate=Date((255, 255, 255, 255)))
    await obj.write_property("effectivePeriod", partial)  # must not raise

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.schedule_effective_start == "2026-01-01"
        assert row.schedule_effective_end is None


@pytest.mark.asyncio
async def test_schedule_config_indexed_write_rejected_for_non_array_property(session_factory):
    """Only weeklySchedule is a real BACnetARRAY[7] -- exceptionSchedule and
    listOfObjectPropertyReferences are plain SequenceOf (LIST) properties,
    which don't support an indexed WriteProperty. An indexed write to
    either previously fell into the generic whole-value-replacement branch,
    stashing a single element where the unbuild helpers expect a list and
    crashing with an unhandled TypeError instead of a clean
    propertyIsNotAnArray error."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    event = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(date=Date((126, 12, 25, 255)))),
        listOfTimeValues=[TimeValue(time=Time("09:00:00"), value=Real(1.0))],
        eventPriority=Unsigned(1),
    )
    with pytest.raises(PropertyError):
        await obj.write_property("exceptionSchedule", event, index=1)


@pytest.mark.asyncio
async def test_schedule_config_write_malformed_value_raises_clean_property_error(session_factory):
    """The JSON "unbuild" conversions (unbuild_special_event etc.) ran
    before/outside the try/except that turns a ValueError into a clean
    PropertyError -- a real external write carrying data those helpers
    can't parse (e.g. a SpecialEventPeriod with no CalendarEntry choice
    set at all) crashed with an uncaught exception instead of a clean
    value-out-of-range error."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    bad_event = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry()),  # no choice set at all
        listOfTimeValues=[TimeValue(time=Time("09:00:00"), value=Real(1.0))],
        eventPriority=Unsigned(1),
    )
    with pytest.raises(PropertyError):
        await obj.write_property("exceptionSchedule", [bad_event])


@pytest.mark.asyncio
async def test_schedule_config_write_after_the_row_was_deleted_still_applies(session_factory):
    """A device keeps serving a schedule whose row was deleted meanwhile: the
    write applies to the live object and the missing row is skipped."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    with session_factory() as session:
        session.delete(session.get(BacnetObject, object_id))
        session.commit()

    await obj.write_property("scheduleDefault", Real(20.0))  # must not raise
    assert obj.scheduleDefault.get_value() == 20.0


@pytest.mark.asyncio
async def test_refused_malformed_schedule_write_changes_nothing(session_factory):
    """A refused write leaves both the live object and the saved row as they were."""
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            exception_schedule=[],
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        saved_before = (row.exception_schedule, row.weekly_schedule, row.schedule_default)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    bad_event = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry()),
        listOfTimeValues=[TimeValue(time=Time("09:00:00"), value=Real(1.0))],
        eventPriority=Unsigned(1),
    )
    with pytest.raises(PropertyError):
        await obj.write_property("exceptionSchedule", [bad_event])

    assert list(obj.exceptionSchedule) == []
    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert (row.exception_schedule, row.weekly_schedule, row.schedule_default) == saved_before


# --- Evaluation fixes -------------------------------------------------------


def _null_entry_schedule(**overrides) -> BacnetObject:
    """Every day: 50.0 from 08:00, then a null entry (relinquish back to
    schedule_default) at 17:00."""
    fields = dict(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=[
            [{"time": "08:00", "value": "50.0"}, {"time": "17:00", "value": None}] for _ in range(7)
        ],
        schedule_priority=8,
    )
    fields.update(overrides)
    return BacnetObject(**fields)


@pytest.mark.asyncio
async def test_schedule_null_weekly_entry_relinquishes_to_schedule_default():
    """bacpypes3's eval() checks isinstance(time_value.value, Null), but a
    TimeValue's value is always AnyAtomic-wrapped, so that check never
    matched: a null entry made presentValue Null instead of falling back to
    Schedule_Default."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    app = build_application(device, [_null_entry_schedule()])
    obj = app.get_object_name("Sched-1")
    current_value, _next = obj.eval(Date.now(), Time((18, 0, 0, 0)))
    assert current_value.get_value() == 10.0


@pytest.mark.asyncio
async def test_schedule_null_exception_entry_falls_through_to_weekly_schedule():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = _null_entry_schedule(
        exception_schedule=[
            {
                "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
                "priority": 1,
                "time_values": [
                    {"time": "09:00", "value": "99.0"},
                    {"time": "10:00", "value": None},
                ],
            }
        ],
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    current_value, _next = obj.eval(Date.now(), Time((11, 0, 0, 0)))
    assert current_value.get_value() == 50.0


@pytest.mark.asyncio
async def test_schedule_outside_effective_period_holds_value_and_keeps_running():
    """eval() returns None outside Effective_Period, and bacpypes3's
    interpret_schedule unpacks it unconditionally -- the TypeError killed the
    schedule's own timer, so a schedule whose season starts in the future
    never started."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = _null_entry_schedule(
        schedule_effective_start="2150-01-01", schedule_effective_end="2150-12-31"
    )
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")
    current_value, next_transition = obj.eval(Date.now(), Time((9, 0, 0, 0)))
    assert current_value is obj.presentValue
    assert tuple(next_transition) == (24, 0, 0, 0)

    obj.interpret_schedule()  # must not raise
    assert obj._interpret_schedule_handle is not None
    assert obj.presentValue.get_value() == 10.0


@pytest.mark.asyncio
async def test_schedule_config_write_open_start_effective_period_still_evaluates_live():
    """A wildcarded start with a concrete end ("in effect until X") was
    saved correctly but left on the live object as-is -- bacpypes3's plain
    tuple comparison puts every real date before a 255 start, so the
    schedule sat out of season until the next restart rebuilt it."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = _null_entry_schedule(bacnet_writable=True)
    app = build_application(device, [sched])
    obj = app.get_object_name("Sched-1")

    open_start = DateRange(startDate=Date((255, 255, 255, 255)), endDate=Date((250, 12, 31, 255)))
    await obj.write_property("effectivePeriod", open_start)

    assert _match_date_range_with_wildcards(Date.now(), obj.effectivePeriod)


# --- External write targets ------------------------------------------------

from bacpack.objects.schedule_config import (
    build_schedule_write_targets,
    unbuild_schedule_write_targets,
    is_external_target,
    normalize_write_targets,
)


def _remote(**overrides):
    target = {
        "device_instance": 599005,
        "object_type": "analog-value",
        "instance_number": 3,
        "property_identifier": "present-value",
    }
    target.update(overrides)
    return target


def test_is_external_target():
    assert is_external_target(_remote()) is True
    assert is_external_target({"object_type": "analog-value", "instance_number": 1}) is False
    assert is_external_target(_remote(device_instance=None)) is False


def test_normalize_drops_own_and_null_device_instance():
    targets = [
        _remote(device_instance=599001),
        {"object_type": "analog-value", "instance_number": 1, "device_instance": None},
        _remote(),
    ]
    # The own-device one ends up exactly like a target added as local.
    assert normalize_write_targets(targets, 599001) == [
        {"object_type": "analog-value", "instance_number": 3, "property_identifier": "presentValue"},
        {"object_type": "analog-value", "instance_number": 1},
        _remote(),
    ]


def test_normalize_canonicalizes_external_names_only():
    targets = [
        _remote(object_type="analogValue", property_identifier="presentValue"),
        {"object_type": "analog-value", "instance_number": 1, "property_identifier": "presentValue"},
    ]
    assert normalize_write_targets(targets, 1) == [
        _remote(),
        {"object_type": "analog-value", "instance_number": 1, "property_identifier": "presentValue"},
    ]


def test_normalize_leaves_bad_names_and_odd_input_for_validation():
    assert normalize_write_targets(None, 1) is None
    assert normalize_write_targets("junk", 1) == "junk"
    assert normalize_write_targets([_remote(object_type="nonsense")], 1) == [_remote(object_type="nonsense")]


def test_validate_accepts_external_target_without_local_type_rules():
    # "loop" isn't a BACpack OBJECT_TYPES entry, but it is a standard type.
    validate_schedule_config(
        "analog", "10.0", _weekly_schedule(), None, None, None, 8, [_remote(object_type="loop")]
    )


def test_external_target_accepts_a_proprietary_numeric_object_type():
    validate_schedule_config(
        "analog", "10.0", _weekly_schedule(), None, None, None, 8, [_remote(object_type="200")]
    )
    ref = build_schedule_write_targets([_remote(object_type="200")])[0]
    assert int(ref.objectIdentifier[0]) == 200


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"device_instance": 4194303}, "device_instance"),
        ({"device_instance": -1}, "device_instance"),
        ({"device_instance": True}, "device_instance"),
        ({"device_instance": "599005"}, "device_instance"),
        ({"object_type": "nonsense"}, "object type"),
        ({"property_identifier": "nonsense"}, "property"),
        ({"object_type": "schedule"}, "schedule"),
        ({"object_type": "calendar"}, "calendar"),
        ({"instance_number": None}, "instance_number"),
        ({"instance_number": 4194303}, "instance_number"),
    ],
)
def test_validate_rejects_bad_external_target(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_schedule_config(
            "analog", "10.0", _weekly_schedule(), None, None, None, 8, [_remote(**overrides)]
        )


def test_build_and_unbuild_round_trip_external_and_local():
    targets = [
        _remote(),
        {"object_type": "analog-value", "instance_number": 1, "property_identifier": "present-value"},
    ]
    refs = build_schedule_write_targets(targets)
    assert refs[0].deviceIdentifier[1] == 599005
    assert refs[1].deviceIdentifier is None
    assert unbuild_schedule_write_targets(refs) == targets


def test_unbuild_rejects_a_device_identifier_that_is_not_a_device():
    ref = DeviceObjectPropertyReference(
        objectIdentifier=("analog-value", 1),
        propertyIdentifier="presentValue",
        deviceIdentifier=("analog-value", 5),
    )
    with pytest.raises(ValueError):
        unbuild_schedule_write_targets([ref])


@pytest.mark.asyncio
async def test_local_write_path_skips_a_remote_target_even_if_a_local_object_matches():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    local = BacnetObject(object_type="analog-value", instance_number=3, name="Same-Id-AV")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[_remote()],
    )
    app = build_application(device, [local, sched])
    for _ in range(10):
        await asyncio.sleep(0.02)
    assert await app.get_object_name("Same-Id-AV").read_property("presentValue") != 60.0


@pytest.mark.asyncio
async def test_local_write_path_still_writes_a_ref_naming_its_own_device():
    # Belt-and-braces: save paths normalize these away, but a live ref that
    # names the own device is still local.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    local = BacnetObject(object_type="analog-value", instance_number=3, name="Own-AV")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[_remote(device_instance=599001)],
    )
    app = build_application(device, [local, sched])
    for _ in range(10):
        await asyncio.sleep(0.02)
    assert await app.get_object_name("Own-AV").read_property("presentValue") == 60.0


def _week_of(value, value_type="analog"):
    from bacpack.objects.schedule_config import build_daily_schedule

    return [build_daily_schedule([{"time": "00:00", "value": value}], value_type) for _ in range(7)]


async def _schedule_app(targets, *, value_type="analog", default="10.0", value="60.0", extra=()):
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type=value_type,
        schedule_default=default,
        weekly_schedule=_weekly_schedule(time="00:00", value=value),
        schedule_priority=8,
        schedule_write_targets=targets,
    )
    app = build_application(device, [*extra, sched])
    for _ in range(10):
        await asyncio.sleep(0.02)
    return app, app.get_object_name("Sched-1")


# Upstream's check_reliability, which a set of Weekly_Schedule,
# Exception_Schedule or Schedule_Default runs, faults any reference naming a
# device (Clause 12.24.10); a faulted schedule stops evaluating.
@pytest.mark.asyncio
@pytest.mark.parametrize("default", ["10.0", None])
async def test_a_schedule_with_a_remote_target_stays_healthy_when_its_week_is_set(default):
    app, sched = await _schedule_app([_remote()], default=default)

    sched.weeklySchedule = _week_of("70.0")
    for _ in range(10):
        await asyncio.sleep(0.02)

    assert str(sched.reliability) == "no-fault-detected"
    assert sched.presentValue.get_value() == 70.0


@pytest.mark.asyncio
async def test_a_ref_naming_its_own_device_stays_healthy_and_is_written_when_the_week_is_set():
    own = BacnetObject(object_type="analog-value", instance_number=3, name="Own-AV")
    app, sched = await _schedule_app([_remote(device_instance=599001)], extra=[own])

    sched.weeklySchedule = _week_of("70.0")
    for _ in range(10):
        await asyncio.sleep(0.02)

    assert str(sched.reliability) == "no-fault-detected"
    assert await app.get_object_name("Own-AV").read_property("presentValue") == 70.0


@pytest.mark.asyncio
@pytest.mark.parametrize("device_instance", [None, 599001])
async def test_a_local_target_of_the_wrong_type_still_faults_beside_a_remote_one(device_instance):
    av = BacnetObject(object_type="analog-value", instance_number=3, name="Local-AV")
    app, sched = await _schedule_app(
        [_remote(), _remote(device_instance=device_instance)],
        value_type="binary", default="inactive", value="active", extra=[av],
    )

    sched.weeklySchedule = _week_of("inactive", "binary")

    assert str(sched.reliability) == "configuration-error"


@pytest.mark.asyncio
async def test_bacnet_write_of_remote_reference_list_persists_and_own_device_is_normalized(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        av = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add_all([device, av, row])
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    await obj.write_property(
        "listOfObjectPropertyReferences",
        [
            DeviceObjectPropertyReference(
                objectIdentifier=("analog-value", 3),
                propertyIdentifier="presentValue",
                deviceIdentifier=("device", 599005),
            ),
            DeviceObjectPropertyReference(
                objectIdentifier=("analog-value", 2),
                propertyIdentifier="presentValue",
                deviceIdentifier=("device", 599001),
            ),
        ],
    )

    with session_factory() as session:
        stored = session.get(BacnetObject, object_id).schedule_write_targets
    assert stored == [
        {"object_type": "analog-value", "instance_number": 3, "property_identifier": "present-value", "device_instance": 599005},
        {"object_type": "analog-value", "instance_number": 2, "property_identifier": "presentValue"},
    ]
    live = obj.listOfObjectPropertyReferences
    assert live[0].deviceIdentifier[1] == 599005
    assert live[1].deviceIdentifier is None


@pytest.mark.asyncio
async def test_bacnet_write_of_remote_reference_skips_the_existence_check(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add_all([device, row])
        session.commit()
        object_id = row.id

    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)

    await obj.write_property(
        "listOfObjectPropertyReferences",
        [
            DeviceObjectPropertyReference(
                objectIdentifier=("analog-value", 999),
                propertyIdentifier="presentValue",
                deviceIdentifier=("device", 599005),
            )
        ],
    )
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).schedule_write_targets[0]["device_instance"] == 599005


@pytest.mark.asyncio
async def test_bacnet_write_with_a_non_device_device_identifier_is_refused(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add_all([device, row])
        session.commit()
        object_id = row.id

    with session_factory() as session:
        obj = build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)

    with pytest.raises(PropertyError):
        await obj.write_property(
            "listOfObjectPropertyReferences",
            [
                DeviceObjectPropertyReference(
                    objectIdentifier=("analog-value", 3),
                    propertyIdentifier="presentValue",
                    deviceIdentifier=("analog-value", 5),
                )
            ],
        )


# --- Local write failures are logged ----------------------------------------
# A failed local write used to be swallowed by a bare `pass`, and a missing
# target skipped silently: nothing in the Logs page to show why a target
# never moved.


@pytest.mark.asyncio
async def test_failed_local_schedule_write_is_logged(caplog):
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="multi-state-value", instance_number=3, name="MSV-3", state_text=["a", "b"])
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "multi-state-value", "instance_number": 3}],
    )
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        build_application(device, [target, sched])
        for _ in range(10):
            await asyncio.sleep(0.02)
    messages = [r.getMessage() for r in caplog.records if r.name == "bacpack.objects.schedule"]
    assert any("Sched-1" in m and "multi-state-value,3" in m for m in messages), messages


@pytest.mark.asyncio
async def test_missing_local_schedule_target_is_logged(caplog, monkeypatch):
    # Every save path and the build refuse a missing target; the
    # runtime log stays as a fallback, so get past the build's check here.
    from bacpack import object_config

    monkeypatch.setattr(object_config, "check_device", lambda device_name, rows: None)
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="60.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 99}],
    )
    with caplog.at_level(logging.WARNING, logger="bacpack"):
        build_application(device, [sched])
        for _ in range(10):
            await asyncio.sleep(0.02)
    messages = [r.getMessage() for r in caplog.records if r.name == "bacpack.objects.schedule"]
    assert any("Sched-1" in m and "analog-value,99" in m for m in messages), messages


# --- Local targets commanded at start and on a target/priority change
# The Present_Value change hook never fires when the first evaluation gives
# Schedule_Default (the value the object starts with), and a new target or
# Priority_For_Writing didn't get the current value until the next change.


def _slot(obj, priority):
    pv = obj.priorityArray[priority - 1]
    return None if pv.null is not None else getattr(pv, pv._choice)


async def _turns():
    for _ in range(10):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_schedule_equal_to_its_default_commands_a_local_target_at_start():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="10.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    app = build_application(device, [target, sched])
    await _turns()
    tgt = app.get_object_name("Target-AV")
    assert _slot(tgt, 8) == 10.0


@pytest.mark.asyncio
async def test_out_of_season_schedule_leaves_its_local_target_alone_at_start():
    # Outside Effective_Period the schedule holds its value so it doesn't
    # fight an in-season sibling for the same slot (ScheduleEvaluationHeld):
    # the start-up command must hold off too, or the out-of-season default
    # can win the race.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")
    summer = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Summer",
        schedule_value_type="analog",
        schedule_default="24.0",
        weekly_schedule=_weekly_schedule(time="00:00", value="24.0"),
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    winter = BacnetObject(
        object_type="schedule",
        instance_number=2,
        name="Winter",
        schedule_value_type="analog",
        schedule_default="18.0",
        schedule_effective_start="2099-01-01",
        schedule_effective_end="2099-12-31",
        schedule_priority=8,
        schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}],
    )
    app = build_application(device, [target, summer, winter])
    await _turns()
    assert _slot(app.get_object_name("Target-AV"), 8) == 24.0


async def _writable_schedule_app(session_factory, targets, **sched_fields):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=5, name="Target-AV"))
        session.add(
            BacnetObject(
                device=device,
                object_type="schedule",
                instance_number=1,
                name="Sched-1",
                schedule_value_type="analog",
                schedule_default="10.0",
                weekly_schedule=_weekly_schedule(time="00:00", value="10.0"),
                schedule_priority=8,
                schedule_write_targets=targets,
                bacnet_writable=True,
                **sched_fields,
            )
        )
        session.commit()
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await _turns()
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["priorityForWriting", PropertyIdentifier("priorityForWriting")])
async def test_priority_for_writing_change_commands_the_new_slot_and_releases_the_old(session_factory, attr):
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    assert _slot(tgt, 8) == 10.0
    await sched.write_property(attr, Unsigned(12))
    await _turns()
    assert _slot(tgt, 12) == 10.0
    # The old slot is released, or slot 8 would outrank the new 12.
    assert _slot(tgt, 8) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr", ["listOfObjectPropertyReferences", PropertyIdentifier("listOfObjectPropertyReferences")]
)
async def test_new_write_target_is_commanded(session_factory, attr):
    app = await _writable_schedule_app(session_factory, [])
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    assert _slot(tgt, 8) is None
    await sched.write_property(
        attr,
        [DeviceObjectPropertyReference(objectIdentifier=("analog-value", 5), propertyIdentifier="presentValue")],
    )
    await _turns()
    assert _slot(tgt, 8) == 10.0


@pytest.mark.asyncio
async def test_out_of_season_schedule_commands_nothing_on_a_target_or_priority_write(session_factory):
    app = await _writable_schedule_app(
        session_factory, [], schedule_effective_start="2099-01-01", schedule_effective_end="2099-12-31"
    )
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    await sched.write_property(
        "listOfObjectPropertyReferences",
        [DeviceObjectPropertyReference(objectIdentifier=("analog-value", 5), propertyIdentifier="presentValue")],
    )
    await sched.write_property("priorityForWriting", Unsigned(12))
    await _turns()
    assert _slot(tgt, 8) is None
    assert _slot(tgt, 12) is None


@pytest.mark.asyncio
async def test_schedule_coming_into_season_with_its_held_value_commands_its_target(session_factory):
    # Out of season it holds Schedule_Default; coming into season with the
    # same value fires no change hook, so it's commanded like at start.
    app = await _writable_schedule_app(
        session_factory,
        [{"object_type": "analog-value", "instance_number": 5}],
        schedule_effective_start="2099-01-01",
        schedule_effective_end="2099-12-31",
    )
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    assert _slot(tgt, 8) is None
    always = DateRange(startDate=Date((255, 255, 255, 255)), endDate=Date((255, 255, 255, 255)))
    await sched.write_property("effectivePeriod", always)
    await _turns()
    assert _slot(tgt, 8) == 10.0


# --- A local target's property is checked -----------------------------------


def test_validate_schedule_config_refuses_a_local_target_with_an_unknown_property():
    with pytest.raises(ValueError, match="unknown property"):
        validate_schedule_config(
            "analog", "10.0", _weekly_schedule(), None, None, None, 8,
            [{"object_type": "analog-value", "instance_number": 1, "property_identifier": "fooBar"}],
        )


@pytest.mark.parametrize("prop", ["presentValue", "present_value", "Present-Value", None])
def test_validate_schedule_config_accepts_a_local_target_property(prop):
    target = {"object_type": "analog-value", "instance_number": 1}
    if prop is not None:
        target["property_identifier"] = prop
    validate_schedule_config("analog", "10.0", _weekly_schedule(), None, None, None, 8, [target])


def _saved_schedule(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type="analog",
            schedule_default="10.0",
            weekly_schedule=_weekly_schedule(),
            schedule_priority=8,
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id
    with session_factory() as session:
        return object_id, build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [(255, 3, 1, 255), (126, 255, 1, 255), (126, 3, 255, 255)])
async def test_schedule_effective_period_with_a_partly_wildcarded_side_is_refused(session_factory, start):
    """A year-only wildcard (255, 3, 1) must not be saved as 2155-03-01;
    month/day-only wildcards are refused too."""
    object_id, obj = _saved_schedule(session_factory)
    with pytest.raises(PropertyError):
        await obj.write_property(
            "effectivePeriod", DateRange(startDate=Date(start), endDate=Date((255, 255, 255, 255)))
        )
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).schedule_effective_start is None


@pytest.mark.parametrize("bad", ["2155-03-01", "1899-12-31"])
def test_validate_schedule_config_refuses_an_effective_date_outside_bacnet_years(bad):
    """A BACnet Date holds years 1900-2154; 2155 would build as the year
    wildcard and make every later wire write refuse."""
    with pytest.raises(ValueError, match="1900"):
        validate_schedule_config("analog", "10.0", _weekly_schedule(), None, None, bad, 8, None)
    with pytest.raises(ValueError, match="1900"):
        validate_schedule_config("analog", "10.0", _weekly_schedule(), None, bad, None, 8, None)


# --- a schedule releases the slots it stops using --------------------


@pytest.mark.asyncio
async def test_a_removed_write_target_is_released(session_factory):
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    assert _slot(tgt, 8) == 10.0
    await sched.write_property("listOfObjectPropertyReferences", [])
    await _turns()
    assert _slot(tgt, 8) is None


def _two_schedules_on_one_slot(winter_in_season=True):
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    target = BacnetObject(object_type="analog-value", instance_number=5, name="Target-AV")

    def sched(instance, name, value, **fields):
        return BacnetObject(
            object_type="schedule", instance_number=instance, name=name, schedule_value_type="analog",
            schedule_default=value, weekly_schedule=_weekly_schedule(time="00:00", value=value), schedule_priority=8,
            schedule_write_targets=[{"object_type": "analog-value", "instance_number": 5}], **fields,
        )

    season = {} if winter_in_season else {"schedule_effective_start": "2099-01-01", "schedule_effective_end": "2099-12-31"}
    return build_application(device, [target, sched(1, "Summer", "24.0"), sched(2, "Winter", "18.0", **season)])


@pytest.mark.asyncio
async def test_releasing_a_slot_an_in_season_sibling_lists_hands_it_over():
    app = _two_schedules_on_one_slot()
    await _turns()
    summer, winter, tgt = (app.get_object_name(n) for n in ("Summer", "Winter", "Target-AV"))
    summer.release_local_targets(app)
    await _turns()
    assert _slot(tgt, 8) == 18.0  # Winter wrote its own value, no gap left


@pytest.mark.asyncio
async def test_a_null_value_leaves_a_slot_an_in_season_sibling_lists_to_the_sibling():
    from bacpypes3.primitivedata import Null

    app = _two_schedules_on_one_slot()
    await _turns()
    summer, tgt = app.get_object_name("Summer"), app.get_object_name("Target-AV")
    await summer._apply_schedule_writes(Null(()))
    await _turns()
    assert _slot(tgt, 8) == 18.0  # Winter wrote its own value, no gap left


@pytest.mark.asyncio
async def test_a_null_value_relinquishes_a_slot_no_sibling_lists():
    from bacpypes3.primitivedata import Null

    app = _two_schedules_on_one_slot(winter_in_season=False)
    await _turns()
    summer, tgt = app.get_object_name("Summer"), app.get_object_name("Target-AV")
    await summer._apply_schedule_writes(Null(()))
    await _turns()
    assert _slot(tgt, 8) is None


@pytest.mark.asyncio
async def test_two_in_season_schedules_both_null_do_not_hand_the_slot_back_and_forth():
    from bacpypes3.primitivedata import Null

    app = _two_schedules_on_one_slot()
    await _turns()
    summer, winter, tgt = (app.get_object_name(n) for n in ("Summer", "Winter", "Target-AV"))
    winter.presentValue = Null(())
    summer.presentValue = Null(())
    await _turns()
    assert _slot(tgt, 8) is None


@pytest.mark.asyncio
async def test_a_schedule_that_never_commanded_releases_nothing():
    app = _two_schedules_on_one_slot(winter_in_season=False)
    await _turns()
    winter, tgt = app.get_object_name("Winter"), app.get_object_name("Target-AV")
    assert _slot(tgt, 8) == 24.0
    winter.release_local_targets(app)
    await _turns()
    assert _slot(tgt, 8) == 24.0


@pytest.mark.asyncio
async def test_a_released_slot_with_no_claimant_is_relinquished():
    app = _two_schedules_on_one_slot(winter_in_season=False)
    await _turns()
    summer, tgt = app.get_object_name("Summer"), app.get_object_name("Target-AV")
    summer.release_local_targets(app)
    await _turns()
    assert _slot(tgt, 8) is None


@pytest.mark.asyncio
async def test_a_priority_change_writes_the_new_slot_before_releasing_the_old(session_factory):
    # The Null went first, so the target dropped
    # to Relinquish_Default for a moment (seen by COV, trends, alarms).
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    seen = []
    bacpypes3_compat.property_monitors(tgt, "presentValue").append(lambda old, new: seen.append(new))
    await sched.write_property("priorityForWriting", Unsigned(12))
    await _turns()
    assert _slot(tgt, 12) == 10.0 and _slot(tgt, 8) is None
    assert 0.0 not in seen


@pytest.mark.asyncio
async def test_a_slot_written_while_the_priority_moved_on_is_released(session_factory):
    # A write that suspends (a proxied target
    # waiting on its remote) was recorded under the priority current when it
    # finished, so the slot it really wrote was never released.
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched, tgt = app.get_object_name("Sched-1"), app.get_object_name("Target-AV")
    gate = asyncio.Event()
    real_write = tgt.write_property

    async def slow_at_9(attr, value, index=None, priority=None):
        if priority == 9:
            await gate.wait()
        return await real_write(attr, value, index=index, priority=priority)

    object.__setattr__(tgt, "write_property", slow_at_9)  # bacpypes3 objects intercept setattr
    await sched.write_property("priorityForWriting", Unsigned(9))
    await _turns()  # the write at 9 is waiting
    await sched.write_property("priorityForWriting", Unsigned(12))
    await _turns()
    gate.set()
    await _turns()
    assert _slot(tgt, 12) == 10.0
    assert _slot(tgt, 9) is None
    assert _slot(tgt, 8) is None


def _typed_schedule(value_type):
    fields = {
        "analog": dict(schedule_default="10.0", weekly_schedule=_weekly_schedule()),
        "binary": dict(schedule_default="inactive", weekly_schedule=_weekly_schedule(value="active")),
        "multi-state": dict(
            schedule_default="1", weekly_schedule=_weekly_schedule(value="2"), state_text=["A", "B", "C"]
        ),
    }[value_type]
    return build_bacnet_object(
        BacnetObject(
            object_type="schedule", instance_number=1, name="Sched-1", schedule_value_type=value_type,
            schedule_priority=8, bacnet_writable=True, **fields,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value_type, wrong",
    [
        ("multi-state", Real(2.7)),
        ("binary", Real(0.4)),
        ("binary", Unsigned(1)),
        ("analog", Unsigned(5)),
        ("analog", BinaryPV("active")),
    ],
)
async def test_schedule_value_of_the_wrong_datatype_is_refused(value_type, wrong):
    obj = _typed_schedule(value_type)
    default_before = obj.scheduleDefault.get_value()
    week_before = obj.weeklySchedule

    with pytest.raises(PropertyError):
        await obj.write_property("scheduleDefault", wrong)
    assert obj.scheduleDefault.get_value() == default_before

    day = DailySchedule(daySchedule=[TimeValue(time=Time("09:00:00"), value=wrong)])
    with pytest.raises(PropertyError):
        await obj.write_property("weeklySchedule", day, index=1)
    assert obj.weeklySchedule == week_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value_type, right, saved",
    [("multi-state", Unsigned(3), "3"), ("binary", BinaryPV("active"), "active"), ("analog", Real(2.5), "2.5")],
)
async def test_schedule_default_of_the_right_datatype_is_accepted(value_type, right, saved):
    obj = _typed_schedule(value_type)
    await obj.write_property("scheduleDefault", right)
    from bacpack.objects.schedule_config import atomic_value_to_schedule_string

    assert atomic_value_to_schedule_string(value_type, obj.scheduleDefault) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("device_ref", [None, ("device", 599001), ("device", 599002)])
async def test_schedule_reference_with_an_array_index_is_refused(session_factory, device_ref):
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched = app.get_object_name("Sched-1")
    before = list(sched.listOfObjectPropertyReferences)
    ref = DeviceObjectPropertyReference(
        objectIdentifier=("analog-value", 5), propertyIdentifier="presentValue", propertyArrayIndex=1
    )
    if device_ref is not None:
        ref.deviceIdentifier = device_ref

    with pytest.raises(PropertyError) as exc:
        await sched.write_property("listOfObjectPropertyReferences", [ref])

    assert exc.value.errorCode == "value-out-of-range"
    assert list(sched.listOfObjectPropertyReferences) == before


@pytest.mark.parametrize("prop", [None, "presentValue", "present-value", "PRESENT_VALUE", "85"])
def test_local_schedule_target_on_present_value_is_accepted(prop):
    target = {"object_type": "analog-value", "instance_number": 5}
    if prop is not None:
        target["property_identifier"] = prop
    validate_schedule_config("analog", "10.0", _weekly_schedule(), None, None, None, 8, [target])


@pytest.mark.parametrize("prop", ["relinquishDefault", "minimum-on-time", "outOfService", "512"])
def test_local_schedule_target_on_another_property_is_refused(prop):
    # BACpack's own writes pass the object's allowlist, which takes nothing a
    # schedule value fits but Present_Value: anything else failed silently at
    # every transition, or rewrote Relinquish_Default's saved config.
    target = {"object_type": "analog-value", "instance_number": 5, "property_identifier": prop}
    with pytest.raises(ValueError, match="Present_Value"):
        validate_schedule_config("analog", "10.0", _weekly_schedule(), None, None, None, 8, [target])


def test_external_schedule_target_may_name_any_property():
    target = {"object_type": "analog-value", "instance_number": 5, "device_instance": 599002,
              "property_identifier": "relinquish-default"}
    validate_schedule_config("analog", "10.0", _weekly_schedule(), None, None, None, 8, [target])


@pytest.mark.asyncio
async def test_bacnet_write_of_a_local_reference_to_another_property_is_refused(session_factory):
    app = await _writable_schedule_app(session_factory, [{"object_type": "analog-value", "instance_number": 5}])
    sched = app.get_object_name("Sched-1")
    ref = DeviceObjectPropertyReference(objectIdentifier=("analog-value", 5), propertyIdentifier="relinquishDefault")

    with pytest.raises(PropertyError) as exc:
        await sched.write_property("listOfObjectPropertyReferences", [ref])

    assert exc.value.errorCode == "value-out-of-range"


# --- Times finer than a minute are refused -----------------------------------
# A schedule's times are kept to the minute. A write with seconds or
# hundredths is refused rather than acknowledged and then cut.


@pytest.mark.asyncio
@pytest.mark.parametrize("time", ["07:30:30", "07:30:00.50"])
async def test_a_weekly_schedule_time_with_seconds_is_refused(time):
    obj = _typed_schedule("analog")
    week_before = obj.weeklySchedule
    day = DailySchedule(daySchedule=[TimeValue(time=Time(time), value=Real(70.0))])

    with pytest.raises(PropertyError) as exc:
        await obj.write_property("weeklySchedule", day, index=1)

    assert exc.value.errorCode == "value-out-of-range"
    assert obj.weeklySchedule == week_before


@pytest.mark.asyncio
async def test_an_exception_schedule_time_with_seconds_is_refused():
    obj = _typed_schedule("analog")
    event = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(date=Date((126, 12, 25, 255)))),
        listOfTimeValues=[TimeValue(time=Time("09:00:15"), value=Real(1.0))],
        eventPriority=Unsigned(1),
    )

    with pytest.raises(PropertyError) as exc:
        await obj.write_property("exceptionSchedule", [event])

    assert exc.value.errorCode == "value-out-of-range"


@pytest.mark.asyncio
async def test_a_weekly_schedule_time_to_the_minute_is_still_accepted():
    obj = _typed_schedule("analog")
    day = DailySchedule(daySchedule=[TimeValue(time=Time("07:30:00.00"), value=Real(70.0))])
    await obj.write_property("weeklySchedule", day, index=1)
    assert tuple(obj.weeklySchedule[0].daySchedule[0].time) == (7, 30, 0, 0)
