"""Wording of the alarming-config validation errors. These strings are shown
verbatim in the web UI, so they must name UI fields rather than database
columns, and must say where to go to fix the problem.
"""
import pytest

from bacpack.point_config import validate_alarming_config


def test_analog_limit_message_names_ui_fields_not_db_columns():
    with pytest.raises(ValueError) as exc:
        validate_alarming_config("analog-input", True)
    message = str(exc.value)
    assert "alarm_high_limit" not in message
    assert "alarm_low_limit" not in message
    assert "high limit" in message.lower()
    assert "Alarming tab" in message


def test_binary_alarm_value_message_names_ui_fields_not_db_columns():
    with pytest.raises(ValueError) as exc:
        validate_alarming_config("binary-input", True)
    message = str(exc.value)
    assert "alarm_value" not in message
    assert "Alarming tab" in message


def test_a_fully_configured_analog_object_still_validates():
    """Guard against the reword accidentally changing when it raises."""
    validate_alarming_config("analog-input", True, alarm_high_limit=100.0)


def test_a_fully_configured_binary_object_still_validates():
    validate_alarming_config("binary-input", True, alarm_value=True)


# --- The rest of the alarm settings -----------------------------------------
# Each of these saved cleanly and then either stopped the device from
# starting (a negative delay can't become an Unsigned) or broke the running
# event algorithm (an enabled limit with no value is compared against None
# inside the event loop).


@pytest.mark.parametrize(
    "object_type, kwargs",
    [
        ("analog-input", dict(alarm_high_limit=100.0, alarm_time_delay=-1)),
        ("analog-input", dict(alarm_high_limit=100.0, alarm_time_delay_normal=-1)),
        ("analog-input", dict(alarm_high_limit=100.0, alarm_deadband=-0.5)),
        ("analog-input", dict(alarm_high_limit=100.0, alarm_event_enable=[True])),
        ("analog-input", dict(alarm_high_limit=100.0, alarm_limit_enable=[True])),
        ("analog-input", dict(alarm_high_limit=100.0, alarm_limit_enable=[True, True])),
        ("multi-state-value", dict(state_text=["a", "b"], alarm_values=[3])),
        ("multi-state-value", dict(state_text=["a", "b"], alarm_values=[0])),
    ],
)
def test_rejects_alarm_settings_the_device_cannot_run(object_type, kwargs):
    with pytest.raises(ValueError):
        validate_alarming_config(object_type, True, **kwargs)


def test_accepts_limit_enable_matching_the_configured_limits():
    validate_alarming_config(
        "analog-input", True, alarm_high_limit=100.0, alarm_limit_enable=[False, True]
    )


def test_accepts_alarm_values_within_the_states():
    validate_alarming_config("multi-state-value", True, state_text=["a", "b"], alarm_values=[1, 2])


def test_settings_on_a_non_alarming_object_are_not_checked():
    validate_alarming_config("analog-input", False, alarm_time_delay=-1)
