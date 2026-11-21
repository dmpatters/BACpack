"""Binary Input/Output/Value object classes."""

from __future__ import annotations

from bacpypes3.errors import PropertyError
from bacpypes3.local.binary import (
    BinaryInputObject,
    BinaryInputObjectIR,
    BinaryOutputObject,
    BinaryValueObject,  # local variant that DOES include Object, unlike BinaryValueObjectCmd
)
from bacpypes3.local.cmd import Commandable
from bacpypes3.local.event import ChangeOfStateEventAlgorithm
from bacpypes3.local.oos import OutOfService

from .. import bacpypes3_compat
from ..bacnet import (
    StatusFlagsMonitored,
    PresentValueMonitored,
    BacnetWriteLocked,
    ProxyForwarding,
    RelinquishDefaultWritable,
    WritePropertyAllowlisted,
    WritePropertyAllowlistedInput,
    is_relinquish,
    normalized_attr,
)
from .alarm_config import BINARY_ALARM_CONFIG_ATTRS, AlarmConfigWritable


def _validate_binary_value(value) -> None:
    if int(value) not in (0, 1):
        raise PropertyError("value-out-of-range")


class BinaryRangeValidated:
    def _check_present_value(self, value) -> None:
        """Also run by ProxyForwarding after a forward."""
        _validate_binary_value(value)

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) == "presentValue" and not is_relinquish(value):
            self._check_present_value(value)
        await super().write_property(attr, value, index, priority)


# OutOfService: a presentValue write is refused unless outOfService is true
class GatedBinaryInputObject(
    StatusFlagsMonitored, PresentValueMonitored, WritePropertyAllowlistedInput, BinaryRangeValidated, OutOfService, BinaryInputObject
):
    pass


class ValidatedBinaryOutputObject(
    StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, BacnetWriteLocked, BinaryRangeValidated, ProxyForwarding, BinaryOutputObject
):
    pass


class ValidatedBinaryValueObjectCmd(
    StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, BacnetWriteLocked, BinaryRangeValidated, ProxyForwarding, Commandable, BinaryValueObject
):
    """Commandable on the local BinaryValueObject: upstream's
    BinaryValueObjectCmd skips bacpypes3.local.object.Object, so it has no
    property monitors or COV criteria (every SubscribeCOV refused)."""


# Intrinsic reporting (ChangeOfState) variants.
class GatedBinaryInputObjectIR(
    StatusFlagsMonitored, PresentValueMonitored, WritePropertyAllowlistedInput, AlarmConfigWritable, BinaryRangeValidated, OutOfService, BinaryInputObjectIR
):
    _WRITABLE_ATTRS = WritePropertyAllowlistedInput._WRITABLE_ATTRS | BINARY_ALARM_CONFIG_ATTRS


class BinaryValueObjectCmdIR(Commandable, BinaryValueObject):
    """Replaces upstream's BinaryValueObjectIR, unusable in 0.0.106: its
    base omits bacpypes3.local.object.Object, so EventAlgorithm.__init__
    crashes on a missing _fault_algorithm. Built on the local
    BinaryValueObject (like AnalogValueObjectCmd), with upstream's
    _required corrected: "alarmValue", not the copy-pasted "feedbackValue"
    (this type uses ChangeOfStateEventAlgorithm)."""

    _event_algorithm: ChangeOfStateEventAlgorithm
    _required = (
        "timeDelay",
        "notificationClass",
        "alarmValue",
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


class ValidatedBinaryValueObjectCmdIR(
    StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, AlarmConfigWritable, BacnetWriteLocked, BinaryRangeValidated, ProxyForwarding, BinaryValueObjectCmdIR
):
    _WRITABLE_ATTRS = WritePropertyAllowlisted._WRITABLE_ATTRS | BINARY_ALARM_CONFIG_ATTRS
