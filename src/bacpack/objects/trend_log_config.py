"""Structural rules for a trended object's trend-reporting fields. No object
classes, so object_config can import it."""
from __future__ import annotations


_MAX_THRESHOLD = 4294967295


def validate_trend_reporting_config(
    *, trended, enabled, notification_class_instance, threshold, notify_type, event_enable
) -> None:
    """Structural checks for the trend_* reporting fields, no DB access."""
    if not enabled:
        return
    if not trended:
        raise ValueError("trend_reporting_enabled requires trended")
    if notification_class_instance is None:
        raise ValueError("trend_notification_class_instance is required when trend_reporting_enabled=True")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not (0 <= threshold <= _MAX_THRESHOLD):
        raise ValueError("trend_notification_threshold must be an integer 0-4294967295")
    if notify_type not in ("alarm", "event"):
        raise ValueError("trend_notify_type must be 'alarm' or 'event'")
    if event_enable is not None and len(event_enable) != 3:
        raise ValueError("trend_event_enable must have exactly 3 entries")
