"""Import an EDE file as new, stopped devices:
read the file's devices, plan the ticked ones, create them. The files are
sent to each call and never stored; a create plans again and refuses a plan
that changed since the preview. Importing into an existing device is the
object import's job (object_io)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import object_config
from ...config_schemas import DeviceCreate
from ...db import lock_both_files
from ...ede_io import EdeError, EdeFile, read_ede, read_state_texts, resolve_state_texts
from ...ede_plan import plan_device, plan_digest
from ...models import BacnetObject, Device
from ..deps import get_session
from ..schemas import EdeDeviceChoice
from ._device_creation import build_new_device
from ._object_validation import check_trend_identity_or_409
from ._uploads import read_upload

router = APIRouter()

_PLAN_CHANGED = "the file or its plan changed since the preview; preview it again"
_CHOICES = TypeAdapter(List[EdeDeviceChoice])


def _parse(file: UploadFile, state_texts: Optional[UploadFile]) -> EdeFile:
    """The file read, with its state texts bound. Blocking: a worker thread."""
    raw = read_upload(file)
    state_texts_raw = read_upload(state_texts) if state_texts is not None else None
    try:
        table = read_state_texts(state_texts_raw) if state_texts_raw is not None else None
        parsed = read_ede(raw)
    except EdeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolve_state_texts(parsed, table)
    return parsed


def _choices(devices: str, parsed: EdeFile) -> List[EdeDeviceChoice]:
    try:
        choices = _CHOICES.validate_json(devices)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not choices:
        raise HTTPException(status_code=422, detail="choose at least one device")
    in_file = parsed.devices()
    for choice in choices:
        if choice.instance not in in_file:
            raise HTTPException(
                status_code=400,
                detail=f"the file has no device {choice.instance} (it has {', '.join(str(i) for i in in_file)})",
            )
    return choices


def _name_taken(session: Session, name: str) -> bool:
    return session.scalar(select(Device.id).where(Device.name == name).limit(1)) is not None


def _planned(session: Session, parsed: EdeFile, choices: List[EdeDeviceChoice]):
    """(choice, plan, problem) per ticked device. A problem is anything that
    would stop that device being created, so the preview can show it."""
    out, ids, names = [], set(), set()
    for choice in choices:
        plan = plan_device(parsed.rows, device_instance=choice.instance, device_id=choice.id, device_name=choice.name)
        problem = plan.problem
        if session.get(Device, choice.id) is not None:
            problem = f"device {choice.id} already exists"
        elif choice.id in ids:
            problem = f"device {choice.id} is also chosen for another device in this import"
        elif _name_taken(session, choice.name):
            problem = f"a device named {choice.name!r} already exists"
        elif choice.name in names:
            problem = f"the name {choice.name!r} is also chosen for another device in this import"
        elif problem is None:
            try:
                DeviceCreate.model_validate({"id": choice.id, "name": choice.name})
            except ValidationError as exc:
                problem = exc.errors()[0]["msg"]
        ids.add(choice.id)
        names.add(choice.name)
        out.append((choice, plan, problem))
    return out


def _digest(planned) -> str:
    return plan_digest([(choice.id, choice.name, plan) for choice, plan, _ in planned])


@router.post("/ede/read")
def read_ede_file(file: UploadFile, state_texts: Optional[UploadFile] = None, session: Session = Depends(get_session)):
    """The devices an EDE file holds, with how many objects each lists and
    whether a BACpack device already has that instance."""
    parsed = _parse(file, state_texts)
    counts: dict = {}
    names: dict = {}
    for row in parsed.rows:
        counts[row.device_instance] = counts.get(row.device_instance, 0) + (row.object_type != "device")
        if row.object_type == "device":
            names.setdefault(row.device_instance, row.name)
    devices = []
    for instance in parsed.devices():
        existing = session.get(Device, instance)
        devices.append(
            {
                "instance": instance,
                "name": names.get(instance, f"Device {instance}"),
                "object_count": counts[instance],
                "existing_device": None if existing is None else {"id": existing.id, "name": existing.name},
            }
        )
    return {
        "project_name": parsed.project_name,
        "version_of_layout": parsed.version_of_layout,
        "columns_not_used": parsed.columns_not_used,
        "file_notes": parsed.file_notes,
        "devices": devices,
    }


@router.post("/ede/plan")
def plan_ede(
    file: UploadFile,
    devices: str = Form(...),
    state_texts: Optional[UploadFile] = None,
    session: Session = Depends(get_session),
):
    """What Create would save for each ticked device, and what it would leave
    out. Plain def: planning a big file takes a moment."""
    parsed = _parse(file, state_texts)
    planned = _planned(session, parsed, _choices(devices, parsed))
    return {
        "plans": [
            {
                "instance": choice.instance,
                "device": plan.device,
                "object_count": len(plan.objects),
                "counts": plan.counts,
                "skipped": plan.skipped,
                "notes": plan.notes,
                "skipped_types": plan.skipped_types,
                "problem": problem,
            }
            for choice, plan, problem in planned
        ],
        "vendor_name": None,
        "plan_digest": _digest(planned),
    }


@router.post("/ede/create", status_code=201)
def create_ede(
    file: UploadFile,
    devices: str = Form(...),
    plan_digest: Optional[str] = Form(None),
    state_texts: Optional[UploadFile] = None,
    session: Session = Depends(get_session),
):
    """Save every ticked device, stopped, in one transaction. Nothing is
    saved if any check fails."""
    parsed = _parse(file, state_texts)
    planned = _planned(session, parsed, _choices(devices, parsed))
    for choice, _, problem in planned:
        if problem is not None:
            raise HTTPException(status_code=409, detail=f"device {choice.id} ({choice.name}): {problem}")
    if plan_digest is not None and _digest(planned) != plan_digest:
        raise HTTPException(status_code=409, detail=_PLAN_CHANGED)

    # Planning a big file takes a moment: end the reads, then take both
    # files' write locks for the save and check the ids and names again.
    session.rollback()
    lock_both_files(session)
    ids = []
    try:
        for choice, plan, _ in planned:
            if _name_taken(session, choice.name):
                raise HTTPException(status_code=409, detail=f"a device named {choice.name!r} already exists")
            device = build_new_device(
                session,
                DeviceCreate.model_validate(
                    {"id": choice.id, "name": choice.name, "description": plan.device["description"]}
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
                raise HTTPException(status_code=409, detail=f"device {choice.id} ({choice.name}): {problem}")
            check_trend_identity_or_409(session, device.id)
            ids.append(device.id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {"device_ids": ids}
