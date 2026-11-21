"""Proxy mappings, the object side:
a local commandable object mirrors the remote point's Present_Value and
Priority_Array; its Reliability says whether the mirror is current.
BacnetConnectorRunner drives these; ProxyForwarding (bacnet.py) is the
write side."""
from __future__ import annotations

from typing import Any, Optional, Sequence

from bacpypes3.basetypes import PriorityValue, Reliability
from bacpypes3.basetypes import OptionalUnsigned
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.errors import ExecutionError
from bacpypes3.primitivedata import Boolean, Enumerated

from . import bacpypes3_compat
from .bacnet import typed_present_value
from .bacnet_client import ClientError
from .connectors import coerce_mapped_value


def _plain(raw: Any) -> float:
    """A remote value as a number: enumerated and boolean values become
    their integer, a Schedule's AnyAtomic is unwrapped. ValueError/TypeError
    for anything that isn't a number (a CharacterString, say)."""
    if isinstance(raw, AnyAtomic):
        raw = raw.get_value()
    if isinstance(raw, (Enumerated, Boolean, bool)):
        raw = int(raw)
    return coerce_mapped_value(raw, scale=1.0, offset=0.0, value_map=None)


def _slot_value(slot: Any) -> Optional[float]:
    """One remote priority-array slot: None when empty."""
    name = bacpypes3_compat.chosen_field(slot)
    if name is None or name == "null":
        return None
    return _plain(getattr(slot, name))


def mirror(obj, present_value: Any, priority_array: Optional[Sequence[Any]]) -> None:
    """Copy the remote's state into `obj` (`priority_array` None: the remote
    has none, every local slot is emptied). Everything is cast first, so a
    bad value raises ValueError/TypeError and changes nothing.

    Slots are written past PriorityArray.__setitem__, which would recalculate
    Present_Value (two changes, two COVs per poll); Present_Value is set
    once, to the remote's own (an out-of-service remote's isn't from its
    array), past Commandable's __setattr__ (which would write slot 16), so
    its monitors fire only when it changed."""
    slots = list(priority_array) if priority_array is not None else [None] * 16
    if len(slots) != 16:
        raise ValueError(f"a priority array has 16 slots, the remote sent {len(slots)}")
    values = [None if slot is None else _slot_value(slot) for slot in slots]
    typed_slots = [
        PriorityValue(null=()) if value is None else PriorityValue(typed_present_value(obj, value))
        for value in values
    ]
    typed_value = typed_present_value(obj, _plain(present_value))
    _store(obj, typed_slots, typed_value)


def _store(obj, typed_slots, typed_value) -> None:
    """Slots, Current_Command_Priority and Present_Value, with Present_Value
    changing at most once."""
    top = next((i for i, slot in enumerate(typed_slots) if slot.null is None), None)
    bacpypes3_compat.store_priority_array_raw(
        obj,
        typed_slots,
        OptionalUnsigned(null=()) if top is None else OptionalUnsigned(unsigned=top + 1),
        typed_value,
    )


def clear_mirror(obj) -> None:
    """A proxy that's gone: every slot empty (Present_Value falls back to
    Relinquish_Default) and no fault."""
    _store(obj, [PriorityValue(null=()) for _ in range(16)], obj.relinquishDefault)
    set_reliability(obj, "no-fault-detected")


def set_reliability(obj, name: str) -> None:
    """Reliability by its kebab-case name; Status_Flags' FAULT follows it
    (bacpypes3 derives it). Only set when it changes, so its monitors don't
    fire every poll."""
    reliability = Reliability(name)
    if getattr(obj, "reliability", None) != reliability:
        obj.reliability = reliability


def forward_error(exc: ClientError) -> ExecutionError:
    """The BACnet Error a client gets when its write couldn't be passed on
    (the table in the design doc)."""
    if exc.reply == "error" and exc.error_class and exc.error_code:
        return ExecutionError(errorClass=exc.error_class, errorCode=exc.error_code)
    if exc.reply in ("reject", "abort"):
        return ExecutionError(errorClass="communication", errorCode="other")
    if exc.kind == "dcc":
        return ExecutionError(errorClass="communication", errorCode="communication-disabled")
    if exc.kind == "transport":
        return ExecutionError(errorClass="communication", errorCode="timeout")
    return ExecutionError(errorClass="property", errorCode="invalid-data-type")
