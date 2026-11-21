"""Bulk object import/export: per-device CSV, and whole-server JSON config."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config_io import (
    export_config,
    NewObjectIds,
    ParsedConfig,
    object_plan_digest,
    parse_config_document,
    plan_digest as config_plan_digest,
    sync_config,
    sync_objects_for_device,
)
from ... import object_config
from ...config_schemas import ConfigImportError
from ...csv_io import (
    CsvImportError,
    export_objects_csv,
    fill_default_state_text,
    read_objects_csv,
    validate_objects_csv,
)
from ...ede_io import EdeError, looks_like_ede, read_ede, read_state_texts, resolve_state_texts
from ...ede_plan import ExistingDevice, plan_for_existing
from ...engine import EngineManager
from ...models import BacnetObject, Connector, Device, ServerSettings
from ...objects.schedule_config import normalize_write_targets
from ...db import lock_both_files
from ...trending import TrendPruner, delete_trend_samples_before_locking, trim_devices_to_caps
from ..deps import get_engine, get_session, get_session_factory, get_trend_pruner
from ._lookups import get_device_or_404
from ._object_validation import check_device_or_409, check_trend_identity_or_409
from ._uploads import read_upload

router = APIRouter()


@router.get("/devices/{device_id}/objects/export.csv")
def export_objects(device_id: int, session: Session = Depends(get_session)):
    device = get_device_or_404(session, device_id)
    csv_text = export_objects_csv(device.objects)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="device-{device_id}-objects.csv"'
        },
    )


_CSV_PLAN_CHANGED = "the device's objects changed since the preview; preview the file again before importing"


def _label(row: dict) -> str:
    return f"{row['object_type']}:{row['instance_number']}"


def _planned_rows(device: Device, rows: list[dict]) -> list[dict]:
    """The device's object list as a sync would leave it."""
    existing = {(o.object_type, o.instance_number): o for o in device.objects}
    listed = {(row["object_type"], row["instance_number"]): row for row in rows}
    return [
        {**object_config.row_from_object(existing[key]), **row}
        if key in existing
        else object_config.row_from_object(BacnetObject(**row))
        for key, row in listed.items()
    ]


def _plan_sync_or_409(session: Session, device: Device, rows: list[dict]) -> dict:
    """The sync's plan, refused (409) exactly as the apply would refuse it."""
    try:
        plan = sync_objects_for_device(session, device, rows, dry_run=True)
    except ConfigImportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    problem = object_config.check_device(device.name, _planned_rows(device, rows))
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)
    return plan


def _check_csv_digest(plan: dict, expected: str | None) -> None:
    if expected is not None and object_plan_digest(plan) != expected:
        raise HTTPException(status_code=409, detail=_CSV_PLAN_CHANGED)


@router.post("/devices/{device_id}/objects/import")
def import_objects(
    device_id: int,
    file: UploadFile,
    mode: Literal["merge", "sync"] = "merge",
    dry_run: bool = False,
    plan_digest: str | None = None,
    state_texts: UploadFile | None = None,
    ede_device: int | None = None,
    session: Session = Depends(get_session),
    session_factory=Depends(get_session_factory),
):
    """Import a CSV point list, or an EDE file (told by its content).
    `mode=merge` (default) only adds, and 409s
    on any identifier or name already on this device. `mode=sync` makes the
    file this device's complete object list: objects it doesn't mention are
    **deleted**, existing ones updated in place. Only object_type,
    instance_number and name are required columns; a missing column keeps
    an existing object's value in sync mode and is the default otherwise.
    Every row is checked before anything is written. `dry_run=true` only
    returns the plan, with a `plan_digest`; an apply carrying that digest
    is refused (409) if the plan has changed since.

    An EDE file is planned first: a row that
    can't be used is skipped and listed, a merge leaves what is on the
    device alone, and a sync keeps whatever the file doesn't carry.
    `state_texts` is its state-texts file; `ede_device` picks one of the
    file's devices (needed when it has several).
    """
    # A plain def route, so a big import runs in a worker thread.
    device = get_device_or_404(session, device_id)
    raw = read_upload(file)
    if looks_like_ede(raw):
        table = read_upload(state_texts) if state_texts is not None else None
        return _import_ede(
            session, session_factory, device, raw, table,
            mode=mode, dry_run=dry_run, plan_digest=plan_digest, ede_device=ede_device,
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"file is not valid UTF-8: {exc}") from exc

    try:
        sheet = read_objects_csv(content)
    except CsvImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    parsed = sheet.rows
    # Outside the plan, so it isn't in the digest.
    skipped = {"skipped_columns": sheet.skipped_columns}
    # Only a row that has the column: a sheet without schedule_write_targets
    # must leave an existing schedule's targets alone in sync mode.
    for _, values in parsed:
        if "schedule_write_targets" in values:
            values["schedule_write_targets"] = normalize_write_targets(values["schedule_write_targets"], device_id)
    rows = [values for _, values in parsed]
    existing = {(o.object_type, o.instance_number): o for o in device.objects} if mode == "sync" else {}
    fill_default_state_text(parsed, existing)
    try:
        validate_objects_csv(parsed, existing)
    except CsvImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _apply_rows(
        session, session_factory, device, rows,
        mode=mode, dry_run=dry_run, plan_digest=plan_digest, existing=existing, extra=skipped,
    )


def _import_ede(
    session: Session,
    session_factory,
    device: Device,
    raw: bytes,
    table: bytes | None,
    *,
    mode: Literal["merge", "sync"],
    dry_run: bool,
    plan_digest: str | None,
    ede_device: int | None,
) -> dict:
    """An EDE file into `device`: planned for the one device of the file the
    caller picked (the only one, when it has one), then fed to the same
    merge or sync as a CSV."""
    try:
        parsed = read_ede(raw)
        resolve_state_texts(parsed, read_state_texts(table) if table is not None else None)
    except EdeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    instances = parsed.devices()
    counts = Counter(row.device_instance for row in parsed.rows if row.object_type != "device")
    names = {row.device_instance: row.name for row in reversed(parsed.rows) if row.object_type == "device"}
    info = {
        "read_as": "EDE",
        "project_name": parsed.project_name,
        "version_of_layout": parsed.version_of_layout,
        "file_notes": parsed.file_notes,
        "devices": [
            {"instance": i, "name": names.get(i, f"Device {i}"), "object_count": counts[i]} for i in instances
        ],
        "device": None,
    }
    chosen = ede_device if ede_device is not None else (instances[0] if len(instances) == 1 else None)
    if chosen is not None and chosen not in instances:
        raise HTTPException(
            status_code=400,
            detail=f"the file has no device {chosen} (it has {', '.join(str(i) for i in instances)})",
        )
    if chosen is None:
        if not dry_run:
            raise HTTPException(
                status_code=400,
                detail="the file has several devices; say which one to import with ede_device",
            )
        return {
            "dry_run": True, "mode": mode, "created": [], "updated": [], "deleted": [],
            "skipped": [], "notes": [], "skipped_types": {}, "skipped_columns": parsed.columns_not_used,
            "ede": info, "needs_device": True,
        }
    info["device"] = chosen
    saved = {(o.object_type, o.instance_number): o for o in device.objects}
    plan = plan_for_existing(
        parsed.rows,
        device_instance=chosen,
        existing=ExistingDevice(mode, {key: object_config.row_from_object(obj) for key, obj in saved.items()}),
        device_name=device.name,
    )
    extra = {
        "skipped": plan.skipped, "notes": plan.notes, "skipped_types": plan.skipped_types,
        "skipped_columns": parsed.columns_not_used, "ede": info,
    }
    return _apply_rows(
        session, session_factory, device, plan.objects,
        mode=mode, dry_run=dry_run, plan_digest=plan_digest, existing=saved if mode == "sync" else {}, extra=extra,
    )


def _apply_rows(
    session: Session,
    session_factory,
    device: Device,
    rows: list[dict],
    *,
    mode: str,
    dry_run: bool,
    plan_digest: str | None,
    existing: dict,
    extra: dict,
) -> dict:
    """Merge or sync `rows` into `device`. `existing` is the device's objects
    by (type, instance) for a sync, `extra` what a dry run reports beside the
    plan (outside the digest)."""
    device_id = device.id
    if mode == "sync":
        plan = _plan_sync_or_409(session, device, rows)
        if dry_run:
            return {"dry_run": True, "mode": "sync", **plan, **extra, "plan_digest": object_plan_digest(plan)}
        _check_csv_digest(plan, plan_digest)
        # The plan passed every refusal, so the dropped objects' samples can
        # go before the lock.
        listed = {(row["object_type"], row["instance_number"]) for row in rows}
        dropped = [o.id for key, o in existing.items() if key not in listed]
        if dropped:
            delete_trend_samples_before_locking(session_factory, dropped)
        # A precaution: no sync reads a file after writing the other today,
        # but it writes both.
        lock_both_files(session)
        # The device was loaded before the lock: sync from its rows as they
        # are now, and check the digest on that, so an edit landing since the
        # check above is caught.
        session.expire_all()
        try:
            summary = sync_objects_for_device(session, device, rows, dry_run=False)
            _check_csv_digest(summary, plan_digest)
        except ConfigImportError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            session.rollback()
            raise
        session.flush()
        check_device_or_409(session, device_id)
        session.commit()
        trim_devices_to_caps(session, [device_id])
        return summary

    # The ids the inserts will get, so a defaulted Trend Log instance is
    # compared now; the apply checks again after its flush.
    new_ids = NewObjectIds(session)
    check_device_or_409(session, device_id, added=tuple({**row, "id": new_ids.take()} for row in rows))
    plan = {"created": [_label(row) for row in rows], "updated": [], "deleted": []}
    if dry_run:
        return {"dry_run": True, "mode": "merge", **plan, **extra, "plan_digest": object_plan_digest(plan)}
    _check_csv_digest(plan, plan_digest)
    created = [BacnetObject(device=device, **row) for row in rows]
    session.add_all(created)
    if any(obj.trended for obj in created):
        session.flush()
        check_trend_identity_or_409(session, device_id)
    session.commit()
    return {"imported": len(created)}


# --- Whole-server JSON config --------
# Defaults to a dry run: a full sync deletes anything not in the file, so the
# caller sees the plan and must pass dry_run=false to commit.


@router.get("/config/export")
def export_config_route(session: Session = Depends(get_session)):
    devices = session.scalars(select(Device).order_by(Device.id)).all()
    connectors = session.scalars(select(Connector)).all()
    # init_db guarantees the row, so an export never writes.
    settings = session.get(ServerSettings, 1)
    return Response(
        content=json.dumps(export_config(devices, connectors=connectors, settings=settings), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="bacpack-config.json"'},
    )


def _parse_config_upload(content: bytes):
    """json.loads + parse_config_document, as HTTPException 400s. Blocking:
    called from a worker thread."""
    try:
        data = json.loads(content)
    except RecursionError as exc:
        # json raises this, not ValueError, on deeply nested input.
        raise HTTPException(status_code=400, detail="not valid JSON: nested too deeply") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"not valid JSON: {exc}") from exc
    try:
        return parse_config_document(data)
    except ConfigImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Apply can carry the preview's plan_digest and is refused (409) if the plan
# no longer matches: checked before any device is stopped, and again on the
# real result before commit, so a change landing while devices stop is
# caught too. No digest skips the check (scripts).
_PLAN_CHANGED = "the configuration changed since the preview; preview the file again before applying"


def _check_plan_digest(plan: dict, expected: str | None) -> None:
    if expected is not None and config_plan_digest(plan) != expected:
        raise HTTPException(status_code=409, detail=_PLAN_CHANGED)


def _sync_config_or_409(session: Session, parsed: ParsedConfig, *, dry_run: bool) -> dict:
    """sync_config, with its refusals as 409s. Blocking: called from a
    worker thread."""
    try:
        return sync_config(
            session, parsed.devices, dry_run=dry_run, connectors=parsed.connectors, settings=parsed.settings
        )
    except ConfigImportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _objects_the_plan_deletes(session: Session, plan: dict) -> list[int]:
    ids = []
    for device_id in plan["devices_deleted"]:
        ids.extend(o.id for o in session.get(Device, device_id).objects)
    for device_id, summary in plan["objects"].items():
        device = session.get(Device, device_id)
        if device is None or not summary["deleted"]:
            continue
        deleted = set(summary["deleted"])
        ids.extend(o.id for o in device.objects if f"{o.object_type}:{o.instance_number}" in deleted)
    return ids


def _apply_config(session: Session, session_factory, parsed, plan: dict, expected_digest: str | None) -> dict:
    # Before the lock, as a delete: the plan has passed every refusal but
    # the digest recheck, and that fails only on a config changed meanwhile.
    delete_trend_samples_before_locking(session_factory, _objects_the_plan_deletes(session, plan))
    lock_both_files(session)
    result = _sync_config_or_409(session, parsed, dry_run=False)
    try:
        _check_plan_digest(result, expected_digest)
    except HTTPException:
        session.rollback()
        raise
    session.commit()
    trim_devices_to_caps(session, list(result["objects"]))
    return result


@router.post("/config/import")
async def import_config_route(
    file: UploadFile,
    dry_run: bool = True,
    plan_digest: str | None = None,
    session: Session = Depends(get_session),
    session_factory=Depends(get_session_factory),
    engine: EngineManager = Depends(get_engine),
    pruner: TrendPruner = Depends(get_trend_pruner),
):
    """Import a whole-server JSON config. By default only previews the plan
    (with a `plan_digest`); `dry_run=false` applies it, deleting devices and
    objects not in the file, and with the preview's `plan_digest` is refused
    (409) if the plan has changed since. The file's connectors are created or
    updated by name (connectors not in the file are left alone, and stored
    credentials kept); a `settings` key sets the retention settings."""
    # Async to await device stops; the blocking work runs in worker threads,
    # and the session is only used by one thread at a time.
    content = await asyncio.to_thread(read_upload, file)
    parsed = await asyncio.to_thread(_parse_config_upload, content)

    plan = await asyncio.to_thread(_sync_config_or_409, session, parsed, dry_run=True)
    if dry_run:
        return {"dry_run": True, **plan, "plan_digest": config_plan_digest(plan)}

    _check_plan_digest(plan, plan_digest)
    for deleted_id in plan["devices_deleted"]:
        if engine.is_running(deleted_id):
            await engine.stop(deleted_id)
    result = await asyncio.to_thread(_apply_config, session, session_factory, parsed, plan, plan_digest)
    if result.get("settings_changed"):
        pruner.kick()
    return {"dry_run": False, **result}
