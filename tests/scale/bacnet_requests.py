"""BACnet requests sent straight to an application's handlers, the way the
unit tests do (no socket)."""
from __future__ import annotations

import asyncio
import itertools

from bacpypes3.apdu import (
    AtomicReadFileACK,
    AtomicReadFileRequest,
    AtomicWriteFileRequest,
    ReinitializeDeviceRequest,
)
from bacpypes3.basetypes import (
    AtomicReadFileRequestAccessMethodChoice,
    AtomicReadFileRequestAccessMethodChoiceStreamAccess,
    AtomicWriteFileRequestAccessMethodChoice,
    AtomicWriteFileRequestAccessMethodChoiceStreamAccess,
)

WRITE_CHUNK = 4096


async def send(app, request):
    """Hand `request` to the application's handler and return its response.
    Several requests may be in flight at once: each gets its own invoke id,
    which the handler copies into its response. Yields to the loop after
    each request, as separate datagrams from a real client would, so a loop
    check measures one request rather than a whole file transfer."""
    pending = app.__dict__.get("_scale_pending")
    if pending is None:
        pending = app._scale_pending = {}
        app._scale_invoke_ids = itertools.cycle(range(256))

        async def dispatch(resp):
            pending[resp.apduInvokeID] = resp

        app.response = dispatch
    request.apduInvokeID = next(app._scale_invoke_ids)
    await getattr(app, f"do_{type(request).__name__}")(request)
    response = pending.pop(request.apduInvokeID, None)
    await asyncio.sleep(0)
    return response


async def reinitialize(app, state: str):
    return await send(app, ReinitializeDeviceRequest(reinitializedStateOfDevice=state))


def _read_request(start: int, count: int):
    request = AtomicReadFileRequest(
        fileIdentifier=("file", 1),
        accessMethod=AtomicReadFileRequestAccessMethodChoice(
            streamAccess=AtomicReadFileRequestAccessMethodChoiceStreamAccess(
                fileStartPosition=start, requestedOctetCount=count
            )
        ),
    )
    request.apduSA = True
    request.apduMaxResp = 5
    request.apduMaxSegs = 0
    return request


async def read_config_file(app) -> bytes:
    data, start = b"", 0
    while True:
        ack = await send(app, _read_request(start, 100000))
        assert isinstance(ack, AtomicReadFileACK)
        chunk = bytes(ack.accessMethod.streamAccess.fileData)
        data += chunk
        start += len(chunk)
        if ack.endOfFile:
            return data


async def write_config_file(app, data: bytes) -> None:
    for start in range(0, len(data), WRITE_CHUNK):
        await send(
            app,
            AtomicWriteFileRequest(
                fileIdentifier=("file", 1),
                accessMethod=AtomicWriteFileRequestAccessMethodChoice(
                    streamAccess=AtomicWriteFileRequestAccessMethodChoiceStreamAccess(
                        fileStartPosition=start, fileData=data[start : start + WRITE_CHUNK]
                    )
                ),
            ),
        )
