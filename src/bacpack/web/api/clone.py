"""Clone a live controller: a long read
job queued for the device's own process, with progress and cancel, then a
plan and a create. The parent never sends BACnet: each route reads or writes
the ClientRequest row the device's process works on."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, defer

from ... import object_config, timeutil
from ...clone_plan import ClonePlan, build_plan
from ...clone_reader import PHASE_OBJECT_LIST
from ...config_schemas import DeviceCreate
from ...db import lock_both_files
from ...device_network import bind_with_port
from ...models import MAX_INSTANCE, BacnetObject, ClientRequest, Device
from ..deps import get_session
from ..schemas import CloneCreate, ClonePlanQuery, CloneStart
from ._client_requests import queue_client_request
from ._device_creation import build_new_device
from ._lookups import get_device_or_404
from ._port_suggestion import server_default_bind, suggest_bind

router = APIRouter()

CLONE_KIND = "clone-read"
_STOPPED_ERROR = "the device stopped before the read finished"


@router.post("/devices/{device_id}/client/clone", status_code=202)
async def start_clone(device_id: int, body: CloneStart, request: Request):
    """Queue the read and return at once; poll GET for its progress."""
    job_id = await queue_client_request(
        request.app.state.session_factory,
        device_id,
        CLONE_KIND,
        body.model_dump(),
        not_running=f"device {device_id} isn't running; start it to use its BACnet client",
        one_unfinished="a clone read is already in progress on this device; cancel or discard it first",
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/devices/{device_id}/client/clone")
async def get_clone(device_id: int, request: Request):
    """The device's newest clone job, without its (large) result."""
    return await asyncio.to_thread(_newest_job, request.app.state.session_factory, device_id)


@router.post("/devices/{device_id}/client/clone/{job_id}/cancel")
async def cancel_clone(device_id: int, job_id: int, request: Request):
    return await asyncio.to_thread(_cancel, request.app.state.session_factory, device_id, job_id)


@router.delete("/devices/{device_id}/client/clone/{job_id}", status_code=204)
async def discard_clone(device_id: int, job_id: int, request: Request):
    await asyncio.to_thread(_discard, request.app.state.session_factory, device_id, job_id)
    return Response(status_code=204)


def _job_or_404(session, device_id: int, job_id: int) -> ClientRequest:
    row = session.get(ClientRequest, job_id)
    if row is None or row.device_id != device_id or row.kind != CLONE_KIND:
        raise HTTPException(status_code=404, detail=f"no clone job {job_id} on device {device_id}")
    return row


def _newest_job(session_factory, device_id: int) -> dict:
    with session_factory() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=f"no device with id {device_id}")
        row = session.scalars(
            select(ClientRequest)
            .where(ClientRequest.device_id == device_id, ClientRequest.kind == CLONE_KIND)
            .order_by(ClientRequest.id.desc())
            .limit(1)
            .options(defer(ClientRequest.result))
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="no clone job on this device")
        if row.status in ("pending", "running") and device.runtime.process_status != "running":
            # The process that was reading is gone (stopped, crashed); nothing
            # will finish this job.
            session.execute(
                update(ClientRequest)
                .where(ClientRequest.id == row.id, ClientRequest.status.in_(("pending", "running")))
                .values(status="error", error=_STOPPED_ERROR, finished_at=timeutil.db_utc_now())
            )
            session.commit()
            session.refresh(row, ["status", "error", "progress"])
        params = row.params or {}
        return {
            "job_id": row.id,
            "status": row.status,
            "progress": {"phase": PHASE_OBJECT_LIST, "done": 0, "total": 0, **(row.progress or {})},
            "error": row.error,
            "created_at": timeutil.iso_utc(row.created_at),
            "instance": params.get("instance"),
            "address": params.get("address"),
        }


def _cancel(session_factory, device_id: int, job_id: int) -> dict:
    with session_factory() as session:
        row = _job_or_404(session, device_id, job_id)
        if row.status in ("pending", "running"):
            row.cancel_requested = True
            session.commit()
        return {"job_id": row.id, "status": row.status, "cancel_requested": row.cancel_requested}


def _discard(session_factory, device_id: int, job_id: int) -> None:
    with session_factory() as session:
        _job_or_404(session, device_id, job_id)
        session.execute(delete(ClientRequest).where(ClientRequest.id == job_id))
        session.commit()


# --- Plan and create ---------------------------------------------------------


def _finished_job(session: Session, device_id: int, job_id: int) -> ClientRequest:
    """The clone job's row once its read is done: 404 if it is not this
    device's, 409 (with the error) if it has not finished or failed."""
    row = _job_or_404(session, device_id, job_id)
    if row.status == "error":
        raise HTTPException(status_code=409, detail=f"the read failed: {row.error}")
    if row.status != "done" or row.result is None:
        raise HTTPException(status_code=409, detail="the controller is still being read")
    return row


def _still_finished(session: Session, device_id: int, job_id: int) -> None:
    """The recheck under the write locks: the job is still this device's and
    done, without reloading its (large) result."""
    found = session.execute(
        select(ClientRequest.status).where(
            ClientRequest.id == job_id, ClientRequest.device_id == device_id, ClientRequest.kind == CLONE_KIND
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail=f"no clone job {job_id} on device {device_id}")
    if found[0] != "done":
        raise HTTPException(status_code=409, detail="the controller is still being read")


def _plan_identity(row: ClientRequest, body: ClonePlanQuery) -> tuple[int, str]:
    device = row.result.get("device", {}) if isinstance(row.result, dict) else {}
    instance = device.get("instance")
    if body.id is not None:
        new_id = body.id
    elif isinstance(instance, int) and 0 <= instance <= MAX_INSTANCE:
        new_id = instance
    else:
        raise HTTPException(status_code=422, detail="the read has no device instance; give an id")
    name = device.get("name")
    if body.name is not None:
        new_name = body.name
    elif isinstance(name, str) and name.strip():
        new_name = name.strip()
    else:
        new_name = f"Device {new_id}"
    return new_id, new_name


def _plan_of(row: ClientRequest, device_id: int, name: str) -> ClonePlan:
    if not isinstance(row.result, dict):
        raise HTTPException(status_code=409, detail="the read left no result")
    return build_plan(row.result, device_id=device_id, device_name=name)


@router.post("/devices/{device_id}/client/clone/{job_id}/plan")
def plan_clone(
    device_id: int, job_id: int, body: ClonePlanQuery, session: Session = Depends(get_session)
):
    """What Create would save under this id and name (the controller's own
    when left out), and what it would leave out. Plain def: planning a big
    controller takes a moment."""
    get_device_or_404(session, device_id)
    row = _finished_job(session, device_id, job_id)
    new_id, new_name = _plan_identity(row, body)
    plan = _plan_of(row, new_id, new_name)
    problem = plan.problem
    if session.get(Device, new_id) is not None:
        problem = f"device {new_id} already exists"
    return {
        "plan": {
            "device": plan.device,
            "vendor_name": plan.device["vendor_name"],
            "object_count": len(plan.objects),
            "counts": plan.counts,
            "skipped": plan.skipped,
            "notes": plan.notes,
            "skipped_types": plan.skipped_types,
            "problem": problem,
        },
        # Where the clone would go if the server's default bind address is
        # already served by another of this server's devices.
        "suggested_bind": suggest_bind(session, server_default_bind(session), only_if_taken=True),
    }


@router.post("/devices/{device_id}/client/clone/{job_id}/create", status_code=201)
def create_clone(
    device_id: int, job_id: int, body: CloneCreate, session: Session = Depends(get_session)
):
    """Save the plan as a new, stopped device with its objects, in one
    transaction, and discard the read. Nothing is saved if any check fails."""
    get_device_or_404(session, device_id)
    row = _finished_job(session, device_id, job_id)
    plan = _plan_of(row, body.id, body.name)
    if plan.problem is not None:
        raise HTTPException(status_code=409, detail=plan.problem)
    # Planning a big controller takes seconds: end the read, then take both
    # files' write locks (the device in one, the read's row in the other) only
    # for the save, and check the job is still there.
    session.rollback()
    lock_both_files(session)
    _still_finished(session, device_id, job_id)

    bind = body.bind_address or None
    if body.port is not None:
        base = bind or server_default_bind(session)
        bind = None if base is None else bind_with_port(base, body.port)
        if bind is None:
            raise HTTPException(status_code=400, detail="the port needs a valid bind address to go with")
    vendor = plan.device["vendor_identifier"]
    device = build_new_device(
        session,
        DeviceCreate(
            id=body.id,
            name=body.name,
            description=plan.device["description"],
            location=plan.device["location"],
            vendor_name=plan.device["vendor_name"],
            bind_address=bind,
            **({} if vendor is None else {"vendor_identifier": vendor}),
        ),
    )
    session.add(device)
    for object_row in plan.objects:
        session.add(BacnetObject(device=device, **object_row))
    # With ids, so a Trend Log instance that is some object's row id is seen.
    session.flush()
    problem = object_config.check_device(
        device.name, [object_config.row_from_object(obj) for obj in device.objects]
    )
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)
    session.execute(delete(ClientRequest).where(ClientRequest.id == job_id))
    session.commit()
    return {"device_id": device.id}
