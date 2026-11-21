"""The one place BACpack reaches into bacpypes3 internals.

Module-global swaps and private-attribute access live here, each with its
reason; upstream methods BACpack classes override are listed in OVERRIDES.
tests/test_bacpypes3_compat.py fingerprints all of it, so an upgrade fails
in one place. Imports only bacpypes3 and the standard library."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import math
import time
import types
from typing import Any, NamedTuple, Optional

import bacpypes3.local.event as _event_module
import bacpypes3.local.object as _local_object_module
import bacpypes3.local.schedule as _schedule_module
from bacpypes3.apdu import ConfirmedEventNotificationRequest, UnconfirmedEventNotificationRequest
from bacpypes3.basetypes import DateRange, EventState, TimeStamp
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.local.cmd import Commandable, PriorityArray
from bacpypes3.pdu import Address, LocalBroadcast, LocalStation, RemoteBroadcast, RemoteStation
from bacpypes3.primitivedata import Date, Null, Time
from bacpypes3.service.cov import Subscription

from . import held_tasks, log_names

logger = logging.getLogger(__name__)


# Upstream definition -> (BACpack's replacement, why). The test discovers
# the real list from the class hierarchy and fails on a missing/stale entry.
_APP = "application.LoggingApplication"
OVERRIDES = {
    "bacpypes3.app.Application.get_object_id": (_APP, "resolves the device wildcard instance 4194303 for every service"),
    "bacpypes3.app.Application.get_services_supported": (_APP, "upstream sets the wrong bit for every unconfirmed service"),
    "bacpypes3.app.Application.indication": (_APP, "DeviceCommunicationControl disable, at the one dispatch point"),
    "bacpypes3.app.Application.request": (_APP, "DCC disable/disableInitiation suppress every initiated request (an I-Am excepted under disableInitiation)"),
    "bacpypes3.basetypes.TimeStamp.as_time": ("bacpypes3_compat._DateTimeStamp", "alarm time stamps carry a date (the TimeStamp swap)"),
    "bacpypes3.local.analog.AnalogInputObjectIR.__init__": ("bacnet.StatusFlagsMonitored", "Status_Flags monitors for COV (bacnet.StatusFlagsMonitored)"),
    "bacpypes3.local.analog.AnalogOutputObjectIR.__init__": ("bacnet.StatusFlagsMonitored", "Status_Flags monitors for COV (bacnet.StatusFlagsMonitored)"),
    "bacpypes3.local.analog.AnalogValueObjectIR.__init__": ("bacnet.StatusFlagsMonitored", "Status_Flags monitors for COV (bacnet.StatusFlagsMonitored)"),
    "bacpypes3.local.binary.BinaryInputObjectIR.__init__": ("bacnet.StatusFlagsMonitored", "Status_Flags monitors for COV (bacnet.StatusFlagsMonitored)"),
    "bacpypes3.local.binary.BinaryValueObject._required": (
        "objects/binary.BinaryValueObjectCmdIR",
        "upstream's binary value IR class is unusable as shipped; hand-built property list",
    ),
    "bacpypes3.local.cmd.Commandable.__init__": (
        "bacnet.StatusFlagsMonitored; objects/binary.BinaryValueObjectCmdIR, objects/multistate.MultiStateValueObjectIR",
        "attach the ChangeOfState algorithm upstream's IR classes never get; Status_Flags monitors for COV",
    ),
    "bacpypes3.local.cmd.Commandable.write_property": (
        "bacnet.WritePropertyAllowlisted/BacnetWriteLocked/RelinquishDefaultWritable/ProxyForwarding/StatusFlagsMonitored; "
        "objects/ alarm_config.AlarmConfigWritable, binary.BinaryRangeValidated, "
        "multistate.MultiStateRangeValidated/StateTextEditable",
        "write allowlist, BACnet write lock, range checks, alarm config, State_Text, Relinquish_Default, proxy forwarding, "
        "Status_Flags COV after a write",
    ),
    "bacpypes3.local.cov.COVDetection.monitored_property_reference": ("objects/trend_log.TrendLogCriteria", "Trend Log COV tracks recordCount; it has no presentValue"),
    "bacpypes3.local.cov.COVDetection.properties_reported": ("objects/trend_log.TrendLogCriteria", "Trend Log COV tracks recordCount; it has no presentValue"),
    "bacpypes3.local.cov.COVDetection.properties_tracked": ("objects/trend_log.TrendLogCriteria", "Trend Log COV tracks recordCount; it has no presentValue"),
    "bacpypes3.local.cov.COVDetection.send_cov_notifications": ("objects/trend_log.TrendLogCriteria", "re-reads the computed recordCount for a new or renewed subscriber"),
    "bacpypes3.local.device.DeviceObject.activeCovSubscriptions": ("objects/device.BacpackDeviceObject", "computed, getter-only so a write is denied"),
    "bacpypes3.local.device.DeviceObject.deviceAddressBinding": ("objects/device.BacpackDeviceObject", "computed, getter-only so a write is denied"),
    "bacpypes3.local.device.DeviceObject.localDate": ("objects/device.BacpackDeviceObject", "follows the device clock; getter-only"),
    "bacpypes3.local.device.DeviceObject.localTime": ("objects/device.BacpackDeviceObject", "follows the device clock; getter-only"),
    "bacpypes3.local.device.DeviceObject.objectList": ("objects/device.BacpackDeviceObject", "computed, getter-only so a write is denied"),
    "bacpypes3.local.device.DeviceObject.protocolObjectTypesSupported": ("objects/device.BacpackDeviceObject", "upstream hard-codes an empty bitstring"),
    "bacpypes3.local.device.DeviceObject.protocolServicesSupported": ("objects/device.BacpackDeviceObject", "reports what BACpack actually executes"),
    "bacpypes3.local.device.DeviceObject.systemStatus": ("objects/device.BacpackDeviceObject", "backup-in-progress / download-in-progress during a backup/restore session"),
    "bacpypes3.local.event.EventAlgorithm.__init__": ("objects/trend_log_reporting.TrendLogBufferReadyAlgorithm", "BUFFER_READY reporting on BACpack's SQLite-backed trend log"),
    "bacpypes3.local.event.EventAlgorithm.event_notification_parameters": ("objects/trend_log_reporting.TrendLogBufferReadyAlgorithm", "BUFFER_READY notification parameters"),
    "bacpypes3.local.event.EventAlgorithm.event_type": ("objects/trend_log_reporting.TrendLogBufferReadyAlgorithm", "BUFFER_READY event type"),
    "bacpypes3.local.event.EventAlgorithm.execute": ("objects/trend_log_reporting.TrendLogBufferReadyAlgorithm", "BUFFER_READY evaluation from the record count"),
    "bacpypes3.local.event.EventAlgorithm.send_notifications": ("objects/trend_log_reporting.TrendLogBufferReadyAlgorithm; bacpypes3_compat._send_notifications_by_weekday", "the trend-log subclass saves Last_Notify_Record when notifying; the compat replacement of the base method checks Valid_Days against today's weekday, not always Thursday"),
    "bacpypes3.local.multistate.MultiStateInputObject._cov_criteria": ("objects/multistate.MultiStateCOV", "upstream's criteria read covIncrement, which multi-state objects lack"),
    "bacpypes3.local.multistate.MultiStateInputObject._required": ("objects/multistate.MultiStateInputObjectIR", "hand-built alarming multi-state input"),
    "bacpypes3.local.multistate.MultiStateOutputObject._cov_criteria": ("objects/multistate.MultiStateCOV", "upstream's criteria read covIncrement, which multi-state objects lack"),
    "bacpypes3.local.multistate.MultiStateValueObject._cov_criteria": ("objects/multistate.MultiStateCOV", "upstream's criteria read covIncrement, which multi-state objects lack"),
    "bacpypes3.local.multistate.MultiStateValueObject._required": (
        "objects/multistate.MultiStateValueObjectCmd, MultiStateValueObjectIR",
        "commandable multi-state value; 0.0.106 ships none",
    ),
    "bacpypes3.local.object.Object.__init__": (
        "bacnet.StatusFlagsMonitored; objects/multistate.MultiStateInputObjectIR, "
        "objects/trend_log_reporting.ReportingTrendLogObject",
        "attach the event algorithm after construction; Status_Flags monitors for COV",
    ),
    "bacpypes3.local.object.Object._post_init": (
        "objects/calendar.LocalCalendarObject, objects/file.ConfigFileObject",
        "upstream reads notificationClass, which Calendar and File don't have",
    ),
    "bacpypes3.local.oos.OutOfService.write_property": (
        "bacnet.WritePropertyAllowlisted/PresentValueMonitored/StatusFlagsMonitored; "
        "objects/ alarm_config.AlarmConfigWritable, binary.BinaryRangeValidated, "
        "multistate.MultiStateRangeValidated/StateTextEditable",
        "Present_Value monitors on input writes, write allowlist, alarm config, input Present_Value range, State_Text, "
        "Status_Flags COV after a write",
    ),
    "bacpypes3.local.schedule.ScheduleObject.check_reliability": ("objects/schedule.ScheduleReliabilityChecked", "upstream takes the schedule's datatype from Schedule_Default, so a Null default reads as a configuration error, and faults any reference naming a device, so a remote write target does too; also uses Null on an unwrapped value (the Null swap must not change it)"),
    "bacpypes3.local.schedule.ScheduleObject.eval": ("objects/schedule.ScheduleEvaluationHeld", "upstream returns None outside Effective_Period and interpret_schedule crashes on it"),
    "bacpypes3.local.schedule.ScheduleObject.interpret_schedule": ("objects/schedule.WritingScheduleObject", "commands local targets on entering season; leaves one pending timer (cancel_pending_interpretation)"),
    "bacpypes3.local.schedule.ScheduleObject.present_value_changed": ("objects/schedule.WritingScheduleObject", "upstream never awaits the write-target writes"),
    "bacpypes3.object.Object.get_property_type": ("objects/alarm_config.AlarmConfigWritable", "keeps a short bit string's length so the write can be refused"),
    "bacpypes3.object.Object.write_property": (
        "bacnet.WritePropertyAllowlisted; objects/ alarm_config.AlarmConfigWritable, "
        "calendar.CalendarConfigPersisted, device.BacpackDeviceObject, "
        "notification_class.RecipientListEditable, schedule.ScheduleConfigPersisted",
        "write allowlist; config written over BACnet is saved to SQLite",
    ),
    "bacpypes3.primitivedata.ElementInterface._optional": (
        "objects/binary.BinaryValueObjectCmdIR, objects/multistate.MultiStateInputObjectIR/MultiStateValueObjectIR",
        "hand-built property lists for the IR classes",
    ),
    "bacpypes3.service.cov.ChangeOfValueServices.cov_notification": (_APP, "traffic log"),
    "bacpypes3.service.cov.ChangeOfValueServices.do_SubscribeCOVRequest": (_APP, "traffic log"),
    "bacpypes3.service.device.WhoIsIAmServices.do_IAmRequest": (_APP, "traffic log; records every I-Am in the device-info cache"),
    "bacpypes3.service.device.WhoIsIAmServices.do_WhoIsRequest": (_APP, "traffic log"),
    "bacpypes3.service.device.WhoIsIAmServices.i_am": (_APP, "traffic log"),
    "bacpypes3.service.object.ReadRangeServices.do_ReadRangeRequest": (_APP, "upstream raises NotImplementedError; ReadRange from SQLite"),
    "bacpypes3.service.object.ReadWritePropertyMultipleServices.do_ReadPropertyMultipleRequest": (_APP, "gives the loop turns while it builds a big reply; names a missing object beside ones that exist; resolves the device wildcard"),
    "bacpypes3.service.object.ReadWritePropertyMultipleServices.do_WritePropertyMultipleRequest": (_APP, "each property through _write_one, the WriteProperty checks"),
    "bacpypes3.service.object.ReadWritePropertyServices.do_ReadPropertyRequest": (_APP, "resolves the device wildcard before the ACK echoes it"),
    "bacpypes3.service.object.ReadWritePropertyServices.do_WritePropertyRequest": (_APP, "the error ASHRAE 135 names for each malformed write"),
}

# Upstream code BACpack doesn't replace but whose internals a swap or a
# helper below relies on.
DEPENDS_ON = {
    "bacpypes3.app.Application.add_object": "sets obj._app (object_app); interprets a schedule, so a put-back one needs no restart (live_config)",
    "bacpypes3.app.DeviceInfoCache.set_device_info": "records an I-Am by source address and instance; the notification send and do_IAmRequest call it",
    "bacpypes3.basetypes.DateTime.now": "calls time.time() as a module global (the device clock swap)",
    "bacpypes3.basetypes.TimeStamp.as_dateTime": "what _DateTimeStamp.as_time returns",
    "bacpypes3.local.cmd.Commandable.__setattr__": "store_priority_array_raw goes around it",
    "bacpypes3.local.cmd.PriorityArray.__setitem__": "store_priority_array_raw goes around it",
    "bacpypes3.local.cov.COVDetection.send_cov_notifications": "restore_cov_subscriptions sends one per moved subscription",
    "bacpypes3.local.cov.DetectionMonitor.property_change": "call_soon through the running loop (DeferredLoopWork)",
    "bacpypes3.local.event.EventAlgorithm._execute": "schedule_algorithm_run schedules it and it clears _execute_handle",
    "bacpypes3.local.event.EventAlgorithm.state_transition": "looks up TimeStamp as a module global (the TimeStamp swap); _transition_timeout_handle",
    "bacpypes3.local.event.EventAlgorithm.state_transition_cancel": "clears _transition_timeout_handle (has_pending_transition)",
    "bacpypes3.local.object.Algorithm.__init__": "_execute_enabled/_execute_handle (schedule_algorithm_run, cancel_algorithm_run)",
    "bacpypes3.local.object.Object.__init__": "creates _property_monitors as a defaultdict(list) (property_monitors); ensure_future(_post_init) through the running loop (DeferredLoopWork)",
    "bacpypes3.local.object.Object.__getattribute__": "looks up inspect as a module global (the getattr_static swap)",
    "bacpypes3.local.object.Object.__setattr__": (
        "calls _property_monitors[attr] with (old, new) (property_monitors); looks up inspect as a module global (the getattr_static swap)"
    ),
    "bacpypes3.local.object.Object._post_init": "links _notification_class_object at startup (notification_class_object)",
    "bacpypes3.local.object.Object.propertyList": (
        "wrapped by _property_list_without_required (the Property_List swap); looks up inspect as a module global (the getattr_static swap)"
    ),
    "bacpypes3.local.object.Parameter.property_change": "the call_soon deferral schedule_algorithm_run copies; call_soon through the running loop (DeferredLoopWork)",
    "bacpypes3.local.schedule.ScheduleObject.__init__": "queues a call_soon whose handle overwrites _interpret_schedule_handle, as the timer does, which is why interpret_schedule cancels first (cancel_pending_interpretation); call_soon through the running loop (DeferredLoopWork)",
    "bacpypes3.local.schedule.ScheduleObject.eval": "looks up match_date_range and Null as module globals (the schedule swaps); keeps one value and one next-transition slot per Event_Priority, last entry wins (ScheduleEvaluationHeld inverts that for a shared priority by reversing the list)",
    "bacpypes3.local.schedule.ScheduleObject.interpret_schedule": "arms _interpret_schedule_handle, overwriting the constructor's queued call or the previous timer (cancel_pending_interpretation)",
    "bacpypes3.local.schedule.date_in_calendar_entry": "looks up match_date_range as a module global (the wildcard swap)",
    "bacpypes3.local.schedule.match_date_range": "wrapped by _match_date_range_with_wildcards",
    "bacpypes3.primitivedata.Date.now": "calls time.time() as a module global (the device clock swap)",
    "bacpypes3.primitivedata.Time.now": "calls time.time() as a module global (the device clock swap)",
    "bacpypes3.service.cov.ChangeOfValueServices.__init__": "creates _cov_detections, keyed by object identifier (take/restore_cov_subscriptions)",
    "bacpypes3.service.cov.ChangeOfValueServices.add_subscription": "indexes _cov_detections by cov.obj_ref.objectIdentifier (restore_cov_subscriptions)",
    "bacpypes3.service.cov.Subscription.__init__": "cancel_handle, covIncrement and a call_later on obj._app (take/restore_cov_subscriptions)",
    "bacpypes3.service.device.WhoIsFuture.match": "a Who-Is for one instance resolves on its first I-Am; the notification send waits on that",
    "bacpypes3.service.object.read_property_to_result_element": "the RPM handler's per-property read; a None object gives an unknown-object element",
}


# --- Module-global swaps --------------------------------------------------
# Each replaces a name upstream looks up as a module global at call time,
# where there's no per-instance hook. Applied at import (before any object
# is built), except the device clock swap, applied at runtime.
#
# bacpypes3.local.object's `inspect` is swapped for a stand-in with a cached
# getattr_static: every property read, write and Property_List looks it up
# at call time.


def _effective_period_is_wildcarded(date_range: DateRange) -> bool:
    return tuple(date_range.startDate) == (255, 255, 255, 255) and tuple(
        date_range.endDate
    ) == (255, 255, 255, 255)


_upstream_match_date_range = _schedule_module.match_date_range


def _match_date_range_with_wildcards(date, date_range) -> bool:
    """Upstream match_date_range is a plain tuple comparison with no
    255-wildcard awareness; recognize the fully wildcarded "always in
    effect" EffectivePeriod that effective_period builds when no dates are
    set. Partial wildcards never occur (configured dates are concrete).
    """
    if _effective_period_is_wildcarded(date_range):
        return True
    return _upstream_match_date_range(date, date_range)


class _NullOrWrappedNullMeta(type):
    def __instancecheck__(cls, obj) -> bool:
        if isinstance(obj, AnyAtomic):
            obj = obj.get_value()
        return isinstance(obj, Null)


class _ScheduleNull(metaclass=_NullOrWrappedNullMeta):
    """Stands in for `Null` in bacpypes3.local.schedule. eval() tests
    `isinstance(time_value.value, Null)` for a null time-value, but the
    value is always AnyAtomic-wrapped, so it never matched and presentValue
    became Null itself. check_reliability (unwrapped value) is unaffected."""


class _DateTimeStamp(TimeStamp):
    """EventAlgorithm.state_transition stamps alarm transitions with
    TimeStamp.as_time(), the time-only choice: legal, but clients (YABE)
    show the date as 1/1/0001, and real devices send dateTime. The stamping
    sits mid-method with no hook, hence the module swap."""

    @classmethod
    def as_time(cls, when: Optional[float] = None) -> TimeStamp:
        return TimeStamp.as_dateTime(when)


# Captured before the swap below replaces it.
_UPSTREAM_PROPERTY_LIST = _local_object_module.Object.propertyList
_NOT_IN_PROPERTY_LIST = frozenset({"objectIdentifier", "objectName", "objectType", "propertyList"})


def _property_list_without_required(obj):
    """Property_List without the four properties ASHRAE 135 says it leaves
    out (upstream lists every element with a value). A class attribute
    swap, not a mixin: BACpack's object classes share no base. RPM ALL is
    unaffected (it reads _elements)."""
    listed = _UPSTREAM_PROPERTY_LIST.fget(obj)
    return type(listed)([p for p in listed if p.attr not in _NOT_IN_PROPERTY_LIST])


_ABSENT = object()
_NO_DEFAULT = object()

# (type(obj), attr) -> (obj is a class, the instance dict is consulted, the
# class's value for attr or _ABSENT, that value is a data descriptor).
_CLASS_LOOKUPS: dict = {}


def _mro_of(klass) -> tuple:
    return type.__dict__["__mro__"].__get__(klass)


def _shadowing_dict_of(klass):
    """What the first class in klass's MRO that defines `__dict__` as
    something other than its own instance-dict getset holds there, or
    _ABSENT."""
    for entry in _mro_of(klass):
        class_dict = type.__dict__["__dict__"].__get__(entry)
        if "__dict__" in class_dict:
            found = class_dict["__dict__"]
            if not (
                type(found) is types.GetSetDescriptorType and found.__name__ == "__dict__" and found.__objclass__ is entry
            ):
                return found
    return _ABSENT


def _class_value(klass, attr):
    for entry in _mro_of(klass):
        if _shadowing_dict_of(type(entry)) is _ABSENT and attr in entry.__dict__:
            return entry.__dict__[attr]
    return _ABSENT


def _class_facts(objtype: type, attr: str) -> tuple:
    if type in _mro_of(objtype):
        return True, False, _ABSENT, False
    shadow = _shadowing_dict_of(objtype)
    consults_instance = shadow is _ABSENT or type(shadow) is types.MemberDescriptorType
    value = _class_value(objtype, attr)
    is_data = (
        value is not _ABSENT
        and _class_value(type(value), "__get__") is not _ABSENT
        and (_class_value(type(value), "__set__") is not _ABSENT or _class_value(type(value), "__delete__") is not _ABSENT)
    )
    return False, consults_instance, value, is_data


def getattr_static(obj, attr, default=_NO_DEFAULT):
    """inspect.getattr_static with the part that depends only on
    (type(obj), attr) cached, so a later call is one instance-dict lookup.
    It mirrors CPython's algorithm and returns the same object, default or
    AttributeError; tests/test_bacpypes3_compat.py compares the two.

    Assumes a class's attributes along its MRO don't change after a name's
    first lookup. BACpack's only class change is the Property_List swap
    below, made at import before any object exists."""
    facts = _CLASS_LOOKUPS.get((type(obj), attr))
    if facts is None:
        facts = _CLASS_LOOKUPS[(type(obj), attr)] = _class_facts(type(obj), attr)
    is_class, consults_instance, class_result, is_data = facts
    if is_class:
        return inspect.getattr_static(obj, attr) if default is _NO_DEFAULT else inspect.getattr_static(obj, attr, default)
    instance_result = _ABSENT
    if consults_instance:
        try:
            instance_result = dict.get(object.__getattribute__(obj, "__dict__"), attr, _ABSENT)
        except AttributeError:
            pass
    if instance_result is not _ABSENT:
        if class_result is not _ABSENT and is_data:
            return class_result
        return instance_result
    if class_result is not _ABSENT:
        return class_result
    if default is not _NO_DEFAULT:
        return default
    raise AttributeError(attr)


class _InspectWithCachedLookup:
    """Stands in for `inspect` in bacpypes3.local.object: the cached
    getattr_static, everything else (iscoroutinefunction, isawaitable) the
    real module's."""

    getattr_static = staticmethod(getattr_static)
    # Bound here too: they're called on every read of a property element.
    iscoroutinefunction = staticmethod(inspect.iscoroutinefunction)
    isawaitable = staticmethod(inspect.isawaitable)

    def __getattr__(self, name: str):
        return getattr(inspect, name)


_WHO_IS_WAIT_SECONDS = 3
# A recipient that didn't answer isn't asked again for this long, so one
# that is offline costs a Who-Is and the wait once a minute, not per alarm.
_UNANSWERED_RETRY_SECONDS = 60.0


async def _ask_for_device(app, instance: int) -> Optional[Address]:
    """One Who-Is for `instance`; the address its I-Am gave, else None."""
    cache = app.device_info_cache
    try:
        i_ams = await app.who_is(instance, instance, timeout=_WHO_IS_WAIT_SECONDS)
    except Exception as exc:
        # A failure here must not end the send for the recipients after this one.
        logger.warning("Who-Is for device %d failed: %s", instance, exc)
        return None
    if not i_ams:
        return None
    # Upstream records it in a separately scheduled task; don't race it.
    await cache.set_device_info(i_ams[0])
    known = await cache.get_device_info(instance)
    return known.address if known else None


async def _device_recipient_address(app, instance: int, notifying) -> Optional[Address]:
    """The address of the device a Notification Class recipient names by
    instance: the cache's, else one Who-Is for it. None when it can't be
    reached, in which case that recipient is skipped."""
    cache = app.device_info_cache
    known = await cache.get_device_info(instance)
    if known:
        return known.address
    # Under DCC the Who-Is would be swallowed, as the notification will be.
    if getattr(app, "initiation_suppressed", False):
        return None
    unanswered = vars(app).setdefault("_unanswered_device_recipients", {})
    # Each transition's send is its own task, so alarms raised together all
    # land here at once: they wait on one Who-Is rather than each sending one.
    asking = vars(app).setdefault("_device_recipient_who_is", {})
    task = asking.get(instance)
    if task is None and unanswered.get(instance, 0.0) <= time.monotonic():
        task = asyncio.ensure_future(_ask_for_device(app, instance))
        asking[instance] = task
        task.add_done_callback(lambda _done: asking.pop(instance, None))
    if task is not None:
        # Shielded: one waiting send being cancelled mustn't cancel the others'.
        address = await asyncio.shield(task)
        if address is not None:
            unanswered.pop(instance, None)
            return address
        unanswered[instance] = time.monotonic() + _UNANSWERED_RETRY_SECONDS
    logger.warning(
        "no I-Am from device %d: its notification from %s was not sent",
        instance,
        log_names.live_object_ref(notifying),
    )
    return None


async def _send_notifications_by_weekday(self, notification_parameters) -> None:
    """EventAlgorithm.send_notifications with two changes: upstream tests
    `validDays[3]` (Thursday) every day (the weekday comes from the same
    Date.now() as the From_Time/To_Time check, so both follow the device
    clock), and a device-instance recipient it hasn't heard from is asked
    for with a Who-Is instead of being skipped. The body is otherwise
    upstream's, minus its debug logging; don't mutate recipientList here:
    the loop awaits between recipients."""
    event_initiating_object = self.monitoring_object or self.monitored_object
    notification_class_object = event_initiating_object._notification_class_object

    current_date = Date.now()
    current_time = Time.now()
    # Date's day of week is 1 (Monday) to 7 (Sunday); Date.now() always
    # sets it. 255 (unspecified) can't be told apart from any day.
    day_of_week = current_date[3]
    weekday = day_of_week - 1 if 1 <= day_of_week <= 7 else None

    recipient_futures = set()

    for destination in notification_class_object.recipientList:
        if weekday is not None and not destination.validDays[weekday]:
            continue
        if (current_time < destination.fromTime) or (current_time > destination.toTime):
            continue

        new_state = notification_parameters["toState"]
        if new_state == EventState.normal:
            new_state_index = 2
        elif new_state == EventState.fault:
            new_state_index = 1
        else:
            new_state_index = 0
        if not destination.transitions[new_state_index]:
            continue

        recipient = destination.recipient

        pdu_destination: Address
        if recipient.device:
            found = await _device_recipient_address(
                event_initiating_object._app, recipient.device[1], event_initiating_object
            )
            if found is None:
                continue
            pdu_destination = found
        elif recipient.address:
            network_number = recipient.address.networkNumber
            mac_address = recipient.address.macAddress

            if network_number == 0:
                if not mac_address:
                    pdu_destination = LocalBroadcast()
                else:
                    pdu_destination = LocalStation(mac_address)
            else:
                if not mac_address:
                    pdu_destination = RemoteBroadcast(network_number)
                else:
                    pdu_destination = RemoteStation(network_number, mac_address)
        else:
            raise RuntimeError("invalid recipient")

        notification_request: Any
        if destination.issueConfirmedNotifications:
            notification_request = ConfirmedEventNotificationRequest(**notification_parameters)
        else:
            notification_request = UnconfirmedEventNotificationRequest(**notification_parameters)

        notification_request.pduDestination = pdu_destination
        notification_request.processIdentifier = destination.processIdentifier

        recipient_futures.add(event_initiating_object._app.request(notification_request))

    if not recipient_futures:
        return

    done, pending = await asyncio.wait(recipient_futures)

    for request_future in done:
        try:
            request_exception = request_future.exception()
            if not request_exception:
                continue
        except asyncio.CancelledError:
            pass


def _install_import_time_swaps() -> None:
    _local_object_module.inspect = _InspectWithCachedLookup()  # pyright: ignore[reportAttributeAccessIssue]
    _schedule_module.match_date_range = _match_date_range_with_wildcards
    _schedule_module.Null = _ScheduleNull
    _event_module.TimeStamp = _DateTimeStamp
    _event_module.EventAlgorithm.send_notifications = _send_notifications_by_weekday  # pyright: ignore[reportAttributeAccessIssue]
    _local_object_module.Object.propertyList = property(_property_list_without_required, _UPSTREAM_PROPERTY_LIST.fset)


_install_import_time_swaps()


# The device clock: every bacpypes3 "now" bottoms
# out in a bare time.time() in these two modules; swapping their `time`
# moves all of them and nothing else.
_CLOCK_MODULES = ("bacpypes3.primitivedata", "bacpypes3.basetypes")
_clock_originals: dict = {}


def swap_clock(time_like) -> None:
    """Point both modules' `time` at `time_like`. A second call while
    swapped is a no-op, so the originals are never the shifted clock."""
    if _clock_originals:
        return
    for name in _CLOCK_MODULES:
        module = importlib.import_module(name)
        _clock_originals[name] = module.time
        module.time = time_like


def restore_clock() -> None:
    for name, original in _clock_originals.items():
        importlib.import_module(name).time = original
    _clock_originals.clear()


def clock_swapped() -> bool:
    return bool(_clock_originals)


# --- Work queued while an object is constructed ----------------------------
# bacpypes3 queues work on the loop from its constructors (Object.__init__'s
# _post_init task, ScheduleObject.__init__'s interpret_schedule, a
# Parameter's or COV DetectionMonitor's call_soon when the build sets a
# property), meant to run once the object is attached. A live edit builds in
# turns, so that work is held back until the swap.


class _DeferredHandle:
    """Stands in for the Handle or Task the constructor gets back, and
    forwards to the real one once replayed (a schedule keeps its
    _interpret_schedule_handle and cancels it later)."""

    def __init__(self) -> None:
        self.real = None
        self._cancelled = False

    def cancel(self, *args) -> bool:
        self._cancelled = True
        return self.real.cancel(*args) if self.real is not None else True

    def cancelled(self) -> bool:
        return self.real.cancelled() if self.real is not None else self._cancelled

    def done(self) -> bool:
        return self.real.done() if self.real is not None else self._cancelled

    def when(self):
        return self.real.when() if self.real is not None else None


class _RecordingLoop(asyncio.AbstractEventLoop):
    """The running loop during one construction. Any loop method not here
    raises NotImplementedError (AbstractEventLoop's), failing that build."""

    def __init__(self, real: asyncio.AbstractEventLoop, calls: list) -> None:
        self._real = real
        self._calls = calls

    def time(self) -> float:
        return self._real.time()

    def get_debug(self) -> bool:
        return self._real.get_debug()

    def _record(self, kind: str, fn, args: tuple, delay: float = 0.0) -> _DeferredHandle:
        handle = _DeferredHandle()
        self._calls.append((kind, fn, args, delay, handle))
        return handle

    def create_task(self, coro, **kwargs):  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._record("task", coro, ())

    def call_soon(self, callback, *args, context=None):  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._record("soon", callback, args)

    def call_later(self, delay, callback, *args, context=None):  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._record("later", callback, args, delay)


class DeferredLoopWork:
    """build() constructs with a recorder as the running loop; replay()
    hands what it queued to the real loop, in order; discard() drops it.
    asyncio._set_running_loop is CPython's own hook for event-loop
    libraries: the recorder is installed only around the one synchronous
    call, so no other code ever sees it."""

    def __init__(self) -> None:
        self._calls: list = []

    def build(self, fn, *args, **kwargs):
        real = asyncio.get_running_loop()
        asyncio._set_running_loop(_RecordingLoop(real, self._calls))
        try:
            return fn(*args, **kwargs)
        finally:
            asyncio._set_running_loop(real)

    def replay(self) -> None:
        """A call_later's delay counts from here, not from the build."""
        loop = asyncio.get_running_loop()
        calls, self._calls = self._calls, []
        for kind, fn, args, delay, handle in calls:
            if handle.cancelled():
                if kind == "task":
                    fn.close()
            elif kind == "task":
                handle.real = held_tasks.hold(loop.create_task(fn))
            elif kind == "soon":
                handle.real = loop.call_soon(fn, *args)
            else:
                handle.real = loop.call_later(delay, fn, *args)

    def discard(self) -> None:
        calls, self._calls = self._calls, []
        for kind, fn, *_ in calls:
            if kind == "task":
                fn.close()


# --- Private attributes ---------------------------------------------------
# Each helper names one bacpypes3-private attribute BACpack needs, so the
# rest of the code never spells a bacpypes3 `_name` (enforced by
# tests/test_bacpypes3_compat.py's AST scan).


def property_monitors(obj, prop: str) -> list:
    """The live list of (old, new) callbacks Object.__setattr__ calls when
    `prop` changes (what COV and event algorithms hook); empty if unwatched."""
    return obj._property_monitors[prop]


def remove_property_monitor(obj, prop: str, callback) -> None:
    monitors = obj._property_monitors.get(prop)
    if monitors is not None and callback in monitors:
        monitors.remove(callback)


def element_class(obj_or_class, prop: str) -> Optional[type]:
    """The datatype class bacpypes3 declares for `prop`; None when the
    object type has no such property."""
    return obj_or_class._elements.get(prop)


def element_names(obj) -> list:
    """Every property the object's type declares, in declaration order."""
    return list(obj._elements)


def required_properties(obj) -> set:
    """The properties the object's type (and its bases) lists as required."""
    return {prop for cls in type(obj).__mro__ for prop in getattr(cls, "_required", ())}


def required_element_class(obj_or_class, prop: str) -> type:
    """element_class for a property the type must have; KeyError otherwise."""
    element = element_class(obj_or_class, prop)
    if element is None:
        raise KeyError(prop)
    return element


def property_name(obj_or_class, identifier: int) -> str:
    """A PropertyIdentifier number as the attribute name ("presentValue"),
    using the object's own (vendor-aware) identifier class."""
    return obj_or_class._property_identifier_class(identifier).attr


def event_algorithm(obj):
    """The object's intrinsic-reporting algorithm; None for an object built
    without one."""
    return getattr(obj, "_event_algorithm", None)


def attach_event_algorithm(obj, algorithm) -> None:
    """For BACpack's hand-built IR classes, after construction -- the same
    attribute bacpypes3's own IR classes set in __init__."""
    obj._event_algorithm = algorithm


def schedule_algorithm_run(algorithm) -> None:
    """Run the algorithm's evaluation on the next loop tick -- the
    call_soon deferral Parameter.property_change uses; a no-op while one is
    already pending or the algorithm is disabled."""
    if algorithm._execute_enabled and not algorithm._execute_handle:
        algorithm._execute_handle = asyncio.get_event_loop().call_soon(algorithm._execute)


def cancel_algorithm_run(algorithm) -> None:
    """Cancel a queued, not yet run evaluation (a retired object's must not run)."""
    handle = algorithm._execute_handle
    if handle is not None:
        handle.cancel()
        algorithm._execute_handle = None


def has_pending_transition(algorithm) -> bool:
    """A transition waiting out Time_Delay (cancel it with the public
    state_transition_cancel())."""
    return bool(algorithm._transition_timeout_handle)


def notification_class_object(obj):
    """The Notification Class object Object._post_init linked at startup;
    None when not linked."""
    return getattr(obj, "_notification_class_object", None)


def link_notification_class(obj, notification_class) -> None:
    """Re-link after a Notification_Class write -- _post_init only links
    once, at startup."""
    obj._notification_class_object = notification_class


def object_app(obj):
    """The Application an object was added to (Application.add_object sets
    it); None before that or for an object never added."""
    return getattr(obj, "_app", None)


def cancel_pending_interpretation(schedule_obj) -> None:
    """Cancel the timer interpret_schedule() armed for the next transition, if any."""
    handle = getattr(schedule_obj, "_interpret_schedule_handle", None)
    if handle is not None:
        handle.cancel()


def enum_name_maps(enum_class) -> tuple:
    """An Enumerated class's names: (every spelling, kebab and camel ->
    number; number -> camelCase; number -> kebab-case)."""
    return enum_class._enum_map, enum_class._attr_map, enum_class._asn1_map


def sequence_fields(value) -> list:
    """A Sequence's element names, in encoding order."""
    return value._order


def chosen_field(value) -> Optional[str]:
    """The element name a Choice (a priority slot, say) holds; None for a
    value that isn't a Choice at all."""
    return getattr(value, "_choice", None)


def array_element_type(array_class) -> type:
    """The element datatype of an ArrayOf(...) class."""
    return array_class._subtype


def is_context_encoded(any_value) -> bool:
    """Whether an Any came context-tagged (WriteProperty's [3], WPM's [2]);
    its tagList then keeps the opening and closing tags."""
    return any_value._context is not None


def store_priority_array_raw(obj, typed_slots: list, current_command_priority, present_value) -> None:
    """Write all 16 slots, Current_Command_Priority and Present_Value as
    given, without Commandable's own command logic (which would re-derive
    Present_Value slot by slot) -- for mirroring a remote point's state.
    Present_Value's monitors fire at most once."""
    super(PriorityArray, obj.priorityArray).__setitem__(slice(0, 16), typed_slots)
    super(Commandable, obj).__setattr__("currentCommandPriority", current_command_priority)
    super(Commandable, obj).__setattr__("presentValue", present_value)


class CovSubscriptionState(NamedTuple):
    """One COV subscription lifted off an object being replaced.
    `lifetime`: seconds left, 0 = permanent."""

    client_addr: object
    proc_id: int
    confirmed: bool
    lifetime: int
    cov_increment: object


def take_cov_subscriptions(app, obj) -> list:
    """Detach `obj`'s COV detection: every subscription's state, with its
    expiry timer cancelled and the detection's monitors unbound. [] when
    nobody is subscribed."""
    detection = app._cov_detections.pop(obj.objectIdentifier, None)
    if detection is None:
        return []
    now = asyncio.get_running_loop().time()
    taken = []
    for cov in list(detection.cov_subscriptions):
        handle = cov.cancel_handle
        remaining = 0 if handle is None else max(1, math.ceil(handle.when() - now))
        taken.append(CovSubscriptionState(cov.client_addr, cov.proc_id, bool(cov.confirmed), remaining, cov.covIncrement))
        detection.cancel_subscription(cov)
    detection.unbind()
    return taken


def restore_cov_subscriptions(app, obj, taken: list) -> None:
    """Subscribe each of `taken` again on `obj` (already added to `app`),
    as SubscribeCOV does, and send each one notification of the current
    values."""
    if not taken:
        return
    criteria = getattr(obj, "_cov_criteria", None)
    if criteria is None:
        return
    detection = app._cov_detections.get(obj.objectIdentifier)
    if detection is None:
        detection = criteria(obj)
        app._cov_detections[obj.objectIdentifier] = detection
    for state in taken:
        cov = Subscription(
            obj, state.client_addr, state.proc_id, obj.objectIdentifier, state.confirmed, state.lifetime, state.cov_increment
        )
        app.add_subscription(cov)
        detection.send_cov_notifications(cov)
