"""Small wrappers over the tool runner for the interop tests."""
from __future__ import annotations

import time
from typing import Callable

from . import parse
from .tools import Result, Tools

REAL = 4
ENUMERATED = 9
NULL = 0


def rp(tools: Tools, device: int, object_type: str, instance: int, prop: str, index: int | None = None) -> str:
    args = [device, object_type, instance, prop] + ([] if index is None else [index])
    return parse.parse_rp(tools.run("bacrp", *args).expect_ok().out)


def rpm(tools: Tools, device: int, *spec) -> dict[tuple[str, int], dict[str, str]]:
    """spec: (object type, instance, "prop1,prop2") triples."""
    flat = [part for triple in spec for part in triple]
    return parse.parse_rpm(tools.run("bacrpm", device, *flat).expect_ok().out)


def write(
    tools: Tools, device: int, object_type: str, instance: int, prop: str, priority: int, tag: int, value, **options
) -> Result:
    # bacwp: index -1 means none.
    return tools.run("bacwp", device, object_type, instance, prop, priority, -1, tag, value, **options)


def relinquish(tools: Tools, device: int, object_type: str, instance: int, priority: int, **options) -> Result:
    return write(tools, device, object_type, instance, "present-value", priority, NULL, 0, **options)


def priority_slots(tools: Tools, device: int, object_type: str, instance: int) -> list[str]:
    return parse.parse_array(rp(tools, device, object_type, instance, "priority-array"))


def poll(check: Callable[[], object], *, timeout: float = 30, interval: float = 1.0, what: str = "condition"):
    """Waits for check() to return something truthy and returns it; a bounded
    poll, never a fixed sleep."""
    deadline = time.monotonic() + timeout
    value = check()
    while not value:
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not hold within {timeout:.0f} s")
        time.sleep(interval)
        value = check()
    return value
