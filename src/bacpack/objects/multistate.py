"""Multi-state Input/Output/Value object classes."""

from __future__ import annotations

from bacpypes3.basetypes import PriorityValue
from bacpypes3.errors import PropertyError
from bacpypes3.local.cov import GenericCriteria
from bacpypes3.local.cmd import Commandable
from bacpypes3.local.event import ChangeOfStateEventAlgorithm
from bacpypes3.local.multistate import (
    MultiStateInputObject,
    MultiStateOutputObject,
    MultiStateValueObject,
)
from bacpypes3.local.oos import OutOfService
from bacpypes3.primitivedata import Null

from .. import bacpypes3_compat, config_save, object_config
from ..bacnet import (
    StatusFlagsMonitored,
    BacnetWriteLocked,
    PresentValueMonitored,
    ProxyForwarding,
    RelinquishDefaultWritable,
    WritePropertyAllowlisted,
    is_relinquish,
    normalized_attr,
)
from ..models import BacnetObject
from .alarm_config import MULTISTATE_ALARM_CONFIG_ATTRS, AlarmConfigWritable


class WritePropertyAllowlistedMultiState(WritePropertyAllowlisted):
    # Output and Value: both have a priority array. StateTextEditable keeps
    # stateText consistent with numberOfStates and persists it.
    _WRITABLE_ATTRS = frozenset({"presentValue", "stateText", "relinquishDefault", "outOfService"})


class WritePropertyAllowlistedMultiStateInput(WritePropertyAllowlisted):
    _WRITABLE_ATTRS = frozenset({"presentValue", "outOfService", "stateText"})


# bacpypes3 decodes a written value into the declared type but never checks
# it's legal (YABE could write 50 to a 3-state object and it stuck). 135
# requires value-out-of-range (clause 19.2.1; 12.22.5 caps Present_Value at
# Number_Of_States), so presentValue writes are validated before upstream.
def _validate_multistate_value(value, number_of_states: int) -> None:
    if not (1 <= int(value) <= number_of_states):
        raise PropertyError("value-out-of-range")


class MultiStateCOV:
    """bacpypes3 0.0.106 gives multi-state objects COVIncrementCriteria,
    which reads covIncrement (they have none): SubscribeCOV failed and the
    half-done bind broke MSO priority writes. The standard's rule is any
    change of Present_Value or Status_Flags: GenericCriteria. Must precede
    the bacpypes3 class in the MRO."""

    _cov_criteria = GenericCriteria


class MultiStateRangeValidated:
    def _check_present_value(self, value) -> None:
        """Also run by ProxyForwarding on the object a forwarded write
        lands on: its state count can change during the forward."""
        _validate_multistate_value(value, self.numberOfStates)

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) == "presentValue" and not is_relinquish(value):
            self._check_present_value(value)
        await super().write_property(attr, value, index, priority)


class StateTextEditable:
    """Makes a State_Text write a persisted config edit, kept consistent.

    Upstream accepts any length and leaves Number_Of_States alone, though
    clause 12.22.5/12.22.6 keep them in sync, and a shrink can leave
    Present_Value on a missing state. Fixes both, then saves the labels to
    the row's `state_text` column.
    """

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) != "stateText" or value is None:
            await super().write_property(attr, value, index, priority)
            return

        if index is not None:
            # `value` is one label. Index 0 (the length) is refused: it would
            # change Number_Of_States without labels for new states.
            current = [str(s) for s in self.stateText or []]
            if index == 0:
                raise PropertyError("writeAccessDenied")
            if not (1 <= index <= len(current)):
                raise PropertyError("invalidArrayIndex")
            new_state_text = current
            new_state_text[index - 1] = str(value)
        else:
            new_state_text = [str(s) for s in value]
        if not new_state_text:
            raise PropertyError("value-out-of-range")
        if len(new_state_text) < len(self.stateText or []):
            self._refuse_dropping_used_states(new_state_text)
        self._check_saved_row({"state_text": new_state_text})

        await super().write_property(attr, value, index, priority)
        self.numberOfStates = len(new_state_text)
        if self.presentValue > self.numberOfStates:
            self.presentValue = self.numberOfStates

        session_factory = getattr(self, "_state_text_session_factory", None)
        object_id = getattr(self, "_state_text_object_id", None)
        if session_factory is not None and object_id is not None:
            await config_save.save_columns(
                session_factory, BacnetObject, object_id, {"state_text": new_state_text},
                f"state_text for object id {object_id}",
            )

    def _refuse_dropping_used_states(self, new_state_text: list) -> None:
        """value-out-of-range if a shrink would drop a state the running
        object still uses: a priority slot, Relinquish_Default or an alarm
        state. Present_Value alone is clamped instead -- nothing holds it
        there once the labels are gone."""
        count = len(new_state_text)
        in_use = [_state_number(slot) for slot in getattr(self, "priorityArray", None) or []]
        in_use.append(_state_number(getattr(self, "relinquishDefault", None)))
        in_use.extend(int(v) for v in getattr(self, "alarmValues", None) or [])
        if any(state is not None and state > count for state in in_use):
            raise PropertyError("value-out-of-range")

    def _check_saved_row(self, changes: dict) -> None:
        """value-out-of-range if the saved row with `changes` wouldn't
        build: a simulator's `states`, Relinquish_Default and alarm states
        the web UI saved, which this process may not have loaded."""
        try:
            object_config.check_saved_row(
                getattr(self, "_state_text_session_factory", None),
                getattr(self, "_state_text_object_id", None),
                changes,
            )
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc


def _state_number(value):
    """A state number from a priority slot or Relinquish_Default; None for
    an empty slot or no value."""
    if isinstance(value, PriorityValue):
        field = bacpypes3_compat.chosen_field(value)
        value = getattr(value, field, None) if field else None
    if value is None or isinstance(value, Null):
        return None
    return int(value)


class GatedMultiStateInputObject(
    StatusFlagsMonitored, PresentValueMonitored,
    WritePropertyAllowlistedMultiStateInput, StateTextEditable, MultiStateCOV, MultiStateRangeValidated, OutOfService,
    MultiStateInputObject,
):
    pass


class ValidatedMultiStateOutputObject(
    StatusFlagsMonitored, WritePropertyAllowlistedMultiState,
    RelinquishDefaultWritable,
    BacnetWriteLocked,
    StateTextEditable,
    MultiStateCOV,
    MultiStateRangeValidated,
    ProxyForwarding,
    MultiStateOutputObject,
):
    pass


class MultiStateValueObjectCmd(Commandable, MultiStateValueObject):
    """Commandable Multi-state Value; bacpypes3 0.0.106 ships none.
    Built on the local MultiStateValueObject: upstream's BinaryValueObjectCmd
    skips bacpypes3.local.object.Object and so loses property monitors and COV."""

    _required = ("priorityArray", "relinquishDefault")


class ValidatedMultiStateValueObject(
    StatusFlagsMonitored, WritePropertyAllowlistedMultiState,
    RelinquishDefaultWritable,
    BacnetWriteLocked,
    StateTextEditable,
    MultiStateCOV,
    MultiStateRangeValidated,
    ProxyForwarding,
    MultiStateValueObjectCmd,
):
    pass


# bacpypes3 0.0.106 has no multi-state IR (intrinsic reporting) classes, so
# these follow its BinaryInputObjectIR pattern. ChangeOfStateEventAlgorithm
# binds pAlarmValues to alarmValues for every non-binary type. No Multi-state
# Output: it needs CommandFailure, which bacpypes3 doesn't implement.
class MultiStateInputObjectIR(MultiStateInputObject):
    _event_algorithm: ChangeOfStateEventAlgorithm
    _required = (
        "timeDelay",
        "notificationClass",
        "alarmValues",
        "eventEnable",
        "ackedTransitions",
        "notifyType",
        "eventTimeStamps",
        "eventDetectionEnable",
    )
    _optional = (
        "eventMessageTexts",
        "eventMessageTextsConfig",
        "eventAlgorithmInhibitReference",
        "timeDelayNormal",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        bacpypes3_compat.attach_event_algorithm(self, ChangeOfStateEventAlgorithm(None, self))


class MultiStateValueObjectIR(MultiStateValueObjectCmd):
    _event_algorithm: ChangeOfStateEventAlgorithm
    _required = (
        "timeDelay",
        "notificationClass",
        "alarmValues",
        "eventEnable",
        "ackedTransitions",
        "notifyType",
        "eventTimeStamps",
        "eventDetectionEnable",
    )
    _optional = (
        "eventMessageTexts",
        "eventMessageTextsConfig",
        "eventAlgorithmInhibitReference",
        "timeDelayNormal",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        bacpypes3_compat.attach_event_algorithm(self, ChangeOfStateEventAlgorithm(None, self))


class GatedMultiStateInputObjectIR(
    StatusFlagsMonitored, PresentValueMonitored,
    WritePropertyAllowlistedMultiStateInput,
    AlarmConfigWritable,
    StateTextEditable,
    MultiStateCOV,
    MultiStateRangeValidated,
    OutOfService,
    MultiStateInputObjectIR,
):
    _WRITABLE_ATTRS = WritePropertyAllowlistedMultiStateInput._WRITABLE_ATTRS | MULTISTATE_ALARM_CONFIG_ATTRS


class ValidatedMultiStateValueObjectIR(
    StatusFlagsMonitored, WritePropertyAllowlistedMultiState,
    RelinquishDefaultWritable,
    AlarmConfigWritable,
    BacnetWriteLocked,
    StateTextEditable,
    MultiStateCOV,
    MultiStateRangeValidated,
    ProxyForwarding,
    MultiStateValueObjectIR,
):
    _WRITABLE_ATTRS = WritePropertyAllowlistedMultiState._WRITABLE_ATTRS | MULTISTATE_ALARM_CONFIG_ATTRS
