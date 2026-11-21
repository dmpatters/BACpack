"""Discovery / ad-hoc BACnet client requests from the web UI. The API inserts a
ClientRequest row; this poller, in the device's own process, claims it, sends it
(or, for a "live" request, reads its own objects) and writes the answer back.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, List, Optional, Set

from sqlalchemy import select, update

from .bacnet_client import ClientError, Point, error_text, read_object_list, to_jsonable
from . import clone_reader, held_tasks, live_state, timeutil
from .db import delete_in_chunks
from .models import ClientRequest

logger = logging.getLogger(__name__)

CLIENT_REQUEST_TICK_SECONDS = 0.25
CLIENT_REQUEST_RETENTION = dt.timedelta(days=1)
# How often a clone read saves its progress (and so notices a cancel).
CLONE_PROGRESS_SECONDS = 0.5
STALE_CLONE_ERROR = "the device restarted before the read finished"


class ClientRequestPoller:
    """One per device process (device_process.py). Each claimed request runs
    as its own task, so a Who-Is listening for a few seconds never holds up
    a read."""

    def __init__(self, session_factory, device_id: int, *, client, app=None) -> None:
        self.session_factory = session_factory
        self.device_id = device_id
        self.client = client
        # For "live" requests, which read this device's own objects.
        self.app = app
        self._task: Optional[asyncio.Task] = None
        self._requests: Set[asyncio.Task] = set()
        self._swept = False

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for task in list(self._requests):
            task.cancel()
        self._requests.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CLIENT_REQUEST_TICK_SECONDS)
            try:
                await self.claim_and_start()
            except Exception:
                logger.exception("client request poller tick failed for device %d", self.device_id)

    async def claim_and_start(self) -> List[asyncio.Task]:
        """Claim this device's pending requests (pending -> running, compare
        and set, so no request runs twice) and start each one. The claim
        waits for the history file's write lock in a worker thread, apart
        from config_save's queue (it would only wait behind main-file saves)."""
        if not self._swept:
            self._swept = True
            await asyncio.to_thread(self._fail_stale_clone_jobs)
        claimed = await asyncio.to_thread(self._claim)
        tasks = []
        for request_id, kind, params in claimed:
            task = asyncio.ensure_future(self._run(request_id, kind, params))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)
            tasks.append(task)
        return tasks

    def _fail_stale_clone_jobs(self) -> None:
        """A clone read still "running" when this process starts was claimed
        by one that is gone (a restart or a crash), and nothing will finish
        it. Only "running" rows: a "pending" one is still runnable. Run
        before the first claim, so every row seen here predates this
        process."""
        with self.session_factory() as session:
            session.execute(
                update(ClientRequest)
                .where(
                    ClientRequest.device_id == self.device_id,
                    ClientRequest.kind == "clone-read",
                    ClientRequest.status == "running",
                )
                .values(status="error", error=STALE_CLONE_ERROR, finished_at=timeutil.db_utc_now())
            )
            session.commit()

    def _claim(self) -> list:
        claimed = []
        with self.session_factory() as session:
            pending = (
                session.query(
                    ClientRequest.id, ClientRequest.kind, ClientRequest.params, ClientRequest.cancel_requested
                )
                .filter_by(device_id=self.device_id, status="pending")
                .order_by(ClientRequest.id)
                .all()
            )
            for request_id, kind, params, cancel_requested in pending:
                if cancel_requested:
                    # Cancelled before it started: it never reads.
                    session.execute(
                        update(ClientRequest)
                        .where(ClientRequest.id == request_id, ClientRequest.status == "pending")
                        .values(status="error", error="cancelled", finished_at=timeutil.db_utc_now())
                    )
                    continue
                result = session.execute(
                    update(ClientRequest)
                    .where(ClientRequest.id == request_id, ClientRequest.status == "pending")
                    .values(status="running")
                )
                if result.rowcount == 1:
                    claimed.append((request_id, kind, params or {}))
            session.commit()
        return claimed

    async def _run(self, request_id: int, kind: str, params: dict) -> None:
        result, error = None, None
        try:
            result = await self.execute(kind, params, request_id)
        except ClientError as exc:
            error = error_text(exc)
        except Exception as exc:
            logger.exception("device %d: client request %d failed", self.device_id, request_id)
            error = f"internal error: {error_text(exc)}"
        await asyncio.to_thread(self._save_answer, request_id, result, error)

    def _save_answer(self, request_id: int, result, error: Optional[str]) -> None:
        with self.session_factory() as session:
            # Only a row still "running": the API marks a request it gave up
            # on as an error, and a late answer must not overwrite that.
            session.execute(
                update(ClientRequest)
                .where(ClientRequest.id == request_id, ClientRequest.status == "running")
                .values(
                    status="error" if error is not None else "done",
                    result=result,
                    error=error,
                    finished_at=timeutil.db_utc_now(),
                )
            )
            session.commit()

    def _still_running(self, request_id: int) -> bool:
        with self.session_factory() as session:
            row = session.get(ClientRequest, request_id)
            return row is not None and row.status == "running"

    async def execute(self, kind: str, params: dict, request_id: Optional[int] = None) -> Any:
        if kind == "live":
            return await live_state.read_live_state(
                self.session_factory, self.app, self.device_id, params.get("object_ids")
            )
        if kind == "clone-read":
            return await self._clone_read(params, request_id)
        if kind == "who-is":
            return await self.client.who_is(
                params.get("low"), params.get("high"), window=float(params.get("window_seconds", 3.0))
            )
        instance = int(params["instance"])
        address = params.get("address")
        timeout = float(params.get("timeout_seconds", 5.0))
        binding = await self.client.bind(instance, address, timeout=timeout)
        try:
            if kind == "object-list":
                return await self._object_list(binding, instance, timeout)
            if kind == "identify":
                return await self._identify(binding, instance, timeout)
            point = Point(
                params["object_type"],
                int(params["object_instance"]),
                params.get("property") or "present-value",
                params.get("array_index"),
            )
            if kind == "read":
                return {"value": to_jsonable(await self.client.read(binding, point, timeout=timeout))}
            if kind == "write":
                # The API may have given up while the bind ran (a Who-Is can
                # take the whole timeout): a write it already answered 504 for
                # must not go out after all. _run only finishes a row still
                # "running", so this never overwrites "timed out".
                if request_id is not None and not self._still_running(request_id):
                    raise ClientError("not sent: the API had already stopped waiting")
                await self.client.write(
                    binding, point, params.get("value"), priority=params.get("priority"), timeout=timeout
                )
                return {"ok": True}
        except ClientError as exc:
            if exc.transport:
                self.client.unbind(instance, address)
            raise
        raise ClientError(f"unknown request kind {kind!r}")

    async def _clone_read(self, params: dict, request_id: Optional[int]) -> Any:
        """No request budget: the web side doesn't wait on this job. It
        polls the row, which shows progress and carries the cancel flag."""
        instance = int(params["instance"])
        address = params.get("address")
        timeout = float(params.get("timeout_seconds", 5.0))
        binding = await self.client.bind(instance, address, timeout=timeout)
        job = _CloneProgress(self, request_id)
        try:
            return await clone_reader.read_controller(
                self.client, binding, instance, timeout, job.report, job.is_cancelled
            )
        except ClientError as exc:
            if exc.transport:
                self.client.unbind(instance, address)
            raise
        finally:
            await job.finish()

    def _save_progress(self, request_id: int, done: int, total: int, phase: str = clone_reader.PHASE_OBJECTS) -> bool:
        """Save a clone read's progress; True when the user cancelled it. A
        row no longer "running" (finished, or deleted by Discard) saves
        nothing and reads as cancelled: the read has no one left to serve."""
        with self.session_factory() as session:
            saved = session.execute(
                update(ClientRequest)
                .where(ClientRequest.id == request_id, ClientRequest.status == "running")
                .values(progress={"phase": phase, "done": done, "total": total})
            ).rowcount
            cancel = bool(
                session.scalar(
                    select(ClientRequest.cancel_requested).where(
                        ClientRequest.id == request_id, ClientRequest.status == "running"
                    )
                )
            )
            session.commit()
        return cancel or not saved

    async def _object_list(self, binding, instance: int, timeout: float) -> List[dict]:
        identifiers = await read_object_list(self.client, binding, instance, timeout)
        points = [Point(str(oid[0]), int(oid[1]), "object-name") for oid in identifiers]
        names = await self.client.read_many(binding, points, timeout=timeout) if points else {}
        return [
            {
                "object_type": point.object_type,
                "instance": point.instance,
                "name": None if isinstance(names.get(point), ClientError) else to_jsonable(names.get(point)),
            }
            for point in points
        ]

    async def _identify(self, binding, instance: int, timeout: float) -> dict:
        name, vendor = Point("device", instance, "object-name"), Point("device", instance, "vendor-name")
        values = await self.client.read_many(binding, [name, vendor], timeout=timeout)

        def text(point: Point):
            value = values.get(point)
            return None if value is None or isinstance(value, ClientError) else to_jsonable(value)

        return {"name": text(name), "vendor_name": text(vendor)}


class _CloneProgress:
    """Carries a clone read's progress to its row without slowing the read:
    the reader's report() only notes the latest count, and at most one save
    (in a worker thread) runs at a time, started at most every
    CLONE_PROGRESS_SECONDS. The save also reads the cancel flag."""

    def __init__(self, poller: ClientRequestPoller, request_id: Optional[int]) -> None:
        self._poller = poller
        self._request_id = request_id
        self._latest: Optional[tuple] = None
        self._saved: Optional[tuple] = None
        self._saving: Optional[asyncio.Future] = None
        self._last_started = float("-inf")
        self._cancel = False

    def report(self, done: int, total: int, phase: str = clone_reader.PHASE_OBJECTS) -> None:
        self._latest = (done, total, phase)
        loop = asyncio.get_running_loop()
        if (
            self._request_id is not None
            and self._saving is None
            and loop.time() - self._last_started >= CLONE_PROGRESS_SECONDS
        ):
            self._last_started = loop.time()
            self._saving = held_tasks.start(self._save(self._request_id, done, total, phase))

    def is_cancelled(self) -> bool:
        return self._cancel

    async def _save(self, request_id: int, done: int, total: int, phase: str) -> None:
        try:
            self._cancel = await asyncio.to_thread(self._poller._save_progress, request_id, done, total, phase)
            self._saved = (done, total, phase)
        finally:
            self._saving = None

    async def finish(self) -> None:
        """Wait out a save in flight, then save the last count if it moved."""
        if self._saving is not None:
            await asyncio.shield(self._saving)
        if self._request_id is not None and self._latest is not None and self._latest != self._saved:
            await asyncio.to_thread(self._poller._save_progress, self._request_id, *self._latest)


def prune_old_client_requests(session_factory) -> int:
    """Delete ClientRequest rows older than CLIENT_REQUEST_RETENTION. Run by
    trending.run_retention_task. Returns the number deleted."""
    cutoff = timeutil.db_cutoff(CLIENT_REQUEST_RETENTION)
    return delete_in_chunks(session_factory, ClientRequest, ClientRequest.created_at < cutoff)
