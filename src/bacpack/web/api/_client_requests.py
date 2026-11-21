"""Queue a ClientRequest for a device's own process and wait for its answer.
Shared by the client tool and the live-state
routes: the parent never sends BACnet or reads a device's objects itself."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from ... import timeutil
from ...db import lock_both_files
from ...models import ClientRequest, Device

POLL_STEP_SECONDS = 0.1


class ClientRequestTimedOut(Exception):
    """No answer before the deadline. The row is marked "timed out", so a
    late answer is ignored (ClientRequestPoller only finishes rows still
    "running")."""


@dataclass
class ClientAnswer:
    ok: bool
    result: Any
    error: Optional[str]


async def queue_client_request(
    session_factory, device_id: int, kind: str, params: dict, *, not_running: str, one_unfinished: Optional[str] = None
) -> int:
    """Queue a request that the caller will not wait for; its id."""
    return await asyncio.to_thread(
        _insert, session_factory, device_id, kind, params, not_running, one_unfinished=one_unfinished
    )


async def run_client_request(
    session_factory,
    device_id: int,
    kind: str,
    params: dict,
    *,
    wait_seconds: float,
    not_running: str,
    delete_when_answered: bool = False,
) -> ClientAnswer:
    # Every step runs in a worker thread: a write can wait out another
    # process's lock for the busy timeout, and on the loop that wait freezes
    # the whole web process.
    request_id = await asyncio.to_thread(_insert, session_factory, device_id, kind, params, not_running)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        await asyncio.sleep(POLL_STEP_SECONDS)
        answer = await asyncio.to_thread(_read_answer, session_factory, request_id)
        if answer is not None:
            break
        if loop.time() < deadline:
            continue
        if await asyncio.to_thread(_mark_timed_out, session_factory, request_id):
            raise ClientRequestTimedOut()
        # Otherwise it finished between the read and the update: loop once more.

    if delete_when_answered:
        await asyncio.to_thread(_delete, session_factory, request_id)
    return answer


def _insert(
    session_factory, device_id: int, kind: str, params: dict, not_running: str, *, one_unfinished: Optional[str] = None
) -> int:
    """`one_unfinished`: refuse (409, with this message) while the device has
    a request of this `kind` that is pending or running."""
    with session_factory() as session:
        if one_unfinished is not None:
            # The check and the insert are one step: a second start waits here
            # and then sees the first one's row (409), instead of both passing
            # the check or one failing on the history file's lock.
            lock_both_files(session)
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=f"no device with id {device_id}")
        if device.runtime.process_status != "running":
            raise HTTPException(status_code=409, detail=not_running)
        if one_unfinished is not None and session.scalar(
            select(ClientRequest.id).where(
                ClientRequest.device_id == device_id,
                ClientRequest.kind == kind,
                ClientRequest.status.in_(("pending", "running")),
            )
        ):
            raise HTTPException(status_code=409, detail=one_unfinished)
        row = ClientRequest(device_id=device_id, kind=kind, params=params)
        session.add(row)
        session.commit()
        return row.id


def _read_answer(session_factory, request_id: int) -> Optional[ClientAnswer]:
    with session_factory() as session:
        row = session.get(ClientRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="the request was removed (device deleted?)")
        if row.status in ("done", "error"):
            return ClientAnswer(row.status == "done", row.result, row.error)
        return None


def _mark_timed_out(session_factory, request_id: int) -> bool:
    with session_factory() as session:
        timed_out = session.execute(
            update(ClientRequest)
            .where(ClientRequest.id == request_id, ClientRequest.status.in_(("pending", "running")))
            .values(status="error", error="timed out", finished_at=timeutil.db_utc_now())
        ).rowcount
        session.commit()
        return bool(timed_out)


def _delete(session_factory, request_id: int) -> None:
    # A session of its own: a delete after a read in one plain BEGIN fails at
    # once while another process holds the history file's write lock.
    with session_factory() as session:
        session.execute(delete(ClientRequest).where(ClientRequest.id == request_id))
        session.commit()
