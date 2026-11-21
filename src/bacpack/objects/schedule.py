"""Schedule object classes. The config helpers they use (parse, validate,
build, unbuild) are in objects/schedule_config.py.
"""

from __future__ import annotations

import logging

from bacpypes3.local.schedule import ScheduleObject as _LocalScheduleObjectBase
from bacpypes3.basetypes import DeviceObjectPropertyReference, SpecialEvent
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Null, ObjectIdentifier

from .. import bacpypes3_compat  # also applies the schedule swaps
from .. import config_save, held_tasks, log_names, object_config
from ..bacnet import WritePropertyAllowlisted, internal_write, normalized_attr, typed_present_value
from ..models import BacnetObject
from .schedule_config import (
    atomic_value_to_schedule_string,
    build_daily_schedule,
    build_schedule_write_targets,
    build_special_event,
    date_range_to_effective_start_end,
    effective_period,
    unbuild_daily_schedule,
    unbuild_schedule_write_targets,
    unbuild_special_event,
    normalize_write_targets,
    schedule_atomic_value,
    validate_schedule_config,
)

logger = logging.getLogger(__name__)


def _is_null(present_value) -> bool:
    value = present_value.get_value() if isinstance(present_value, AnyAtomic) else present_value
    return value is None or isinstance(value, Null)


# Present_Value is never externally writable on a Schedule or Calendar,
# whatever `bacnet_writable` says: it's derived (interpret_schedule assigns it
# directly, bypassing write_property), and the standard agrees. The config
# properties (weeklySchedule, ... / dateList) are writable when
# bacnet_writable is set, each accepted write persisted to the row's config
# columns so it survives a restart.


class ScheduleEvaluationHeld:
    """bacpypes3's eval() returns None outside Effective_Period, and
    interpret_schedule() unpacks it unconditionally: the TypeError escapes
    the timer callback before it re-arms, so the schedule never evaluates
    again. Outside the period, hold the current value (no target writes to
    fight a sibling seasonal schedule) and look again at midnight.
    `_in_effective_period` stays False until a successful evaluation."""

    _in_effective_period = False

    def eval(self, edate, etime):
        result = self._eval_first_wins(edate, etime)
        self._in_effective_period = result is not None
        if result is None:
            return self.presentValue, (24, 0, 0, 0)
        return result

    def _eval_first_wins(self, edate, etime):
        events = list(self.exceptionSchedule or [])  # pyright: ignore[reportAttributeAccessIssue] -- a ScheduleObject property
        prepared = _first_wins_events(events, etime)
        if prepared is None:
            return super().eval(edate, etime)  # pyright: ignore[reportAttributeAccessIssue] -- the upstream base follows in the MRO
        reordered, future_times = prepared
        result = _LocalScheduleObjectBase.eval(
            _CheckedView(self, {"exceptionSchedule": reordered}), edate, etime  # pyright: ignore[reportArgumentType] -- stands in for the schedule
        )
        if result is None:
            return None
        value, next_transition = result
        return value, min([tuple(next_transition), *map(tuple, future_times)])


def _first_wins_events(events: list, etime):
    """(events to evaluate, their pending times) when two Exception_Schedule
    entries share an Event_Priority, else None (upstream's path, unchanged).

    Upstream is last-wins per Event_Priority; with the list reversed, the
    first entry in the list is the one it keeps, as bacnet-stack does. An
    entry whose value in effect is Null does not compete, and one whose
    first time is still ahead holds nothing yet. An entry's next time is
    returned for the caller to fold into the transition time, because
    upstream keeps one slot per priority and a later entry overwrites it.
    Whether an entry's period matches today is not checked for that (a
    calendar reference needs the application), so an entry for another day
    can add a spare, harmless early re-evaluation."""
    priorities = [int(e.eventPriority) for e in events]
    tied = {p for p in priorities if priorities.count(p) > 1}
    if not tied:
        return None
    kept, future_times = [], []
    for event in events:
        if int(event.eventPriority) not in tied:
            kept.append(event)
            continue
        time_values = list(event.listOfTimeValues)
        active = [tv for tv in time_values if tv.time <= etime]
        future = [tv for tv in time_values if tv.time > etime]
        if future:
            future_times.append(future[0].time)
        if active and _is_null(active[-1].value):
            time_values = future
        kept.append(
            SpecialEvent(period=event.period, listOfTimeValues=time_values, eventPriority=event.eventPriority)
        )
    return list(reversed(kept)), future_times


_TYPING_VALUES = {"analog": "0", "binary": "inactive", "multi-state": "1"}


class _CheckedView:
    """The schedule as check_reliability sees it: reads come from the
    schedule, except the properties given, writes go to the schedule."""

    def __init__(self, schedule, replaced: dict) -> None:
        object.__setattr__(self, "_schedule", schedule)
        for name, value in replaced.items():
            object.__setattr__(self, name, value)

    def __getattr__(self, name):
        return getattr(self._schedule, name)

    def __setattr__(self, name, value) -> None:
        setattr(self._schedule, name, value)


def _local_references(schedule, refs):
    """The references this device writes itself, a ref naming the own device
    as a plain local one, or `refs` itself when nothing changes."""
    if not refs or all(ref.deviceIdentifier is None for ref in refs):
        return refs
    app = bacpypes3_compat.object_app(schedule)
    own = app.device_object.objectIdentifier[1] if app else None
    local = []
    for ref in refs:
        if ref.deviceIdentifier is None:
            local.append(ref)
        elif ref.deviceIdentifier[1] == own:
            local.append(
                DeviceObjectPropertyReference(
                    objectIdentifier=ref.objectIdentifier,
                    propertyIdentifier=ref.propertyIdentifier,
                    propertyArrayIndex=ref.propertyArrayIndex,
                )
            )
    return local


class ScheduleReliabilityChecked:
    """Upstream's check_reliability reads two things BACpack supports as a
    configuration error, and a faulted schedule never evaluates again:
    - a Null Schedule_Default (valid: outside the scheduled times the value
      is Null), since it takes the schedule's datatype from the default;
    - a write target on another device (Clause 12.24.10 limits a schedule to
      its own device; ExternalScheduleWriter sends those).
    The check runs against a default of the schedule's value type and only
    the local targets instead."""

    def check_reliability(self, old_value=None, new_value=None):
        replaced = {}
        default = self.scheduleDefault  # pyright: ignore[reportAttributeAccessIssue] -- a ScheduleObject property
        if default is not None and isinstance(default.get_value(), Null):
            value_type = getattr(self, "_schedule_value_type", "analog") or "analog"
            replaced["scheduleDefault"] = AnyAtomic(schedule_atomic_value(value_type, _TYPING_VALUES[value_type]))
        refs = self.listOfObjectPropertyReferences  # pyright: ignore[reportAttributeAccessIssue] -- a ScheduleObject property
        local = _local_references(self, refs)
        if local is not refs:
            replaced["listOfObjectPropertyReferences"] = local
        if not replaced:
            return super().check_reliability(old_value, new_value)  # pyright: ignore[reportAttributeAccessIssue] -- the upstream base follows in the MRO
        return _LocalScheduleObjectBase.check_reliability(
            _CheckedView(self, replaced), old_value, new_value  # pyright: ignore[reportArgumentType] -- stands in for the schedule
        )


# What a client may write when bacnet_writable=True.
_SCHEDULE_WRITABLE_ATTRS = frozenset(
    {
        "weeklySchedule",
        "exceptionSchedule",
        "effectivePeriod",
        "scheduleDefault",
        "priorityForWriting",
        "listOfObjectPropertyReferences",
    }
)


class WritePropertyAllowlistedSchedule(WritePropertyAllowlisted):
    @property
    def _WRITABLE_ATTRS(self) -> frozenset:  # type: ignore[override]
        return _SCHEDULE_WRITABLE_ATTRS if getattr(self, "_bacnet_writable", False) else frozenset()


class WritingScheduleObject:
    """Upstream present_value_changed calls the coroutine write_property
    without `await` (property monitors run synchronously), so the target
    write is silently dropped. Overridden to run it as an asyncio task.
    """

    def present_value_changed(self, old_value, new_value) -> None:
        if not bacpypes3_compat.object_app(self) or not self.listOfObjectPropertyReferences:
            return
        held_tasks.start(self._apply_schedule_writes(new_value.get_value()))

    # Local targets are commanded at start and on coming into season: the
    # change hook above never fires when the evaluated value equals the one
    # already held. Out of season nothing is commanded, so it can't fight an
    # in-season sibling. ExternalScheduleWriter sends remote targets itself.
    _local_targets_commanded = False

    def interpret_schedule(self):
        # Every entry point (the constructor's queued call, add_object, a
        # property monitor, the timer) arms a new timer and overwrites
        # _interpret_schedule_handle, so cancel the one it replaces: one
        # chain, and retire() can cancel it.
        bacpypes3_compat.cancel_pending_interpretation(self)
        before = self.presentValue
        was_in_effective_period = self._in_effective_period
        super().interpret_schedule()
        if not bacpypes3_compat.object_app(self) or not self._in_effective_period:
            return
        if self._local_targets_commanded and was_in_effective_period:
            return
        self._local_targets_commanded = True
        if self.presentValue == before:
            # Unchanged, so present_value_changed didn't run; a change
            # already wrote the targets.
            self.command_local_targets()

    def command_local_targets(self) -> None:
        """Write the current Present_Value to every local target (nothing
        out of season), then release every slot this schedule wrote and no
        longer uses, so a moved slot never leaves the target on the old one."""
        app = bacpypes3_compat.object_app(self)
        if not app:
            return
        commanded = self._commanded_slots()
        stale = {key: slot for key, slot in commanded.items() if key not in self._local_slots(app)}
        for key in stale:
            del commanded[key]
        write = bool(self.listOfObjectPropertyReferences) and self._in_effective_period
        if write or stale:
            held_tasks.start(self._write_then_release(app, write, self.presentValue.get_value(), stale))

    async def _write_then_release(self, app, write: bool, atomic_value, stale: dict) -> None:
        if write:
            await self._apply_schedule_writes(atomic_value)
        if stale:
            await self._release(app, stale)

    # --- Releasing slots -------------
    # A slot's key is plain values (type, instance, property, index,
    # priority); its value is the reference's own fields.

    def _commanded_slots(self) -> dict:
        """Local slots this schedule wrote and hasn't released. Carried
        across a live swap by live_swap.carry_runtime."""
        commanded = self.__dict__.get("_commanded")
        if commanded is None:
            commanded = self._commanded = {}
        return commanded

    @staticmethod
    def _slot(ref, priority: int):
        index = ref.propertyArrayIndex
        key = (
            str(ref.objectIdentifier[0]),
            int(ref.objectIdentifier[1]),
            str(ref.propertyIdentifier),
            None if index is None else int(index),
            priority,
        )
        return key, (ref.objectIdentifier, ref.propertyIdentifier, index, priority)

    def _local_slots(self, app) -> dict:
        own = app.device_object.objectIdentifier[1]
        priority = int(self.priorityForWriting)
        slots = {}
        for ref in self.listOfObjectPropertyReferences or []:
            if ref.deviceIdentifier is not None and ref.deviceIdentifier[1] != own:
                continue  # ExternalScheduleWriter's
            key, slot = self._slot(ref, priority)
            slots[key] = slot
        return slots

    def release_local_targets(self, app) -> None:
        """Release every slot this schedule wrote, as it leaves the running
        device (deleted or renumbered)."""
        commanded = self._commanded_slots()
        if commanded:
            slots = dict(commanded)
            commanded.clear()
            held_tasks.start(self._release(app, slots))

    async def _release(self, app, slots: dict) -> None:
        """Relinquish each slot, unless another in-season schedule claims it:
        that one writes its own value instead, so a summer/winter pair hands
        over without a gap."""
        for key, (object_identifier, property_identifier, index, priority) in slots.items():
            claimants = [
                other
                for other in list(app.objectIdentifier.values())
                if isinstance(other, WritingScheduleObject)
                and other is not self
                and getattr(other, "_in_effective_period", True)
                and key in other._local_slots(app)
                and not _is_null(getattr(other, "presentValue", None))  # a Null one would only relinquish it again
            ]
            if claimants:
                for other in claimants:
                    other.command_local_targets()
                continue
            obj = app.get_object_id(ObjectIdentifier((key[0], key[1])))
            if obj is None:
                continue  # the target itself is gone
            try:
                with internal_write(obj):
                    await obj.write_property(property_identifier, Null(()), index=index, priority=priority)
                if getattr(app, "traffic_log", None) is not None:
                    app.traffic_log.add(
                        "schedule-write",
                        f"{log_names.live_object_ref(self)} released {object_identifier}.{property_identifier} at priority {priority}",
                    )
            except Exception as exc:
                logger.warning(
                    "schedule %s: releasing %s.%s at priority %d failed: %r",
                    log_names.live_object_ref(self), object_identifier, property_identifier, priority, exc,
                )

    async def _note_commanded(self, app, ref, priority: int, atomic_value) -> None:
        """Record the slot at the priority the write was sent at: a write can
        suspend (a proxied target waits on its remote) while the priority or
        targets change or the schedule is swapped out. A slot no longer in
        use is released now."""
        key, slot = self._slot(ref, priority)
        commanded = self._commanded_slots()
        if atomic_value is None or isinstance(atomic_value, Null):
            commanded.pop(key, None)  # a Null write relinquished it
            return
        attached = app.objectIdentifier.get(self.objectIdentifier) is self
        if attached and key in self._local_slots(app):
            commanded[key] = slot
        else:
            commanded.pop(key, None)
            await self._release(app, {key: slot})

    async def _apply_schedule_writes(self, atomic_value) -> None:
        app = bacpypes3_compat.object_app(self)
        if app is None:
            return  # retired after this write was queued
        for ref in self.listOfObjectPropertyReferences:
            if ref.deviceIdentifier is not None and ref.deviceIdentifier[1] != app.device_object.objectIdentifier[1]:
                continue  # ExternalScheduleWriter sends it
            obj = app.get_object_id(ref.objectIdentifier)
            if obj is None:
                logger.warning(
                    "schedule %s: write target %s isn't in the running device",
                    log_names.live_object_ref(self), ref.objectIdentifier
                )
                continue
            # BACpack's own write, not a client's: bypass BacnetWriteLocked.
            try:
                priority = int(self.priorityForWriting)
                if isinstance(atomic_value, Null):
                    # A Null value (an unset Schedule_Default, or a Null time
                    # value) relinquishes the slot, unless a sibling claims it.
                    key, slot = self._slot(ref, priority)
                    self._commanded_slots().pop(key, None)
                    await self._release(app, {key: slot})
                    continue
                typed_value = typed_present_value(obj, atomic_value)
                with internal_write(obj):
                    await obj.write_property(
                        ref.propertyIdentifier,
                        typed_value,
                        index=ref.propertyArrayIndex,
                        priority=priority,
                    )
                await self._note_commanded(app, ref, priority, atomic_value)
                if getattr(app, "traffic_log", None) is not None:
                    # The typed value: AnyAtomic demotes a BinaryPV to a bare
                    # Enumerated ("<Enumerated: 1>" instead of "active").
                    app.traffic_log.add(
                        "schedule-write",
                        f"{log_names.live_object_ref(self)} -> {ref.objectIdentifier}."
                        f"{ref.propertyIdentifier} = {typed_value}",
                    )
            except Exception as exc:
                # Best-effort: one bad target shouldn't stop the rest.
                logger.warning(
                    "schedule %s: write to %s.%s failed: %r",
                    log_names.live_object_ref(self),
                    ref.objectIdentifier,
                    ref.propertyIdentifier,
                    exc,
                )


class ScheduleConfigPersisted:
    """Persists an accepted external write to a Schedule config property to
    this row's DB columns, so it survives a restart.

    Validates the prospective post-write config before super().write_property(),
    so a rejected write never changes the live object either. Also runs
    object_config.check_saved_row (local write targets and calendar
    references must exist): this is external input no API layer validated.
    """

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        normalized = normalized_attr(self, attr)
        if normalized not in _SCHEDULE_WRITABLE_ATTRS:
            await super().write_property(attr, value, index, priority)
            return

        value_type = getattr(self, "_schedule_value_type", "analog") or "analog"
        prospective = self._prospective_schedule_config(normalized, value, index)
        columns = self._validated_schedule_columns(prospective, value_type)

        write_value = self._canonical_schedule_write_value(normalized, value, index, columns, value_type)
        await super().write_property(attr, write_value, index, priority)
        await self._after_schedule_config_write(normalized, columns)

    def _prospective_schedule_config(self, normalized: str, value, index) -> dict:
        prospective = {
            "weeklySchedule": self.weeklySchedule,
            "exceptionSchedule": self.exceptionSchedule,
            "effectivePeriod": self.effectivePeriod,
            "scheduleDefault": self.scheduleDefault,
            "priorityForWriting": self.priorityForWriting,
            "listOfObjectPropertyReferences": self.listOfObjectPropertyReferences,
        }
        if index is not None:
            if normalized != "weeklySchedule":
                # Only weeklySchedule is an array; the others are LISTs,
                # which take no indexed write.
                raise PropertyError("propertyIsNotAnArray")
            # A client (YABE) can write one day by index (1-7): splice it
            # into a copy of the current week, since everything downstream
            # expects all 7 days.
            current_week = list(prospective["weeklySchedule"] or [])
            if len(current_week) != 7 or not (1 <= index <= 7):
                raise PropertyError("invalidArrayIndex")
            current_week[index - 1] = value
            prospective["weeklySchedule"] = current_week
        else:
            prospective[normalized] = value
        return prospective

    def _validated_schedule_columns(self, prospective: dict, value_type: str) -> dict:
        # The unbuild conversions raise on malformed client data too (e.g. a
        # SpecialEventPeriod with no choice set, a wrongly typed TimeValue),
        # so they share validation's try and its clean PropertyError.
        try:
            weekly_schedule = None
            if prospective["weeklySchedule"]:
                days = [
                    unbuild_daily_schedule(day, value_type) for day in prospective["weeklySchedule"]
                ]
                # The all-empty-days placeholder means "not configured".
                weekly_schedule = None if all(not day for day in days) else days
            exception_schedule = (
                [unbuild_special_event(e, value_type) for e in prospective["exceptionSchedule"]]
                if prospective["exceptionSchedule"]
                else None
            )
            schedule_default = atomic_value_to_schedule_string(value_type, prospective["scheduleDefault"])
            effective_start, effective_end = date_range_to_effective_start_end(
                prospective["effectivePeriod"]
            )
            priority_for_writing = int(prospective["priorityForWriting"])
            write_targets = normalize_write_targets(
                unbuild_schedule_write_targets(prospective["listOfObjectPropertyReferences"])
                if prospective["listOfObjectPropertyReferences"]
                else None,
                getattr(self, "_schedule_device_instance", None),
            )

            validate_schedule_config(
                value_type,
                schedule_default,
                weekly_schedule,
                exception_schedule,
                effective_start,
                effective_end,
                priority_for_writing,
                write_targets,
                getattr(self, "stateText", None),
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise PropertyError("value-out-of-range") from exc

        columns = {
            "weekly_schedule": weekly_schedule,
            "exception_schedule": exception_schedule,
            "schedule_default": schedule_default,
            "schedule_effective_start": effective_start,
            "schedule_effective_end": effective_end,
            "schedule_priority": priority_for_writing,
            "schedule_write_targets": write_targets,
        }
        try:
            object_config.check_saved_row(
                getattr(self, "_schedule_session_factory", None),
                getattr(self, "_schedule_object_id", None),
                columns,
            )
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc
        return columns

    def _canonical_schedule_write_value(self, normalized: str, value, index, columns: dict, value_type: str):
        # Write back a rebuilt, canonical value, not the client's raw one:
        # bacpypes3's eval() assumes time-values in ascending order (see
        # _sorted_by_time), and a client (YABE) can send them unsorted, which
        # silently evaluates to the wrong value.
        if normalized == "weeklySchedule":
            # None (every day empty) becomes the all-empty-days placeholder.
            week = columns["weekly_schedule"] or [[] for _ in range(7)]
            if index is not None:
                return build_daily_schedule(week[index - 1], value_type)
            return [build_daily_schedule(day, value_type) for day in week]
        if normalized == "exceptionSchedule":
            return [build_special_event(e, value_type) for e in columns["exception_schedule"] or []]
        if normalized == "effectivePeriod":
            # A client may wildcard one side, which bacpypes3's tuple
            # comparison can't evaluate (a 255 start sorts after every date);
            # effective_period fills it in.
            return effective_period(columns["schedule_effective_start"], columns["schedule_effective_end"])
        if normalized == "listOfObjectPropertyReferences":
            # Normalized (own-device refs made local), matching what's saved.
            return build_schedule_write_targets(columns["schedule_write_targets"])
        return value

    async def _after_schedule_config_write(self, normalized: str, columns: dict) -> None:
        if normalized in ("listOfObjectPropertyReferences", "priorityForWriting"):
            # A new target or slot gets the current value now.
            self.command_local_targets()

        if normalized in ("weeklySchedule", "exceptionSchedule", "effectivePeriod", "scheduleDefault"):
            # Upstream's property monitors (-> interpret_schedule) never fire
            # for a write_property() write: the base write_property's
            # zero-arg super().__setattr__ skips local.object.Object's
            # __setattr__, which calls them. effectivePeriod/scheduleDefault
            # have no monitor at all. So re-evaluate here, cancelling the old
            # transition timer.
            reinterpret_schedule(self)

        session_factory = getattr(self, "_schedule_session_factory", None)
        object_id = getattr(self, "_schedule_object_id", None)
        if session_factory is not None and object_id is not None:
            await config_save.save_columns(
                session_factory, BacnetObject, object_id, columns, f"{normalized} for object id {object_id}"
            )


class GatedScheduleObject(
    WritePropertyAllowlistedSchedule,
    ScheduleConfigPersisted,
    WritingScheduleObject,
    ScheduleEvaluationHeld,
    ScheduleReliabilityChecked,
    _LocalScheduleObjectBase,
):
    pass


# --- Re-evaluating a running Schedule on demand -----------------------------
# Used by a config write (above), a calendar change and a device clock change.


def reinterpret_schedule(obj) -> None:
    """Re-run interpret_schedule(), for a config write, a calendar change and a
    device clock change (whose pending timer was computed against the old
    clock)."""
    obj.interpret_schedule()


def reschedule_all_schedules(app) -> None:
    """Re-evaluate every Schedule after a device clock change, rather than
    waiting (possibly hours) for timers armed against the old clock."""
    for obj in app.objectIdentifier.values():
        if isinstance(obj, _LocalScheduleObjectBase):
            reinterpret_schedule(obj)
