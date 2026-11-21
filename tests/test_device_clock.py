"""device_clock -- the per-process device clock."""
from __future__ import annotations

import datetime as dt
import time

import pytest
from bacpypes3 import basetypes, primitivedata
from bacpypes3.basetypes import DateTime, TimeStamp
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Date, Time

from bacpack import device_clock
from tests.local_time import local_target


def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _close(a: int, b: int) -> bool:
    """Minutes-of-day equal within 1, tolerating a minute (or midnight)
    rolling over between the two reads."""
    return min((a - b) % 1440, (b - a) % 1440) <= 1


def _expected_local(offset_seconds: float) -> time.struct_time:
    return time.localtime(time.time() + offset_seconds)


def test_install_and_uninstall_swap_only_the_two_bacpypes3_time_attributes():
    device_clock.install()
    assert primitivedata.time is not time
    assert basetypes.time is not time
    assert device_clock.is_installed()
    device_clock.install()  # idempotent
    device_clock.uninstall()
    assert primitivedata.time is time
    assert basetypes.time is time
    assert not device_clock.is_installed()
    device_clock.uninstall()  # idempotent
    assert primitivedata.time is time


def test_uninstall_resets_the_offset():
    device_clock.install()
    device_clock.set_offset(3600)
    device_clock.uninstall()
    assert device_clock.offset() == 0.0


def test_shim_passes_every_other_time_attribute_through():
    device_clock.install()
    assert primitivedata.time.localtime is time.localtime
    assert basetypes.time.mktime is time.mktime


def test_bacpypes3_now_calls_follow_the_offset_once_installed():
    device_clock.install()
    device_clock.set_offset(3 * 3600)
    expected = _expected_local(3 * 3600)
    want = _minute_of_day(expected.tm_hour, expected.tm_min)

    t = Time.now()
    assert _close(_minute_of_day(t[0], t[1]), want)
    stamp = TimeStamp.as_time()
    assert _close(_minute_of_day(stamp.time[0], stamp.time[1]), want)
    now_dt = DateTime.now()
    assert _close(_minute_of_day(now_dt.time[0], now_dt.time[1]), want)

    device_clock.set_offset(3 * 86400)  # whole days: the date must move
    expected = _expected_local(3 * 86400)
    d = Date.now()
    assert (d[0] + 1900, d[1], d[2]) == (expected.tm_year, expected.tm_mon, expected.tm_mday)


def test_offset_has_no_effect_until_installed():
    device_clock.set_offset(3 * 3600)
    expected = time.localtime()
    t = Time.now()
    assert _close(_minute_of_day(t[0], t[1]), _minute_of_day(expected.tm_hour, expected.tm_min))


def test_real_time_time_is_never_shifted():
    device_clock.install()
    device_clock.set_offset(3600)
    assert abs((device_clock.now() - 3600) - time.time()) < 1


def test_offset_for_local_round_trips():
    target, ahead = local_target(3600)
    assert abs(device_clock.offset_for_local(*target[:6]) - ahead) < 2


def test_offset_for_utc_round_trips():
    target = time.gmtime(time.time() - 7200)
    assert abs(device_clock.offset_for_utc(*target[:6]) + 7200) < 2


def test_hundredths_are_included():
    target = time.gmtime(time.time())
    whole = device_clock.offset_for_utc(*target[:6])
    half = device_clock.offset_for_utc(*target[:6], hundredths=50)
    assert abs((half - whole) - 0.5) < 0.05


def test_offset_for_local_raises_value_error_when_the_host_cannot_represent_it():
    with pytest.raises(ValueError):
        device_clock.offset_for_local(10**10, 1, 1, 0, 0, 0)


def test_is_representable_bounds():
    assert device_clock.is_representable(0)
    assert device_clock.is_representable(3600)
    assert not device_clock.is_representable(1e12)  # far past year 2154
    assert not device_clock.is_representable(-1e12)


def test_utc_offset_minutes_matches_time_timezone():
    assert device_clock.utc_offset_minutes() == time.timezone // 60


def test_clock_fields_shape():
    fields = device_clock.clock_fields(3600.0, None)
    assert fields["clock_offset_seconds"] == 3600.0
    assert fields["clock_change_pending"] is False
    shown = dt.datetime.fromisoformat(fields["device_time"])
    assert shown.tzinfo is not None  # carries its UTC offset
    real_now = dt.datetime.now(dt.timezone.utc)
    assert abs((shown - real_now).total_seconds() - 3600) < 5
    assert device_clock.clock_fields(0.0, 12.5)["clock_change_pending"] is True


_needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="time.tzset is Unix-only; these run on Linux"
)


@pytest.fixture
def new_york_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@_needs_tzset
def test_utc_offset_is_positive_west_of_utc(new_york_tz):
    assert device_clock.utc_offset_minutes() == 300  # EST is UTC-5: 300 minutes west


@_needs_tzset
def test_is_dst_follows_the_device_clock_not_the_host(new_york_tz):
    device_clock.set_offset(device_clock.offset_for_local(2026, 7, 1, 12, 0, 0))
    assert device_clock.is_dst() is True
    device_clock.set_offset(device_clock.offset_for_local(2026, 1, 15, 12, 0, 0))
    assert device_clock.is_dst() is False


@_needs_tzset
def test_local_sync_lands_on_the_right_wall_time_across_a_dst_boundary(new_york_tz):
    # 2026-11-01 is the US fall-back date; noon that day is unambiguously EST.
    device_clock.set_offset(device_clock.offset_for_local(2026, 11, 1, 12, 0, 0))
    t = time.localtime(device_clock.now())
    assert (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_isdst) == (2026, 11, 1, 12, 0)


# --- Wired into a real application -------------------------------------

import asyncio

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application

from tests.fakes import _device_and_objects


@pytest.mark.asyncio
async def test_device_local_time_follows_the_offset():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    device_clock.install()
    device_clock.set_offset(3 * 3600)

    local_time = await app.device_object.read_property("localTime")
    expected = _expected_local(3 * 3600)
    assert _close(
        _minute_of_day(local_time[0], local_time[1]),
        _minute_of_day(expected.tm_hour, expected.tm_min),
    )


@pytest.mark.asyncio
async def test_device_reports_utc_offset_and_daylight_savings_status():
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    assert await app.device_object.read_property("utcOffset") == time.timezone // 60
    assert await app.device_object.read_property("daylightSavingsStatus") == device_clock.is_dst()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr",
    [
        "utcOffset",
        "daylightSavingsStatus",
        "localDate",
        "localTime",
        "protocolObjectTypesSupported",
        "protocolServicesSupported",
        "objectList",
        "deviceAddressBinding",
        "activeCovSubscriptions",
    ],
)
async def test_device_rejects_writes_to_computed_properties(attr):
    # Computed live, so a write can't take effect -- it must say so with
    # writeAccessDenied rather than Simple-ACK and silently drop the value.
    # Writing back the current value rules out a type error as the cause.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    current = await app.device_object.read_property(attr)

    with pytest.raises(PropertyError) as exc:
        await app.device_object.write_property(attr, current)
    assert exc.value.errorCode == "writeAccessDenied"


@pytest.mark.asyncio
async def test_alarm_event_time_stamp_follows_the_offset():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
    )
    app = build_application(device, [nc, ai])
    device_clock.install()
    offset = 29 * 3600  # a day and 5 hours, so the date moves too
    device_clock.set_offset(offset)

    live = app.get_object_name("AI-1")
    live.presentValue = 150.0  # above highLimit
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # bacpypes3 stamps the time-only choice; clients show the missing date
    # as 1/1/0001, so BACpack stamps the dateTime choice instead.
    stamp = live.eventTimeStamps[0]  # to-offnormal
    assert stamp._choice == "dateTime"
    expected = _expected_local(offset)
    date, time_of_day = stamp.dateTime.date, stamp.dateTime.time
    assert (date[0] + 1900, date[1], date[2]) == (expected.tm_year, expected.tm_mon, expected.tm_mday)
    assert _close(
        _minute_of_day(time_of_day[0], time_of_day[1]),
        _minute_of_day(expected.tm_hour, expected.tm_min),
    )


def test_device_utc_now_follows_the_offset():
    device_clock.set_offset(3600.0)
    try:
        now = device_clock.device_utc_now()
        assert now.tzinfo is dt.timezone.utc
        assert abs(now.timestamp() - (time.time() + 3600.0)) < 2
    finally:
        device_clock.set_offset(0.0)


def test_device_local_iso_formats_a_stored_value_with_its_offset():
    stored = dt.datetime(2026, 9, 26, 10, 30, 0, 123456)
    expected = stored.replace(tzinfo=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    assert device_clock.device_local_iso(stored) == expected
    assert expected[-6] in "+-"
