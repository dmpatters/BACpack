"""Value simulators: per-object signal generators that move a point's
Present_Value through the feed write at FEED_PRIORITY.

Settings are one JSON object per object (BacnetObject.simulation), one
Pydantic model per kind. SimulationRunner (simulation_runner.py) runs the
generators in each device's child process.
"""
from __future__ import annotations

import math
import random
from typing import Annotated, Callable, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .models import ANALOG_TYPES, BINARY_TYPES, MULTISTATE_TYPES, MAX_INSTANCE

KINDS_BY_FAMILY = {
    "analog": ("sine", "triangle", "sawtooth", "square", "random_walk", "noise", "follow"),
    "binary": ("square", "random"),
    "multi-state": ("cycle", "random"),
}
# Kinds with a repeating cycle, where a phase offset means something; bulk
# apply's random phase only touches these.
PERIODIC_KINDS = ("sine", "triangle", "sawtooth", "square", "cycle")


def family(object_type: str) -> Optional[str]:
    if object_type in ANALOG_TYPES:
        return "analog"
    if object_type in BINARY_TYPES:
        return "binary"
    if object_type in MULTISTATE_TYPES:
        return "multi-state"
    return None


def parse_source(source) -> Tuple[str, int]:
    """("analog-value", 3) from a follow source "analog-value:3"."""
    if isinstance(source, str):
        object_type, sep, instance = source.rpartition(":")
        if sep and object_type in ANALOG_TYPES and instance.isdigit() and int(instance) <= MAX_INSTANCE:
            return object_type, int(instance)
    raise ValueError(f"source must name an analog object on this device, like 'analog-value:3', got {source!r}")


# --- Settings models -------------------------------------------------------


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    phase_s: float = Field(0.0, ge=0)


class _Analog(_Settings):
    interval_s: float = Field(5.0, ge=1)
    decimals: int = Field(2, ge=0, le=6)
    min: Optional[float] = None
    max: Optional[float] = None

    @model_validator(mode="after")
    def _check_clamps(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not be above max")
        return self


class _LowHigh(_Analog):
    low: float
    high: float
    period_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_low_high(self):
        if not self.low < self.high:
            raise ValueError("low must be below high")
        return self


class Sine(_Analog):
    kind: Literal["sine"]
    center: float
    amplitude: float = Field(ge=0)
    period_s: float = Field(gt=0)


class Triangle(_LowHigh):
    kind: Literal["triangle"]


class Sawtooth(_LowHigh):
    kind: Literal["sawtooth"]


class AnalogSquare(_LowHigh):
    kind: Literal["square"]
    duty: float = Field(0.5, ge=0, le=1)


class RandomWalk(_Analog):
    kind: Literal["random_walk"]
    start: float
    max_step: float = Field(gt=0)
    low: float
    high: float

    @model_validator(mode="after")
    def _check_bounds(self):
        if not self.low < self.high:
            raise ValueError("low must be below high")
        if not self.low <= self.start <= self.high:
            raise ValueError("start must be between low and high")
        return self


class Noise(_Analog):
    kind: Literal["noise"]
    center: float
    amplitude: float = Field(ge=0)


class Follow(_Analog):
    kind: Literal["follow"]
    source: str
    scale: float = 1.0
    offset: float = 0.0
    noise: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _check_source(self):
        parse_source(self.source)
        return self


class BinarySquare(_Settings):
    kind: Literal["square"]
    period_s: float = Field(gt=0)
    duty: float = Field(0.5, ge=0, le=1)


class _Dwell(_Settings):
    min_dwell_s: float = Field(ge=1)
    max_dwell_s: float = Field(ge=1)

    @model_validator(mode="after")
    def _check_dwell(self):
        if self.min_dwell_s > self.max_dwell_s:
            raise ValueError("min_dwell_s must not be above max_dwell_s")
        return self


class BinaryRandom(_Dwell):
    kind: Literal["random"]


def _check_states(states: Optional[List[int]]) -> None:
    if states is None:
        return
    if any(state < 1 for state in states):
        raise ValueError("states are numbered from 1")
    if len(set(states)) < 2:
        raise ValueError("states needs at least 2 different states")


class MultiCycle(_Settings):
    kind: Literal["cycle"]
    dwell_s: float = Field(ge=1)
    states: Optional[List[int]] = None

    @model_validator(mode="after")
    def _check_cycle_states(self):
        _check_states(self.states)
        return self


class MultiRandom(_Dwell):
    kind: Literal["random"]
    states: Optional[List[int]] = None

    @model_validator(mode="after")
    def _check_random_states(self):
        _check_states(self.states)
        return self


AnalogSimulation = Annotated[
    Union[Sine, Triangle, Sawtooth, AnalogSquare, RandomWalk, Noise, Follow],
    Field(discriminator="kind"),
]
BinarySimulation = Annotated[Union[BinarySquare, BinaryRandom], Field(discriminator="kind")]
MultiStateSimulation = Annotated[Union[MultiCycle, MultiRandom], Field(discriminator="kind")]

_ADAPTERS = {
    "analog": TypeAdapter(AnalogSimulation),
    "binary": TypeAdapter(BinarySimulation),
    "multi-state": TypeAdapter(MultiStateSimulation),
}


def parse_simulation(object_type: str, simulation, state_count: Optional[int] = None):
    """The validated settings model for `object_type`. Raises ValueError,
    message starting with "simulation", for anything the object can't run.
    `state_count` (a multi-state object's number of states) is checked
    against `states` when given."""
    kind_family = family(object_type)
    if kind_family is None:
        raise ValueError(f"simulation: {object_type} objects can't be simulated")
    if not isinstance(simulation, dict):
        raise ValueError("simulation must be an object with a 'kind'")
    kinds = KINDS_BY_FAMILY[kind_family]
    if simulation.get("kind") not in kinds:
        raise ValueError(
            f"simulation kind for {object_type} must be one of {', '.join(kinds)}, got {simulation.get('kind')!r}"
        )
    try:
        settings = _ADAPTERS[kind_family].validate_python(simulation)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"][1:])  # loc[0] is the kind tag
        raise ValueError(f"simulation{'.' + where if where else ''}: {error['msg']}") from None
    states = getattr(settings, "states", None)
    if states and state_count is not None and max(states) > state_count:
        raise ValueError(f"simulation.states: {max(states)} is above this object's {state_count} states")
    return settings


def normalize_simulation(object_type: str, simulation, state_count: Optional[int] = None) -> dict:
    """parse_simulation, as the dict to store: defaults filled in, unset
    optional settings left out."""
    return parse_simulation(object_type, simulation, state_count).model_dump(exclude_none=True)


def states_of(settings, state_count: int) -> List[int]:
    """The states a multi-state simulator moves through, within the object's count."""
    if settings.states:
        return [state for state in settings.states if state <= state_count]
    return list(range(1, state_count + 1))


def random_phase(normalized: dict, object_type: str, state_count: Optional[int]) -> Optional[float]:
    """A random phase_s within one cycle, for bulk apply; None for a kind
    without a cycle."""
    settings = parse_simulation(object_type, normalized)
    if settings.kind not in PERIODIC_KINDS:
        return None
    if settings.kind == "cycle":
        fitting = states_of(settings, state_count or 2)
        if not fitting:
            return None  # no state fits the object: nothing cycles
        length = settings.dwell_s * len(fitting)
    else:
        length = settings.period_s
    return round(random.uniform(0, length), 3) % length


# --- Generators -------------------------------------------------------------
# Each takes the parsed settings, the time (seconds, wall clock), a per-object
# state dict the runner keeps between calls, and a random.Random.


def _position(settings, t: float) -> float:
    """0..1 through the current cycle."""
    return ((t + settings.phase_s) % settings.period_s) / settings.period_s


def _finish(settings: _Analog, value: float) -> float:
    if settings.min is not None:
        value = max(value, settings.min)
    if settings.max is not None:
        value = min(value, settings.max)
    return round(value, settings.decimals)


def analog_value(
    settings, t: float, state: dict, rng: random.Random, read_source: Callable[[Tuple[str, int]], Optional[float]]
) -> Optional[float]:
    """The next analog value, or None when there's nothing to write (a
    follow source that isn't there)."""
    kind = settings.kind
    if kind == "sine":
        value = settings.center + settings.amplitude * math.sin(2 * math.pi * _position(settings, t))
    elif kind == "triangle":
        p = _position(settings, t)
        value = settings.low + (settings.high - settings.low) * (2 * p if p < 0.5 else 2 * (1 - p))
    elif kind == "sawtooth":
        value = settings.low + (settings.high - settings.low) * _position(settings, t)
    elif kind == "square":
        value = settings.high if _position(settings, t) < settings.duty else settings.low
    elif kind == "random_walk":
        value = state.get("walk", settings.start) + rng.uniform(-settings.max_step, settings.max_step)
        if value > settings.high:
            value = 2 * settings.high - value
        if value < settings.low:
            value = 2 * settings.low - value
        value = min(max(value, settings.low), settings.high)
        state["walk"] = value
    elif kind == "noise":
        value = settings.center + rng.uniform(-settings.amplitude, settings.amplitude)
    else:  # follow
        source = read_source(parse_source(settings.source))
        if source is None:
            return None
        value = source * settings.scale + settings.offset
        if settings.noise:
            value += rng.uniform(-settings.noise, settings.noise)
    return _finish(settings, value)


def _redwell(settings, t: float, state: dict, rng: random.Random) -> None:
    state["until"] = t + rng.uniform(settings.min_dwell_s, settings.max_dwell_s)


def binary_value(settings, t: float, state: dict, rng: random.Random) -> int:
    """1 (active) or 0 (inactive)."""
    if settings.kind == "square":
        return 1 if _position(settings, t) < settings.duty else 0
    if "until" not in state:
        state["value"] = rng.choice((0, 1))
        _redwell(settings, t, state, rng)
    elif t >= state["until"]:
        state["value"] = 1 - state["value"]
        _redwell(settings, t, state, rng)
    return state["value"]


def multistate_value(settings, t: float, state: dict, rng: random.Random, state_count: int) -> Optional[int]:
    """A state number, or None when none of `states` fits the object."""
    states = states_of(settings, state_count)
    if not states:
        return None
    if settings.kind == "cycle":
        step = math.floor((t + settings.phase_s) / settings.dwell_s)
        return states[step % len(states)]
    if "until" not in state or state.get("value") not in states:
        state["value"] = rng.choice(states)
        _redwell(settings, t, state, rng)
    elif t >= state["until"]:
        others = [s for s in states if s != state["value"]] or states
        state["value"] = rng.choice(others)
        _redwell(settings, t, state, rng)
    return state["value"]
