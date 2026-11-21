"""Pydantic models for the shapes saved config takes: devices, objects,
connectors and their mappings, network settings. The API, a JSON config
import, a CSV import and a BACnet restore all validate through them, so
they accept the same things."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from .bacnet_client import normalize_object_type, normalize_property, parse_address
from .choices import (
    AUTH_TYPES,
    CONNECTOR_TYPES,
    MAPPING_DIRECTIONS,
    NETWORK_ROLES,
    RESPONSE_FORMATS,
    canonical,
    canonicalize_object_fields,
)
from .models import MAX_INSTANCE, MAX_TREND_SAMPLE_CAP


class Body(BaseModel):
    """Base for every request body: refuses NaN and +/-Infinity in float
    fields. Python's JSON parser accepts them, but once saved they can't be
    JSON-encoded back out."""

    model_config = ConfigDict(allow_inf_nan=False)


def refuse_non_finite(value):
    """For `Any`-typed values, which allow_inf_nan doesn't reach."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


class DeviceCreate(Body):
    id: int = Field(..., ge=0, le=MAX_INSTANCE, description="BACnet device instance number")
    name: str
    description: Optional[str] = None
    location: Optional[str] = None
    # Vendor_Identifier is an Unsigned16 -- out of range, the device can't start.
    vendor_identifier: int = Field(999, ge=0, le=65535)
    # None falls back to ServerSettings.bind_address; validated in
    # web/api/devices.py.
    bind_address: Optional[str] = None
    vendor_name: Optional[str] = None
    # Checked together, with the links, by device_network.check_network.
    network_role: Literal["normal", "foreign", "bbmd"] = "normal"
    foreign_bbmd: Optional[str] = None
    # The foreign-device registration lifetime is an Unsigned16 of seconds.
    foreign_ttl: int = Field(60, ge=1, le=65535)
    bbmd_peers: Optional[List[str]] = None
    bbmd_links: List[int] = []

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        # A method, not the shared one-liner: _trimmed_name is defined below.
        return _trimmed_name(value)

    @field_validator("network_role", mode="before")
    @classmethod
    def _canonical_role(cls, value):
        return canonical(value, NETWORK_ROLES)


# Bounds for object int fields: past 64 bits SQLite can't store the int, and
# past the BACnet range the device can't start. Every import path validates
# through ObjectCreate, so these cover the API, JSON config and CSV alike.
UNSIGNED32_MAX = 4294967295
# Log_Interval is sent in hundredths of a second, as an Unsigned32.
_TREND_INTERVAL_MAX = UNSIGNED32_MAX // 100

# Connector and settings upper bounds: SQLite's 64-bit ints, and a huge
# retention would overflow its cutoff date.
_POLL_INTERVAL_MAX = 86400
_CONNECTOR_TIMEOUT_MAX = 300
_REWRITE_INTERVAL_MAX = 86400
RETENTION_DAYS_MAX = 36500

# value_map values: what coerce_mapped_value can use.
_ValueMap = Optional[Dict[str, Union[StrictBool, StrictInt, StrictFloat]]]


def _reject_blank(value: Optional[str]) -> Optional[str]:
    """Refuse an explicit null/blank for a NOT NULL column. *Update models
    type such fields Optional only so they can be omitted; pydantic v2 skips
    validators for an omitted field, so this fires only on a supplied value
    and turns it into a 422 instead of an IntegrityError 500. Spaces-only is
    blank; the value is checked, not trimmed."""
    if not value or not value.strip():
        raise ValueError("must not be null or blank")
    return value


def _trimmed_name(value: Optional[str]) -> Optional[str]:
    """A device name: refused blank, and trimmed, like an object name."""
    return _reject_blank(value).strip()


def _named_headers(value: Optional[dict]) -> Optional[dict]:
    if value is not None and any(not name.strip() for name in value):
        raise ValueError("a header needs a name")
    return value


def _reject_null(value):
    """Same idea as _reject_blank for non-string NOT NULL columns, where
    False/0 are real values and only an explicit null must be refused."""
    if value is None:
        raise ValueError("must not be null")
    return value


def _canonical_object_body(data):
    """Canonicalize fixed-choice values (any case accepted) before the
    Literal fields see them."""
    return canonicalize_object_fields(data) if isinstance(data, dict) else data


def _choice(*choices: str):
    def check(value):
        return canonical(value, choices)

    return check


class DeviceUpdate(Body):
    name: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    vendor_identifier: Optional[int] = Field(None, ge=0, le=65535)
    bind_address: Optional[str] = None
    vendor_name: Optional[str] = None
    network_role: Optional[Literal["normal", "foreign", "bbmd"]] = None
    foreign_bbmd: Optional[str] = None
    foreign_ttl: Optional[int] = Field(None, ge=1, le=65535)
    bbmd_peers: Optional[List[str]] = None
    bbmd_links: Optional[List[int]] = None
    backup_failure_timeout: Optional[int] = Field(None, ge=1, le=65535)
    # Sets the DCC/ReinitializeDevice password; null clears it, a name left
    # out keeps it. BACnet's password is a CharacterString of at most 20.
    bacnet_password: Optional[str] = Field(None, min_length=1, max_length=20)

    _validate_name = field_validator("name")(_trimmed_name)
    _validate_not_null = field_validator(
        "vendor_identifier", "network_role", "foreign_ttl", "bbmd_links", "backup_failure_timeout"
    )(_reject_null)
    _canonical_role = field_validator("network_role", mode="before")(_choice(*NETWORK_ROLES))


class ScheduleTimeValue(Body):
    time: str
    value: Optional[str] = None


class ScheduleCalendarEntry(Body):
    """One calendar `date_list` entry, or a schedule exception's `period`.
    Which fields apply depends on `kind`; the calendar/schedule validators
    check the rest."""

    kind: Literal["date", "date-range", "week-and-day", "calendar-reference"]
    year: Optional[Any] = None
    month: Optional[Any] = None
    day: Optional[Any] = None
    start: Optional[str] = None
    end: Optional[str] = None
    week_of_month: Optional[Any] = None
    day_of_week: Optional[Any] = None
    instance_number: Optional[int] = None


class ScheduleException(Body):
    period: ScheduleCalendarEntry
    priority: int = Field(..., ge=1, le=16)
    time_values: List[ScheduleTimeValue] = Field(default_factory=list)


class ScheduleWriteTarget(Body):
    object_type: str
    instance_number: int
    property_identifier: str = "presentValue"
    # Another device's instance;
    # None means this device.
    device_instance: Optional[int] = None


class NotificationRecipientEntry(Body):
    """One notification_recipient_list entry;
    the Notification Class validator checks its shape and ranges."""

    recipient: dict
    valid_days: List[bool]
    from_time: str
    to_time: str
    transitions: List[bool]
    confirmed: bool
    process_identifier: int = Field(ge=0, le=UNSIGNED32_MAX)


class ObjectCreate(Body):
    object_type: str
    instance_number: int = Field(..., ge=0, le=MAX_INSTANCE)
    name: str
    description: Optional[str] = None
    units: Optional[str] = None
    bacnet_writable: bool = True
    out_of_service_default: bool = False
    relinquish_default: Optional[str] = None
    active_text: Optional[str] = None
    inactive_text: Optional[str] = None
    start_value: Optional[str] = None
    state_text: Optional[List[str]] = None
    trended: bool = False
    trend_mode: Literal["interval", "cov"] = "interval"
    trend_interval_seconds: Optional[int] = Field(None, ge=1, le=_TREND_INTERVAL_MAX)
    trend_sample_cap: Optional[int] = Field(None, ge=1, le=MAX_TREND_SAMPLE_CAP)
    trend_log_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    trend_log_name: Optional[str] = None
    # BUFFER_READY reporting settings; trend_last_notify_record is
    # runtime state (TrendState), deliberately absent.
    trend_reporting_enabled: bool = False
    trend_notification_threshold: int = Field(10, ge=0, le=4294967295)
    trend_notification_class_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    trend_event_enable: Optional[List[bool]] = None
    trend_notify_type: Literal["alarm", "event"] = "event"
    trend_event_detection_enable: bool = True
    # Schedule/calendar only (fields documented on models.BacnetObject).
    schedule_value_type: Optional[Literal["analog", "binary", "multi-state"]] = None
    schedule_default: Optional[str] = None
    weekly_schedule: Optional[List[List[ScheduleTimeValue]]] = None
    exception_schedule: Optional[List[ScheduleException]] = None
    schedule_effective_start: Optional[str] = None
    schedule_effective_end: Optional[str] = None
    schedule_priority: int = Field(14, ge=1, le=16)
    schedule_write_targets: Optional[List[ScheduleWriteTarget]] = None
    date_list: Optional[List[ScheduleCalendarEntry]] = None
    # Alarming; ignored for non-alarmable types.
    alarming_enabled: bool = False
    notification_class_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    alarm_time_delay: int = Field(0, ge=0, le=UNSIGNED32_MAX)
    alarm_time_delay_normal: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    alarm_notify_type: Literal["alarm", "event"] = "alarm"
    alarm_event_enable: Optional[List[bool]] = None
    alarm_high_limit: Optional[float] = None
    alarm_low_limit: Optional[float] = None
    alarm_deadband: float = 0.0
    alarm_limit_enable: Optional[List[bool]] = None
    alarm_value: Optional[bool] = None
    alarm_values: Optional[List[int]] = None
    alarm_event_detection_enable: bool = True
    # Notification Class object only.
    notification_priority: Optional[List[int]] = None
    notification_ack_required: Optional[List[bool]] = None
    notification_recipient_list: Optional[List[NotificationRecipientEntry]] = None
    # Value simulator: checked in the route, since it depends on the object type.
    simulation: Optional[Dict[str, Any]] = None

    _validate_name = field_validator("name")(_reject_blank)

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, data):
        return _canonical_object_body(data)


class ObjectUpdate(Body):
    name: Optional[str] = None
    description: Optional[str] = None
    units: Optional[str] = None
    bacnet_writable: Optional[bool] = None
    out_of_service_default: Optional[bool] = None
    relinquish_default: Optional[str] = None
    active_text: Optional[str] = None
    inactive_text: Optional[str] = None
    start_value: Optional[str] = None
    state_text: Optional[List[str]] = None
    trended: Optional[bool] = None
    trend_mode: Optional[Literal["interval", "cov"]] = None
    trend_interval_seconds: Optional[int] = Field(None, ge=1, le=_TREND_INTERVAL_MAX)
    trend_sample_cap: Optional[int] = Field(None, ge=1, le=MAX_TREND_SAMPLE_CAP)
    trend_log_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    trend_log_name: Optional[str] = None
    trend_reporting_enabled: Optional[bool] = None
    trend_notification_threshold: Optional[int] = Field(None, ge=0, le=4294967295)
    trend_notification_class_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    trend_event_enable: Optional[List[bool]] = None
    trend_notify_type: Optional[Literal["alarm", "event"]] = None
    trend_event_detection_enable: Optional[bool] = None
    schedule_value_type: Optional[Literal["analog", "binary", "multi-state"]] = None
    schedule_default: Optional[str] = None
    weekly_schedule: Optional[List[List[ScheduleTimeValue]]] = None
    exception_schedule: Optional[List[ScheduleException]] = None
    schedule_effective_start: Optional[str] = None
    schedule_effective_end: Optional[str] = None
    schedule_priority: Optional[int] = Field(None, ge=1, le=16)
    schedule_write_targets: Optional[List[ScheduleWriteTarget]] = None
    date_list: Optional[List[ScheduleCalendarEntry]] = None
    alarming_enabled: Optional[bool] = None
    notification_class_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    alarm_time_delay: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    alarm_time_delay_normal: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    alarm_notify_type: Optional[Literal["alarm", "event"]] = None
    alarm_event_enable: Optional[List[bool]] = None
    alarm_high_limit: Optional[float] = None
    alarm_low_limit: Optional[float] = None
    alarm_deadband: Optional[float] = None
    alarm_limit_enable: Optional[List[bool]] = None
    alarm_value: Optional[bool] = None
    alarm_values: Optional[List[int]] = None
    alarm_event_detection_enable: Optional[bool] = None
    notification_priority: Optional[List[int]] = None
    notification_ack_required: Optional[List[bool]] = None
    notification_recipient_list: Optional[List[NotificationRecipientEntry]] = None
    simulation: Optional[Dict[str, Any]] = None

    _validate_name = field_validator("name")(_reject_blank)
    _validate_not_null = field_validator(
        "bacnet_writable",
        "out_of_service_default",
        "trended",
        "trend_mode",
        "trend_reporting_enabled",
        "trend_notification_threshold",
        "trend_notify_type",
        "trend_event_detection_enable",
        "schedule_priority",
        "alarming_enabled",
        "alarm_time_delay",
        "alarm_notify_type",
        "alarm_deadband",
        "alarm_event_detection_enable",
    )(_reject_null)


    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, data):
        return _canonical_object_body(data)


def _require_http_scheme(url: Optional[str]) -> Optional[str]:
    """Restrict connector URLs to http(s): a sanity check, not SSRF
    hardening. Private/loopback addresses stay allowed by design, since
    anyone who can reach the web UI is trusted."""
    if not url:
        # Only an explicitly supplied null/"" gets here (see _reject_blank).
        raise ValueError("url must not be null or blank")
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"url must start with http:// or https:// (got {url!r})")
    return url


def bacnet_address(value: Optional[str]) -> Optional[str]:
    """A bacnet connector's static remote address; blank means "bind by
    Who-Is". Accepted forms: bacnet_client.parse_address."""
    if value is None or not value.strip():
        return None
    parse_address(value)
    return value.strip()


def _optional_object_type(value: Optional[str]) -> Optional[str]:
    return None if value is None else normalize_object_type(value)


def _optional_property(value: Optional[str]) -> Optional[str]:
    return None if value is None else normalize_property(value)


class ConnectorCreate(Body):
    name: str
    # "bacnet": one remote device, run by device_id's own process.
    type: Literal["http-poll", "bacnet"] = "http-poll"
    url: Optional[str] = None
    enabled: bool = True
    poll_interval_seconds: int = Field(30, ge=1, le=_POLL_INTERVAL_MAX)
    timeout_seconds: int = Field(10, ge=1, le=_CONNECTOR_TIMEOUT_MAX)
    response_format: Literal["json", "plain-text"] = "json"
    auth_type: Literal["none", "basic", "bearer"] = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None
    # A null value is a header not set yet.
    headers: Optional[Dict[str, Optional[str]]] = None
    # False skips the TLS certificate check; http-poll only.
    verify_tls: bool = True
    device_id: Optional[int] = None
    remote_device_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    remote_address: Optional[str] = None
    rewrite_interval_seconds: int = Field(60, ge=0, le=_REWRITE_INTERVAL_MAX)

    _validate_name = field_validator("name")(_reject_blank)
    _validate_url = field_validator("url")(_require_http_scheme)
    _validate_remote_address = field_validator("remote_address")(bacnet_address)
    _validate_headers = field_validator("headers")(_named_headers)
    _canonical_type =field_validator("type", mode="before")(_choice(*CONNECTOR_TYPES))
    _canonical_format = field_validator("response_format", mode="before")(_choice(*RESPONSE_FORMATS))
    _canonical_auth = field_validator("auth_type", mode="before")(_choice(*AUTH_TYPES))

    @model_validator(mode="after")
    def _fields_for_type(self):
        if self.type == "http-poll":
            if not self.url:
                raise ValueError("url is required for an http-poll connector")
        else:
            if self.device_id is None:
                raise ValueError("device_id is required for a bacnet connector")
            if self.remote_device_instance is None:
                raise ValueError("remote_device_instance is required for a bacnet connector")
            # Connector.url is NOT NULL; bacnet connectors store "".
            self.url = ""
        return self


class ConnectorUpdate(Body):
    # type and device_id aren't here: they can't change after creation
    # (moving a connector would mean moving its mappings too).
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None
    poll_interval_seconds: Optional[int] = Field(None, ge=1, le=_POLL_INTERVAL_MAX)
    timeout_seconds: Optional[int] = Field(None, ge=1, le=_CONNECTOR_TIMEOUT_MAX)
    response_format: Optional[Literal["json", "plain-text"]] = None
    auth_type: Optional[Literal["none", "basic", "bearer"]] = None
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None
    # A null value keeps the saved one; a name left out is removed.
    headers: Optional[Dict[str, Optional[str]]] = None
    verify_tls: Optional[bool] = None
    remote_device_instance: Optional[int] = Field(None, ge=0, le=MAX_INSTANCE)
    remote_address: Optional[str] = None
    rewrite_interval_seconds: Optional[int] = Field(None, ge=0, le=_REWRITE_INTERVAL_MAX)

    _validate_name = field_validator("name")(_reject_blank)
    _validate_url = field_validator("url")(_require_http_scheme)
    _validate_remote_address = field_validator("remote_address")(bacnet_address)
    _validate_headers = field_validator("headers")(_named_headers)
    _validate_not_null = field_validator(
        "enabled", "poll_interval_seconds", "timeout_seconds", "response_format", "auth_type",
        "verify_tls", "remote_device_instance", "rewrite_interval_seconds",
    )(_reject_null)
    _canonical_format = field_validator("response_format", mode="before")(_choice(*RESPONSE_FORMATS))
    _canonical_auth = field_validator("auth_type", mode="before")(_choice(*AUTH_TYPES))


class ConnectorMappingCreate(Body):
    object_id: int
    json_path: Optional[str] = None
    scale: float = 1.0
    offset: float = 0.0
    value_map: _ValueMap = None
    # bacnet connectors: the remote point to poll.
    remote_object_type: Optional[str] = None
    remote_object_instance: Optional[int] = Field(None, ge=0, le=4194303)
    remote_property: Optional[str] = None
    remote_array_index: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    direction: Literal["read", "proxy"] = "read"

    _normalize_type = field_validator("remote_object_type")(_optional_object_type)
    _normalize_property = field_validator("remote_property")(_optional_property)
    _canonical_direction = field_validator("direction", mode="before")(_choice(*MAPPING_DIRECTIONS))


class ConnectorMappingUpdate(Body):
    json_path: Optional[str] = None
    scale: Optional[float] = None
    offset: Optional[float] = None
    value_map: _ValueMap = None
    remote_object_type: Optional[str] = None
    remote_object_instance: Optional[int] = Field(None, ge=0, le=4194303)
    remote_property: Optional[str] = None
    remote_array_index: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    direction: Optional[Literal["read", "proxy"]] = None

    _normalize_type = field_validator("remote_object_type")(_optional_object_type)
    _normalize_property = field_validator("remote_property")(_optional_property)
    _canonical_direction = field_validator("direction", mode="before")(_choice(*MAPPING_DIRECTIONS))
    _validate_not_null = field_validator(
        "scale", "offset", "remote_object_type", "remote_object_instance", "remote_property", "direction"
    )(_reject_null)


class ConnectorWriteMappingCreate(Body):
    """A bacnet connector's write mapping: object_id's Present_Value is
    written to the remote point at `priority`."""

    object_id: int
    remote_object_type: str
    remote_object_instance: int = Field(ge=0, le=4194303)
    remote_property: str = "present-value"
    remote_array_index: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    priority: int = Field(8, ge=1, le=16)

    _normalize_type = field_validator("remote_object_type")(normalize_object_type)
    _normalize_property = field_validator("remote_property")(normalize_property)


class ConnectorWriteMappingUpdate(Body):
    remote_object_type: Optional[str] = None
    remote_object_instance: Optional[int] = Field(None, ge=0, le=4194303)
    remote_property: Optional[str] = None
    remote_array_index: Optional[int] = Field(None, ge=0, le=UNSIGNED32_MAX)
    priority: Optional[int] = Field(None, ge=1, le=16)

    _normalize_type = field_validator("remote_object_type")(_optional_object_type)
    _normalize_property = field_validator("remote_property")(_optional_property)
    _validate_not_null = field_validator(
        "remote_object_type", "remote_object_instance", "remote_property", "priority"
    )(_reject_null)


_PER_DEVICE_NETWORK_FIELDS = ("network_role", "foreign_bbmd", "foreign_ttl", "bbmd_peers")


class NetworkSettingsUpdate(Body):
    # Refuses unknown keys, so a stale client sending the retired auth_* fields
    # gets a 422 rather than a silent no-op.
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    bind_address: Optional[str] = None
    trend_retention_days: Optional[int] = Field(None, ge=1, le=RETENTION_DAYS_MAX)
    trend_soft_limit_mb: Optional[int] = Field(None, ge=0, le=1024)

    @model_validator(mode="before")
    @classmethod
    def _refuse_per_device_fields(cls, data):
        if isinstance(data, dict):
            moved = [name for name in _PER_DEVICE_NETWORK_FIELDS if name in data]
            if moved:
                raise ValueError(f"{', '.join(moved)}: the network role is set per device (PUT /api/devices/{{id}})")
        return data


class ConfigImportError(ValueError):
    """Message names the offending device/object."""


def refuse_unknown_keys(data: dict, known, *, context: str) -> None:
    """An import is a full sync, so a misspelled key would silently reset
    the real field to its default."""
    unknown = sorted(set(data) - set(known))
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ConfigImportError(f"{context}: unknown {'field' if len(unknown) == 1 else 'fields'} {names}")
