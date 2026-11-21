"""Object-config checks shared by the object routes, as HTTP errors:
object_config's build checks,
plus database checks (a connector already feeding the object, deleting
something still referenced)."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import object_config
from ...models import BacnetObject, ConnectorMapping, Device
from ...object_references import find_references_to
from ...simulation import normalize_simulation


def check_object_or_400(row: dict, prefix: str = "") -> None:
    """HTTP 400 with check_object's message (prefixed for batch edits)."""
    try:
        object_config.check_object(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{prefix}{exc}") from exc


def check_device_or_409(
    session: Session, device_id: int, *, changed: Optional[dict] = None, added: tuple = ()
) -> None:
    """HTTP 409 if the device's object list -- saved rows, with `changed`
    ({object id: row}) replacing theirs and `added` rows appended --
    wouldn't build. Reads without flushing pending changes, so call it
    before applying them: a duplicate name would otherwise hit the unique
    constraint first. Pending inserts (no id yet) arrive through `added`."""
    changed = changed or {}
    device = session.get(Device, device_id)
    with session.no_autoflush:
        saved = session.scalars(select(BacnetObject).filter_by(device_id=device_id)).all()
        rows = [changed.get(obj.id) or object_config.row_from_object(obj) for obj in saved if obj.id is not None]
    rows.extend(added)
    problem = object_config.check_device(device.name, rows)
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)


def check_trend_identity_or_409(session: Session, device_id: int) -> None:
    """HTTP 409 if two of the device's trended objects are the same Trend Log
    instance. Call it after the flush of a save that created trended objects:
    only then does a defaulted instance (the row id) exist to compare."""
    saved = session.scalars(select(BacnetObject).filter_by(device_id=device_id)).all()
    problem = object_config.trend_log_identity_problem(object_config.row_from_object(obj) for obj in saved)
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)


def normalized_simulation(
    session: Session,
    *,
    object_type: str,
    object_id: Optional[int],
    simulation,
    state_text,
    prefix: str = "",
) -> Optional[dict]:
    """The simulator settings to store (defaults filled in), or None. 409
    when a connector already feeds the object (one feed per object); its
    other rules are check_object's."""
    if simulation is None:
        return None
    if object_id is not None and session.scalar(select(ConnectorMapping).filter_by(object_id=object_id)) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{prefix}this object is fed by a connector mapping; remove the mapping before simulating it",
        )
    try:
        return normalize_simulation(object_type, simulation, len(state_text) if state_text else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{prefix}{exc}") from exc


def refuse_delete_of_referenced(session: Session, device_id: int, objs: list) -> None:
    """409 if any object left on the device refers to one of `objs`.
    Objects in `objs` don't count as referrers, so a referrer and its
    target can go together."""
    doomed_ids = {o.id for o in objs}
    targets = {(o.object_type, o.instance_number) for o in objs}
    remaining = session.scalars(select(BacnetObject).filter_by(device_id=device_id)).all()
    messages = find_references_to((o for o in remaining if o.id not in doomed_ids), targets)
    if messages:
        raise HTTPException(status_code=409, detail="still referenced: " + "; ".join(messages))
