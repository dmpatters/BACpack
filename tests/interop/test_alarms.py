"""Intrinsic alarming: event state, GetEventInformation, AcknowledgeAlarm."""
from __future__ import annotations

from datetime import datetime

from . import parse
from .helpers import poll, rpm
from .stack import IOP1

ANALOG_INPUT = 0  # object type number in bacge's table
HOT = 2  # "IOP Hot Input": present value 80, high limit 70


def _alarm_state(tools):
    return rpm(tools, IOP1, ("analog-input", HOT, "event-state,acked-transitions,event-time-stamps"))[("analog-input", HOT)]


def test_alarm_is_reported_listed_and_acknowledged(stack):
    t = stack.tools
    got = poll(lambda: (s := _alarm_state(t))["event-state"] == "high-limit" and s, timeout=30, what="high-limit event")
    # to-offnormal is the first transition; unacknowledged until acknowledged.
    assert parse.parse_bool_array(got["acked-transitions"])[0] is False
    offnormal = parse.parse_array(got["event-time-stamps"])[0]
    assert parse.parse_timestamp(offnormal).year >= 2026

    # bacge fails on an empty event list (a bacnet-stack 1.7.0 limit), so the
    # alarming input is also what keeps this list non-empty.
    listed = parse.parse_event_information(t.run("bacge", IOP1).expect_ok().out)
    assert (IOP1, ANALOG_INPUT, HOT, "HL") in listed

    acknowledged_at = datetime.now().strftime("%Y/%m/%d-%H:%M:%S.00")
    # A time stamp that isn't the transition's is refused and changes nothing.
    wrong = t.run(
        "bacackalarm", IOP1, 1, "analog-input", HOT, "high-limit", "2000/01/01-00:00:00.00", "interop", acknowledged_at
    )
    assert parse.parse_error(wrong.text) == ("services", "invalid-time-stamp"), wrong.describe()
    assert parse.parse_bool_array(_alarm_state(t)["acked-transitions"])[0] is False

    # The acknowledged time stamp is the event's own, in bacackalarm's form.
    t.run("bacackalarm", IOP1, 1, "analog-input", HOT, "high-limit", offnormal, "interop", acknowledged_at).expect_ok()
    after = _alarm_state(t)
    assert parse.parse_bool_array(after["acked-transitions"])[0] is True
