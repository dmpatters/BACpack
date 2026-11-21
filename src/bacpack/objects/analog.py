"""Analog Input/Output/Value object classes."""

from __future__ import annotations

from bacpypes3.local.analog import (
    AnalogInputObject,
    AnalogInputObjectIR,
    AnalogOutputObject,
    AnalogOutputObjectIR,
    AnalogValueObjectCmd,
    AnalogValueObjectIR,
)
from bacpypes3.local.oos import OutOfService

from ..bacnet import (
    StatusFlagsMonitored,
    BacnetWriteLocked,
    PresentValueMonitored,
    ProxyForwarding,
    RelinquishDefaultWritable,
    WritePropertyAllowlisted,
    WritePropertyAllowlistedInput,
)
from .alarm_config import ANALOG_ALARM_CONFIG_ATTRS, AlarmConfigWritable


# OutOfService: a presentValue write is refused unless outOfService is true
class GatedAnalogInputObject(StatusFlagsMonitored, PresentValueMonitored, WritePropertyAllowlistedInput, OutOfService, AnalogInputObject):
    pass


class WriteLockedAnalogOutputObject(StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, BacnetWriteLocked, ProxyForwarding, AnalogOutputObject):
    pass


class WriteLockedAnalogValueObjectCmd(StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, BacnetWriteLocked, ProxyForwarding, AnalogValueObjectCmd):
    pass


# Intrinsic reporting (OutOfRange) variants, on bacpypes3's *ObjectIR classes.
class GatedAnalogInputObjectIR(
    StatusFlagsMonitored, PresentValueMonitored, WritePropertyAllowlistedInput, AlarmConfigWritable, OutOfService, AnalogInputObjectIR
):
    _WRITABLE_ATTRS = WritePropertyAllowlistedInput._WRITABLE_ATTRS | ANALOG_ALARM_CONFIG_ATTRS


class WriteLockedAnalogOutputObjectIR(
    StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, AlarmConfigWritable, BacnetWriteLocked, ProxyForwarding, AnalogOutputObjectIR
):
    _WRITABLE_ATTRS = WritePropertyAllowlisted._WRITABLE_ATTRS | ANALOG_ALARM_CONFIG_ATTRS


class WriteLockedAnalogValueObjectCmdIR(
    StatusFlagsMonitored, WritePropertyAllowlisted, RelinquishDefaultWritable, AlarmConfigWritable, BacnetWriteLocked, ProxyForwarding, AnalogValueObjectIR
):
    _WRITABLE_ATTRS = WritePropertyAllowlisted._WRITABLE_ATTRS | ANALOG_ALARM_CONFIG_ATTRS
