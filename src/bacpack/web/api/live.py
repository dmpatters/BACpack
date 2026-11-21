"""Live object state, read from the device's own process on demand."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request

from ..schemas import DeviceLive, ObjectLive
from ._client_requests import ClientRequestTimedOut, run_client_request
from ._lookups import get_object_or_404

router = APIRouter()

LIVE_WAIT_SECONDS = 2.0
# What a device process started before this API answers a "live" request
# with: its execute() reads params["instance"] before it looks at the kind.
_OLDER_PROCESS_ERROR = "internal error: 'instance'"


async def _live(request: Request, device_id: int, object_ids: Optional[List[int]]) -> dict:
    try:
        answer = await run_client_request(
            request.app.state.session_factory,
            device_id,
            "live",
            {"object_ids": object_ids},
            wait_seconds=LIVE_WAIT_SECONDS,
            not_running=f"device {device_id} isn't running; start it to see live values",
            delete_when_answered=True,
        )
    except ClientRequestTimedOut:
        raise HTTPException(status_code=504, detail=f"device {device_id} didn't answer in time")
    if not answer.ok:
        if answer.error == _OLDER_PROCESS_ERROR:
            raise HTTPException(
                status_code=409,
                detail=f"device {device_id} is running an older BACpack version; restart the device to use live values",
            )
        raise HTTPException(status_code=502, detail=answer.error)
    return answer.result


@router.get("/devices/{device_id}/live", response_model=DeviceLive)
async def get_device_live(device_id: int, request: Request):
    """Every object's present value, priority array, status flags, event
    state, reliability and out-of-service, as the running device serves them."""
    return await _live(request, device_id, None)


@router.get("/devices/{device_id}/objects/{object_id}/live", response_model=ObjectLive)
async def get_object_live(device_id: int, object_id: int, request: Request):
    """One object's live state; 404 "isn't live yet" while a running device
    hasn't built a just-saved object."""
    with request.app.state.session_factory() as session:
        get_object_or_404(session, device_id, object_id)
    result = await _live(request, device_id, [object_id])
    if not result["objects"]:
        raise HTTPException(
            status_code=404,
            detail=f"object {object_id} isn't live yet: a running device builds it within a second of the save",
        )
    return {**result["objects"][0], "read_at": result["read_at"]}
