"""Bulk edit/delete of a device's objects. Included ahead of objects.py so
.../objects/batch isn't captured by .../objects/{object_id}."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import object_config
from ...objects.schedule_config import normalize_write_targets
from ...db import lock_both_files
from ..deps import get_session, get_session_factory
from ..schemas import ObjectBatchDelete, ObjectBatchPatch
from ._lookups import get_device_or_404, get_objects_or_404
from ...simulation import random_phase
from ...trending import (
    apply_sample_cap,
    delete_trend_samples_before_locking,
    release_trend_samples_for_objects,
    seed_last_notify_record,
)
from ._object_validation import (
    check_device_or_409,
    check_object_or_400,
    normalized_simulation,
    refuse_delete_of_referenced,
)
from ._serializers import objects_response

router = APIRouter()


@router.patch("/devices/{device_id}/objects/batch")
def batch_update_objects(
    device_id: int, body: ObjectBatchPatch, session: Session = Depends(get_session)
):
    """Apply the same partial update to every listed object, all or
    nothing: an object_id not on this device 404s, and an object the update
    would make invalid refuses the whole batch.
    """
    get_device_or_404(session, device_id)
    objs = get_objects_or_404(session, device_id, body.object_ids)
    updates = body.patch.model_dump(exclude_unset=True)
    if "schedule_write_targets" in updates:
        updates["schedule_write_targets"] = normalize_write_targets(updates["schedule_write_targets"], device_id)
    # One name on several objects always breaks the per-device unique name.
    if "name" in updates and len(objs) > 1:
        raise HTTPException(
            status_code=409,
            detail="batch update cannot set the same name on more than one object",
        )
    # Each object is checked as it will be, so one bad object refuses the
    # whole batch before anything is applied.
    rows = {}
    simulations = {}
    for obj in objs:
        row = {**object_config.row_from_object(obj), **updates}
        check_object_or_400(row, prefix=f"{obj.name}: ")
        if "simulation" in updates:
            normalized = normalized_simulation(
                session,
                object_type=obj.object_type,
                object_id=obj.id,
                simulation=updates["simulation"],
                state_text=row["state_text"],
                prefix=f"{obj.name}: ",
            )
            if normalized is not None and body.random_phase:
                state_text = row["state_text"]
                phase = random_phase(normalized, obj.object_type, len(state_text) if state_text else None)
                if phase is not None:
                    normalized["phase_s"] = phase
            simulations[obj.id] = normalized
            row["simulation"] = normalized
        rows[obj.id] = row
    check_device_or_409(session, device_id, changed=rows)
    lock_both_files(session)
    was_reporting = {obj.id: obj.trend_reporting_enabled for obj in objs}
    for obj in objs:
        for field, value in updates.items():
            setattr(obj, field, simulations[obj.id] if field == "simulation" else value)
        seed_last_notify_record(obj, was_reporting[obj.id])
    if "trend_sample_cap" in updates:
        for obj in objs:
            apply_sample_cap(session, obj)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="batch update failed (likely a name conflict)"
        )
    return objects_response(objs)


@router.delete("/devices/{device_id}/objects/batch")
def batch_delete_objects(
    device_id: int,
    body: ObjectBatchDelete,
    session: Session = Depends(get_session),
    session_factory=Depends(get_session_factory),
):
    """Delete every listed object, with its trend samples and connector
    mapping, in one transaction. Refused if another object still refers to
    one of them.
    """
    get_device_or_404(session, device_id)
    objs = get_objects_or_404(session, device_id, body.object_ids)
    refuse_delete_of_referenced(session, device_id, objs)
    delete_trend_samples_before_locking(session_factory, [o.id for o in objs])
    lock_both_files(session)
    # Keeps the history file's sample count in sync before the cascade delete.
    release_trend_samples_for_objects(session, [o.id for o in objs])
    for obj in objs:
        session.delete(obj)
    session.commit()
    return {"deleted": len(objs)}
