"""The connector rules in one place, taking plain values: the connector API
checks one save at a time, and the config import checks the whole file as it
will be after the sync. A new
rule goes here, not at a call site.

Every check raises ConnectorRuleError; `status` is the HTTP status the API
answers with, `str(exc)` its detail.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from .models import COMMANDABLE_TYPES
from .secrets_store import Unreadable

# (remote_object_type, remote_object_instance, remote_property, remote_array_index)
Point = tuple[str, int, str, "int | None"]

NOT_MAPPABLE_TYPES = ("schedule", "calendar", "notification-class")


class ConnectorRuleError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def check_remote_instance(device_id: int, remote_device_instance: int) -> None:
    if remote_device_instance == device_id:
        raise ConnectorRuleError(422, f"device {device_id} can't be its own remote device")


def check_same_device(connector_device_id: int, object_label: str, object_device_id: int) -> None:
    if object_device_id != connector_device_id:
        raise ConnectorRuleError(
            422,
            f"{object_label} is on device {object_device_id}; this connector belongs "
            f"to device {connector_device_id}",
        )


def check_mapping_target(object_label: str, object_type: str, simulated: bool) -> None:
    # A schedule/calendar Present_Value is calculated: a fed value would
    # bypass the scheduler until its next transition reverted it. A
    # notification class has no Present_Value at all.
    if object_type in NOT_MAPPABLE_TYPES:
        raise ConnectorRuleError(400, f"{object_type} objects can't be a connector-mapping target")
    # One feed per object: a simulator writes the same slot.
    if simulated:
        raise ConnectorRuleError(
            409,
            f"{object_label} is simulated; turn its simulator off before mapping a connector onto it",
        )


def check_json_path(response_format: str, json_path: str | None) -> None:
    # Without a json_path the mapping would fail every poll, only logged.
    if response_format == "json" and not json_path:
        raise ConnectorRuleError(400, "json_path is required for a json-format connector")


def check_remote_point_given(remote_object_type: str | None, remote_object_instance: int | None) -> None:
    if remote_object_type is None or remote_object_instance is None:
        raise ConnectorRuleError(
            422, "remote_object_type and remote_object_instance are required for a bacnet connector"
        )


def check_proxy_connector(connector_type: str) -> None:
    if connector_type != "bacnet":
        raise ConnectorRuleError(422, "proxy mappings are only for bacnet connectors")


def check_proxy(connector_type: str, object_type: str, fields: dict) -> None:
    """A proxy mapping's own rules. `fields` are the mapping's values after
    this create or edit. The echo guard needs the other connectors, so it is
    check_not_written / check_not_proxied."""
    if fields["direction"] != "proxy":
        return
    check_proxy_connector(connector_type)
    if object_type not in COMMANDABLE_TYPES:
        raise ConnectorRuleError(
            422,
            "a proxy needs a commandable local object (analog, binary or multi-state output or value); "
            f"{object_type} has no priority array to mirror",
        )
    if fields["scale"] != 1 or fields["offset"] != 0 or fields["value_map"] is not None:
        raise ConnectorRuleError(
            422, "a proxy passes values as they are: scale must be 1, offset 0, and no value map"
        )
    if (fields["remote_property"] or "present-value") != "present-value" or fields["remote_array_index"] is not None:
        raise ConnectorRuleError(422, "a proxy mirrors the remote's present-value, with no array index")


def check_not_written(point: Point, writes: Iterable[tuple[str, str, Point]]) -> None:
    """A proxy on `point`: no write mapping may write it. `writes` are the
    write mappings of every connector at the same remote device, on any local
    device, as (label, connector name, point)."""
    for label, connector_name, written in writes:
        if written == point:
            raise ConnectorRuleError(
                422,
                f"{label} (connector {connector_name!r}) already writes "
                f"{point[0]},{point[1]} present-value; a proxy on the same point would send its own value back",
            )


def check_not_proxied(target: Point, proxies: Iterable[tuple[str, str, Point]]) -> None:
    """The other side of the echo guard: a write mapping onto a point a proxy
    mirrors. `proxies` are the proxy mappings as (label, connector name,
    proxied point)."""
    for label, connector_name, proxied in proxies:
        if proxied == target:
            raise ConnectorRuleError(
                422,
                f"{label} (connector {connector_name!r}) proxies {target[0]},{target[1]} "
                "present-value; a write mapping on the same point would send the mirrored value back",
            )


def check_write_target_free(target: Point, others: Iterable[tuple[str, Point]]) -> None:
    """One write mapping per remote point per connector. Checked here, not by
    a UniqueConstraint: SQLite treats NULL array indexes as distinct."""
    for label, existing in others:
        if existing == target:
            raise ConnectorRuleError(409, f"{label} already writes {target[0]},{target[1]} {target[2]}")


def check_write_source(object_type: str) -> None:
    if object_type == "notification-class":
        raise ConnectorRuleError(400, "notification-class objects have no Present_Value to send")


def missing_credentials(
    *, auth_type: str, auth_password: str | None, auth_token: str | None, headers: dict | None
) -> list[str]:
    """What an http-poll connector still needs before it polls: the password,
    the token, `header <name>` for each header with no value."""
    missing = []
    if auth_type == "basic" and not auth_password:
        missing.append("password")
    if auth_type == "bearer" and not auth_token:
        missing.append("token")
    missing.extend(f"header {name}" for name, value in (headers or {}).items() if not value)
    return missing


def unreadable_credentials(
    *, auth_password: str | None, auth_token: str | None, headers: dict | None
) -> list[str]:
    """The saved credentials that can't be decrypted (secret.key lost or
    replaced), labelled as missing_credentials labels them. Each is also
    missing, because an unreadable value is empty."""
    unreadable = []
    if isinstance(auth_password, Unreadable):
        unreadable.append("password")
    if isinstance(auth_token, Unreadable):
        unreadable.append("token")
    unreadable.extend(f"header {name}" for name, value in (headers or {}).items() if isinstance(value, Unreadable))
    return unreadable


def missing_credentials_message(missing: list[str], unreadable: Sequence[str] = ()) -> str:
    message = f"Missing credentials: {', '.join(missing)}"
    if unreadable:
        message += f" (the saved {', '.join(unreadable)} can't be read; enter it again)"
    return message
