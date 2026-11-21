"""Write-path mixins shared by BACpack's object classes
and the internal feed/manual write helpers. Building objects and the
Application from config lives in `object_builder.py`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from bacpypes3.basetypes import PriorityValue, PropertyIdentifier, StatusFlags
from bacpypes3.errors import PropertyError
from bacpypes3.primitivedata import Null, Real

from . import bacpypes3_compat, config_save, log_names, object_config
from .models import ANALOG_TYPES, BINARY_TYPES, BacnetObject
from .point_config import validate_relinquish_default

logger = logging.getLogger(__name__)


def normalized_attr(obj, attr) -> Any:
    """bacpypes3's write_property() takes `attr` as a name or (off the wire)
    a PropertyIdentifier int; normalize to the name."""
    if isinstance(attr, int):
        return bacpypes3_compat.property_name(obj, attr)
    return attr


def is_relinquish(value) -> bool:
    """True for a presentValue relinquish in either form: internal callers
    pass `None`, while a real WriteProperty relinquish decodes off the wire
    as a `Null` instance (application._write_one). `Null() is None` is
    False, so checking only `value is None` misses the real-client case
    (a range validator then crashes on `int(Null())`)."""
    return value is None or isinstance(value, Null)


class WritePropertyAllowlisted:
    """Rejects a BACnet WriteProperty to any property outside a per-class
    allowlist with writeAccessDenied (clause 15.9), as a real device does.
    bacpypes3's Object.write_property() has no notion of a read-only
    property, so without this any client could rewrite objectName, units,
    reliability etc. on every object.

    Listed first (leftmost) in every object class's bases, so a disallowed
    attr never reaches the other write mixins. The allowlist is fixed per
    class, not per object. Relinquish_Default and Out_Of_Service are in the
    base set because real devices allow writing them; ask the user before
    widening it further.
    """

    _WRITABLE_ATTRS: frozenset = frozenset({"presentValue", "relinquishDefault", "outOfService"})

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) not in self._WRITABLE_ATTRS:
            raise PropertyError("writeAccessDenied")
        await super().write_property(attr, value, index, priority)


class WritePropertyAllowlistedInput(WritePropertyAllowlisted):
    # No relinquishDefault (no priority array). outOfService stays writable:
    # it's how a technician takes manual control of an Input.
    _WRITABLE_ATTRS = frozenset({"presentValue", "outOfService"})


class RelinquishDefaultWritable:
    """A Relinquish_Default write over BACnet: validated, recomputes
    Present_Value (bacpypes3's Commandable doesn't), and saved to config.
    Placed right after the allowlist; allowed regardless of bacnet_writable."""

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) != "relinquishDefault":
            await super().write_property(attr, value, index, priority)
            return
        plain = self._relinquish_default_text(value)
        try:
            object_config.check_saved_row(
                getattr(self, "_relinquish_default_session_factory", None),
                getattr(self, "_relinquish_default_object_id", None),
                {"relinquish_default": plain},
            )
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc
        await super().write_property(attr, value, index, priority)
        # Commandable only recomputes on a priority-array change; with no
        # slot set, Present_Value is the relinquish default.
        self.recalculating()
        await self._save_relinquish_default(plain)

    def _relinquish_default_text(self, value) -> str:
        """The column form ("42.5", "active"/"inactive", "3"), validated the
        way the API validates it; value-out-of-range otherwise."""
        object_type = str(self.objectIdentifier[0])
        if is_relinquish(value):
            raise PropertyError("value-out-of-range")
        try:
            if object_type in ANALOG_TYPES:
                plain = str(float(value))
            elif object_type in BINARY_TYPES:
                number = int(value)
                if number not in (0, 1):
                    raise ValueError(number)
                plain = "active" if number == 1 else "inactive"
            else:
                plain = str(int(value))
            state_text = [str(text) for text in (getattr(self, "stateText", None) or [])]
            validate_relinquish_default(object_type, state_text, plain)
        except (TypeError, ValueError) as exc:
            raise PropertyError("value-out-of-range") from exc
        return plain

    async def _save_relinquish_default(self, plain: str) -> None:
        session_factory = getattr(self, "_relinquish_default_session_factory", None)
        object_id = getattr(self, "_relinquish_default_object_id", None)
        if session_factory is None or object_id is None:
            return
        await config_save.save_columns(
            session_factory, BacnetObject, object_id, {"relinquish_default": plain},
            f"relinquish_default for object id {object_id}",
        )


class PresentValueMonitored:
    """Fires Present_Value's monitors for a write_property() on an object
    without a priority array (the inputs). bacpypes3 stores that value via a
    super().__setattr__ that skips the code calling _property_monitors, so
    COV, intrinsic alarms and COV trending would never see it (schedule.py
    works around the same gap). Commandable objects don't need it.
    Listed first, so it sees the value before and after every other mixin."""

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) != "presentValue" or getattr(self, "priorityArray", None) is not None:
            await super().write_property(attr, value, index, priority)
            return
        before = self.presentValue
        await super().write_property(attr, value, index, priority)
        after = self.presentValue
        if after != before:
            for fn in list(bacpypes3_compat.property_monitors(self, "presentValue")):
                fn(before, after)


_STATUS_FLAG_SOURCES = ("eventState", "reliability", "outOfService")


class StatusFlagsMonitored:
    """Fires Status_Flags' monitors (COV detection's) when the flags change,
    since 135 13.1 wants a notification for a Status_Flags-only change.
    bacpypes3 computes statusFlags on read and never sets it, so its
    monitors never fire on their own. An attribute change reaches the
    source properties' monitors; a write_property() write (a client's
    Out_Of_Service) skips those, so each write is checked too. Listed
    first, so it sees the flags after every other mixin."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status_flags_seen = list(self.statusFlags)
        for prop in _STATUS_FLAG_SOURCES:
            if bacpypes3_compat.element_class(self, prop) is not None:
                bacpypes3_compat.property_monitors(self, prop).append(self._status_flag_source_changed)

    def _status_flag_source_changed(self, old_value, new_value) -> None:
        self._check_status_flags()

    def _check_status_flags(self) -> None:
        old, new = self._status_flags_seen, list(self.statusFlags)
        if new == old:
            return
        self._status_flags_seen = new
        for fn in list(bacpypes3_compat.property_monitors(self, "statusFlags")):
            fn(StatusFlags(old), StatusFlags(new))

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        await super().write_property(attr, value, index, priority)
        self._check_status_flags()


# The object BACpack itself is writing in the current task (see
# internal_write). A ContextVar, not an object attribute: a proxy's write
# awaits the remote for up to 2.5 s, and a client's write to the same object
# meanwhile runs in its own task and must not see the bypass. Overlapping
# internal writes can't clear each other's bypass either.
_internal_write: ContextVar = ContextVar("bacpack_internal_write", default=None)


@contextmanager
def internal_write(obj):
    """Marks writes to `obj` in this task as BACpack's own, not a client's:
    BacnetWriteLocked and ProxyForwarding let them through."""
    token = _internal_write.set(obj)
    try:
        yield
    finally:
        _internal_write.reset(token)


def _is_internal_write(obj) -> bool:
    return _internal_write.get() is obj


class BacnetWriteLocked:
    """Rejects a client's presentValue write, relinquishes included, when
    the object's `bacnet_writable` config flag is False (the inverse of
    bacpypes3's OutOfService mixin). BACpack's own writes go through the
    same write_property() and pass by wrapping it in `internal_write(obj)`.
    """

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if (
            normalized_attr(self, attr) == "presentValue"
            and not getattr(self, "_bacnet_writable", True)
            and not _is_internal_write(self)
        ):
            raise PropertyError("writeAccessDenied")
        await super().write_property(attr, value, index, priority)


def _current_object(obj, app):
    """The object serving `obj`'s row now, or None. A live swap can retire
    `obj` while a proxy forward is awaited; `app` is the Application `obj`
    was in before the await (None: never added, so `obj` itself). Found by
    saved row id and object type, so a renumbered object still gets its
    write but a row re-typed by a direct DB edit doesn't; an unsaved row
    falls back to its identifier."""
    if app is None:
        return obj
    row_id = getattr(obj, "_row_id", None)
    if row_id is None:
        return app.get_object_id(obj.objectIdentifier)
    object_type = obj.objectIdentifier[0]
    return next(
        (
            o
            for o in app.objectIdentifier.values()
            if getattr(o, "_row_id", None) == row_id and o.objectIdentifier[0] == object_type
        ),
        None,
    )


class ProxyForwarding:
    """A proxy mapping's write path.
    While a proxy is active, BacnetConnectorRunner sets `_proxy_forward`: an
    async callable(value, priority) that writes the remote point and raises
    ExecutionError on failure. A Present_Value write goes to the remote
    first and lands locally only once the remote took it. Placed after the
    allowlist, BacnetWriteLocked and the range validators, so those refuse
    before anything is sent. The mirror sets the priority array directly
    and never comes through here.

    If the proxy is removed while the remote answers, nothing lands locally
    (a stray slot would outrank the next feed). If a live swap replaced the
    object meanwhile, the value lands on the object serving its row now,
    after re-checking it fits (warning and skip if not). A renumber is a
    remove + add that carries no hook, so a renumbered object takes the
    value only if the runner re-attached its hook before the forward
    returned; otherwise nothing lands locally."""

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        forward = getattr(self, "_proxy_forward", None)
        if forward is not None and index is None and normalized_attr(self, attr) == "presentValue":
            app = bacpypes3_compat.object_app(self)
            await forward(None if is_relinquish(value) else value, 16 if priority is None else priority)
            # `is None`, not `is not forward`: the runner attaches a fresh
            # hook every tick, so a still-live proxy's hook can change
            # under a forward.
            target = _current_object(self, app)
            if target is None or getattr(target, "_proxy_forward", None) is None:
                return
            check = getattr(target, "_check_present_value", None)
            if check is not None and not is_relinquish(value):
                try:
                    check(value)
                except PropertyError as exc:
                    logger.warning(
                        "%s: the remote took %s at priority %s, but it no longer fits this object (%s); "
                        "not applied locally",
                        log_names.live_object_ref(target), value, 16 if priority is None else priority, exc,
                    )
                    return
            if target is not self:
                # Skip the checks already passed and don't forward twice.
                if isinstance(target, ProxyForwarding):
                    await super(ProxyForwarding, target).write_property(attr, value, index, priority)
                return
        await super().write_property(attr, value, index, priority)


def relinquish_default_for(object_type: str, value: str):
    """A validated relinquish_default string as the raw Python value
    build_bacnet_object passes for that type."""
    if object_type in ANALOG_TYPES:
        return float(value)
    if object_type in BINARY_TYPES:
        return value
    return int(value)


def _class_default_present_value(object_type: str):
    """The presentValue a freshly built Input gets; write_feed_value(obj,
    None) reverts an Input to it, having no priority slot to clear."""
    if object_type in ANALOG_TYPES:
        return 0.0
    if object_type in BINARY_TYPES:
        return "inactive"
    return 1  # multi-state


# The internal feed writes at a fixed priority, manual edits at a higher-precedence one.
FEED_PRIORITY = 10
MANUAL_PRIORITY = 8


def typed_present_value(obj, value):
    """Cast a plain Python value to `obj`'s presentValue type. A
    whole-number float becomes an int for non-Real types: BinaryPV/Unsigned
    reject any float, and connectors always produce floats."""
    element_class = bacpypes3_compat.required_element_class(obj, "presentValue")
    if isinstance(value, float) and value.is_integer() and not issubclass(element_class, Real):
        value = int(value)
    return element_class(element_class.cast(value))


def _relinquish(obj, priority: int) -> None:
    """Clear a priority-array slot, working around a bacpypes3 bug:
    Commandable.write_property() builds `PriorityValue(value)`, and
    `PriorityValue(None)` means "no choice given", not null, so
    recalculating() then fails on it. Only `PriorityValue(null=())` reads
    back as empty. Setting the slot directly still triggers
    recalculating() via PriorityArray.__setitem__.
    """
    obj.priorityArray[priority - 1] = PriorityValue(null=())


async def write_feed_value(obj, value, forward: bool = True) -> bool:
    """Write a value from the internal feed (simulator or connector);
    False if skipped. Inputs are written only while not out of service;
    Output/Value objects always, at FEED_PRIORITY. `value=None` relinquishes the feed's slot.

    On a proxy object a relinquish goes to the remote first. `forward=False`
    keeps it local: the BACnet connector releasing its own read feed must
    not clear the remote's priority 10.
    """
    if not hasattr(obj, "priorityArray"):
        if obj.outOfService:
            return False
        if value is None:
            # Every presentValue caster raises TypeError on None, which
            # would kill the connector scheduler task.
            value = _class_default_present_value(str(obj.objectIdentifier[0]))
        obj.presentValue = typed_present_value(obj, value)
        return True

    if value is None:
        # _relinquish skips write_property, so the proxy hook is called here.
        hook = getattr(obj, "_proxy_forward", None) if forward else None
        if hook is not None:
            app = bacpypes3_compat.object_app(obj)
            await hook(None, FEED_PRIORITY)
            obj = _current_object(obj, app)  # a live swap may have replaced it
            if obj is None:
                return True
        _relinquish(obj, FEED_PRIORITY)
        return True

    with internal_write(obj):
        await obj.write_property(
            PropertyIdentifier.presentValue,
            typed_present_value(obj, value),
            priority=FEED_PRIORITY,
        )
    return True


async def write_manual_value(obj, value) -> None:
    """A manual write at MANUAL_PRIORITY on a commandable object;
    `value=None` relinquishes that slot. Inputs are overridden through
    Out_Of_Service instead."""
    if not hasattr(obj, "priorityArray"):
        raise TypeError(f"{log_names.live_object_ref(obj)} is not commandable, has no priority array")

    if value is None:
        # _relinquish skips write_property, so the proxy hook is called here.
        forward = getattr(obj, "_proxy_forward", None)
        if forward is not None:
            app = bacpypes3_compat.object_app(obj)
            await forward(None, MANUAL_PRIORITY)
            obj = _current_object(obj, app)  # a live swap may have replaced it
            if obj is None:
                return
        _relinquish(obj, MANUAL_PRIORITY)
        return

    with internal_write(obj):
        await obj.write_property(
            PropertyIdentifier.presentValue,
            typed_present_value(obj, value),
            priority=MANUAL_PRIORITY,
        )
