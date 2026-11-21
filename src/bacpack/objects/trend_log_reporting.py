"""BUFFER_READY intrinsic reporting on Trend Log objects (T-ATR-B).

bacpypes3's BufferReadyEventAlgorithm is unusable (execute() raises
NotImplementedError; bind() reads the threshold once). Record counts are
computed, never set, so no monitor fires on a new record: the sampler calls
ReportingTrendLogObject.records_added() instead. Imports trend_log.py, never
the reverse.
"""
from __future__ import annotations

from bacpypes3.basetypes import (
    DeviceObjectPropertyReference,
    EventState,
    EventType,
    NotificationParameters,
    NotificationParametersBufferReady,
)
from bacpypes3.local.event import EventAlgorithm
from bacpypes3.primitivedata import Unsigned

from .. import bacpypes3_compat, config_save, held_tasks
from ..models import BacnetObject, Device, trend_state_of
from ..trending import pending_records
from .alarm_config import (
    AlarmConfigWritable,
    transition_needs_ack,
    unset_time_stamp,
    with_notification_class_slots,
)
from .trend_log import LocalTrendLogObject, build_trend_log_object


class TrendLogBufferReadyAlgorithm(EventAlgorithm):
    """Clause 13.3.7: pMonitoredValue is Total_Record_Count, pPreviousCount
    Last_Notify_Record. Event_State stays NORMAL; a notification is a
    NORMAL-to-NORMAL transition, which state_transition(immediate=True)
    sends in full (timestamp, recipient filter, DCC). Priority/ackRequired
    come from the to-normal slot (bacpypes3 reads the wrong one)."""

    event_type = EventType.bufferReady

    def __init__(self, monitoring_object, monitored_object):
        super().__init__(monitoring_object, monitored_object)
        self._previous = 0
        self._current = 0
        # Monitored, so lowering Notification_Threshold below the pending
        # count notifies at once.
        self.bind(pNotificationThreshold=(monitored_object, "notificationThreshold"))

    def execute(self) -> None:
        log = self.monitored_object
        threshold = int(log.notificationThreshold or 0)
        if threshold == 0:
            return
        total = int(log.totalRecordCount)
        last = int(log.lastNotifyRecord)
        if pending_records(total, last) < threshold:
            return
        # state_transition calls event_notification_parameters() synchronously.
        self._previous, self._current = last, total
        self.state_transition(EventState.normal, True)
        log.lastNotifyRecord = Unsigned(total)
        log._save_last_notify_record(total)
        # The alarm objects clear an acked bit from an eventState monitor,
        # which never fires on NORMAL-to-NORMAL; do it here instead.
        if transition_needs_ack(log, 2):
            acked = list(log.ackedTransitions)
            acked[2] = False
            log.ackedTransitions = acked

    async def send_notifications(self, notification_parameters) -> None:
        # bacpypes3 never consults Event_Enable. The transition still
        # happens; only the notification is withheld.
        if not self.monitored_object.eventEnable[2]:
            return
        await super().send_notifications(with_notification_class_slots(notification_parameters, self.monitored_object))

    def event_notification_parameters(self) -> NotificationParameters:
        log = self.monitored_object
        return NotificationParameters(
            bufferReady=NotificationParametersBufferReady(
                bufferProperty=DeviceObjectPropertyReference(
                    objectIdentifier=log.objectIdentifier,
                    propertyIdentifier="logBuffer",
                    deviceIdentifier=("device", log._trend_device_id),
                ),
                previousNotification=self._previous,
                currentNotification=self._current,
            )
        )


class TrendLogConfigWritable(AlarmConfigWritable):
    """AlarmConfigWritable's validate/apply/save rules, saved to the
    trend_* columns of the trended object's row."""

    _ALARM_CONFIG_COLUMNS = {
        "notificationThreshold": "trend_notification_threshold",
        "notificationClass": "trend_notification_class_instance",
        "eventEnable": "trend_event_enable",
        "notifyType": "trend_notify_type",
        "eventDetectionEnable": "trend_event_detection_enable",
    }


class ReportingTrendLogObject(TrendLogConfigWritable, LocalTrendLogObject):
    """A Trend Log with BUFFER_READY intrinsic reporting.

    TrendLogConfigWritable comes first, so LocalTrendLogObject's empty
    allowlist refuses only the rest. Listing the allowlist first, as on the
    alarm IR classes, would make the MRO impossible."""

    _trend_device_id: int

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        bacpypes3_compat.attach_event_algorithm(self, TrendLogBufferReadyAlgorithm(None, self))

    @property
    def recordsSinceNotification(self):
        # Unsigned, not int -- see LocalTrendLogObject.recordCount.
        return Unsigned(pending_records(int(self.totalRecordCount), int(self.lastNotifyRecord)))

    def records_added(self) -> None:
        """Schedule an evaluation after records were committed (a no-op
        while one is pending)."""
        bacpypes3_compat.schedule_algorithm_run(bacpypes3_compat.event_algorithm(self))

    def _trend_session_factory_for_save(self):
        return self._trend_session_factory()

    def _save_last_notify_record(self, value: int) -> None:
        # Called from the algorithm's synchronous evaluation, so the save is
        # a held task. The notification has gone out; a failed save is
        # logged (the next previousNotification still tells the client).
        object_id = self._trend_object_id

        def change(session) -> None:
            row = session.get(BacnetObject, object_id)
            if row is not None:
                trend_state_of(row).last_notify_record = value

        held_tasks.start(
            config_save.save(
                self._trend_session_factory_for_save, change,
                f"Last_Notify_Record for trend log of object id {object_id}",
            )
        )


def build_reporting_trend_log_object(
    device: Device, obj: BacnetObject, session_factory
) -> ReportingTrendLogObject:
    trend_log = build_trend_log_object(
        device,
        obj,
        session_factory,
        cls=ReportingTrendLogObject,
        extra_kwargs=dict(
            notificationThreshold=obj.trend_notification_threshold,
            lastNotifyRecord=obj.trend_state.last_notify_record if obj.trend_state is not None else 0,
            notificationClass=obj.trend_notification_class_instance,
            eventEnable=obj.trend_event_enable or [True, True, True],
            ackedTransitions=[True, True, True],
            notifyType=obj.trend_notify_type,
            # Three separate instances, as for the alarm objects.
            eventTimeStamps=[unset_time_stamp() for _ in range(3)],
            eventDetectionEnable=obj.trend_event_detection_enable,
        ),
    )
    trend_log._trend_device_id = device.id
    trend_log._alarm_config_session_factory = session_factory
    trend_log._alarm_config_object_id = obj.id
    return trend_log
