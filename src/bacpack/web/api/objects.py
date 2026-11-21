"""Single-object CRUD for a device's BACnet objects
(/devices/{device_id}/objects, .../objects/{object_id})."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ... import object_config
from ...models import BacnetObject, ConnectorMapping
from ...objects.schedule_config import normalize_write_targets
from ...db import lock_both_files
from ..deps import get_session, get_session_factory
from ...config_schemas import ObjectCreate, ObjectUpdate
from ._lookups import get_device_or_404, get_object_or_404
from ...trending import (
    apply_sample_cap,
    delete_trend_samples_before_locking,
    release_trend_samples_for_objects,
    seed_last_notify_record,
)
from ._object_validation import (
    check_device_or_409,
    check_object_or_400,
    check_trend_identity_or_409,
    normalized_simulation,
    refuse_delete_of_referenced,
)
from ._serializers import object_dict, objects_response

router = APIRouter()


# --- Objects -----------------------------------------------------------


@router.get("/devices/{device_id}/objects")
def list_objects(device_id: int, session: Session = Depends(get_session)):
    device = get_device_or_404(session, device_id)
    # One query for every object's feeding connector, not one per object.
    objects = session.scalars(
        select(BacnetObject)
        .where(BacnetObject.device_id == device.id)
        .order_by(BacnetObject.id)
        .options(selectinload(BacnetObject.connector_mapping).selectinload(ConnectorMapping.connector))
    ).all()
    return objects_response(objects)


@router.post("/devices/{device_id}/objects", status_code=201)
def create_object(device_id: int, body: ObjectCreate, session: Session = Depends(get_session)):
    device = get_device_or_404(session, device_id)
    data = body.model_dump()
    data["schedule_write_targets"] = normalize_write_targets(data["schedule_write_targets"], device_id)
    check_object_or_400(data)
    data["simulation"] = normalized_simulation(
        session,
        object_type=body.object_type,
        object_id=None,
        simulation=data["simulation"],
        state_text=data["state_text"],
    )
    check_device_or_409(session, device_id, added=(data,))
    obj = BacnetObject(device=device, **data)
    session.add(obj)
    if obj.trended:
        session.flush()
        check_trend_identity_or_409(session, device_id)
    session.commit()
    return object_dict(obj)


@router.get("/devices/{device_id}/objects/{object_id}")
def get_object(device_id: int, object_id: int, session: Session = Depends(get_session)):
    return object_dict(get_object_or_404(session, device_id, object_id))


@router.put("/devices/{device_id}/objects/{object_id}")
def update_object(
    device_id: int,
    object_id: int,
    body: ObjectUpdate,
    session: Session = Depends(get_session),
):
    obj = get_object_or_404(session, device_id, object_id)
    updates = body.model_dump(exclude_unset=True)
    if "schedule_write_targets" in updates:
        updates["schedule_write_targets"] = normalize_write_targets(updates["schedule_write_targets"], device_id)
    row = {**object_config.row_from_object(obj), **updates}
    check_object_or_400(row)
    if "simulation" in updates:
        updates["simulation"] = normalized_simulation(
            session,
            object_type=obj.object_type,
            object_id=obj.id,
            simulation=updates["simulation"],
            state_text=row["state_text"],
        )
        row["simulation"] = updates["simulation"]
    check_device_or_409(session, device_id, changed={obj.id: row})
    lock_both_files(session)
    was_reporting = obj.trend_reporting_enabled
    for field, value in updates.items():
        setattr(obj, field, value)
    seed_last_notify_record(obj, was_reporting)
    if "trend_sample_cap" in updates:
        apply_sample_cap(session, obj)
    session.commit()
    return object_dict(obj)


@router.delete("/devices/{device_id}/objects/{object_id}", status_code=204)
def delete_object(
    device_id: int,
    object_id: int,
    session: Session = Depends(get_session),
    session_factory=Depends(get_session_factory),
):
    obj = get_object_or_404(session, device_id, object_id)
    refuse_delete_of_referenced(session, device_id, [obj])
    delete_trend_samples_before_locking(session_factory, [obj.id])
    lock_both_files(session)
    release_trend_samples_for_objects(session, [obj.id])
    session.delete(obj)
    session.commit()
    return Response(status_code=204)
