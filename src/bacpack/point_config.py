"""Value rules for analog, binary and multi-state objects: units,
Relinquish_Default and alarming config. No object classes, so object_config
can import it."""
from __future__ import annotations

import math
from typing import List, Optional

from bacpypes3.basetypes import EngineeringUnits

from .models import ANALOG_TYPES, BINARY_TYPES, COMMANDABLE_TYPES, INPUT_TYPES, MULTISTATE_TYPES


def finite_float(value) -> float:
    """float(value), refusing NaN and +/-infinity with ValueError: float()
    accepts "nan"/"inf"/"1e999", which can't be encoded as JSON and break
    every comparison they meet."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} is not a finite number")
    return number


def validate_units(object_type: str, units: Optional[str]) -> None:
    """Raises ValueError if `units` isn't a BACnet EngineeringUnits name
    (e.g. "F" instead of "degreesFahrenheit"), which would otherwise crash
    the build. Analog objects only."""
    if object_type not in ANALOG_TYPES or not units:
        return
    try:
        EngineeringUnits(units)
    except ValueError:
        raise ValueError(
            f"{units!r} is not a valid BACnet engineering unit name "
            "(e.g. 'degreesFahrenheit', 'percent', 'noUnits')"
        ) from None


def list_engineering_units() -> List[str]:
    """All valid EngineeringUnits names (for /api/units). The Enumerated type
    has no public name->value mapping, so filter its attributes to the int
    ones (the rest are dunders and methods)."""
    return sorted(
        name
        for name in dir(EngineeringUnits)
        if not name.startswith("_") and isinstance(getattr(EngineeringUnits, name), int)
    )


def validate_relinquish_default(
    object_type: str, state_text: Optional[list], value: Optional[str]
) -> None:
    """Raises ValueError if `value` isn't a legal relinquish-default for
    `object_type`; no-op for no value or a type without a priority array
    (COMMANDABLE_TYPES). Stored as text: a float string (analog),
    "active"/"inactive" (binary), a 1-based state number (multi-state).
    """
    if value is None or object_type not in COMMANDABLE_TYPES:
        return
    if not _is_legal_point_value(object_type, state_text, value):
        raise ValueError(
            f"{value!r} is not a valid relinquish-default for a {object_type} object"
        )


def validate_start_value(
    object_type: str, state_text: Optional[list], value: Optional[str]
) -> None:
    """Raises ValueError if `value` isn't a legal start value for an input
    object, in the same stored forms as Relinquish_Default; no-op for no
    value or a type that isn't an input."""
    if value is None or object_type not in INPUT_TYPES:
        return
    if not _is_legal_point_value(object_type, state_text, value):
        raise ValueError(f"{value!r} is not a valid start-value for a {object_type} object")


def _is_legal_point_value(object_type: str, state_text: Optional[list], value: str) -> bool:
    ok = True
    if object_type in ANALOG_TYPES:
        try:
            finite_float(value)
        except ValueError:
            ok = False
    elif object_type in BINARY_TYPES:
        ok = value in ("active", "inactive")
    elif object_type in MULTISTATE_TYPES:
        number_of_states = len(state_text) if state_text else 0
        try:
            ok = 1 <= int(value) <= number_of_states
        except ValueError:
            ok = False
    return ok


# Every BacnetObject column validate_alarming_config reads besides the
# enable flag itself -- callers build its keyword arguments from this, so a
# check added there can't be skipped by a call site that forgot a field.
ALARM_VALIDATION_FIELDS = (
    "alarm_value",
    "alarm_high_limit",
    "alarm_low_limit",
    "alarm_time_delay",
    "alarm_time_delay_normal",
    "alarm_deadband",
    "alarm_event_enable",
    "alarm_limit_enable",
    "alarm_values",
    "state_text",
)


def validate_alarming_config(
    object_type: str,
    alarming_enabled: bool,
    *,
    alarm_value: Optional[bool] = None,
    alarm_high_limit: Optional[float] = None,
    alarm_low_limit: Optional[float] = None,
    alarm_time_delay: Optional[int] = None,
    alarm_time_delay_normal: Optional[int] = None,
    alarm_deadband: Optional[float] = None,
    alarm_event_enable: Optional[list] = None,
    alarm_limit_enable: Optional[list] = None,
    alarm_values: Optional[list] = None,
    state_text: Optional[list] = None,
) -> None:
    """Raises ValueError for alarming config that bacpypes3's event
    algorithms crash on:
    a binary alarm with no alarm_value (ChangeOfState raises an empty
    TypeError), an analog alarm with no limit or an enabled side with no
    limit (OutOfRange compares against None). The rest mirror the BACnet
    write checks in objects/alarm_config.py: a negative delay can't become
    an Unsigned, bit lists need their exact length, and an alarm state
    outside the object's states can never match.

    Called through object_config.check_object;
    callers pass every name in
    ALARM_VALIDATION_FIELDS.
    """
    if not alarming_enabled:
        return
    for label, delay in (("Time delay", alarm_time_delay), ("Time delay to normal", alarm_time_delay_normal)):
        if delay is not None and delay < 0:
            raise ValueError(f"{label} can't be negative")
    if alarm_deadband is not None and not (math.isfinite(alarm_deadband) and alarm_deadband >= 0):
        raise ValueError("Deadband must be zero or more")
    if alarm_event_enable is not None and len(alarm_event_enable) != 3:
        raise ValueError("Event enable needs exactly 3 entries (to-offnormal, to-fault, to-normal)")
    if object_type in ANALOG_TYPES and alarm_limit_enable is not None:
        if len(alarm_limit_enable) != 2:
            raise ValueError("Limit enable needs exactly 2 entries (low, high)")
        low_enabled, high_enabled = alarm_limit_enable
        if (low_enabled and alarm_low_limit is None) or (high_enabled and alarm_high_limit is None):
            raise ValueError("A limit is enabled on this object's Alarming tab but has no value set")
    if object_type in MULTISTATE_TYPES and alarm_values:
        number_of_states = len(state_text) if state_text else 0
        if any(not (1 <= int(v) <= number_of_states) for v in alarm_values):
            raise ValueError(f"Alarm state numbers must be between 1 and {number_of_states}")
    if object_type in BINARY_TYPES and alarm_value is None:
        raise ValueError(
            "Set an alarm value on this object's Alarming tab before enabling "
            "alarming: a binary alarm has no event algorithm without one"
        )
    if object_type in ANALOG_TYPES and alarm_high_limit is None and alarm_low_limit is None:
        raise ValueError(
            "Set a high limit or a low limit on this object's Alarming tab "
            "before enabling alarming: with neither set, the alarm could "
            "never fire"
        )
