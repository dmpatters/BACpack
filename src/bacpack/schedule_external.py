"""External schedule writes.

A Schedule reference whose deviceIdentifier isn't this device is skipped by
WritingScheduleObject and sent here through the device's BacnetClient,
retried until the remote holds the current value, then not re-sent until
the value or priority changes. A slot it wrote and no longer uses is
released with a Null, best effort: retried while the device runs, forgotten
if it stops first.

Each tick reads the live Schedule objects, so config changes and rebuilds
need no extra wiring.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.local.schedule import ScheduleObject as _LocalScheduleObjectBase
from bacpypes3.primitivedata import Enumerated, Null

from . import config_save, timeutil
from .bacnet_client import ClientError, Point, error_text, to_jsonable
from .connectors import MAX_BACKOFF_SECONDS
from .models import ObjectRuntime

logger = logging.getLogger(__name__)

WRITER_TICK_SECONDS = 1.0
# BACnet's customary APDU timeout: there's no connector config to take one from.
WRITE_TIMEOUT_SECONDS = 3.0
FIRST_BACKOFF_SECONDS = 5.0


def _now() -> float:
    return asyncio.get_running_loop().time()


@dataclass(frozen=True)
class Target:
    device_instance: int
    point: Point

    @property
    def key(self) -> str:
        return f"{self.device_instance}:{self.point.object_type}:{self.point.instance}:{self.point.prop}"


@dataclass
class _State:
    target: Target
    delivered: Optional[tuple] = None  # (jsonable value, priority) the remote holds
    wanted: Optional[tuple] = None  # what the schedule wanted when last looked at
    next_attempt_at: float = 0.0
    backoff: float = 0.0
    held: Set[int] = field(default_factory=set)  # priorities it delivered a value at


@dataclass
class _Release:
    """A slot to relinquish: a priority a schedule wrote and no longer uses."""
    target: Target
    priority: int
    next_attempt_at: float = 0.0
    backoff: float = 0.0
    waiting: bool = False  # another schedule claims the slot: wait for it (not _State.held, a set)
    claimant_failed: bool = False  # ...and its write was refused: release after all


@dataclass
class _Send:
    object_id: Optional[int]  # None: a release
    target: Target
    value: Any  # as BacnetClient.write takes it; None relinquishes
    wanted: tuple
    native: Any  # the value in the schedule's own type, see _native_value

    @property
    def release_key(self) -> Tuple[str, int]:
        return (self.target.key, self.wanted[1])


def _send_value(obj) -> Any:
    """The schedule's Present_Value as BacnetClient.write takes it. Null is a
    relinquish (None). AnyAtomic.get_value() demotes a BinaryPV to a bare
    Enumerated, so enumerated values go as their integer, which every remote
    enumerated datatype accepts."""
    raw = obj.presentValue
    if isinstance(raw, AnyAtomic):
        raw = raw.get_value()
    if raw is None or isinstance(raw, Null):
        return None
    if isinstance(raw, Enumerated):
        return int(raw)
    return raw


def _native_value(obj) -> Any:
    """The schedule's Present_Value as its own BACnet type -- Real, Enumerated
    (a binary schedule's BinaryPV, demoted by get_value()) or Unsigned; Null
    to relinquish. Sent as-is to a target with no standard datatype (see
    BacnetClient.write's `native`)."""
    raw = obj.presentValue
    if isinstance(raw, AnyAtomic):
        raw = raw.get_value()
    return Null(()) if raw is None else raw


def external_targets(obj, own_instance: int) -> List[Target]:
    targets = []
    for ref in obj.listOfObjectPropertyReferences or []:
        device = ref.deviceIdentifier
        if device is None or int(device[1]) == own_instance:
            continue  # local: WritingScheduleObject writes it
        targets.append(
            Target(
                int(device[1]),
                Point(str(ref.objectIdentifier[0]), int(ref.objectIdentifier[1]), str(ref.propertyIdentifier)),
            )
        )
    return targets


def _unexpected(exc: Exception) -> ClientError:
    logger.warning("external schedule write failed unexpectedly: %r", exc)
    return ClientError(error_text(exc))


class ExternalScheduleWriter:
    """One per device process, sharing the device's BacnetClient (and its
    bindings). One send task per remote device, so one that isn't answering
    never holds up another."""

    def __init__(self, session_factory, app, device_id: int, *, client) -> None:
        self.session_factory = session_factory
        self.app = app
        self.device_id = device_id
        self.client = client
        self._task: Optional[asyncio.Task] = None
        self._sending: Dict[int, asyncio.Task] = {}
        self._states: Dict[Tuple[int, str], _State] = {}
        # (target key, priority) -> a slot to relinquish
        self._releases: Dict[Tuple[str, int], _Release] = {}
        # schedule object id -> its status as it should be saved
        self._status: Dict[int, dict] = {}
        # schedule object id -> its status as last saved (None: nothing saved)
        self._saved: Optional[Dict[int, Optional[dict]]] = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for task in self._sending.values():
            task.cancel()
        self._sending.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(WRITER_TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                # One bad tick mustn't stop this device's external writes
                # until its process restarts.
                logger.exception("external schedule tick failed for device %d", self.device_id)

    async def idle(self) -> None:
        """Wait for every send tick() started -- for tests."""
        tasks = [t for t in self._sending.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedules(self) -> Iterator[Tuple[int, Any]]:
        for obj in list(self.app.objectIdentifier.values()):
            if isinstance(obj, _LocalScheduleObjectBase):
                object_id = getattr(obj, "_schedule_object_id", None)
                if object_id is not None:
                    yield object_id, obj

    def _load_saved(self, object_ids: List[int]) -> None:
        with self.session_factory() as session:
            self._saved = {}
            for object_id in object_ids:
                row = session.get(ObjectRuntime, object_id)
                self._saved[object_id] = None if row is None else row.schedule_write_status
                # Start from what was saved, so a restart keeps each target's
                # last delivery next to any new error.
                self._status[object_id] = copy.deepcopy(self._saved[object_id] or {})

    async def tick(self) -> None:
        now = _now()
        schedules = list(self._schedules())
        if self._saved is None:
            self._load_saved([object_id for object_id, _ in schedules])
        live: Set[Tuple[int, str]] = set()
        due: Dict[int, List[_Send]] = {}
        # (target key, priority) an in-season schedule writes -> its state keys
        claims: Dict[Tuple[str, int], List[Tuple[int, str]]] = {}
        for object_id, obj in schedules:
            if getattr(obj, "_in_effective_period", True):
                for target in external_targets(obj, self.device_id):
                    claims.setdefault((target.key, int(obj.priorityForWriting)), []).append((object_id, target.key))
        for object_id, obj in schedules:
            value = _send_value(obj)
            native = _native_value(obj)
            wanted = (None if value is None else to_jsonable(value), int(obj.priorityForWriting))
            status = self._status.setdefault(object_id, {})
            targets = external_targets(obj, self.device_id)
            # Out of season it sends nothing, as for local targets, so it
            # can't fight an in-season sibling for a slot. Its states are kept
            # but forget what was delivered: the sibling may have overwritten
            # the slot, so coming back into season re-sends.
            in_season = getattr(obj, "_in_effective_period", True)
            keys = {target.key for target in targets}
            for key in [k for k in status if k not in keys]:
                del status[key]
            for target in targets:
                state_key = (object_id, target.key)
                live.add(state_key)
                state = self._states.setdefault(state_key, _State(target))
                for priority in state.held - {wanted[1]}:
                    self._queue_release(target, priority)
                state.held &= {wanted[1]}
                if not in_season:
                    state.delivered = None
                    continue
                if state.delivered == wanted:
                    continue
                if state.wanted != wanted:
                    # A new value or priority: try it now, whatever the backoff.
                    state.wanted = wanted
                    state.backoff = 0.0
                    state.next_attempt_at = 0.0
                if now < state.next_attempt_at:
                    continue
                running = self._sending.get(target.device_instance)
                if running is not None and not running.done():
                    continue
                due.setdefault(target.device_instance, []).append(_Send(object_id, target, value, wanted, native))
        for state_key in [k for k in self._states if k not in live]:
            state = self._states.pop(state_key)
            for priority in state.held:
                self._queue_release(state.target, priority)
        self._plan_releases(now, claims, due)
        for device_instance, sends in due.items():
            self._sending[device_instance] = asyncio.ensure_future(self._send(device_instance, sends))
        for object_id, _ in schedules:
            if object_id not in self._touched_by(due):
                await self._save_if_changed(object_id)

    @staticmethod
    def _touched_by(due: Dict[int, List[_Send]]) -> Set[int]:
        return {send.object_id for sends in due.values() for send in sends if send.object_id is not None}

    # --- Releases ---------

    def _queue_release(self, target: Target, priority: int) -> None:
        self._releases.setdefault((target.key, priority), _Release(target, priority))

    def _plan_releases(self, now: float, claims, due: Dict[int, List[_Send]]) -> None:
        for key, release in list(self._releases.items()):
            claimants = claims.get(key)
            if claimants and not release.claimant_failed:
                # Another schedule writes this slot: it re-sends its own
                # value instead, so the slot isn't left empty in between.
                # Held until a claimant delivers; sent if its write is refused.
                if not release.waiting:
                    release.waiting = True
                    for state_key in claimants:
                        state = self._states.get(state_key)
                        if state is not None:
                            state.delivered = None
                continue
            if now < release.next_attempt_at:
                continue
            device_instance = release.target.device_instance
            running = self._sending.get(device_instance)
            if running is not None and not running.done():
                continue
            due.setdefault(device_instance, []).append(
                _Send(None, release.target, None, (None, release.priority), Null(()))
            )

    async def _send(self, device_instance: int, sends: List[_Send]) -> None:
        try:
            try:
                binding = await self.client.bind(device_instance, timeout=WRITE_TIMEOUT_SECONDS)
            except ClientError as exc:
                self._group_failed(device_instance, sends, exc)
                return
            except Exception as exc:
                # Anything else is still this device's failure, not the
                # writer's: record it and back off like any other.
                self._group_failed(device_instance, sends, _unexpected(exc))
                return
            for i, send in enumerate(sends):
                try:
                    await self.client.write(
                        binding,
                        send.target.point,
                        send.value,
                        priority=send.wanted[1],
                        timeout=WRITE_TIMEOUT_SECONDS,
                        native=send.native,
                    )
                except ClientError as exc:
                    if exc.transport:
                        self._group_failed(device_instance, sends[i:], exc)
                        return
                    if exc.kind == "dcc":
                        self._suppressed(send)
                    else:
                        self._failed(send, exc)
                    continue
                except Exception as exc:
                    # e.g. bacpypes3 can't build the request for this
                    # property's datatype: this target's error only, so the
                    # device's other targets still go out.
                    self._failed(send, _unexpected(exc))
                    continue
                self._delivered(send)
        finally:
            for object_id in {send.object_id for send in sends if send.object_id is not None}:
                await self._save_if_changed(object_id)

    def _entry(self, send: _Send) -> dict:
        return self._status.setdefault(send.object_id, {}).setdefault(
            send.target.key,
            {"last_sent_at": None, "last_sent_value": None, "error": None, "suppressed_by_dcc": False},
        )

    def _group_failed(self, device_instance: int, sends: List[_Send], exc: ClientError) -> None:
        if exc.kind == "dcc":
            for send in sends:
                self._suppressed(send)
            return
        if exc.transport:
            self.client.unbind(device_instance)
        for send in sends:
            self._failed(send, exc)

    def _live(self, send: _Send) -> bool:
        """Whether `send`'s target is still one of its schedule's targets: a
        send can finish after tick() pruned a removed target, and its result
        mustn't bring the status entry back."""
        return (send.object_id, send.target.key) in self._states

    def _failed(self, send: _Send, exc: ClientError) -> None:
        if send.object_id is None:
            release = self._releases.get(send.release_key)
            if release is not None:
                if not exc.transport:
                    # A definite answer (unknown-object, write-access-denied):
                    # retrying won't change it.
                    del self._releases[send.release_key]
                    logger.warning(
                        "device %d: releasing %s at priority %d refused, not retried: %s",
                        self.device_id, send.target.key, release.priority, error_text(exc),
                    )
                    return
                release.backoff = (
                    FIRST_BACKOFF_SECONDS if release.backoff == 0 else min(release.backoff * 2, MAX_BACKOFF_SECONDS)
                )
                release.next_attempt_at = _now() + release.backoff
                logger.warning(
                    "device %d: releasing %s at priority %d failed: %s",
                    self.device_id, send.target.key, release.priority, error_text(exc),
                )
            return
        if not self._live(send):
            return
        if not exc.transport:
            waiting = self._releases.get(send.release_key)
            if waiting is not None and waiting.waiting:
                # The claimant can't write this slot: release it after all.
                waiting.claimant_failed = True
        state = self._states.get((send.object_id, send.target.key))
        # Only back off the value that failed: if the schedule has moved on
        # since this send started, the new value is due at once.
        if state is not None and state.wanted == send.wanted:
            state.backoff = (
                FIRST_BACKOFF_SECONDS if state.backoff == 0 else min(state.backoff * 2, MAX_BACKOFF_SECONDS)
            )
            state.next_attempt_at = _now() + state.backoff
        self._entry(send).update(error=error_text(exc), suppressed_by_dcc=False)

    def _suppressed(self, send: _Send) -> None:
        if send.object_id is None or not self._live(send):
            return  # a release is simply tried again next tick
        # Nothing was sent: not a failure, no backoff -- retried next tick.
        self._entry(send)["suppressed_by_dcc"] = True

    def _delivered(self, send: _Send) -> None:
        if send.object_id is None:
            if self._releases.pop(send.release_key, None) is not None:
                logger.info("device %d: released %s at priority %d", self.device_id, send.target.key, send.wanted[1])
                traffic_log = getattr(self.app, "traffic_log", None)
                if traffic_log is not None:
                    traffic_log.add("schedule-write", f"released {send.target.key} at priority {send.wanted[1]}")
            return
        if not self._live(send):
            if send.wanted[0] is not None:
                # Landed after its target went away: release it too.
                self._queue_release(send.target, send.wanted[1])
            return
        state = self._states.get((send.object_id, send.target.key))
        if state is not None:
            state.delivered = send.wanted
            state.backoff = 0.0
            state.next_attempt_at = 0.0
            if send.wanted[0] is None:
                state.held.discard(send.wanted[1])
            else:
                state.held.add(send.wanted[1])
        if send.wanted[0] is not None:
            # A claimant holds the slot now: a release held for it is moot.
            self._releases.pop(send.release_key, None)
        self._entry(send).update(
            last_sent_at=timeutil.iso_utc(timespec="seconds"), last_sent_value=send.wanted[0], error=None, suppressed_by_dcc=False
        )

    async def _save_if_changed(self, object_id: int) -> None:
        current = self._status.get(object_id) or None
        if self._saved is not None and self._saved.get(object_id) == current:
            return
        await self._save_status(object_id)

    async def _save_status(self, object_id: int) -> None:
        # Copied before the await: sends keep changing the live status while
        # the save waits, and `_saved` must hold what was written.
        current = copy.deepcopy(self._status.get(object_id) or None)

        def save(session) -> None:
            row = session.get(ObjectRuntime, object_id)
            if row is not None:
                row.schedule_write_status = current

        try:
            await config_save.save_or_raise(self.session_factory, save)
        except Exception:
            logger.exception("device %d: couldn't save schedule %d's write status", self.device_id, object_id)
            return
        if self._saved is not None:
            self._saved[object_id] = current
