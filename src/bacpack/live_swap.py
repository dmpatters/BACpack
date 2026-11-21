"""Replacing one live BACnet object with a rebuilt one on a running
Application: retire(), carry_runtime()
(an edited column takes the new config value, everything else keeps its
runtime value), attach(). live_config.ObjectReloader decides what to swap.
No DB access here."""
from __future__ import annotations

import logging

from bacpypes3.basetypes import PriorityValue
from bacpypes3.local.cmd import Commandable, PriorityArray
from bacpypes3.local.schedule import ScheduleObject

from . import bacpypes3_compat, log_names
from .models import INPUT_TYPES
from .objects.schedule import WritingScheduleObject

logger = logging.getLogger(__name__)



def _has(obj, prop: str) -> bool:
    return bacpypes3_compat.element_class(obj, prop) is not None


def _label(obj) -> str:
    return log_names.live_object_ref(obj)


def retire(app, obj) -> list:
    """Take `obj` out of `app`. Returns its COV subscriptions for attach();
    a deleted object's are simply dropped -- the client's next renew gets
    unknown-object, as from a real controller."""
    taken = bacpypes3_compat.take_cov_subscriptions(app, obj)
    if isinstance(obj, ScheduleObject):
        bacpypes3_compat.cancel_pending_interpretation(obj)
    algorithm = bacpypes3_compat.event_algorithm(obj)
    if algorithm is not None:
        if bacpypes3_compat.has_pending_transition(algorithm):
            algorithm.state_transition_cancel()
        bacpypes3_compat.cancel_algorithm_run(algorithm)
    app.delete_object(obj)
    app.untrack_trend_log(obj)
    return taken


def attach(app, obj, taken: list) -> None:
    app.add_object(obj)
    app.track_trend_log(obj)
    bacpypes3_compat.restore_cov_subscriptions(app, obj, taken)


def restore_order(app, before: list) -> None:
    """Object_List is app.objectIdentifier's key order, and retire + attach
    moves a swapped object to the end. Put every identifier in `before`
    back in its place; new ones follow in attach order (sorted is stable)."""
    rank = {key: i for i, key in enumerate(before)}
    items = sorted(app.objectIdentifier.items(), key=lambda item: rank.get(item[0], len(rank)))
    app.objectIdentifier.clear()
    app.objectIdentifier.update(items)


def start_alarm_evaluation(obj) -> None:
    """One evaluation of `obj`'s intrinsic alarm algorithm, on the next loop
    tick: bacpypes3 evaluates only when a bound property changes, so an
    object built already past a limit would stay `normal`."""
    algorithm = bacpypes3_compat.event_algorithm(obj)
    if algorithm is not None:
        bacpypes3_compat.schedule_algorithm_run(algorithm)


def _state_count(obj):
    return int(obj.numberOfStates) if _has(obj, "numberOfStates") and obj.numberOfStates is not None else None


def _kept_slot(new, index: int, slot):
    """`slot` as it can stand on `new`: a multi-state value naming a state
    `new` no longer has is dropped (logged), not carried."""
    states = _state_count(new)
    field = bacpypes3_compat.chosen_field(slot)
    if states is not None and field not in (None, "null"):
        value = getattr(slot, field)
        if int(value) > states:
            logger.warning(
                "%s: dropped the priority %d value %s: State_Text now has %d states",
                _label(new), index + 1, value, states,
            )
            return PriorityValue(null=())
    return slot


def carry_runtime(old, new, changed: frozenset) -> None:
    """Copy `old`'s runtime state onto `new` (not yet attached). `changed`:
    the DB columns whose edit produced `new` -- an edited column wins."""
    if "out_of_service_default" not in changed and _has(old, "outOfService") and _has(new, "outOfService"):
        if old.outOfService is not None:
            new.outOfService = old.outOfService
    if not isinstance(new, ScheduleObject) and _has(old, "reliability") and _has(new, "reliability"):
        if old.reliability is not None:
            new.reliability = old.reliability
    forward = getattr(old, "_proxy_forward", None)
    if forward is not None:
        new._proxy_forward = forward
    if hasattr(old, "_commanded_slots") and hasattr(new, "_commanded_slots"):
        # So the rebuilt schedule can release slots it no longer uses.
        new._commanded = dict(old._commanded_slots())
    if (
        isinstance(old, WritingScheduleObject)
        and isinstance(new, WritingScheduleObject)
        and "schedule_value_type" not in changed
    ):
        # So the rebuilt schedule's first evaluation writes only a real
        # change. Set before attach (present_value_changed is inert without
        # an app). Not across a value-type change: out of season it would
        # keep serving the old type.
        new.presentValue = old.presentValue
        new._in_effective_period = old._in_effective_period
        new._local_targets_commanded = old._local_targets_commanded

    if isinstance(old, Commandable) and isinstance(new, Commandable):
        slots = [_kept_slot(new, i, old.priorityArray[i]) for i in range(16)]
        new.priorityArray = PriorityArray(slots, obj=new)
        new.recalculating()
    elif str(new.objectIdentifier[0]) in INPUT_TYPES:
        value = old.presentValue
        states = _state_count(new)
        if states is not None and int(value) > states:
            logger.warning("%s: Present_Value %s not carried: State_Text now has %d states", _label(new), value, states)
        else:
            new.presentValue = value

    old_algorithm = bacpypes3_compat.event_algorithm(old)
    new_algorithm = bacpypes3_compat.event_algorithm(new)
    if old_algorithm is not None and new_algorithm is not None:
        # Event_State first: BACpack's own monitor on it clears an
        # Acked_Transitions bit, which the carried value then overwrites.
        new.eventState = old.eventState
        new.ackedTransitions = list(old.ackedTransitions)
        new.eventTimeStamps = list(old.eventTimeStamps)
    # One evaluation against the current value: a limit raised past it
    # returns to normal through a normal transition.
    start_alarm_evaluation(new)


def relink_notification_class(app, old_nc, new_nc) -> None:
    """Every live object Object._post_init linked to `old_nc` now uses
    `new_nc` (objects built in the same apply link themselves)."""
    for obj in list(app.objectIdentifier.values()):
        if bacpypes3_compat.notification_class_object(obj) is old_nc:
            bacpypes3_compat.link_notification_class(obj, new_nc)
