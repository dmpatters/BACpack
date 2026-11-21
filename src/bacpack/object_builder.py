"""The dispatcher: builds live bacpypes3 objects and Applications from
BacnetObject/Device rows, using every objects/*.py module. Separate from
bacnet.py and application.py to avoid import cycles.
"""
from __future__ import annotations

from typing import List, Optional

from bacpypes3.basetypes import Reliability, StatusFlags
from bacpypes3.primitivedata import Unsigned

from .application import LoggingApplication
from .backup_restore import BackupRestore
from . import bacpypes3_compat  # also applies the import-time swaps
from . import live_swap, log_names, object_config
from .objects.file import build_config_file_object
from .objects.alarm_config import (
    state_group_index,
    transition_needs_ack,
    unset_time_stamp,
    withhold_disabled_notifications,
)
from .bacnet import relinquish_default_for
from .models import (
    BacnetObject,
    Device,
    ALARMABLE_TYPES,
    ANALOG_TYPES,
    BINARY_TYPES,
    COMMANDABLE_TYPES,
    DEFAULT_ACTIVE_TEXT,
    DEFAULT_INACTIVE_TEXT,
    INPUT_TYPES,
    MULTISTATE_TYPES,
)
from .objects.analog import (
    GatedAnalogInputObject,
    GatedAnalogInputObjectIR,
    WriteLockedAnalogOutputObject,
    WriteLockedAnalogOutputObjectIR,
    WriteLockedAnalogValueObjectCmd,
    WriteLockedAnalogValueObjectCmdIR,
)
from .objects.binary import (
    GatedBinaryInputObject,
    GatedBinaryInputObjectIR,
    ValidatedBinaryOutputObject,
    ValidatedBinaryValueObjectCmd,
    ValidatedBinaryValueObjectCmdIR,
)
from .objects.calendar import LocalCalendarObject
from .objects.calendar_config import build_calendar_entry
from .objects.device import build_device_object
from .objects.multistate import (
    GatedMultiStateInputObject,
    GatedMultiStateInputObjectIR,
    ValidatedMultiStateOutputObject,
    ValidatedMultiStateValueObject,
    ValidatedMultiStateValueObjectIR,
)
from .objects.network_port import build_network_port_object
from .objects.notification_class import LocalNotificationClassObject, build_recipient_list
from .objects.schedule import GatedScheduleObject
from .objects.schedule_config import (
    build_daily_schedule,
    build_schedule_write_targets,
    build_special_event,
    effective_period,
    schedule_atomic_value,
)
from .objects.trend_log import build_trend_log_object
from .objects.trend_log_reporting import build_reporting_trend_log_object
from .readrange_cursors import ReadRangeCursors
from .traffic import TrafficLog


# Value types always use a commandable variant; multi-state-value's is local
# because bacpypes3 0.0.106 ships none. The Validated* classes reject
# out-of-range writes, which bacpypes3 doesn't. Every Output/Value type carries
# BacnetWriteLocked.
OBJECT_CLASSES = {
    "analog-input": GatedAnalogInputObject,
    "analog-output": WriteLockedAnalogOutputObject,
    "analog-value": WriteLockedAnalogValueObjectCmd,
    "binary-input": GatedBinaryInputObject,
    "binary-output": ValidatedBinaryOutputObject,
    "binary-value": ValidatedBinaryValueObjectCmd,
    "multi-state-input": GatedMultiStateInputObject,
    "multi-state-output": ValidatedMultiStateOutputObject,
    "multi-state-value": ValidatedMultiStateValueObject,
    "schedule": GatedScheduleObject,
    "calendar": LocalCalendarObject,
    "notification-class": LocalNotificationClassObject,
}

# Intrinsic-reporting variants, used when alarming_enabled. No
# binary-output/multi-state-output: their alarm type is Command Failure, which
# bacpypes3 never implemented.
OBJECT_CLASSES_IR = {
    "analog-input": GatedAnalogInputObjectIR,
    "analog-output": WriteLockedAnalogOutputObjectIR,
    "analog-value": WriteLockedAnalogValueObjectCmdIR,
    "binary-input": GatedBinaryInputObjectIR,
    "binary-value": ValidatedBinaryValueObjectCmdIR,
    "multi-state-input": GatedMultiStateInputObjectIR,
    "multi-state-value": ValidatedMultiStateValueObjectIR,
}

# Not configurable per object.
DEFAULT_COV_INCREMENT = 1.0


def _object_class_for(obj: BacnetObject) -> type:
    if obj.object_type in ALARMABLE_TYPES and obj.alarming_enabled:
        object_class = OBJECT_CLASSES_IR.get(obj.object_type)
        if object_class is None:
            raise ValueError(
                f"{obj.object_type} does not support alarming"
            )
    else:
        object_class = OBJECT_CLASSES.get(obj.object_type)
    if object_class is None:
        raise ValueError(f"unknown object_type: {obj.object_type!r}")
    return object_class


def _add_value_kwargs(kwargs: dict, obj: BacnetObject) -> None:
    start = obj.start_value if obj.object_type in INPUT_TYPES else None
    if obj.object_type in ANALOG_TYPES:
        kwargs.update(
            presentValue=0.0 if start is None else float(start),
            units=obj.units or "noUnits",
            covIncrement=DEFAULT_COV_INCREMENT,
        )
    elif obj.object_type in BINARY_TYPES:
        kwargs.update(
            presentValue=start or "inactive",
            activeText=obj.active_text or DEFAULT_ACTIVE_TEXT,
            inactiveText=obj.inactive_text or DEFAULT_INACTIVE_TEXT,
        )
    elif obj.object_type in MULTISTATE_TYPES:
        kwargs.update(
            presentValue=1 if start is None else int(start),
            numberOfStates=len(obj.state_text),
            stateText=list(obj.state_text),
        )
    elif obj.object_type == "notification-class":
        kwargs.pop("outOfService", None)  # NotificationClassObject has no such property
        kwargs.update(
            notificationClass=obj.instance_number,
            priority=obj.notification_priority or [100, 100, 100],
            ackRequired=obj.notification_ack_required or [True, True, True],
            recipientList=build_recipient_list(obj.notification_recipient_list),
        )

    if obj.object_type in ANALOG_TYPES | BINARY_TYPES | MULTISTATE_TYPES:
        # Required on these types whether or not they alarm (ASHRAE 135
        # clause 12); a revision-4 client's import stops at an unknown-property
        # for it. An alarming object needs it "normal" too: EventAlgorithm
        # only ever transitions out of EventState.normal, never out of None.
        kwargs["eventState"] = "normal"


def _add_alarm_kwargs(kwargs: dict, obj: BacnetObject) -> None:
    kwargs.update(
        notificationClass=obj.notification_class_instance,
        timeDelay=obj.alarm_time_delay,
        timeDelayNormal=obj.alarm_time_delay_normal,
        notifyType=obj.alarm_notify_type,
        eventEnable=obj.alarm_event_enable or [True, True, True],
        ackedTransitions=[True, True, True],  # nothing pending ack on a fresh/still-normal object
        # TimeStamp is a Choice with no null variant, so None can't be
        # encoded; an all-wildcard dateTime means "no transition yet".
        # Not sequenceNumber=0: YABE's GetEventInformation decoder hangs
        # on it. Three separate instances: each slot is overwritten alone.
        eventTimeStamps=[unset_time_stamp() for _ in range(3)],
        # `is None`: an unsaved row reads None, not the column default --
        # same guard as _bacnet_writable in _attach_row_attributes.
        eventDetectionEnable=(
            True if obj.alarm_event_detection_enable is None else obj.alarm_event_detection_enable
        ),
    )
    if obj.object_type in ANALOG_TYPES:
        kwargs.update(
            highLimit=obj.alarm_high_limit,
            lowLimit=obj.alarm_low_limit,
            deadband=obj.alarm_deadband,
            # Default from which limits are set: an enabled side with no
            # limit makes OutOfRangeEventAlgorithm compare against None.
            limitEnable=obj.alarm_limit_enable
            or [obj.alarm_low_limit is not None, obj.alarm_high_limit is not None],
        )
    elif obj.object_type in BINARY_TYPES:
        kwargs["alarmValue"] = obj.alarm_value
    elif obj.object_type in MULTISTATE_TYPES:
        kwargs["alarmValues"] = obj.alarm_values or []


def _add_schedule_kwargs(kwargs: dict, obj: BacnetObject) -> None:
    value_type = obj.schedule_value_type
    kwargs.update(
        effectivePeriod=effective_period(obj.schedule_effective_start, obj.schedule_effective_end),
        scheduleDefault=schedule_atomic_value(value_type, obj.schedule_default),
        presentValue=schedule_atomic_value(value_type, obj.schedule_default),
        priorityForWriting=Unsigned(obj.schedule_priority),
        listOfObjectPropertyReferences=build_schedule_write_targets(obj.schedule_write_targets),
        statusFlags=StatusFlags([0, 0, 0, 0]),
        reliability=Reliability("noFaultDetected"),
    )
    # Always set both (user decision): an omitted one reads as
    # unknown-property, and an editor that reads config first may then
    # never write it. bacpypes3's check_reliability tests both for None,
    # but validate_schedule_config guarantees one has content.
    # weeklySchedule is an Array[7] (bacpypes3 refuses an empty list),
    # so the unconfigured placeholder is 7 empty days.
    kwargs["weeklySchedule"] = (
        [build_daily_schedule(day, value_type) for day in obj.weekly_schedule]
        if obj.weekly_schedule
        else [build_daily_schedule([], value_type) for _ in range(7)]
    )
    kwargs["exceptionSchedule"] = (
        [build_special_event(event, value_type) for event in obj.exception_schedule]
        if obj.exception_schedule
        else []
    )


def _attach_ack_reset(built) -> None:
    def _reset_acked_transition_on_state_change(old_value, new_value) -> None:
        if old_value == new_value:
            return
        index = state_group_index(new_value)
        if not transition_needs_ack(built, index):
            return
        acked = list(built.ackedTransitions)
        acked[index] = False
        built.ackedTransitions = acked

    bacpypes3_compat.property_monitors(built, "eventState").append(_reset_acked_transition_on_state_change)
    withhold_disabled_notifications(bacpypes3_compat.event_algorithm(built))


def _attach_row_attributes(built, obj: BacnetObject, config_session_factory) -> None:
    """BACpack's own attributes the live object reads, not BACnet properties."""
    # Read by BacnetWriteLocked. `is None`: an unflushed row reads None, not
    # the column default True, which would mean "not writable".
    built._bacnet_writable = True if obj.bacnet_writable is None else obj.bacnet_writable

    if obj.object_type in COMMANDABLE_TYPES and config_session_factory is not None:
        built._relinquish_default_session_factory = config_session_factory
        built._relinquish_default_object_id = obj.id

    if obj.object_type in MULTISTATE_TYPES and config_session_factory is not None:
        built._state_text_session_factory = config_session_factory
        built._state_text_object_id = obj.id

    if obj.object_type in ALARMABLE_TYPES and obj.alarming_enabled and config_session_factory is not None:
        built._alarm_config_session_factory = config_session_factory
        built._alarm_config_object_id = obj.id

    if obj.object_type == "notification-class":
        built._recipient_entries = list(obj.notification_recipient_list or [])
        if config_session_factory is not None:
            built._recipient_list_session_factory = config_session_factory
            built._recipient_list_object_id = obj.id

    if obj.object_type == "schedule":
        built._schedule_value_type = obj.schedule_value_type
        built._schedule_device_instance = obj.device_id
        if config_session_factory is not None:
            built._schedule_session_factory = config_session_factory
            built._schedule_object_id = obj.id
    elif obj.object_type == "calendar" and config_session_factory is not None:
        built._calendar_session_factory = config_session_factory
        built._calendar_object_id = obj.id

    # A proxy write that waits out a live swap finds its object by row id
    # (bacnet._current_object).
    built._row_id = obj.id


def build_bacnet_object(obj: BacnetObject, *, config_session_factory=None):
    """`config_session_factory` enables saving BACnet config writes
    (State_Text, Relinquish_Default, alarm config, ...) back to this row;
    omitted in most tests.
    """
    object_class = _object_class_for(obj)

    # Again here so a row edited straight in SQLite fails clearly.
    object_config.check_object(object_config.row_from_object(obj))

    kwargs = dict(
        objectIdentifier=(obj.object_type, obj.instance_number),
        objectName=obj.name,
        description=obj.description or "",
    )
    # CalendarObject has no Out_Of_Service; passing it fails construction.
    if obj.object_type != "calendar":
        kwargs["outOfService"] = obj.out_of_service_default

    _add_value_kwargs(kwargs, obj)

    if obj.object_type in ALARMABLE_TYPES and obj.alarming_enabled:
        _add_alarm_kwargs(kwargs, obj)
    elif obj.object_type == "schedule":
        _add_schedule_kwargs(kwargs, obj)
    elif obj.object_type == "calendar":
        kwargs["dateList"] = [build_calendar_entry(entry) for entry in obj.date_list or []]

    # The priority array always starts empty (runtime-only), so
    # Present_Value starts at the relinquish default.
    if obj.relinquish_default is not None and obj.object_type in COMMANDABLE_TYPES:
        kwargs["relinquishDefault"] = relinquish_default_for(obj.object_type, obj.relinquish_default)
        kwargs["presentValue"] = kwargs["relinquishDefault"]

    built = object_class(**kwargs)
    if obj.object_type in ALARMABLE_TYPES and obj.alarming_enabled:
        _attach_ack_reset(built)
    _attach_row_attributes(built, obj, config_session_factory)
    return built


def build_application(
    device: Device,
    objects: List[BacnetObject],
    *,
    bind_address: Optional[str] = None,
    foreign_bbmd: Optional[str] = None,
    foreign_ttl: int = 60,
    bbmd_peers: Optional[List[str]] = None,
    traffic_log: Optional[TrafficLog] = None,
    trend_session_factory=None,
) -> LoggingApplication:
    """Build the Application for one device (one device per process; a
    bacpypes3 Application holds a single device object).

    `bind_address` (CIDR, e.g. "192.168.1.50/24") is omitted in tests to
    avoid a real socket; `foreign_bbmd`/`bbmd_peers` need it.
    `trend_session_factory` also builds a Trend Log per trended object and
    enables config write-back; omitted in most tests.
    """
    # Trend logs are only built with a trend_session_factory, so only then
    # do their names (and their notification classes) count.
    rows = [
        {**object_config.row_from_object(obj), "trended": bool(obj.trended and trend_session_factory is not None)}
        for obj in objects
    ]
    problem = object_config.check_device(device.name, rows)
    if problem is not None:
        raise ValueError(f"{problem}; fix or rename it")
    device_object = build_device_object(device)
    backup_restore = BackupRestore(timeout=int(device_object.backupFailureTimeout))
    device_object._backup_restore = backup_restore

    all_objects: list = [device_object, build_config_file_object(device.name, backup_restore)]
    if bind_address:
        all_objects.append(
            build_network_port_object(
                bind_address,
                foreign_bbmd=foreign_bbmd,
                foreign_ttl=foreign_ttl,
                bbmd_peers=bbmd_peers,
            )
        )
    # Calendars first: bacpypes3 resolves a schedule's calendar reference as
    # the schedule is added, and imports save objects in file order.
    row_objects: list = []
    for obj in sorted(objects, key=lambda o: o.object_type != "calendar"):
        try:
            row_objects.append(
                build_bacnet_object(obj, config_session_factory=trend_session_factory)
            )
        except Exception as exc:
            # Name the offending object rather than surface a bare library error.
            raise ValueError(
                f"object {log_names.object_ref(obj.object_type, obj.instance_number, obj.name)}: {exc}"
            ) from exc
    all_objects.extend(row_objects)
    if trend_session_factory is not None:
        all_objects.extend(
            (build_reporting_trend_log_object if obj.trend_reporting_enabled else build_trend_log_object)(
                device, obj, trend_session_factory
            )
            for obj in objects
            if obj.trended
        )

    app = LoggingApplication.from_object_list(all_objects)
    for obj in all_objects:
        app.track_trend_log(obj)  # pyright: ignore[reportAttributeAccessIssue] -- from_object_list builds a LoggingApplication
    app.traffic_log = traffic_log if traffic_log is not None else TrafficLog()
    app._read_range_cursors = ReadRangeCursors()  # pyright: ignore[reportAttributeAccessIssue] -- from_object_list builds a LoggingApplication
    app.session_factory = trend_session_factory
    app.backup_restore = backup_restore
    # An object built already past a limit must alarm now: bacpypes3 only
    # evaluates on a change. Each object's queued _post_init (the
    # Notification Class link) runs first.
    for obj in row_objects:
        live_swap.start_alarm_evaluation(obj)
    return app
