"""404-raising lookups shared by more than one router module."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...models import BacnetObject, ConnectorMapping, Device


def get_device_or_404(session: Session, device_id: int) -> Device:
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"no device with id {device_id}")
    return device


def get_object_or_404(session: Session, device_id: int, object_id: int) -> BacnetObject:
    obj = session.get(BacnetObject, object_id)
    if obj is None or obj.device_id != device_id:
        raise HTTPException(status_code=404, detail=f"no object with id {object_id}")
    return obj


# SQLite refuses a statement with more than 32766 bound variables, and a
# batch request's ids are client-supplied.
_ID_CHUNK = 500


def get_objects_or_404(session: Session, device_id: int, object_ids: list) -> list:
    """`object_ids`' objects, in that order, loading each one's feeding
    connector too (`object_dict` reads it), one query per 500 distinct ids.
    404s on the first id that is missing or belongs to another device, as
    `get_object_or_404` would."""
    distinct_ids = list(dict.fromkeys(object_ids))
    found = {}
    for start in range(0, len(distinct_ids), _ID_CHUNK):
        chunk = distinct_ids[start : start + _ID_CHUNK]
        for obj in session.scalars(
            select(BacnetObject)
            .where(BacnetObject.id.in_(chunk))
            .options(selectinload(BacnetObject.connector_mapping).selectinload(ConnectorMapping.connector))
        ):
            found[obj.id] = obj
    objs = []
    for object_id in object_ids:
        obj = found.get(object_id)
        if obj is None or obj.device_id != device_id:
            raise HTTPException(status_code=404, detail=f"no object with id {object_id}")
        objs.append(obj)
    return objs
