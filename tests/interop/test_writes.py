"""Prioritized writes, write errors and WritePropertyMultiple."""
from __future__ import annotations

from . import parse
from .helpers import ENUMERATED, REAL, priority_slots, relinquish, rp, write
from .stack import IOP1


def _clean(tools, object_type, instance, priorities=(5, 8)):
    for priority in priorities:
        relinquish(tools, IOP1, object_type, instance, priority)


def test_prioritized_writes_and_relinquish(stack):
    t = stack.tools
    try:
        write(t, IOP1, "analog-output", 1, "present-value", 8, REAL, 33.0).expect_ok()
        assert rp(t, IOP1, "analog-output", 1, "present-value") == "33.000000"
        write(t, IOP1, "analog-output", 1, "present-value", 5, REAL, 44.0).expect_ok()
        assert rp(t, IOP1, "analog-output", 1, "present-value") == "44.000000"
        slots = priority_slots(t, IOP1, "analog-output", 1)
        assert slots[4] == "44.000000" and slots[7] == "33.000000"
        assert [s for i, s in enumerate(slots) if i not in (4, 7)] == ["Null"] * 14

        relinquish(t, IOP1, "analog-output", 1, 5).expect_ok()
        assert rp(t, IOP1, "analog-output", 1, "priority-array", 5) == "Null"
        assert rp(t, IOP1, "analog-output", 1, "present-value") == "33.000000"

        relinquish(t, IOP1, "analog-output", 1, 8).expect_ok()
        # Relinquish_Default in the committed config.
        assert rp(t, IOP1, "analog-output", 1, "present-value") == "10.000000"
    finally:
        _clean(t, "analog-output", 1)


def test_binary_output_written_active_and_relinquished(stack):
    t = stack.tools
    try:
        write(t, IOP1, "binary-output", 1, "present-value", 8, ENUMERATED, 1).expect_ok()
        assert rp(t, IOP1, "binary-output", 1, "present-value") == "active"
        relinquish(t, IOP1, "binary-output", 1, 8).expect_ok()
        assert rp(t, IOP1, "binary-output", 1, "present-value") == "inactive"
    finally:
        _clean(t, "binary-output", 1, (8,))


def test_write_to_an_input_is_denied(stack):
    result = write(stack.tools, IOP1, "analog-input", 1, "present-value", 8, REAL, 5.0)
    assert result.code != 0
    assert parse.parse_error(result.text) == ("property", "write-access-denied")


def test_multi_state_value_beyond_its_states_is_out_of_range(stack):
    t = stack.tools
    try:
        # Tag 2 is an unsigned integer; the object has 3 states.
        result = write(t, IOP1, "multi-state-value", 1, "present-value", 8, 2, 9)
        assert parse.parse_error(result.text) == ("property", "value-out-of-range")
        write(t, IOP1, "multi-state-value", 1, "present-value", 8, 2, 3).expect_ok()
        assert rp(t, IOP1, "multi-state-value", 1, "present-value") == "3"
    finally:
        _clean(t, "multi-state-value", 1, (8,))


def test_write_property_multiple_lands_both_writes(stack):
    t = stack.tools
    try:
        # bacwpm takes numeric property ids only: a name silently encodes as
        # property 0. 85 is present-value.
        t.run("bacwpm", IOP1, "analog-output", 1, 85, 8, REAL, 12.5, "analog-value", 1, 85, 8, REAL, 22.5).expect_ok()
        assert rp(t, IOP1, "analog-output", 1, "present-value") == "12.500000"
        assert rp(t, IOP1, "analog-value", 1, "present-value") == "22.500000"
    finally:
        _clean(t, "analog-output", 1, (8,))
        _clean(t, "analog-value", 1, (8,))


def test_write_property_multiple_names_the_first_failed_write(stack):
    t = stack.tools
    try:
        result = t.run("bacwpm", IOP1, "analog-output", 1, 85, 8, REAL, 13.5, "analog-input", 1, 85, 8, REAL, 3.0)
        assert result.code != 0
        assert parse.parse_wpm_failed(result.text) == (
            "property", "write-access-denied", "analog-input", 1, "present-value"
        )
        # Whether the write before the failed one is kept is not asserted: the
        # standard does not require the request to be atomic.
    finally:
        _clean(t, "analog-output", 1, (8,))
