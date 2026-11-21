"""The BACnet client tool: Who-Is, browse a remote device, read or write any
property. Each route queues a ClientRequest
for the device's own process through _client_requests.run_client_request
and waits for the answer; the parent never sends BACnet.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import ClientRead, ClientTarget, ClientWhoIs, ClientWrite
from ._client_requests import ClientRequestTimedOut, run_client_request

router = APIRouter()

# Added to every wait, for the device process's own polling (every 0.25 s)
# and SQLite round trips.
WAIT_MARGIN_SECONDS = 2.0
# How many BACnet requests each kind can take one after another (a bind by
# Who-Is, then the request itself; object-list reads the list, then names;
# identify reads the name and vendor in one RPM, or, on a device without RPM,
# in two single reads after the rejected RPM).
_REQUEST_BUDGET = {"read": 2, "write": 2, "object-list": 10, "identify": 4}


def _wait_seconds(kind: str, params: dict) -> float:
    if kind == "who-is":
        return params["window_seconds"] + WAIT_MARGIN_SECONDS
    return params["timeout_seconds"] * _REQUEST_BUDGET[kind] + WAIT_MARGIN_SECONDS


async def _run(request: Request, device_id: int, kind: str, params: dict):
    try:
        answer = await run_client_request(
            request.app.state.session_factory,
            device_id,
            kind,
            params,
            wait_seconds=_wait_seconds(kind, params),
            not_running=f"device {device_id} isn't running; start it to use its BACnet client",
        )
    except ClientRequestTimedOut:
        detail = f"device {device_id} didn't answer in time"
        if kind == "write":
            # Not sent if the device process hadn't started it yet
            # (ClientRequestPoller checks); one already on the wire can't
            # be called back.
            detail += "; a write already sent may still take effect"
        raise HTTPException(status_code=504, detail=detail)
    if not answer.ok:
        raise HTTPException(status_code=502, detail=answer.error)
    return answer.result


@router.post("/devices/{device_id}/client/who-is")
async def client_who_is(device_id: int, body: ClientWhoIs, request: Request):
    return await _run(request, device_id, "who-is", body.model_dump())


@router.post("/devices/{device_id}/client/object-list")
async def client_object_list(device_id: int, body: ClientTarget, request: Request):
    return await _run(request, device_id, "object-list", body.model_dump())


@router.post("/devices/{device_id}/client/identify")
async def client_identify(device_id: int, body: ClientTarget, request: Request):
    """The remote device's Object_Name and Vendor_Name (null where it
    doesn't answer)."""
    return await _run(request, device_id, "identify", body.model_dump())


@router.post("/devices/{device_id}/client/read")
async def client_read(device_id: int, body: ClientRead, request: Request):
    return await _run(request, device_id, "read", body.model_dump())


@router.post("/devices/{device_id}/client/write")
async def client_write(device_id: int, body: ClientWrite, request: Request):
    return await _run(request, device_id, "write", body.model_dump())
