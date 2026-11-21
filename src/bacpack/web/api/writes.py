"""Hand-offs to a running device through SQLite (feed and manual writes,
clock changes), and engine
control."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import device_clock, lifecycle
from ...engine import EngineError, EngineManager
from ...choices import canonical
from ...models import (
    INPUT_TYPES,
    OBJECT_TYPES,
    BacnetObject,
    Device,
    ANALOG_TYPES,
    BINARY_TYPES,
    MULTISTATE_TYPES,
)
from ...point_config import finite_float
from ..deps import get_engine, get_session
from ..schemas import ClockSet, EngineStart, FeedWrite
from ._lookups import get_device_or_404

router = APIRouter()


# --- Feed and manual writes ---------------------------------------------


# A schedule's or calendar's Present_Value comes from its own evaluation, a
# notification class has none. The same set connector mappings refuse.
_NOT_FED = ("schedule", "calendar", "notification-class")


def _validate_queued_value(obj: BacnetObject, value, *, manual: bool):
    """Check a feed/manual write before queuing it and return the value to
    queue. The write is applied later in the device process, where a
    failure only reaches the log, so plainly wrong values are refused here.
    Deliberately narrow: per object type, not every cast bacpypes3 makes.
    An input has no priority array, so it takes no manual write. Binary
    text comes back canonical, a whole-number float as an int; `None` (a
    relinquish) is valid wherever a write is.
    """
    object_type = obj.object_type
    if object_type in _NOT_FED:
        raise HTTPException(status_code=400, detail=f"{object_type} objects don't take feed or manual writes")
    if manual and object_type in INPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"{object_type} isn't commandable; set out_of_service and write it over BACnet",
        )
    if value is None:
        return None
    if object_type in ANALOG_TYPES:
        try:
            finite_float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"value {value!r} is not a valid number for {object_type}",
            )
        return value
    if object_type in BINARY_TYPES:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return int(value)
        if isinstance(value, str):
            text = canonical(value, ("active", "inactive"))
            if text in ("active", "inactive"):
                return text
        raise HTTPException(
            status_code=400,
            detail=f"value {value!r} is not active/inactive, 0/1 or true/false for {object_type}",
        )
    if object_type in MULTISTATE_TYPES:
        states = len(obj.state_text or [])
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value).is_integer()
            and 1 <= value <= states
        ):
            return int(value)
        raise HTTPException(
            status_code=400,
            detail=f"value {value!r} is not a state number 1-{states} for {object_type}",
        )
    return value


@router.post("/feed/{device_id}/{object_type}/{instance_number}")
def write_feed(
    device_id: int,
    object_type: str,
    instance_number: int,
    body: FeedWrite,
    session: Session = Depends(get_session),
):
    device = session.get(Device, device_id)
    if device is None or device.runtime.process_status != "running":
        raise HTTPException(status_code=409, detail=f"device {device_id} is not currently live")
    object_type = canonical(object_type, OBJECT_TYPES)

    obj = (
        session.query(BacnetObject)
        .filter_by(device_id=device_id, object_type=object_type, instance_number=instance_number)
        .first()
    )
    if obj is None:
        raise HTTPException(
            status_code=404, detail=f"no live object {object_type}:{instance_number}"
        )

    value = _validate_queued_value(obj, body.value, manual=False)

    obj.queue_feed_write(value)
    session.commit()
    return {"queued": True, "value": value}


@router.post("/manual/{device_id}/{object_type}/{instance_number}")
def write_manual(
    device_id: int,
    object_type: str,
    instance_number: int,
    body: FeedWrite,
    session: Session = Depends(get_session),
):
    """Manual write to a running device's object at the manual priority
    (8); `null` relinquishes it. Queued for the device process to apply."""
    device = session.get(Device, device_id)
    if device is None or device.runtime.process_status != "running":
        raise HTTPException(status_code=409, detail=f"device {device_id} is not currently live")
    object_type = canonical(object_type, OBJECT_TYPES)

    obj = (
        session.query(BacnetObject)
        .filter_by(device_id=device_id, object_type=object_type, instance_number=instance_number)
        .first()
    )
    if obj is None:
        raise HTTPException(
            status_code=404, detail=f"no live object {object_type}:{instance_number}"
        )

    value = _validate_queued_value(obj, body.value, manual=True)

    obj.queue_manual_write(value)
    session.commit()
    return {"queued": True, "value": value}


# --- Device clock --------------------------------

_NO_LIVE_PROCESS = ("stopped", "crashed")


@router.put("/devices/{device_id}/clock")
def set_device_clock(
    device_id: int,
    body: ClockSet,
    session: Session = Depends(get_session),
    engine: EngineManager = Depends(get_engine),
):
    """Set or reset a device's clock. A device with a live process gets the
    change through pending_clock_offset, applied by its DeviceWatch on the
    next tick along with a Trend Log time-change record. A stopped or crashed
    device has no process to hand it to, so the saved clock is written
    directly. A starting device still reads "stopped" but its child has
    already read the saved clock, so a tracked process means a hand-off; a
    start that fails folds it back (EngineManager._fold_pending_clock_for)."""
    device = get_device_or_404(session, device_id)

    if body.device_time is not None:
        when = body.device_time
        if not device_clock.MIN_YEAR <= when.year <= device_clock.MAX_YEAR:
            raise HTTPException(status_code=422, detail="device_time is outside what a BACnet date can hold")
        try:
            new_offset = device_clock.offset_for_local(
                when.year, when.month, when.day, when.hour, when.minute, when.second,
                when.microsecond // 10000,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        new_offset = body.offset_seconds

    if not device_clock.is_representable(new_offset):
        raise HTTPException(status_code=422, detail="that device time is outside what a BACnet date can hold")

    if device.runtime.process_status in _NO_LIVE_PROCESS and device_id not in engine.processes:
        device.clock_offset_seconds = new_offset
        device.runtime.pending_clock_offset = None
    else:
        device.runtime.pending_clock_offset = new_offset
    session.commit()
    return device_clock.clock_fields(device.clock_offset_seconds, device.runtime.pending_clock_offset)


# --- Engine control ----------------------------------------------------


@router.get("/engine/status")
def engine_status(engine: EngineManager = Depends(get_engine)):
    return engine.status()


@router.post("/engine/start")
async def engine_start(body: EngineStart, engine: EngineManager = Depends(get_engine)):
    try:
        await engine.start(body.device_id)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return engine.status()


@router.post("/engine/stop")
async def engine_stop(body: EngineStart, engine: EngineManager = Depends(get_engine)):
    try:
        await engine.stop(body.device_id)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return engine.status()


@router.post("/engine/restart")
async def engine_restart(body: EngineStart, engine: EngineManager = Depends(get_engine)):
    try:
        await engine.restart(body.device_id)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return engine.status()


@router.post("/engine/restart-ui", status_code=202)
def engine_restart_ui(background_tasks: BackgroundTasks):
    """Restart the web process only: it exits once this answer is sent and
    the service manager starts it again; devices keep running and are
    reattached. Only under a service manager, which is what restarts it."""
    if not lifecycle.service_mode():
        raise HTTPException(
            status_code=409,
            detail="not running as a service: nothing would start the web UI again",
        )
    background_tasks.add_task(lambda: lifecycle.exit_process(lifecycle.RESTART_UI_EXIT_CODE))
    return {"restarting": True}


@router.post("/engine/restart-all")
async def engine_restart_all(engine: EngineManager = Depends(get_engine)):
    """Restart every running device; stopped and crashed ones are left alone.
    A device that fails to come back is listed in "failed" with the reason."""
    return await engine.restart_all()
