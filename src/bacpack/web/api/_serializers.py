"""Response-dict builders shared by more than one router module: most
responses are plain dicts built from ORM objects, not models."""
from __future__ import annotations

from fastapi.responses import JSONResponse

from ...models import BacnetObject


def objects_response(objects) -> JSONResponse:
    """A device's objects, encoded here: a plain def route calls this in its
    worker thread, while a returned list would be run through
    jsonable_encoder on the event loop (~0.3 s for 5,000 objects). Every
    value object_dict gives is already JSON-native."""
    return JSONResponse([object_dict(o) for o in objects])


def object_dict(obj: BacnetObject) -> dict:
    return {
        "id": obj.id,
        "device_id": obj.device_id,
        "object_type": obj.object_type,
        "instance_number": obj.instance_number,
        "name": obj.name,
        "description": obj.description,
        "units": obj.units,
        "bacnet_writable": obj.bacnet_writable,
        "out_of_service_default": obj.out_of_service_default,
        "relinquish_default": obj.relinquish_default,
        "active_text": obj.active_text,
        "inactive_text": obj.inactive_text,
        "start_value": obj.start_value,
        "state_text": obj.state_text,
        "trended": obj.trended,
        "trend_mode": obj.trend_mode,
        "trend_interval_seconds": obj.trend_interval_seconds,
        "trend_sample_cap": obj.trend_sample_cap,
        "trend_log_instance": obj.trend_log_instance,
        "trend_log_name": obj.trend_log_name,
        "trend_reporting_enabled": obj.trend_reporting_enabled,
        "trend_notification_threshold": obj.trend_notification_threshold,
        "trend_notification_class_instance": obj.trend_notification_class_instance,
        "trend_event_enable": obj.trend_event_enable,
        "trend_notify_type": obj.trend_notify_type,
        "trend_event_detection_enable": obj.trend_event_detection_enable,
        "trend_last_notify_record": obj.trend_state.last_notify_record if obj.trend_state is not None else 0,
        "schedule_value_type": obj.schedule_value_type,
        "schedule_default": obj.schedule_default,
        "weekly_schedule": obj.weekly_schedule,
        "exception_schedule": obj.exception_schedule,
        "schedule_effective_start": obj.schedule_effective_start,
        "schedule_effective_end": obj.schedule_effective_end,
        "schedule_priority": obj.schedule_priority,
        "schedule_write_targets": obj.schedule_write_targets,
        "schedule_write_status": obj.runtime.schedule_write_status,
        "date_list": obj.date_list,
        "alarming_enabled": obj.alarming_enabled,
        "notification_class_instance": obj.notification_class_instance,
        "alarm_time_delay": obj.alarm_time_delay,
        "alarm_time_delay_normal": obj.alarm_time_delay_normal,
        "alarm_notify_type": obj.alarm_notify_type,
        "alarm_event_enable": obj.alarm_event_enable,
        "alarm_high_limit": obj.alarm_high_limit,
        "alarm_low_limit": obj.alarm_low_limit,
        "alarm_deadband": obj.alarm_deadband,
        "alarm_limit_enable": obj.alarm_limit_enable,
        "alarm_value": obj.alarm_value,
        "alarm_values": obj.alarm_values,
        "alarm_event_detection_enable": obj.alarm_event_detection_enable,
        "notification_priority": obj.notification_priority,
        "notification_ack_required": obj.notification_ack_required,
        "notification_recipient_list": obj.notification_recipient_list,
        "simulation": obj.simulation,
        # The connector whose read mapping feeds this object, if any.
        "fed_by_connector_id": obj.connector_mapping.connector_id if obj.connector_mapping else None,
        "fed_by_connector_name": obj.connector_mapping.connector.name if obj.connector_mapping else None,
    }
