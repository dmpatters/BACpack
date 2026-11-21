"""A device's own objects as the live-state API reports them.
Runs in the device process for
ClientRequestPoller's "live" requests; never goes on the wire."""
from __future__ import annotations

from typing import Any, List, Optional

from bacpypes3.primitivedata import ObjectIdentifier

from . import timeutil
from .bacnet_client import to_jsonable
from .models import BacnetObject

# BACnetStatusFlags bit order.
STATUS_FLAG_NAMES = ("in-alarm", "fault", "overridden", "out-of-service")

_PROPERTIES = (
    ("present_value", "presentValue"),
    ("priority_array", "priorityArray"),
    ("relinquish_default", "relinquishDefault"),
    ("status_flags", "statusFlags"),
    ("event_state", "eventState"),
    ("reliability", "reliability"),
    ("out_of_service", "outOfService"),
)


async def read_live_state(session_factory, app, device_id: int, object_ids: Optional[List[int]] = None) -> dict:
    """Every object of the device, or those in `object_ids`, that the
    application serves. A saved row not yet built (a live apply runs within
    a second of a save) is left out."""
    with session_factory() as session:
        query = session.query(
            BacnetObject.id, BacnetObject.object_type, BacnetObject.instance_number, BacnetObject.schedule_value_type
        ).filter(BacnetObject.device_id == device_id)
        if object_ids is not None:
            query = query.filter(BacnetObject.id.in_(object_ids))
        rows = query.order_by(BacnetObject.id).all()

    objects = []
    for object_id, object_type, instance, schedule_value_type in rows:
        live = app.get_object_id(ObjectIdentifier((object_type, instance)))
        if live is None:
            continue
        entry = {"object_id": object_id, "object_type": object_type, "instance": instance}
        for key, prop in _PROPERTIES:
            entry[key] = await _read(live, prop)
        if schedule_value_type == "binary" and entry["present_value"] in ("0", "1"):
            # A schedule's Present_Value is AnyAtomic, whose get_value()
            # demotes the BinaryPV to an Enumerated reading "0"/"1".
            entry["present_value"] = "active" if entry["present_value"] == "1" else "inactive"
        if entry["status_flags"] is not None:
            entry["status_flags"] = [name for name, bit in zip(STATUS_FLAG_NAMES, entry["status_flags"], strict=False) if bit]
        entry["controlling_priority"] = _controlling(entry["priority_array"])
        objects.append(entry)
    return {"read_at": timeutil.iso_utc(), "objects": objects}


async def _read(live, prop: str) -> Any:
    # read_property, not the attribute: Status_Flags is computed on read.
    try:
        return to_jsonable(await live.read_property(prop))
    except AttributeError:
        # bacpypes3's "not a property": the object type doesn't have it.
        return None


def _controlling(priority_array: Optional[list]) -> Optional[int]:
    if priority_array is None:
        return None
    return next((slot for slot, value in enumerate(priority_array, start=1) if value is not None), None)
