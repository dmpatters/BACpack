"""BACnet connector config and status types and pure helpers, shared by the
runner (bacnet_connectors.py) and its mixins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import Boolean, Enumerated

from .bacnet_client import Point
from .connectors import coerce_mapped_value


NOT_LIVE = "local object not in the running device: its live apply failed (see the device's Process tab)"
LOCAL_REFUSED = "local object refused the value ({})"


# A Commanded.last_value that never equals a real value, so the next tick
# re-sends it (a relinquish of the same target landed after it -- see
# _relinquish).
RESEND = object()


@dataclass
class ReadMapping:
    id: int
    object_type: str  # the local object fed
    instance: int
    point: Point
    scale: float
    offset: float
    value_map: Optional[dict]
    direction: str = "read"  # or "proxy"


@dataclass
class WriteMapping:
    id: int
    object_type: str  # the local object sent
    instance: int
    point: Point
    priority: int


@dataclass
class ConnectorConfig:
    id: int
    remote_instance: int
    remote_address: Optional[str]
    poll_interval: float
    timeout: float
    rewrite_interval: float
    reads: List[ReadMapping]
    writes: List[WriteMapping]


@dataclass
class Commanded:
    """A remote point this device wrote, kept until it's relinquished --
    including after its mapping or connector is gone."""

    remote_instance: int
    remote_address: Optional[str]
    point: Point
    priority: int
    timeout: float
    last_value: Any  # to_jsonable form, for change detection
    last_sent_at: float  # loop time
    # Transport failures in a row sending last_value; 0 once it was
    # delivered or refused. Undelivered, it's sent again once the remote is
    # bound again, or at retry_at (loop time).
    undelivered: int = 0
    retry_at: float = 0.0


def same_target(commanded: "Commanded", config: "ConnectorConfig", mapping: "WriteMapping") -> bool:
    return (
        commanded.remote_instance == config.remote_instance
        and commanded.remote_address == config.remote_address
        and commanded.point == mapping.point
        and commanded.priority == mapping.priority
    )


def settings_of(config: "ConnectorConfig") -> tuple:
    """What a connector edit that clears the backoff changes."""
    return (config.remote_instance, config.remote_address, config.poll_interval, config.timeout)


def prune_stale_write_status(config: "ConnectorConfig", status: dict) -> bool:
    """Remove any write_status entry whose mapping is gone; True if any was.
    Run every time, not only from _push, which is skipped with no writes."""
    write_status = status["write_status"]
    stale = [k for k in write_status if k not in {str(m.id) for m in config.writes}]
    for key in stale:
        del write_status[key]
    return bool(stale)


def prune_stale_forward_status(config: "ConnectorConfig", status: dict) -> bool:
    """Same as prune_stale_write_status, for last_forward."""
    forwards = status.setdefault("last_forward", {})
    live = {str(m.id) for m in config.reads if m.direction == "proxy"}
    stale = [k for k in forwards if k not in live]
    for key in stale:
        del forwards[key]
    return bool(stale)


def array_point(mapping: "ReadMapping") -> Point:
    """The Priority_Array of a proxy's remote point."""
    return Point(mapping.point.object_type, mapping.point.instance, "priority-array")


def new_status() -> dict:
    return {
        "last_poll_at": None,
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "bound_address": None,
        "no_rpm": False,
        "suppressed_by_dcc": False,
        "read_errors": {},
        "write_status": {},
        "last_forward": {},
    }


def read_value_for_feed(raw: Any, mapping: ReadMapping) -> float:
    """A remote value ready for write_feed_value. Enumerated and boolean
    values become their integer, so binary and multi-state reads need no
    value map; a value map, if set, is looked up by name ("active"). A
    remote Schedule's Present_Value arrives AnyAtomic-wrapped and is
    unwrapped first."""
    if isinstance(raw, AnyAtomic):
        raw = raw.get_value()
    if mapping.value_map is None and isinstance(raw, (Enumerated, Boolean, bool)):
        raw = int(raw)
    return coerce_mapped_value(raw, scale=mapping.scale, offset=mapping.offset, value_map=mapping.value_map)
