"""Value simulator settings and generators. Pure functions: fixed times, seeded random."""
from __future__ import annotations

import math
import random

import pytest

from bacpack.choices import SIMULATION_KINDS
from bacpack.simulation import (
    KINDS_BY_FAMILY,
    analog_value,
    binary_value,
    family,
    multistate_value,
    normalize_simulation,
    parse_simulation,
    parse_source,
    random_phase,
)


def _no_source(identifier):
    raise AssertionError("not a follow")


def _analog(simulation, t, state=None, rng=None, read_source=_no_source):
    settings = parse_simulation("analog-value", simulation)
    return analog_value(settings, t, {} if state is None else state, rng or random.Random(1), read_source)


def test_choice_list_matches_the_kinds():
    assert set(SIMULATION_KINDS) == {k for kinds in KINDS_BY_FAMILY.values() for k in kinds}


def test_family():
    assert family("analog-input") == "analog"
    assert family("binary-output") == "binary"
    assert family("multi-state-value") == "multi-state"
    assert family("schedule") is None


def test_normalize_fills_defaults():
    assert normalize_simulation("analog-value", {"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600}) == {
        "kind": "sine",
        "center": 72.0,
        "amplitude": 3.0,
        "period_s": 600.0,
        "phase_s": 0.0,
        "interval_s": 5.0,
        "decimals": 2,
    }
    assert normalize_simulation("binary-value", {"kind": "square", "period_s": 60}) == {
        "kind": "square",
        "period_s": 60.0,
        "duty": 0.5,
        "phase_s": 0.0,
    }


@pytest.mark.parametrize(
    "object_type,simulation,message",
    [
        ("schedule", {"kind": "sine"}, "can't be simulated"),
        ("analog-value", "sine", "must be an object"),
        ("analog-value", {"kind": "cycle", "dwell_s": 5}, "must be one of"),
        ("binary-value", {"kind": "follow", "source": "analog-value:1"}, "must be one of"),
        ("analog-value", {"kind": "sine", "center": 1, "amplitude": 1, "period_s": 0}, "period_s"),
        ("analog-value", {"kind": "sine", "center": 1, "amplitude": -1, "period_s": 5}, "amplitude"),
        ("analog-value", {"kind": "sine", "center": float("nan"), "amplitude": 1, "period_s": 5}, "center"),
        ("analog-value", {"kind": "noise", "center": float("inf"), "amplitude": 1}, "center"),
        ("analog-value", {"kind": "sine", "center": 1, "amplitude": 1, "period_s": 5, "bogus": 1}, "bogus"),
        ("analog-value", {"kind": "triangle", "low": 5, "high": 5, "period_s": 10}, "low must be below high"),
        ("analog-value", {"kind": "random_walk", "start": 200, "max_step": 1, "low": 0, "high": 100}, "start"),
        ("analog-value", {"kind": "noise", "center": 1, "amplitude": 1, "min": 5, "max": 1}, "min"),
        ("analog-value", {"kind": "noise", "center": 1, "amplitude": 1, "interval_s": 0.5}, "interval_s"),
        ("analog-value", {"kind": "follow", "source": "binary-value:1"}, "source"),
        ("analog-value", {"kind": "follow", "source": "analog-value"}, "source"),
        ("binary-value", {"kind": "random", "min_dwell_s": 10, "max_dwell_s": 5}, "min_dwell_s"),
        ("binary-value", {"kind": "square", "period_s": 10, "duty": 1.5}, "duty"),
        ("multi-state-value", {"kind": "cycle", "dwell_s": 5, "states": [2, 2]}, "at least 2"),
        ("multi-state-value", {"kind": "cycle", "dwell_s": 5, "states": [0, 1]}, "numbered from 1"),
    ],
)
def test_parse_refuses_bad_settings(object_type, simulation, message):
    with pytest.raises(ValueError, match=message) as info:
        parse_simulation(object_type, simulation)
    assert str(info.value).startswith("simulation")


def test_parse_checks_states_against_the_state_count():
    simulation = {"kind": "cycle", "dwell_s": 5, "states": [1, 4]}
    parse_simulation("multi-state-value", simulation)  # count unknown: not checked
    with pytest.raises(ValueError, match="above this object's 3 states"):
        parse_simulation("multi-state-value", simulation, state_count=3)


def test_parse_source():
    assert parse_source("analog-input:7") == ("analog-input", 7)
    for bad in ("analog-input", "analog-input:x", "binary-value:1", "analog-value:4194303", 3, None):
        with pytest.raises(ValueError):
            parse_source(bad)


def test_sine():
    sine = {"kind": "sine", "center": 50, "amplitude": 10, "period_s": 100}
    assert _analog(sine, 1000) == 50.0
    assert _analog(sine, 1025) == 60.0
    assert _analog(sine, 1075) == 40.0
    assert _analog({**sine, "phase_s": 25}, 1000) == 60.0


def test_triangle_sawtooth_square():
    triangle = {"kind": "triangle", "low": 0, "high": 100, "period_s": 100}
    assert [_analog(triangle, t) for t in (1000, 1025, 1050, 1075)] == [0.0, 50.0, 100.0, 50.0]
    sawtooth = {"kind": "sawtooth", "low": 0, "high": 100, "period_s": 100}
    assert [_analog(sawtooth, t) for t in (1000, 1050, 1099)] == [0.0, 50.0, 99.0]
    square = {"kind": "square", "low": 1, "high": 2, "period_s": 100, "duty": 0.25}
    assert [_analog(square, t) for t in (1000, 1024, 1025, 1099)] == [2.0, 2.0, 1.0, 1.0]


def test_clamps_and_decimals():
    sine = {"kind": "sine", "center": 50, "amplitude": 10, "period_s": 100, "min": 45, "max": 55}
    assert _analog(sine, 1025) == 55.0
    assert _analog(sine, 1075) == 45.0
    assert _analog({"kind": "sine", "center": 0, "amplitude": 1, "period_s": 7, "decimals": 1}, 1001) == round(
        math.sin(2 * math.pi * (1001 % 7) / 7), 1
    )


def test_random_walk_stays_in_bounds_and_steps_from_start():
    walk = {"kind": "random_walk", "start": 50, "max_step": 5, "low": 40, "high": 60}
    settings = parse_simulation("analog-value", walk)
    state, rng = {}, random.Random(3)
    first = analog_value(settings, 0, state, rng, _no_source)
    assert 45 <= first <= 55
    values = [analog_value(settings, t, state, rng, _no_source) for t in range(1, 2000)]
    assert all(40 <= v <= 60 for v in values)
    assert len(set(values)) > 100


def test_noise_stays_within_its_band():
    rng = random.Random(4)
    values = [_analog({"kind": "noise", "center": 10, "amplitude": 2}, t, rng=rng) for t in range(500)]
    assert all(8 <= v <= 12 for v in values)


def test_follow_reads_its_source():
    follow = {"kind": "follow", "source": "analog-value:3", "scale": 2, "offset": 1}
    assert _analog(follow, 0, read_source=lambda identifier: 5.0 if identifier == ("analog-value", 3) else None) == 11.0
    assert _analog(follow, 0, read_source=lambda identifier: None) is None


def test_binary_square_and_random():
    square = parse_simulation("binary-value", {"kind": "square", "period_s": 100, "duty": 0.3})
    assert [binary_value(square, t, {}, random.Random(1)) for t in (1000, 1029, 1030, 1099)] == [1, 1, 0, 0]

    flip = parse_simulation("binary-value", {"kind": "random", "min_dwell_s": 10, "max_dwell_s": 20})
    state, rng = {}, random.Random(5)
    changes, last, t = [], binary_value(flip, 0, state, rng), 0
    while t < 2000:
        t += 1
        value = binary_value(flip, t, state, rng)
        if value != last:
            changes.append(t)
            last = value
    gaps = [b - a for a, b in zip(changes, changes[1:])]
    assert gaps and all(10 <= gap <= 21 for gap in gaps)


def test_multistate_cycle_and_random():
    cycle = parse_simulation("multi-state-value", {"kind": "cycle", "dwell_s": 10, "states": [1, 3, 4]})
    assert [multistate_value(cycle, t, {}, random.Random(1), 4) for t in (0, 9, 10, 20, 30)] == [1, 1, 3, 4, 1]
    every = parse_simulation("multi-state-value", {"kind": "cycle", "dwell_s": 10, "phase_s": 10})
    assert multistate_value(every, 0, {}, random.Random(1), 3) == 2

    pick = parse_simulation("multi-state-value", {"kind": "random", "min_dwell_s": 1, "max_dwell_s": 1})
    state, rng = {}, random.Random(6)
    values = [multistate_value(pick, t, state, rng, 3) for t in range(0, 200)]
    assert all(a != b for a, b in zip(values, values[1:]) if a is not None)
    assert set(values) == {1, 2, 3}


def test_random_phase_is_within_the_cycle():
    sine = normalize_simulation("analog-value", {"kind": "sine", "center": 0, "amplitude": 1, "period_s": 60})
    assert all(0 <= random_phase(sine, "analog-value", None) < 60 for _ in range(200))
    cycle = normalize_simulation("multi-state-value", {"kind": "cycle", "dwell_s": 10})
    assert all(0 <= random_phase(cycle, "multi-state-value", 3) < 30 for _ in range(200))
    noise = normalize_simulation("analog-value", {"kind": "noise", "center": 0, "amplitude": 1})
    assert random_phase(noise, "analog-value", None) is None


def test_random_phase_is_none_when_no_state_fits():
    cycle = normalize_simulation("multi-state-value", {"kind": "cycle", "dwell_s": 10, "states": [5, 6]})
    assert random_phase(cycle, "multi-state-value", 3) is None
