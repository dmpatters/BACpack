"""Writable alarm configuration (limits, delays, enables, notification
routing), as on a real controller: each write is validated, applied live and
saved to its column.
"""

from __future__ import annotations

import math
from typing import Optional

from bacpypes3.basetypes import BinaryPV, EventState, TimeStamp
from bacpypes3.constructeddata import ListOf
from bacpypes3.errors import PropertyError
from bacpypes3.object import NotificationClassObject
from bacpypes3.primitivedata import BitString

from .. import bacpypes3_compat, config_save, object_config, timeutil
from ..bacnet import normalized_attr
from ..models import BacnetObject

_COMMON_ALARM_CONFIG_ATTRS = frozenset(
    {"timeDelay", "timeDelayNormal", "eventEnable", "notifyType", "notificationClass", "eventDetectionEnable"}
)
ANALOG_ALARM_CONFIG_ATTRS = _COMMON_ALARM_CONFIG_ATTRS | {"highLimit", "lowLimit", "deadband", "limitEnable"}
BINARY_ALARM_CONFIG_ATTRS = _COMMON_ALARM_CONFIG_ATTRS | {"alarmValue"}
MULTISTATE_ALARM_CONFIG_ATTRS = _COMMON_ALARM_CONFIG_ATTRS | {"alarmValues"}

# BACnet property -> the BacnetObject column it's saved to.
_COLUMNS = {
    "highLimit": "alarm_high_limit",
    "lowLimit": "alarm_low_limit",
    "deadband": "alarm_deadband",
    "limitEnable": "alarm_limit_enable",
    "alarmValue": "alarm_value",
    "alarmValues": "alarm_values",
    "timeDelay": "alarm_time_delay",
    "timeDelayNormal": "alarm_time_delay_normal",
    "eventEnable": "alarm_event_enable",
    "notifyType": "alarm_notify_type",
    "notificationClass": "notification_class_instance",
    "eventDetectionEnable": "alarm_event_detection_enable",
}


def unset_time_stamp() -> TimeStamp:
    """An Event_Time_Stamps slot with no transition yet: dateTime, every
    field unspecified."""
    return TimeStamp(dateTime=timeutil.wildcard_datetime())


def time_stamp_matches(stored: TimeStamp, given: TimeStamp) -> bool:
    """An acknowledgement's time stamp names the transition it acknowledges
    (135 13.5). Only a dateTime stamp that isn't the unset slot can match, on
    date and time to the hundredth; the day of week is derived from the date,
    and a client may send it as 255."""
    if stored.dateTime is None or given.dateTime is None:
        return False
    if stored == unset_time_stamp():
        return False

    def fields(stamp):
        return tuple(stamp.dateTime.date)[:3] + tuple(stamp.dateTime.time)

    return fields(stored) == fields(given)


def state_group_index(state) -> int:
    """The Event_Enable/Acked_Transitions/Event_Time_Stamps slot for a
    transition into `state`: normal 2, fault 1, any off-normal state 0."""
    if state == EventState.normal:
        return 2
    if state == EventState.fault:
        return 1
    return 0


def with_notification_class_slots(notification_parameters: dict, event_object) -> dict:
    """Priority and Ack_Required re-read from the Notification Class at the
    transition's slot: bacpypes3 0.0.106 indexes them by EventState number,
    but they're in transition order (to-offnormal, to-fault, to-normal), so
    to-offnormal and to-normal were swapped. Ack notifications carry no
    Ack_Required."""
    notification_class = bacpypes3_compat.notification_class_object(event_object)
    if notification_class is None:
        # Not linked: bacpypes3's own send can't reach anyone either.
        return notification_parameters
    index = state_group_index(notification_parameters["toState"])
    params = dict(notification_parameters)
    params["priority"] = notification_class.priority[index]
    if "ackRequired" in params:
        params["ackRequired"] = notification_class.ackRequired[index]
    return params


def transition_needs_ack(obj, index: int) -> bool:
    """Whether a transition into state group `index` clears
    Acked_Transitions[index]: only when reported (Event_Enable bit on) and
    the Notification Class requires an ack."""
    notification_class = bacpypes3_compat.notification_class_object(obj)
    return (
        bool(obj.eventEnable[index])
        and notification_class is not None
        and bool(notification_class.ackRequired[index])
    )


def withhold_disabled_notifications(algorithm) -> None:
    """bacpypes3 never consults Event_Enable. The transition still happens;
    only the send is withheld. Wraps the instance, since bacpypes3 builds
    the analog objects' algorithm itself."""
    send = algorithm.send_notifications
    event_object = algorithm.monitoring_object or algorithm.monitored_object

    async def send_if_enabled(notification_parameters) -> None:
        if not event_object.eventEnable[state_group_index(notification_parameters["toState"])]:
            return
        await send(with_notification_class_slots(notification_parameters, event_object))

    algorithm.send_notifications = send_if_enabled


def _plain(name: str, typed):
    """The column form of an already-cast property value."""
    if name in ("highLimit", "lowLimit", "deadband"):
        return float(typed)
    if name in ("limitEnable", "eventEnable"):
        return [bool(bit) for bit in typed]
    if name == "alarmValue":
        # The BinaryPV cast keeps any enumerated number; only 0 and 1 are states.
        if typed.attr not in ("active", "inactive"):
            raise ValueError(f"alarmValue {int(typed)} is not a binary state")
        return typed.attr == "active"
    if name == "alarmValues":
        return [int(v) for v in typed]
    if name == "notifyType":
        return typed.attr
    if name == "eventDetectionEnable":
        return bool(typed)
    return int(typed)  # timeDelay, timeDelayNormal, notificationClass


def _reject() -> None:
    raise PropertyError("value-out-of-range")


# The exact bit count of each BIT STRING alarm property.
_BIT_LENGTHS = {"eventEnable": 3, "limitEnable": 2}


class AlarmConfigWritable:
    """Goes right after the allowlist mixin, which still rejects non-alarm
    properties first. Validates before applying, so a rejected write leaves
    the object and the DB untouched."""

    # A class attribute so TrendLogConfigWritable can reuse these rules
    # against the trend_* columns.
    _ALARM_CONFIG_COLUMNS = _COLUMNS

    @classmethod
    def get_property_type(cls, attr):
        # Requests decode as this type, and EventTransitionBits/LimitEnable
        # pad a short bit string ([1, 1] -> [1, 1, 0]). A plain BitString
        # keeps the sent length and encodes the same.
        element = super().get_property_type(attr)
        if element is not None and normalized_attr(cls, attr) in _BIT_LENGTHS:
            return BitString
        return element

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        name = normalized_attr(self, attr)
        if name not in self._ALARM_CONFIG_COLUMNS:
            await super().write_property(attr, value, index, priority)
            return
        # None of these is a BACnet array (the bitstrings and Alarm_Values
        # are a BIT STRING and a LIST).
        if index is not None:
            raise PropertyError("propertyIsNotAnArray")
        # Checked before the cast below, which would pad a short one.
        if name in _BIT_LENGTHS and isinstance(value, BitString) and len(value) != _BIT_LENGTHS[name]:
            _reject()

        element = bacpypes3_compat.required_element_class(self, name)
        try:
            typed = element(element.cast(value))
            plain = _plain(name, typed)
        except (TypeError, ValueError, AttributeError) as exc:
            raise PropertyError("value-out-of-range") from exc
        self._validate_alarm_config(name, plain)
        try:
            object_config.check_saved_row(
                getattr(self, "_alarm_config_session_factory", None),
                getattr(self, "_alarm_config_object_id", None),
                {self._ALARM_CONFIG_COLUMNS[name]: plain},
            )
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc

        # Not super().write_property: its super().__setattr__ skips the local
        # Object.__setattr__ where property monitors fire, so the event
        # algorithm wouldn't re-run. Nothing further down the MRO handles
        # these properties.
        setattr(self, name, typed)
        self._apply_alarm_config(name)
        await self._save_alarm_config(name, plain)

    def _validate_alarm_config(self, name: str, plain) -> None:
        # NaN would save as NULL (the device then can't start) and infinity
        # breaks the objects API's JSON.
        if name in ("highLimit", "lowLimit", "deadband") and not math.isfinite(plain):
            _reject()
        # The Unsigned cast refuses negatives but accepts 2**32 and up, which
        # then can't be read back and makes the API refuse the row.
        if name in ("notificationThreshold", "timeDelay", "timeDelayNormal") and not (0 <= plain <= 4294967295):
            _reject()
        if name == "deadband" and plain < 0:
            _reject()
        # The NotifyType cast also takes ackNotification and unknown numbers;
        # this property only allows ALARM or EVENT.
        if name == "notifyType" and plain not in ("alarm", "event"):
            _reject()
        if name == "eventEnable" and len(plain) != 3:
            _reject()
        if name == "limitEnable":
            # The LimitEnable cast accepts any length, so check it here.
            if len(plain) != 2:
                _reject()
            low_enabled, high_enabled = plain
            # Enabling a side with no limit crashes OutOfRangeEventAlgorithm
            # (a None comparison inside the event loop).
            if (low_enabled and self.lowLimit is None) or (high_enabled and self.highLimit is None):
                _reject()
        if name == "alarmValues" and any(not (1 <= v <= self.numberOfStates) for v in plain):
            _reject()
        if name == "notificationClass" and self._find_notification_class(plain) is None:
            _reject()

    def _apply_alarm_config(self, name: str) -> None:
        """Live effects bacpypes3 doesn't apply itself. Limits, deadband,
        limit enable, time delays and detection enable are monitored
        parameters, so the write itself already re-runs the algorithm."""
        if name == "alarmValue":
            # Same shape ChangeOfStateEventAlgorithm.__init__ builds for binary.
            self._replace_alarm_values(ListOf(BinaryPV)([self.alarmValue]))
        elif name == "alarmValues":
            self._replace_alarm_values(self.alarmValues)
        elif name == "notificationClass":
            # Object._post_init links _notification_class_object once, at
            # startup; notifications and GetEventInformation read the link.
            bacpypes3_compat.link_notification_class(self, self._find_notification_class(int(self.notificationClass)))
        elif name == "eventDetectionEnable" and not self.eventDetectionEnable:
            # bacpypes3 goes Normal on its next run but leaves
            # Acked_Transitions/Event_Time_Stamps, and BACpack's ack monitor
            # would mark to-normal unacknowledged. Go Normal here, then reset
            # both. Also cancel a transition waiting out Time_Delay: the
            # disabled branch never does.
            algorithm = bacpypes3_compat.event_algorithm(self)
            if bacpypes3_compat.has_pending_transition(algorithm):
                algorithm.state_transition_cancel()
            self.eventState = EventState.normal
            self.ackedTransitions = [True, True, True]
            self.eventTimeStamps = [unset_time_stamp() for _ in range(3)]

    def _replace_alarm_values(self, alarm_values) -> None:
        # ChangeOfStateEventAlgorithm binds pAlarmValues as a constant, not a
        # monitored property, so it keeps the old list after a write. Swap it
        # and schedule a re-evaluation the way Parameter.property_change does.
        algorithm = bacpypes3_compat.event_algorithm(self)
        algorithm.pAlarmValues = alarm_values
        bacpypes3_compat.schedule_algorithm_run(algorithm)

    def _find_notification_class(self, number: int) -> Optional[NotificationClassObject]:
        app = bacpypes3_compat.object_app(self)
        if app is None:
            return None
        for obj in app.objectIdentifier.values():
            if isinstance(obj, NotificationClassObject) and obj.notificationClass == number:
                return obj
        return None

    async def _save_alarm_config(self, name: str, plain) -> None:
        session_factory = getattr(self, "_alarm_config_session_factory", None)
        object_id = getattr(self, "_alarm_config_object_id", None)
        if session_factory is None or object_id is None:
            return
        await config_save.save_columns(
            session_factory, BacnetObject, object_id, {self._ALARM_CONFIG_COLUMNS[name]: plain},
            f"{name} for object id {object_id}",
        )
