"""The sample building: a plan to show, and
an add-only load. The save runs in a worker thread (it waits on the database
locks); the starts come after the commit, one device's failure leaving the
others to start."""
from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...engine import EngineError, EngineManager
from ...sample_building import SampleRefused, plan_sample, save_sample
from ..deps import get_engine, get_session
from ..schemas import SampleLoad

router = APIRouter()


@router.get("/sample")
def sample_plan(session: Session = Depends(get_session)):
    """What loading would do here: each device with its planned address and
    port, why it can't be loaded (null when it can), and notes to show."""
    plan = plan_sample(session)
    return {
        "devices": [asdict(device) for device in plan.devices],
        "refusal": plan.refusal,
        "notes": [asdict(note) for note in plan.notes],
    }


@router.post("/sample/load", status_code=201)
async def load_sample(
    body: SampleLoad, session: Session = Depends(get_session), engine: EngineManager = Depends(get_engine)
):
    """Add the sample devices (nothing existing is touched), then start them
    unless `start` is false. A start that fails is reported on its device."""
    try:
        plan = await asyncio.to_thread(save_sample, session)
    except SampleRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    loaded = []
    for device in plan.devices:
        started, error = None, None
        if body.start:
            try:
                await engine.start(device.id)
                started = True
            except EngineError as exc:
                started, error = False, str(exc)
        loaded.append(
            {
                "id": device.id,
                "name": device.name,
                "bind_address": device.bind_address,
                "port": device.port,
                "started": started,
                "error": error,
            }
        )
    return {"devices": loaded}
