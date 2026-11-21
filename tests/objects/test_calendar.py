"""Tests for bacpack.objects.calendar."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from bacpypes3.basetypes import CalendarEntry, DateRange
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Date

from bacpack.object_builder import build_application, build_bacnet_object
from bacpack.objects.calendar_config import build_calendar_entry, validate_calendar_config
from bacpack.models import BacnetObject, Device
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked
from tests.session_fakes import CommitFailingSessionFactory


def test_validate_calendar_config_accepts_valid_entries():
    validate_calendar_config(
        [
            {"kind": "date", "year": "*", "month": 12, "day": 25},
            {"kind": "date-range", "start": "2026-01-01", "end": "2026-01-31"},
            {"kind": "week-and-day", "month": "*", "week_of_month": "last", "day_of_week": 5},
        ]
    )


def test_validate_calendar_config_accepts_an_empty_date_list():
    validate_calendar_config(None)
    validate_calendar_config([])


@pytest.mark.asyncio
async def test_a_calendar_with_no_dates_builds_and_reads_false():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    cal = BacnetObject(object_type="calendar", instance_number=1, name="Cal-1", date_list=None)
    app = build_application(device, [cal])
    obj = app.get_object_name("Cal-1")
    assert await obj.read_property("presentValue") == 0
    assert list(await obj.read_property("dateList")) == []


@pytest.mark.asyncio
async def test_writing_an_empty_date_list_saves_no_dates(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[{"kind": "date", "year": "2026", "month": "1", "day": "1"}],
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    await obj.write_property("dateList", [])

    with session_factory() as session:
        assert session.get(BacnetObject, object_id).date_list is None


def test_validate_calendar_config_rejects_bad_kind():
    with pytest.raises(ValueError, match="not a valid date_list"):
        validate_calendar_config([{"kind": "not-a-real-kind"}])


def test_validate_calendar_config_rejects_bad_date_component():
    with pytest.raises(ValueError, match="month"):
        validate_calendar_config([{"kind": "date", "year": "*", "month": 13, "day": 1}])


def test_validate_calendar_config_accepts_null_week_and_day_component_as_wildcard():
    """ScheduleCalendarEntry types month/week_of_month/day_of_week as
    Optional -- a client (or the API's own serialization of an unset field)
    can send the key with an explicit JSON `null` rather than omitting it,
    so `entry.get("month", "*")` returns `None` (the key IS present), not
    the "*" default. Previously `int(None)` raised an uncaught TypeError
    here instead of being treated as a wildcard -- same as the "date"
    kind's own explicit None-as-wildcard handling via
    _validate_wildcard_date_component, which the week-and-day kind now
    reuses instead of its own bespoke (and buggy) inline checks."""
    validate_calendar_config(
        [{"kind": "week-and-day", "month": None, "week_of_month": "*", "day_of_week": None}]
    )  # must not raise


@pytest.mark.parametrize(
    "entry, wildcarded",
    [
        (
            {"kind": "date", "year": None, "month": 12, "day": 25},
            {"kind": "date", "year": "*", "month": 12, "day": 25},
        ),
        (
            {"kind": "week-and-day", "month": None, "week_of_month": None, "day_of_week": 1},
            {"kind": "week-and-day", "month": "*", "week_of_month": "*", "day_of_week": 1},
        ),
    ],
)
def test_build_calendar_entry_treats_null_component_as_wildcard(entry, wildcarded):
    """Validation accepts a null component as "*" -- and the API stores one
    for every field a body leaves out (ScheduleCalendarEntry's Optional
    defaults, dumped by create_object) -- so the build must too. It used to
    int(None), so the entry saved fine and the device then failed to start
   ."""
    validate_calendar_config([entry])
    assert build_calendar_entry(entry) == build_calendar_entry(wildcarded)


@pytest.mark.asyncio
async def test_calendar_created_through_api_schema_with_omitted_year_builds():
    from bacpack.config_schemas import ObjectCreate

    body = ObjectCreate(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        date_list=[{"kind": "date", "month": 12, "day": 25}],
    )
    build_bacnet_object(BacnetObject(**body.model_dump()))  # must not raise


@pytest.mark.asyncio
async def test_build_application_builds_calendar_object_present_value_true_for_matching_date():
    today = dt.date.today()
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    cal = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        date_list=[
            {"kind": "date", "year": today.year, "month": today.month, "day": today.day}
        ],
    )
    app = build_application(device, [cal])
    obj = app.get_object_name("Cal-1")
    # Boolean reads back int-like (1/0), same as outOfService elsewhere in
    # this codebase -- not a real Python bool.
    assert await obj.read_property("presentValue") == 1


@pytest.mark.asyncio
async def test_calendar_object_present_value_false_for_non_matching_date():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    cal = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        date_list=[{"kind": "date", "year": 1999, "month": 1, "day": 1}],
    )
    app = build_application(device, [cal])
    obj = app.get_object_name("Cal-1")
    assert await obj.read_property("presentValue") == 0


@pytest.mark.asyncio
async def test_calendar_config_write_persists_to_db_when_bacnet_writable(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[{"kind": "date", "year": "2026", "month": "1", "day": "1"}],
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        obj = build_bacnet_object(row, config_session_factory=session_factory)

    new_entries = [CalendarEntry(date=Date((126, 12, 25, 255)))]
    await obj.write_property("dateList", new_entries)

    with session_factory() as session:
        row = session.get(BacnetObject, object_id)
        assert row.date_list == [{"kind": "date", "year": "2026", "month": "12", "day": "25"}]


@pytest.mark.asyncio
async def test_calendar_config_write_rejected_when_not_bacnet_writable():
    obj = build_bacnet_object(
        BacnetObject(
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[{"kind": "date", "year": "2026", "month": "1", "day": "1"}],
            bacnet_writable=False,
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("dateList", [CalendarEntry(date=Date((126, 12, 25, 255)))])


@pytest.mark.asyncio
async def test_calendar_config_write_malformed_entry_raises_clean_property_error():
    """CalendarConfigPersisted.write_property's own unbuild_calendar_entry
    call ran before/outside the try/except that turns a ValueError into a
    clean PropertyError -- the same bug shape as
    ScheduleConfigPersisted.write_property (see
    test_schedule_config_write_malformed_value_raises_clean_property_error).
    A CalendarEntry with no choice set at all (date/dateRange/weekNDay all
    unset) makes unbuild_calendar_entry raise ValueError, which previously
    propagated uncaught instead of becoming a clean value-out-of-range
    error."""
    obj = build_bacnet_object(
        BacnetObject(
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[{"kind": "date", "year": "2026", "month": "1", "day": "1"}],
            bacnet_writable=True,
        )
    )
    with pytest.raises(PropertyError):
        await obj.write_property("dateList", [CalendarEntry()])  # no choice set at all


@pytest.mark.asyncio
async def test_date_list_write_re_evaluates_a_schedule_that_references_the_calendar():
    # The schedule kept its old value until its own next
    # transition after a client added today to the calendar.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    cal = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        bacnet_writable=True,
        date_list=[{"kind": "date", "year": 1999, "month": 1, "day": 1}],
    )
    sched = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        exception_schedule=[
            {
                "period": {"kind": "calendar-reference", "instance_number": 1},
                "priority": 1,
                "time_values": [{"time": "00:00", "value": "99.0"}],
            }
        ],
        schedule_priority=8,
    )
    app = build_application(device, [cal, sched])
    for _ in range(5):
        await asyncio.sleep(0.02)  # construction-time interpret_schedule
    schedule = app.get_object_name("Sched-1")
    assert schedule.presentValue.get_value() == 10.0

    calendar = app.get_object_name("Cal-1")
    await calendar.write_property("dateList", [CalendarEntry(date=Date((255, 255, 255, 255)))])

    assert schedule.presentValue.get_value() == 99.0


def _saved_calendar(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        row = BacnetObject(
            device=device,
            object_type="calendar",
            instance_number=1,
            name="Cal-1",
            date_list=[{"kind": "date", "year": "2026", "month": "1", "day": "1"}],
            bacnet_writable=True,
        )
        session.add(device)
        session.add(row)
        session.commit()
        object_id = row.id
    with session_factory() as session:
        return object_id, build_bacnet_object(session.get(BacnetObject, object_id), config_session_factory=session_factory)


@pytest.mark.asyncio
async def test_date_list_save_waits_for_a_held_lock_off_the_loop(session_factory):
    object_id, obj = _saved_calendar(session_factory)

    run = await run_while_locked(
        session_factory, lambda: obj.write_property("dateList", [CalendarEntry(date=Date((127, 1, 1, 255)))])
    )

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        (entry,) = session.get(BacnetObject, object_id).date_list
        assert (entry["year"], entry["month"], entry["day"]) == ("2027", "1", "1")


@pytest.mark.asyncio
async def test_date_list_failed_save_is_logged_and_the_write_still_succeeds(session_factory, caplog):
    object_id, obj = _saved_calendar(session_factory)
    obj._calendar_session_factory = CommitFailingSessionFactory(session_factory)

    await obj.write_property("dateList", [CalendarEntry(date=Date((127, 1, 1, 255)))])

    assert obj.dateList[0].date == Date((127, 1, 1, 255))
    assert "could not save" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start", [(255, 3, 1, 255), (126, 255, 1, 255), (126, 3, 255, 255), (255, 255, 255, 255)]
)
async def test_calendar_date_range_with_a_wildcarded_date_is_refused(session_factory, start):
    object_id, obj = _saved_calendar(session_factory)
    entry = CalendarEntry(dateRange=DateRange(startDate=Date(start), endDate=Date((126, 12, 31, 255))))
    with pytest.raises(PropertyError):
        await obj.write_property("dateList", [entry])
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).date_list == [
            {"kind": "date", "year": "2026", "month": "1", "day": "1"}
        ]


@pytest.mark.asyncio
async def test_calendar_date_patterns_still_round_trip(session_factory):
    object_id, obj = _saved_calendar(session_factory)
    entries = [
        CalendarEntry(date=Date((255, 12, 25, 255))),
        CalendarEntry(date=Date((126, 255, 255, 255))),
        CalendarEntry(dateRange=DateRange(startDate=Date((126, 1, 1, 255)), endDate=Date((126, 1, 31, 255)))),
    ]
    await obj.write_property("dateList", entries)
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).date_list == [
            {"kind": "date", "year": "*", "month": "12", "day": "25"},
            {"kind": "date", "year": "2026", "month": "*", "day": "*"},
            {"kind": "date-range", "start": "2026-01-01", "end": "2026-01-31"},
        ]


@pytest.mark.parametrize("bad", ["2155-03-01", "1899-12-31"])
def test_validate_calendar_config_refuses_a_date_range_outside_bacnet_years(bad):
    with pytest.raises(ValueError, match="1900"):
        validate_calendar_config([{"kind": "date-range", "start": "2026-01-01", "end": bad}])


# --- a date pattern's day of week --------------------------------------------


@pytest.mark.asyncio
async def test_a_date_pattern_with_a_day_of_week_round_trips(session_factory):
    object_id, obj = _saved_calendar(session_factory)
    entries = [
        CalendarEntry(date=Date((255, 255, 255, 1))),  # every Monday
        CalendarEntry(date=Date((255, 255, 13, 5))),  # Friday the 13th
    ]
    await obj.write_property("dateList", entries)
    with session_factory() as session:
        saved = session.get(BacnetObject, object_id).date_list
    assert saved == [
        {"kind": "date", "year": "*", "month": "*", "day": "*", "day_of_week": "1"},
        {"kind": "date", "year": "*", "month": "*", "day": "13", "day_of_week": "5"},
    ]
    assert [tuple(build_calendar_entry(entry).date) for entry in saved] == [(255, 255, 255, 1), (255, 255, 13, 5)]


@pytest.mark.asyncio
async def test_a_calendar_with_a_day_of_week_matches_only_that_weekday():
    today = dt.date.today()
    other = today.isoweekday() % 7 + 1  # ISO and BACnet both number Monday 1
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    rows = [
        BacnetObject(
            object_type="calendar", instance_number=i, name=f"Cal-{i}",
            date_list=[{"kind": "date", "day_of_week": str(dow)}],
        )
        for i, dow in ((1, today.isoweekday()), (2, other))
    ]
    app = build_application(device, rows)
    assert await app.get_object_name("Cal-1").read_property("presentValue") == 1
    assert await app.get_object_name("Cal-2").read_property("presentValue") == 0


@pytest.mark.parametrize("bad", [0, 8, "Mon"])
def test_validate_calendar_config_refuses_a_bad_date_day_of_week(bad):
    with pytest.raises(ValueError, match="day_of_week"):
        validate_calendar_config([{"kind": "date", "day_of_week": bad}])
