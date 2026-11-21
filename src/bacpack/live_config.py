"""Applying object edits to a running device.

ObjectReloader keeps a snapshot of every object row it built. When the
device's config_seq moves, DeviceWatch calls apply: reload the rows, run the
whole-set check and diff in a worker thread, rebuild only changed objects on
the loop in turns, then swap them in (live_swap.py) in one synchronous step,
so no BACnet request sees a half-applied change; then report
applied_seq / apply_error / restart_pending."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, NamedTuple, Optional, Tuple

from bacpypes3.primitivedata import ObjectIdentifier
from sqlalchemy import select, text

from . import bacpypes3_compat, live_swap, log_names, object_config
from .device_network import NetworkConfig, resolve_network
from .models import BacnetObject, Device
from .objects.reserved_names import effective_trend_log_instance
from .object_builder import build_bacnet_object
from .objects.calendar import LocalCalendarObject
from .objects.device import DEFAULT_VENDOR_NAME
from .objects.notification_class import LocalNotificationClassObject
from .objects.schedule import WritingScheduleObject
from .objects.trend_log import build_trend_log_object
from .objects.trend_log_reporting import build_reporting_trend_log_object

logger = logging.getLogger(__name__)

# A change to these alone rebuilds only the object's trend log.
TREND_COLUMNS = frozenset({
    "trended", "trend_mode", "trend_interval_seconds", "trend_sample_cap", "trend_log_instance", "trend_log_name",
    "trend_reporting_enabled",
    "trend_notification_threshold", "trend_notification_class_instance", "trend_event_enable",
    "trend_notify_type", "trend_event_detection_enable",
})
# Read live by a runner already (SimulationRunner): nothing to rebuild.
RUNNER_COLUMNS = frozenset({"simulation"})
# A replaced schedule re-commands its local targets only when these change;
# a value change writes through its own evaluation.
_SCHEDULE_COMMAND_COLUMNS = frozenset({"schedule_write_targets", "schedule_priority"})
# A new BACnet identity: the old object goes, a fresh one comes, nothing carried.
IDENTITY_COLUMNS = frozenset({"object_type", "instance_number"})
# The build gives the loop a turn at least this often (section 7 of the design).
BUILD_TURN_SECONDS = 0.02
# Attached first: alarming objects link their notification class, and
# schedules evaluate against their calendars, as soon as they're added.
_ATTACH_ORDER = {"notification-class": 0, "calendar": 1}

_WRITE_STATUS = text(
    "UPDATE main.device_runtime SET applied_seq = :seq, apply_error = :error, "
    "restart_pending = COALESCE(:pending, restart_pending) WHERE device_id = :device_id"
)


class Diff(NamedTuple):
    removed: List[int]  # ids whose live object (and trend log) is taken out, carrying nothing
    rebuilt: Dict[int, frozenset]  # id -> changed columns; built anew (added when also removed or new)
    trend_only: List[int]  # ids whose trend log alone is rebuilt
    adopted: List[int]  # ids whose change needs nothing live


def diff_rows(old: Dict[int, dict], new: Dict[int, dict]) -> Diff:
    removed = [i for i in old if i not in new]
    rebuilt: Dict[int, frozenset] = {}
    trend_only: List[int] = []
    adopted: List[int] = []
    for i, row in new.items():
        before = old.get(i)
        if before is None:
            rebuilt[i] = frozenset(row)
            continue
        changed = frozenset(key for key in row if row[key] != before.get(key))
        if not changed:
            continue
        if changed & IDENTITY_COLUMNS:
            removed.append(i)
            rebuilt[i] = frozenset(row)
        elif changed - TREND_COLUMNS - RUNNER_COLUMNS:
            rebuilt[i] = changed
        elif changed & TREND_COLUMNS:
            trend_only.append(i)
        else:
            adopted.append(i)
    return Diff(removed, rebuilt, trend_only, adopted)


def _row_ref(row: dict) -> str:
    return log_names.object_ref(row["object_type"], row["instance_number"], row["name"])


def _object_error(row: dict, exc: Exception) -> str:
    return f"object {_row_ref(row)}: {exc}"


_SUMMARY_NAMES = 5


def _changes_summary(names: Dict[str, List[str]]) -> str:
    """'added 7 (<ref>, ... and 2 more), removed 1 (<ref>)', each ref as log_names.object_ref; '' for none."""
    parts = []
    for kind, listed in names.items():
        if listed:
            shown = ", ".join(listed[:_SUMMARY_NAMES])
            if len(listed) > _SUMMARY_NAMES:
                shown += f" and {len(listed) - _SUMMARY_NAMES} more"
            parts.append(f"{kind} {len(listed)} ({shown})")
    return ", ".join(parts)


def _drop_unattached(obj) -> None:
    """A built object that never joins the device: its constructor queued an
    evaluation that would otherwise run, and re-arm itself, for good."""
    if obj is not None:
        bacpypes3_compat.cancel_pending_interpretation(obj)


class ObjectReloader:
    def __init__(
        self, session_factory, app, device: Device, objects: List[BacnetObject], trend_sampler=None, refreezer=None,
        network: Optional[NetworkConfig] = None,
    ) -> None:
        self.session_factory = session_factory
        self.app = app
        # gc_freeze.Refreezer: told how many objects each apply retired
        # (frozen, so only a refreeze frees them; a put-back counts too, which
        # only refreezes a little early) or added (unfrozen, so in every full
        # collection until a refreeze).
        self.refreezer = refreezer
        self._changed = 0
        # Re-resolved after an apply that swapped, added or removed a live
        # object: it holds the live objects it samples (trending.py).
        self.trend_sampler = trend_sampler
        self._swapped = False
        self._last_unexpected: Optional[str] = None
        self.device_id = device.id
        # Device.bind_address as this process started with it (None = the
        # global default); a different saved one needs a restart.
        self._started_bind_address = device.bind_address
        # The role and BBMD table the process was built with; None = not compared.
        self._started_network = network
        self._rows: Dict[int, dict] = {obj.id: object_config.row_from_object(obj) for obj in objects}
        # The worker thread reads _rows while the swap that writes it waits.
        self._lock = asyncio.Lock()

    async def apply(self, change_seq: int) -> bool:
        """Apply whatever changed since the last apply. Never raises: a
        failure is logged and reported as apply_error. False after an
        unexpected error, which DeviceWatch retries next tick; an error the
        apply reported itself (a bad row) waits for the next edit."""
        async with self._lock:
            self._swapped = False
            handled = True
            try:
                error, pending = await self._apply()
            except Exception as exc:
                error, pending, handled = f"unexpected error: {exc}", None, False
                # Retried every tick: the same failure is logged once, not each time.
                if error != self._last_unexpected:
                    logger.exception("device %d: applying config changes failed", self.device_id)
            self._last_unexpected = None if handled else error
            if self._swapped and self.trend_sampler is not None:
                try:
                    self.trend_sampler.resync()
                except Exception as exc:
                    logger.exception("device %d: re-resolving trended objects failed", self.device_id)
                    error = "; ".join(filter(None, [error, f"trend sampling: {exc}"]))
            if self.refreezer is not None and self._changed > 0:
                try:
                    self.refreezer.changed(self._changed, len(self._rows))
                except Exception:
                    logger.exception("device %d: refreezing garbage collection failed", self.device_id)
                self._changed = 0
            # Off the loop: under another writer's lock it waits out the busy timeout.
            await asyncio.to_thread(self._write_status, change_seq, error, pending)
            return handled

    def _write_status(self, change_seq: int, error: Optional[str], pending: Optional[bool]) -> None:
        try:
            with self.session_factory() as session:
                session.execute(
                    _WRITE_STATUS,
                    {"seq": change_seq, "error": error, "pending": pending, "device_id": self.device_id},
                )
                session.commit()
        except Exception:
            logger.exception("device %d: could not save the live-apply status", self.device_id)

    def _load(self) -> Optional[Tuple[Device, List[BacnetObject], NetworkConfig]]:
        with self.session_factory() as session:
            device = session.get(Device, self.device_id)
            if device is None:
                return None
            objects = list(session.scalars(select(BacnetObject).filter_by(device_id=self.device_id)))
            network = resolve_network(session, device)
            session.expunge_all()
        return device, objects, network

    def _read(self, device_name: str):
        """The worker-thread half: on a big device this is nearly all of an
        apply's work."""
        loaded = self._load()
        if loaded is None:
            return None
        device, objects, network = loaded
        by_id = {obj.id: obj for obj in objects}
        rows = {obj.id: object_config.row_from_object(obj) for obj in objects}
        problem = object_config.check_device(device_name, rows.values())
        return device, by_id, rows, problem, diff_rows(self._rows, rows), network

    async def _apply(self) -> Tuple[Optional[str], Optional[bool]]:
        read = await asyncio.to_thread(self._read, str(self.app.device_object.objectName))
        if read is None:
            return None, None  # device deleted: DeviceWatch stops this process
        device, by_id, rows, problem, diff, network = read
        pending = self._restart_pending(device, network)
        if problem is not None:
            logger.warning("device %d: config changes not applied: %s", self.device_id, problem)
            return f"{problem}; nothing applied", pending
        built, logs, work, errors = await self._build(device, by_id, rows, diff)
        # From here to the return, nothing awaits: the swap is one step.
        errors += self._apply_objects(device, by_id, rows, diff, built, logs, work)
        live = self.app.device_object
        live.description = device.description or ""
        live.location = device.location or ""
        live.backupFailureTimeout = device.backup_failure_timeout
        if live._backup_restore is not None:
            # Takes effect from the open session's next request.
            live._backup_restore.timeout = device.backup_failure_timeout
        live.databaseRevision = device.database_revision or 1
        return ("; ".join(errors) or None), pending

    def _restart_pending(self, device: Device, network: NetworkConfig) -> bool:
        live = self.app.device_object
        return (
            device.name != str(live.objectName)
            or device.vendor_identifier != int(live.vendorIdentifier)
            or (device.vendor_name or DEFAULT_VENDOR_NAME) != str(live.vendorName)
            or device.bind_address != self._started_bind_address
            or (self._started_network is not None and network != self._started_network)
        )

    def _live_object(self, row: dict):
        return self.app.get_object_id(ObjectIdentifier((row["object_type"], row["instance_number"])))

    def _live_trend_log(self, row: dict):
        """The live Trend Log of the point `row` describes, at the instance
        that row gives it (not necessarily the row id)."""
        log = self.app.get_object_id(ObjectIdentifier(("trend-log", effective_trend_log_instance(row))))
        return log if getattr(log, "_trend_object_id", row["id"]) == row["id"] else None

    def _build_trend_log(self, device: Device, obj: BacnetObject):
        build = build_reporting_trend_log_object if obj.trend_reporting_enabled else build_trend_log_object
        return build(device, obj, self.session_factory)

    async def _build(
        self, device: Device, objects: Dict[int, BacnetObject], rows: Dict[int, dict], diff: Diff
    ) -> Tuple[Dict[int, object], Dict[int, object], bacpypes3_compat.DeferredLoopWork, List[str]]:
        """1. Build everything first, giving the loop turns: nothing live
        changes here, and what each build queues is held back for the swap.
        A failed build drops that row from this apply, so its old version
        keeps serving."""
        errors: List[str] = []
        built: Dict[int, object] = {}
        logs: Dict[int, object] = {}
        work = bacpypes3_compat.DeferredLoopWork()
        loop = asyncio.get_running_loop()
        turn_started = loop.time()
        try:
            for i in diff.rebuilt:
                try:
                    built[i] = work.build(build_bacnet_object, objects[i], config_session_factory=self.session_factory)
                except Exception as exc:
                    errors.append(_object_error(rows[i], exc))
                if loop.time() - turn_started >= BUILD_TURN_SECONDS:
                    await asyncio.sleep(0)
                    turn_started = loop.time()
            for i in [*built, *diff.trend_only]:
                if objects[i].trended:
                    try:
                        logs[i] = work.build(self._build_trend_log, device, objects[i])
                    except Exception as exc:
                        errors.append(_object_error(rows[i], exc))
                        _drop_unattached(built.pop(i, None))
                    if loop.time() - turn_started >= BUILD_TURN_SECONDS:
                        await asyncio.sleep(0)
                        turn_started = loop.time()
        except BaseException:
            work.discard()  # a stop mid-build: nothing was swapped
            raise
        return built, logs, work, errors

    def _apply_objects(
        self, device: Device, objects: Dict[int, BacnetObject], rows: Dict[int, dict], diff: Diff,
        built: Dict[int, object], logs: Dict[int, object], work: bacpypes3_compat.DeferredLoopWork,
    ) -> List[str]:
        errors: List[str] = []
        # Before anything is attached, so it queues ahead of what the attach
        # queues, as when the build ran in this same step.
        work.replay()
        trend_only = [i for i in diff.trend_only if i in logs or not objects[i].trended]
        removed = [i for i in diff.removed if i not in rows or i in built]
        swapped = [i for i in built if i in self._rows and i not in removed]

        self._swapped = bool(built or trend_only or removed)

        order = list(self.app.objectIdentifier)

        # 2. Detach every old object before attaching any new one, so a
        # rename onto a name another object is giving up can't collide.
        old_objects: Dict[int, tuple] = {}
        for i in {*removed, *swapped}:
            obj = self._live_object(self._rows[i])
            if obj is not None:
                if i in removed and isinstance(obj, WritingScheduleObject):
                    # Deleted or renumbered: its slots are released (a
                    # renumbered one's new identity re-claims them).
                    obj.release_local_targets(self.app)
                old_objects[i] = (obj, live_swap.retire(self.app, obj))
                self._changed += 1
        # An apply that raised can leave a new object attached with the
        # snapshot behind it. Whatever is still live at a new identity is
        # that leftover (identities are unique across the rows), so it goes
        # rather than failing as a duplicate.
        for i in built:
            if i not in swapped:
                leftover = self._live_object(rows[i])
                if leftover is not None:
                    live_swap.retire(self.app, leftover)
                    self._changed += 1
        # An edit can move a log to another instance, so the old one is found
        # from the snapshot row, and every old log goes before any new one is
        # attached (two points can swap instances).
        old_logs: Dict[int, tuple] = {}
        for i in {*removed, *built, *trend_only}:
            old_row = self._rows.get(i)
            log = self._live_trend_log(old_row) if old_row is not None and old_row["trended"] else None
            if log is not None:
                old_logs[i] = (log, live_swap.retire(self.app, log))
                self._changed += 1
        # Like the objects above: a log still live at a new instance is a
        # leftover of an apply that raised.
        for i in {*built, *trend_only}:
            if objects[i].trended:
                leftover = self._live_trend_log(rows[i])
                if leftover is not None:
                    live_swap.retire(self.app, leftover)
                    self._changed += 1
        # One log line per apply, not per object: a device writes each line
        # to SQLite on this loop, inside the swap.
        names: Dict[str, List[str]] = {"added": [], "replaced": [], "removed": []}
        for i in removed:
            if i not in rows:
                names["removed"].append(_row_ref(self._rows[i]))

        # 3. Attach: notification classes, calendars, the rest.
        replaced_ncs, replaced_calendars, replaced_schedules = [], [], []
        for i in sorted(built, key=lambda i: _ATTACH_ORDER.get(rows[i]["object_type"], 2)):
            new = built[i]
            old, taken = old_objects.get(i, (None, []))
            carry = i in swapped and old is not None
            if carry:
                live_swap.carry_runtime(old, new, diff.rebuilt[i])
            else:
                taken = []  # a new BACnet identity: its old subscriptions end, as on a delete
            try:
                live_swap.attach(self.app, new, taken)
            except Exception as exc:
                errors.append(_object_error(rows[i], exc))
                # Its old snapshot stays, so the next apply tries it again.
                _drop_unattached(built.pop(i))
                logs.pop(i, None)
                if carry:
                    try:
                        live_swap.attach(self.app, old, taken)
                        # retire cancelled any queued evaluation; add_object
                        # already re-armed a schedule's timer.
                        live_swap.start_alarm_evaluation(old)
                    except Exception as put_back:
                        logger.exception("device %d: could not put %s back", self.device_id, _row_ref(rows[i]))
                        errors.append(_object_error(rows[i], f"could not put the previous version back either: {put_back}"))
                    old_log, log_taken = old_logs.pop(i, (None, []))
                    if old_log is not None:
                        try:
                            live_swap.attach(self.app, old_log, log_taken)
                        except Exception as put_back:
                            logger.exception("device %d: could not put the trend log of %s back", self.device_id, _row_ref(rows[i]))
                            errors.append(_object_error(rows[i], f"could not put its trend log back: {put_back}"))
                continue
            names["replaced" if carry else "added"].append(_row_ref(rows[i]))
            if not carry:
                # Nothing carried: evaluate once, as start does.
                live_swap.start_alarm_evaluation(new)
            if carry and isinstance(new, LocalNotificationClassObject):
                replaced_ncs.append((old, new))
            if carry and isinstance(new, LocalCalendarObject):
                replaced_calendars.append(new)
            if carry and isinstance(new, WritingScheduleObject) and diff.rebuilt[i] & _SCHEDULE_COMMAND_COLUMNS:
                replaced_schedules.append(new)
        for i, log in logs.items():
            old_log, taken = old_logs.get(i, (None, []))
            if old_log is not None:
                live_swap.carry_runtime(old_log, log, frozenset())
                if old_log.objectIdentifier != log.objectIdentifier:  # pyright: ignore[reportAttributeAccessIssue] -- old_logs holds untyped tuples
                    taken = []  # a subscriber watched the old identity
            live_swap.attach(self.app, log, taken)
        live_swap.restore_order(self.app, order)
        for old_nc, new_nc in replaced_ncs:
            live_swap.relink_notification_class(self.app, old_nc, new_nc)
        for calendar in replaced_calendars:
            calendar._reevaluate_referencing_schedules()
        for schedule in replaced_schedules:
            # An edited target list or priority applies now, releasing the
            # slots it no longer uses.
            schedule.command_local_targets()

        # 4. The snapshot follows what is now live; a failed row keeps its
        # old snapshot, so the next apply tries it again.
        for i in removed:
            if i not in rows:
                del self._rows[i]
        for i in [*built, *trend_only, *diff.adopted]:
            self._rows[i] = rows[i]
        self._changed += len(names["added"])
        summary = _changes_summary(names)
        if summary:
            logger.info("device %d: %s", self.device_id, summary)
        return errors
