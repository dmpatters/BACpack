"""timeutil."""
from __future__ import annotations

import datetime as dt
import time

import pytest

from bacpack import timeutil

UTC = dt.timezone.utc
PLUS_NINE = dt.timezone(dt.timedelta(hours=9))


def test_utc_now_is_aware_utc():
    now = timeutil.utc_now()
    assert now.tzinfo is UTC
    assert abs(now.timestamp() - time.time()) < 2


def test_db_utc_now_is_naive_utc():
    now = timeutil.db_utc_now()
    assert now.tzinfo is None
    assert abs(now.replace(tzinfo=UTC).timestamp() - time.time()) < 2


def test_to_db_strips_utc():
    assert timeutil.to_db(dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)) == dt.datetime(2026, 10, 5, 12, 0)


def test_to_db_converts_other_zones():
    assert timeutil.to_db(dt.datetime(2026, 10, 5, 21, 0, tzinfo=PLUS_NINE)) == dt.datetime(2026, 10, 5, 12, 0)


def test_to_db_refuses_a_naive_value():
    with pytest.raises(ValueError, match="aware"):
        timeutil.to_db(dt.datetime(2026, 10, 5, 12, 0))


def test_from_db_marks_naive_as_utc():
    assert timeutil.from_db(dt.datetime(2026, 10, 5, 12, 0)) == dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert timeutil.from_db(dt.datetime(2026, 10, 5, 12, 0)).tzinfo is UTC


def test_from_db_converts_aware_to_utc():
    value = timeutil.from_db(dt.datetime(2026, 10, 5, 21, 0, tzinfo=PLUS_NINE))
    assert value.tzinfo is UTC
    assert value.hour == 12


def test_db_cutoff_is_naive_utc_in_the_past():
    cutoff = timeutil.db_cutoff(dt.timedelta(days=1))
    assert cutoff.tzinfo is None
    assert abs(cutoff.replace(tzinfo=UTC).timestamp() - (time.time() - 86400)) < 2


def test_utc_from_timestamp():
    assert timeutil.utc_from_timestamp(0) == dt.datetime(1970, 1, 1, tzinfo=UTC)


def test_iso_utc_of_a_naive_db_value_carries_the_offset():
    assert timeutil.iso_utc(dt.datetime(2026, 10, 5, 12, 0, 1, 500)) == "2026-10-05T12:00:01.000500+00:00"


def test_iso_utc_converts_an_aware_value():
    assert timeutil.iso_utc(dt.datetime(2026, 10, 5, 21, 0, tzinfo=PLUS_NINE), "seconds") == "2026-10-05T12:00:00+00:00"


def test_iso_utc_with_no_value_is_now():
    stamp = timeutil.iso_utc(timespec="seconds")
    assert stamp.endswith("+00:00")
    assert abs(dt.datetime.fromisoformat(stamp).timestamp() - time.time()) < 2


def test_host_local_matches_astimezone():
    value = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert timeutil.host_local(value) == value.astimezone().replace(tzinfo=None)
    assert timeutil.host_local(value).tzinfo is None


def test_host_utc_offsets_near_bound_the_offsets_around_a_wall_time():
    wall = dt.datetime(2026, 3, 8, 12, 0)
    lowest, highest = timeutil.host_utc_offsets_near(wall)
    for hours in range(-30, 31):
        offset = (wall.replace(tzinfo=UTC) + dt.timedelta(hours=hours)).astimezone().utcoffset()
        assert lowest <= offset <= highest


def test_host_utc_offsets_near_falls_back_to_every_offset_where_it_cant_convert():
    assert timeutil.host_utc_offsets_near(dt.datetime(9999, 12, 31)) == (
        -timeutil.MAX_UTC_OFFSET,
        timeutil.MAX_UTC_OFFSET,
    )


def test_host_local_refuses_a_naive_value():
    with pytest.raises(ValueError, match="aware"):
        timeutil.host_local(dt.datetime(2026, 10, 5, 12, 0))


from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time


def test_year_conversions():
    assert timeutil.bacnet_year(2026) == 126
    assert timeutil.python_year(126) == 2026
    assert timeutil.bacnet_year(timeutil.BACNET_MIN_YEAR) == 0
    assert timeutil.bacnet_year(timeutil.BACNET_MAX_YEAR) == 254


def test_bacnet_date_wildcards_the_day_of_week():
    assert tuple(timeutil.bacnet_date(dt.date(2026, 10, 5))) == (126, 10, 5, 255)


def test_bacnet_date_fields_wildcards_none():
    assert tuple(timeutil.bacnet_date_fields(None, 12, 25)) == (255, 12, 25, 255)
    assert tuple(timeutil.bacnet_date_fields(2026, None, None)) == (126, 255, 255, 255)
    assert tuple(timeutil.bacnet_date_fields(2026, 1, 2)) == (126, 1, 2, 255)


def test_python_date_of_a_concrete_date():
    assert timeutil.python_date(Date((126, 10, 5, 1))) == dt.date(2026, 10, 5)


def test_python_date_of_a_fully_wildcarded_date_is_none():
    assert timeutil.python_date(Date((255, 255, 255, 255))) is None
    assert timeutil.python_date(Date((255, 255, 255, 3))) is None  # day of week ignored


@pytest.mark.parametrize("fields", [(255, 3, 1, 255), (126, 255, 1, 255), (126, 3, 255, 255), (255, 255, 1, 255)])
def test_python_date_refuses_a_partial_wildcard(fields):
    with pytest.raises(ValueError, match="wildcard"):
        timeutil.python_date(Date(fields))


def test_wildcard_values_are_fresh_each_call():
    first, second = timeutil.wildcard_date(), timeutil.wildcard_date()
    assert tuple(first) == (255, 255, 255, 255)
    assert first is not second
    stamp = timeutil.wildcard_datetime()
    assert isinstance(stamp, DateTime)
    assert tuple(stamp.date) == (255, 255, 255, 255)
    assert tuple(stamp.time) == (255, 255, 255, 255)
    assert stamp is not timeutil.wildcard_datetime()


def test_python_datetime():
    value = DateTime(date=Date((126, 10, 5, 1)), time=Time((14, 3, 12, 0)))
    assert timeutil.python_datetime(value) == dt.datetime(2026, 10, 5, 14, 3, 12)


def test_python_datetime_ignores_an_unspecified_day_of_week():
    value = DateTime(date=Date((126, 10, 5, 255)), time=Time((14, 3, 12, 255)))
    assert timeutil.python_datetime(value) == dt.datetime(2026, 10, 5, 14, 3, 12)


@pytest.mark.parametrize(
    "date, time_",
    [((255, 10, 5, 1), (14, 3, 12, 0)), ((126, 10, 5, 1), (255, 3, 12, 0)), ((126, 10, 5, 1), (14, 3, 255, 0))],
)
def test_python_datetime_refuses_a_wildcarded_field(date, time_):
    with pytest.raises(ValueError, match="wildcard"):
        timeutil.python_datetime(DateTime(date=Date(date), time=Time(time_)))
