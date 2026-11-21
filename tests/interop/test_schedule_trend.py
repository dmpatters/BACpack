"""Schedule, Calendar and Trend Log reads, as configured."""
from __future__ import annotations

from datetime import date

from . import parse
from .helpers import poll, rpm
from .stack import IOP1

WEEKDAY = "[07:00:00.00 1, 18:00:00.00 0]"


def test_schedule_properties(stack):
    got = rpm(
        stack.tools,
        IOP1,
        (
            "schedule",
            1,
            "weekly-schedule,exception-schedule,schedule-default,list-of-object-property-references,"
            "priority-for-writing,reliability",
        ),
    )[("schedule", 1)]
    weekly = got["weekly-schedule"]
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        assert f"{day}: {WEEKDAY}" in weekly, weekly
    assert "Sat: []" in weekly and "Sun: []" in weekly, weekly
    # The exception refers to Calendar 1 and sets the inactive (0) value.
    assert "(calendar, 1)" in got["exception-schedule"] and "00:00:00.00,0" in got["exception-schedule"]
    assert got["schedule-default"].endswith(" 0")  # inactive
    assert "(binary-value, 1),present-value" in got["list-of-object-property-references"]
    assert got["priority-for-writing"] == "14"
    assert got["reliability"] == "no-fault-detected"


def test_calendar_properties(stack):
    before = date.today()
    got = rpm(stack.tools, IOP1, ("calendar", 1, "date-list,present-value"))[("calendar", 1)]
    after = date.today()
    for text in ("January 1,", "July 4,", "December 25,"):
        assert text in got["date-list"], got["date-list"]
    # A run across local midnight may have read either day.
    holidays = {(1, 1), (7, 4), (12, 25)}
    allowed = {"TRUE" if (day.month, day.day) in holidays else "FALSE" for day in (before, after)}
    assert got["present-value"] in allowed


def _trend(tools):
    return rpm(tools, IOP1, ("trend-log", 1, "record-count,total-record-count,buffer-size,log-interval,enable,logging-type"))[
        ("trend-log", 1)
    ]


def _records(tools, range_type, start, count, *extra):
    result = tools.run("bacrr", IOP1, "trend-log", 1, "log-buffer", range_type, start, *extra, count).expect_ok()
    return parse.parse_trend_records(result.out)


def _as_range_time(stamp: str):
    moment = parse.parse_timestamp(stamp)
    return moment.strftime("%Y/%m/%d"), moment.strftime("%H:%M:%S") + f".{moment.microsecond // 10000:02d}"


def test_trend_log_properties_as_configured(stack):
    got = _trend(stack.tools)
    assert got["buffer-size"] == "1000"
    assert got["log-interval"] == "100"  # hundredths of a second: 1 s
    assert got["enable"] == "TRUE"
    assert got["logging-type"] == "polled"


def test_trend_log_read_range_agrees_with_itself(stack):
    t = stack.tools
    poll(lambda: int(_trend(t)["record-count"]) >= 5, timeout=90, what="five Trend Log records")
    everything = _records(t, 4, 0, 0)
    assert len(everything) >= 5
    stamps = [parse.parse_timestamp(stamp) for stamp, _ in everything]
    assert stamps == sorted(stamps)

    # By position (1 = oldest).
    assert _records(t, 1, 1, 3) == everything[:3]
    assert _records(t, 1, 2, 2) == everything[1:3]

    # By sequence number: the oldest record's number is total - count + 1.
    props = _trend(t)
    first_sequence = int(props["total-record-count"]) - int(props["record-count"]) + 1
    # The log may have grown between the reads; the oldest stays the oldest.
    assert _records(t, 2, first_sequence, 3) == everything[:3]

    # By time: records from the third record's time on; the standard leaves
    # the reference record's own inclusion to the server, so only order and
    # membership are checked.
    day, clock = _as_range_time(everything[2][0])
    timed = _records_by_time(t, day, clock, 2)
    assert 1 <= len(timed) <= 2
    latest = _records(t, 4, 0, 0)
    assert all(record in latest for record in timed)
    assert all(parse.parse_timestamp(stamp) >= stamps[2] for stamp, _ in timed)
    assert [parse.parse_timestamp(s) for s, _ in timed] == sorted(parse.parse_timestamp(s) for s, _ in timed)

    # Count and Record_Count agree: nothing past the end of the log.
    props = _trend(t)
    assert len(_records(t, 4, 0, 0)) <= int(props["record-count"])


def _records_by_time(tools, day, clock, count):
    result = tools.run("bacrr", IOP1, "trend-log", 1, "log-buffer", 3, day, clock, count).expect_ok()
    return parse.parse_trend_records(result.out)
