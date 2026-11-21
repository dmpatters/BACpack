"""Pydantic request bodies for the JSON API that aren't saved config (those
live in config_schemas.py), and the live-state and trend-range response
models. Other responses are plain dicts built in the web/api/ package.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from ..bacnet_client import normalize_object_type, normalize_property
from ..config_schemas import (
    ObjectUpdate,
    bacnet_address,
    Body,
    refuse_non_finite,
    UNSIGNED32_MAX,
)
from ..models import MAX_INSTANCE, WILDCARD_DEVICE_INSTANCE


class ObjectBatchPatch(Body):
    """Bulk edit: apply one `patch` to every object in `object_ids`."""

    object_ids: List[int]
    patch: ObjectUpdate
    # With a periodic simulator in `patch`, give each object its own random
    # phase_s so bulk-simulated points don't move in lockstep.
    random_phase: bool = False


class ObjectBatchDelete(Body):
    object_ids: List[int]


class FeedWrite(Body):
    value: Any

    _finite = field_validator("value")(refuse_non_finite)


class EngineStart(Body):
    device_id: int


class SampleLoad(Body):
    start: bool = True


class ClockSet(Body):
    """PUT /api/devices/{id}/clock -- exactly one of the two. device_time is
    the device's local wall time, without a UTC offset; offset_seconds 0 is
    "reset to host time"."""

    device_time: Optional[dt.datetime] = None
    offset_seconds: Optional[float] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.device_time is None) == (self.offset_seconds is None):
            raise ValueError("give exactly one of device_time or offset_seconds")
        if self.device_time is not None and self.device_time.tzinfo is not None:
            raise ValueError("device_time is the device's local wall time; leave off the UTC offset")
        return self


# --- BACnet client tool ----------------------


class ClientWhoIs(Body):
    low: Optional[int] = Field(None, ge=0, le=4194303)
    high: Optional[int] = Field(None, ge=0, le=4194303)
    window_seconds: float = Field(3.0, ge=0.5, le=30)

    @model_validator(mode="after")
    def _range(self):
        # One end alone means "from there to the edge".
        if self.low is not None and self.high is None:
            self.high = 4194303
        elif self.high is not None and self.low is None:
            self.low = 0
        if self.low is not None and self.low > self.high:
            raise ValueError("low must not be above high")
        return self


class ClientTarget(Body):
    """A remote device: by instance (bound by Who-Is), or at a static address.
    The wildcard instance needs the address: Who-Is can't bind it."""

    instance: int = Field(ge=0, le=WILDCARD_DEVICE_INSTANCE)
    address: Optional[str] = None
    timeout_seconds: float = Field(5.0, ge=1, le=60)

    _validate_address = field_validator("address")(bacnet_address)

    @model_validator(mode="after")
    def _wildcard_needs_an_address(self):
        if self.instance == WILDCARD_DEVICE_INSTANCE and not (self.address or "").strip():
            raise ValueError(
                f"instance {WILDCARD_DEVICE_INSTANCE} (any device) needs a static address; Who-Is can't bind it"
            )
        return self


class ClonePlanRequest(Body):
    """The new device's id and name, which the plan is built for."""

    id: int = Field(ge=0, le=MAX_INSTANCE)
    name: str

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class ClonePlanQuery(Body):
    """A plan's id and name; either left out is the controller's own."""

    id: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    name: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value.strip() if value is not None else None


class EdeDeviceChoice(ClonePlanRequest):
    """One device of an EDE file the user ticked, with the id and name it is
    saved under."""

    instance: int = Field(ge=0, le=MAX_INSTANCE)


class CloneCreate(ClonePlanRequest):
    """`port` replaces the port of `bind_address` (or of the server's default
    bind address when that is blank)."""

    bind_address: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)


class CloneStart(ClientTarget):
    """The controller to clone."""


class ClientRead(ClientTarget):
    object_type: str
    object_instance: int = Field(ge=0, le=4194303)
    property: str = "present-value"
    array_index: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)

    _normalize_type = field_validator("object_type")(normalize_object_type)
    _normalize_property = field_validator("property")(normalize_property)


class ClientWrite(ClientRead):
    value: Any = None
    priority: Optional[int] = Field(None, ge=1, le=16)

    _finite = field_validator("value")(refuse_non_finite)

    @model_validator(mode="after")
    def _relinquish_needs_priority(self):
        if self.value is None and self.priority is None:
            raise ValueError("a null value (relinquish) needs a priority")
        return self


class LiveObject(BaseModel):
    """One object's live state, as its device serves it."""

    object_id: int
    object_type: str
    instance: int
    present_value: Any = None
    # 16 slots, slot 1 first; null for an object that isn't commandable.
    priority_array: Optional[List[Any]] = None
    # Lowest occupied slot; null when Relinquish_Default is in control.
    controlling_priority: Optional[int] = None
    relinquish_default: Any = None
    status_flags: Optional[List[str]] = None
    event_state: Optional[str] = None
    reliability: Optional[str] = None
    out_of_service: Optional[bool] = None


class DeviceLive(BaseModel):
    read_at: str
    objects: List[LiveObject]


class ObjectLive(LiveObject):
    read_at: str


class TrendPoint(BaseModel):
    timestamp: str
    value: float


class TrendGap(BaseModel):
    start: str
    end: str


class TrendRange(BaseModel):
    """A trend over a time range."""

    # The window used, which a client sending `last` doesn't know.
    start: str
    end: str
    mode: Optional[str] = None
    interval_seconds: Optional[int] = None
    step: bool
    samples: List[TrendPoint]
    carry_in: Optional[TrendPoint] = None
    gaps: List[TrendGap]
    thinned: bool
    count_in_range: int
    earliest_sample: Optional[str] = None
