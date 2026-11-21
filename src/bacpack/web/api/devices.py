"""Device CRUD (/devices, /devices/{device_id})."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth import hash_password
from ...engine import EngineManager
from ...device_network import check_linking_bbmds, check_network, effective_bind
from ...models import Device, DeviceLink
from ... import object_config, trending
from ...objects.network_port import validate_network_settings
from ...db import lock_both_files
from ..deps import get_engine, get_session, get_session_factory
from ...config_schemas import DeviceCreate, DeviceUpdate
from ._device_creation import build_new_device
from ._lookups import get_device_or_404
from ._port_suggestion import suggest_bind

router = APIRouter()

# A change to any of these needs the role and links checked again.
_NETWORK_FIELDS = frozenset({"bind_address", "network_role", "foreign_bbmd", "foreign_ttl", "bbmd_peers"})


def _device_dict(device: Device) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "description": device.description,
        "location": device.location,
        "vendor_identifier": device.vendor_identifier,
        "vendor_name": device.vendor_name,
        "bind_address": device.bind_address,
        "network_role": device.network_role,
        "foreign_bbmd": device.foreign_bbmd,
        "foreign_ttl": device.foreign_ttl,
        "bbmd_peers": device.bbmd_peers,
        "bbmd_links": sorted(link.linked_device_id for link in device.bbmd_links),
        "backup_failure_timeout": device.backup_failure_timeout,
        "bacnet_password_set": device.bacnet_password_hash is not None,
        "object_count": len(device.objects),
    }


# --- Devices ---------------------------------------------------------------


@router.get("/devices")
def list_devices(session: Session = Depends(get_session)):
    devices = session.scalars(select(Device).order_by(Device.id)).all()
    return [_device_dict(d) for d in devices]


@router.post("/devices", status_code=201)
def create_device(body: DeviceCreate, session: Session = Depends(get_session)):
    device = build_new_device(session, body)
    session.add(device)
    session.commit()
    return _device_dict(device)


@router.get("/devices/{device_id}")
def get_device(device_id: int, session: Session = Depends(get_session)):
    return _device_dict(get_device_or_404(session, device_id))


@router.put("/devices/{device_id}")
def update_device(device_id: int, body: DeviceUpdate, session: Session = Depends(get_session)):
    device = get_device_or_404(session, device_id)
    updates = body.model_dump(exclude_unset=True)
    if "bind_address" in updates:
        try:
            validate_network_settings(bind_address=updates["bind_address"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "name" in updates:
        problem = object_config.check_device(
            updates["name"], [object_config.row_from_object(obj) for obj in device.objects]
        )
        if problem is not None:
            raise HTTPException(status_code=409, detail=problem)
    new_links = updates.pop("bbmd_links", None)
    if "bacnet_password" in updates:
        password = updates.pop("bacnet_password")
        device.bacnet_password_hash = None if password is None else hash_password(password)
    for field, value in updates.items():
        setattr(device, field, value)
    if new_links is not None or _NETWORK_FIELDS & updates.keys():
        saved = {link.linked_device_id: link for link in device.bbmd_links}
        links = sorted(set(new_links)) if new_links is not None else sorted(saved)
        problem = check_network(
            session,
            device,
            role=device.network_role,
            foreign_bbmd=device.foreign_bbmd,
            foreign_ttl=device.foreign_ttl,
            bbmd_peers=device.bbmd_peers,
            links=links,
        )
        if problem is None:
            problem = check_linking_bbmds(session, device)
        if problem is not None:
            session.rollback()
            raise HTTPException(status_code=400, detail=problem)
        if new_links is not None:
            # Rows kept as they are: a replaced row with the same pair would
            # insert before its old one is deleted.
            device.bbmd_links = [saved.get(i) or DeviceLink(linked_device_id=i) for i in links]
    session.commit()
    return _device_dict(device)


@router.get("/devices/{device_id}/port-suggestion")
def port_suggestion(device_id: int, session: Session = Depends(get_session)):
    """A free UDP port for this device on its own host, for when its port is
    taken. Plain def: the probe binds block."""
    device = get_device_or_404(session, device_id)
    return suggest_bind(session, effective_bind(session, device), ignore_device_id=device.id)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    session: Session = Depends(get_session),
    engine: EngineManager = Depends(get_engine),
    session_factory=Depends(get_session_factory),
):
    device = get_device_or_404(session, device_id)
    if engine.is_running(device_id):
        await engine.stop(device_id)
    # Off the event loop: a device can hold millions of trend samples.
    await asyncio.to_thread(_delete_device_rows, session, session_factory, device)
    return Response(status_code=204)


def _delete_device_rows(session: Session, session_factory, device: Device) -> None:
    object_ids = [o.id for o in device.objects]
    trending.delete_trend_samples_before_locking(session_factory, object_ids)
    lock_both_files(session)
    trending.release_trend_samples_for_objects(session, object_ids)
    session.delete(device)
    session.commit()
