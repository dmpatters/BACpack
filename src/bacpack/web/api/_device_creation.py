"""The checks a new device passes before it is saved, shared by the device
API and the clone's create (routers never import each other)."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ...config_schemas import DeviceCreate
from ...device_network import check_network
from ...models import Device, DeviceLink
from ...objects.network_port import validate_network_settings


def build_new_device(session: Session, body: DeviceCreate) -> Device:
    """The Device (not added to the session) for `body`: HTTP 409 if its id is
    taken, 400 if its bind address or network settings can't be served."""
    if session.get(Device, body.id) is not None:
        raise HTTPException(status_code=409, detail=f"device {body.id} already exists")
    # Refused now rather than only when the device starts.
    try:
        validate_network_settings(bind_address=body.bind_address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    device = Device(**body.model_dump(exclude={"bbmd_links"}))
    problem = check_network(
        session,
        device,
        role=body.network_role,
        foreign_bbmd=body.foreign_bbmd,
        foreign_ttl=body.foreign_ttl,
        bbmd_peers=body.bbmd_peers,
        links=body.bbmd_links,
    )
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)
    device.bbmd_links = [DeviceLink(linked_device_id=i) for i in sorted(set(body.bbmd_links))]
    return device
