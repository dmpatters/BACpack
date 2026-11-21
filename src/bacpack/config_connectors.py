"""Connectors and retention settings in the whole-server JSON config:
export, parse, plan and apply.

Secrets never reach the file: auth_password and auth_token are left out and
a header's value is written as null, so an export can be shared and an
import over an existing install keeps what it already has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .choices import CONNECTOR_TYPES, canonical
from .config_schemas import (
    RETENTION_DAYS_MAX,
    ConfigImportError,
    ConnectorCreate,
    ConnectorMappingCreate,
    ConnectorWriteMappingCreate,
    NetworkSettingsUpdate,
    refuse_unknown_keys,
)
from .connector_config import (
    ConnectorRuleError,
    check_json_path,
    check_mapping_target,
    check_not_proxied,
    check_not_written,
    check_proxy,
    check_remote_instance,
    check_remote_point_given,
    check_write_source,
    check_write_target_free,
    missing_credentials,
)
from .connectors import release_http_mappings
from .models import (
    MAX_INSTANCE,
    OBJECT_TYPES,
    BacnetObject,
    Connector,
    ConnectorMapping,
    ConnectorWriteMapping,
    ServerSettings,
    get_or_create_settings,
)

SETTINGS_DEFAULTS = {"trend_retention_days": 90, "log_retention_days": 14, "trend_soft_limit_mb": None}


def object_label(device_id: int, object_type: str, instance: int) -> str:
    """How every message and plan key names an object: `599001 analog-value:1`."""
    return f"{device_id} {object_type}:{instance}"


def _object_ref(object_type: str, instance: int) -> str:
    return f"{object_type}:{instance}"


def write_point(mapping: dict) -> tuple:
    """A parsed write mapping's remote point."""
    return (
        mapping["remote_object_type"],
        mapping["remote_object_instance"],
        mapping["remote_property"],
        mapping["remote_array_index"],
    )


# --- Export ----------------------------------------------------------------


def export_settings(settings: ServerSettings) -> dict:
    return {key: getattr(settings, key) for key in SETTINGS_DEFAULTS}


def export_connectors(connectors: Iterable[Connector]) -> list[dict]:
    return [_connector_to_dict(c) for c in sorted(connectors, key=lambda c: c.name)]


def _connector_to_dict(connector: Connector) -> dict:
    if connector.type == "bacnet":
        return {
            "name": connector.name,
            "type": connector.type,
            "enabled": connector.enabled,
            "device": connector.device_id,
            "remote_device_instance": connector.remote_device_instance,
            "remote_address": connector.remote_address,
            "poll_interval_seconds": connector.poll_interval_seconds,
            "timeout_seconds": connector.timeout_seconds,
            "rewrite_interval_seconds": connector.rewrite_interval_seconds,
            "mappings": [_bacnet_mapping_to_dict(m) for m in _by_object(connector.mappings)],
            "write_mappings": [_write_mapping_to_dict(m) for m in _by_remote_point(connector.write_mappings)],
        }
    return {
        "name": connector.name,
        "type": connector.type,
        "enabled": connector.enabled,
        "url": connector.url,
        "poll_interval_seconds": connector.poll_interval_seconds,
        "timeout_seconds": connector.timeout_seconds,
        "response_format": connector.response_format,
        "auth_type": connector.auth_type,
        "auth_username": connector.auth_username,
        "headers": {name: None for name in (connector.headers or {})},
        "verify_tls": connector.verify_tls,
        "mappings": [_http_mapping_to_dict(m) for m in _by_object(connector.mappings)],
    }


def _by_object(mappings) -> list[ConnectorMapping]:
    return sorted(mappings, key=lambda m: (m.object.device_id, m.object.object_type, m.object.instance_number))


def _by_remote_point(mappings) -> list[ConnectorWriteMapping]:
    return sorted(
        mappings,
        key=lambda m: (
            m.remote_object_type,
            m.remote_object_instance,
            m.remote_property,
            -1 if m.remote_array_index is None else m.remote_array_index,
        ),
    )


def _http_mapping_to_dict(mapping: ConnectorMapping) -> dict:
    obj = mapping.object
    return {
        "device": obj.device_id,
        "object": _object_ref(obj.object_type, obj.instance_number),
        "json_path": mapping.json_path,
        "scale": mapping.scale,
        "offset": mapping.offset,
        "value_map": mapping.value_map,
    }


def _bacnet_mapping_to_dict(mapping: ConnectorMapping) -> dict:
    obj = mapping.object
    return {
        "object": _object_ref(obj.object_type, obj.instance_number),
        "remote_object_type": mapping.remote_object_type,
        "remote_object_instance": mapping.remote_object_instance,
        "remote_property": mapping.remote_property,
        "remote_array_index": mapping.remote_array_index,
        "scale": mapping.scale,
        "offset": mapping.offset,
        "value_map": mapping.value_map,
        "direction": mapping.direction,
    }


def _write_mapping_to_dict(mapping: ConnectorWriteMapping) -> dict:
    obj = mapping.object
    return {
        "object": _object_ref(obj.object_type, obj.instance_number),
        "remote_object_type": mapping.remote_object_type,
        "remote_object_instance": mapping.remote_object_instance,
        "remote_property": mapping.remote_property,
        "remote_array_index": mapping.remote_array_index,
        "priority": mapping.priority,
    }


# --- Parse -------------------------------------------------------------------

_HTTP_KEYS = (
    "name", "type", "enabled", "url", "poll_interval_seconds", "timeout_seconds", "response_format",
    "auth_type", "auth_username", "headers", "verify_tls", "mappings",
)
_BACNET_KEYS = (
    "name", "type", "enabled", "device", "remote_device_instance", "remote_address",
    "poll_interval_seconds", "timeout_seconds", "rewrite_interval_seconds", "mappings", "write_mappings",
)
_HTTP_MAPPING_KEYS = ("device", "object", "json_path", "scale", "offset", "value_map")
_BACNET_MAPPING_KEYS = (
    "object", "remote_object_type", "remote_object_instance", "remote_property", "remote_array_index",
    "scale", "offset", "value_map", "direction",
)
_WRITE_MAPPING_KEYS = (
    "object", "remote_object_type", "remote_object_instance", "remote_property", "remote_array_index", "priority",
)
_HEADER_VALUE_REFUSAL = "header values aren't carried in the file; set them on the Connectors page"


def _refuse(context: str, message: str):
    return ConfigImportError(f"{context}: {message}")


def _validated(model: type[BaseModel], data: dict, context: str) -> BaseModel:
    """`data` through the API's own request model, so a file can't store what
    the API would refuse. Strict, so "no" isn't read as False; the model's
    validators also give a fixed choice its canonical spelling."""
    try:
        return model.model_validate(data, strict=True)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        raise _refuse(context, f"{where}: {message}" if where else message) from None


def parse_settings(raw) -> dict:

    if not isinstance(raw, dict):
        raise _refuse("settings", "must be an object")
    refuse_unknown_keys(raw, SETTINGS_DEFAULTS, context="settings")
    # A missing key is the default; null is a value: keep forever (or no soft
    # limit), which is how the export writes it.
    values = {key: raw[key] if key in raw else SETTINGS_DEFAULTS[key] for key in SETTINGS_DEFAULTS}
    # The settings API's own ranges; log_retention_days has no API, so it
    # takes the trend retention's.
    _validated(
        NetworkSettingsUpdate,
        {"trend_retention_days": values["trend_retention_days"], "trend_soft_limit_mb": values["trend_soft_limit_mb"]},
        "settings",
    )
    days = values["log_retention_days"]
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= RETENTION_DAYS_MAX):
        raise _refuse("settings", f"log_retention_days: must be a whole number 1-{RETENTION_DAYS_MAX}, or null to keep forever")
    return values


def parse_connectors(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise _refuse("connectors", "must be a list")
    parsed, seen_names = [], set()
    for i, connector_raw in enumerate(raw):
        context = f"connectors[{i}]"
        if not isinstance(connector_raw, dict):
            raise _refuse(context, "must be an object")
        if isinstance(connector_raw.get("name"), str):
            context = f"{context} ({connector_raw['name']!r})"
        connector = _parse_connector(connector_raw, context)
        if connector["name"] in seen_names:
            raise _refuse(context, f"duplicate connector name {connector['name']!r} in this file")
        seen_names.add(connector["name"])
        parsed.append(connector)
    return parsed


def _parse_connector(raw: dict, context: str) -> dict:

    connector_type = canonical(raw.get("type", "http-poll"), CONNECTOR_TYPES)
    if connector_type not in CONNECTOR_TYPES:
        raise _refuse(context, "type: must be 'http-poll' or 'bacnet'")
    bacnet = connector_type == "bacnet"
    refuse_unknown_keys(raw, _BACNET_KEYS if bacnet else _HTTP_KEYS, context=context)

    if bacnet:
        if raw.get("device") is None:
            raise _refuse(context, "device is required for a bacnet connector")
        fields = {k: raw[k] for k in _BACNET_KEYS if k in raw and k not in ("device", "mappings", "write_mappings")}
        fields["device_id"] = raw["device"]
    else:
        fields = {k: raw[k] for k in _HTTP_KEYS if k in raw and k not in ("headers", "mappings")}
    headers = _parse_headers(raw.get("headers"), context)
    model = _validated(ConnectorCreate, fields, context)
    if bacnet:
        try:
            check_remote_instance(model.device_id, model.remote_device_instance)
        except ConnectorRuleError as exc:
            raise _refuse(context, str(exc)) from exc

    connector = {
        "name": model.name,
        "type": model.type,
        "enabled": model.enabled,
        "url": model.url,
        "poll_interval_seconds": model.poll_interval_seconds,
        "timeout_seconds": model.timeout_seconds,
        "response_format": model.response_format,
        "auth_type": model.auth_type,
        "auth_username": model.auth_username,
        "headers": {} if bacnet else headers,
        "verify_tls": model.verify_tls,
        "device_id": model.device_id if bacnet else None,
        "remote_device_instance": model.remote_device_instance if bacnet else None,
        "remote_address": model.remote_address if bacnet else None,
        "rewrite_interval_seconds": model.rewrite_interval_seconds,
    }
    connector["mappings"] = _parse_mappings(raw.get("mappings", []), connector, context)
    connector["write_mappings"] = (
        _parse_write_mappings(raw.get("write_mappings", []), connector, context) if bacnet else []
    )
    return connector


def _parse_headers(raw, context: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _refuse(context, "headers: must be an object of header names")
    if any(value is not None for value in raw.values()):
        raise _refuse(context, _HEADER_VALUE_REFUSAL)
    return dict(raw)


def _parse_object_ref(raw, context: str) -> tuple[str, int]:

    if isinstance(raw, str) and raw.count(":") == 1:
        type_part, _, instance = raw.partition(":")
        object_type = canonical(type_part.strip(), OBJECT_TYPES)
        if object_type in OBJECT_TYPES and instance.isascii() and instance.isdigit() and int(instance) <= MAX_INSTANCE:
            return object_type, int(instance)
    raise _refuse(context, f"object must be '<object-type>:<instance>', got {raw!r}")


def _list_of(raw, key: str, context: str) -> list:
    if not isinstance(raw, list):
        raise _refuse(context, f"{key}: must be a list")
    return raw


def _parse_mappings(raw, connector: dict, context: str) -> list[dict]:
    bacnet = connector["type"] == "bacnet"
    mappings, seen = [], set()
    for j, mapping_raw in enumerate(_list_of(raw, "mappings", context)):
        mapping_context = f"{context}.mappings[{j}]"
        if not isinstance(mapping_raw, dict):
            raise _refuse(mapping_context, "must be an object")
        refuse_unknown_keys(
            mapping_raw, _BACNET_MAPPING_KEYS if bacnet else _HTTP_MAPPING_KEYS, context=mapping_context
        )
        object_type, instance = _parse_object_ref(mapping_raw.get("object"), mapping_context)
        if bacnet:
            device_id = connector["device_id"]
        else:
            device_id = mapping_raw.get("device")
            if isinstance(device_id, bool) or not isinstance(device_id, int) or not 0 <= device_id <= MAX_INSTANCE:
                raise _refuse(mapping_context, f"device must be a device id, 0-{MAX_INSTANCE}")
        key = (device_id, object_type, instance)
        if key in seen:
            raise _refuse(mapping_context, f"{object_type}:{instance} is mapped twice in this connector")
        seen.add(key)

        fields = {k: v for k, v in mapping_raw.items() if k not in ("device", "object")}
        model = _validated(ConnectorMappingCreate, {"object_id": 0, **fields}, mapping_context)
        mapping = {
            "object_key": key,
            "json_path": None if bacnet else model.json_path,
            "scale": model.scale,
            "offset": model.offset,
            "value_map": model.value_map,
            "remote_object_type": model.remote_object_type if bacnet else None,
            "remote_object_instance": model.remote_object_instance if bacnet else None,
            "remote_property": (model.remote_property or "present-value") if bacnet else None,
            "remote_array_index": model.remote_array_index if bacnet else None,
            "direction": model.direction if bacnet else "read",
        }
        try:
            if bacnet:
                check_remote_point_given(mapping["remote_object_type"], mapping["remote_object_instance"])
                check_proxy("bacnet", object_type, mapping)
            else:
                check_json_path(connector["response_format"], mapping["json_path"])
        except ConnectorRuleError as exc:
            raise _refuse(mapping_context, str(exc)) from exc
        mappings.append(mapping)
    return mappings


def _parse_write_mappings(raw, connector: dict, context: str) -> list[dict]:
    mappings = []
    for j, mapping_raw in enumerate(_list_of(raw, "write_mappings", context)):
        mapping_context = f"{context}.write_mappings[{j}]"
        if not isinstance(mapping_raw, dict):
            raise _refuse(mapping_context, "must be an object")
        refuse_unknown_keys(mapping_raw, _WRITE_MAPPING_KEYS, context=mapping_context)
        object_type, instance = _parse_object_ref(mapping_raw.get("object"), mapping_context)
        fields = {k: v for k, v in mapping_raw.items() if k != "object"}
        model = _validated(ConnectorWriteMappingCreate, {"object_id": 0, **fields}, mapping_context)
        mapping = {
            "object_key": (connector["device_id"], object_type, instance),
            "remote_object_type": model.remote_object_type,
            "remote_object_instance": model.remote_object_instance,
            "remote_property": model.remote_property,
            "remote_array_index": model.remote_array_index,
            "priority": model.priority,
        }
        try:
            check_write_source(object_type)
            check_write_target_free(
                write_point(mapping), [(f"write_mappings[{k}]", write_point(m)) for k, m in enumerate(mappings)]
            )
        except ConnectorRuleError as exc:
            raise _refuse(mapping_context, str(exc)) from exc
        mappings.append(mapping)
    return mappings


# --- Plan --------------------------------------------------------------------
# Nothing here adds to the session: a dry run plans exactly what an apply
# would, against the state the sync will leave.

ObjectKey = tuple[int, str, int]


@dataclass
class ObjectAfter:
    object_type: str
    simulated: bool


@dataclass
class ConnectorPlan:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    # connector name -> {"created": [...], "updated": [...], "deleted": [...]}
    mappings: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # connector name -> what a stored credential is missing for
    needs_credentials: dict[str, list[str]] = field(default_factory=dict)
    parsed: list[dict] = field(default_factory=list)


def objects_after_sync(
    session: Session, parsed_devices: list[dict], *, only_device_id: int | None = None
) -> dict[ObjectKey, ObjectAfter]:
    """The objects that will exist once the sync has run. A whole-server
    import leaves exactly the file's; a restore (`only_device_id`) replaces
    one device's and keeps every other device's as stored."""
    after: dict[ObjectKey, ObjectAfter] = {}
    if only_device_id is not None:
        for obj in session.scalars(select(BacnetObject).where(BacnetObject.device_id != only_device_id)):
            after[(obj.device_id, obj.object_type, obj.instance_number)] = ObjectAfter(
                obj.object_type, bool(obj.simulation)
            )
    for device in parsed_devices:
        for row in device["objects"]:
            key = (device["id"], row["object_type"], row["instance_number"])
            after[key] = ObjectAfter(row["object_type"], bool(row.get("simulation")))
    return after


def _write_label(point: tuple) -> str:
    index = f"[{point[3]}]" if point[3] is not None else ""
    return f"write {point[0]},{point[1]} {point[2]}{index}"


def _stored_key(mapping: ConnectorMapping) -> ObjectKey:
    obj = mapping.object
    return (obj.device_id, obj.object_type, obj.instance_number)


def _stored_point(mapping: ConnectorWriteMapping) -> tuple:
    return (
        mapping.remote_object_type,
        mapping.remote_object_instance,
        mapping.remote_property,
        mapping.remote_array_index,
    )


def plan_connectors(
    session: Session, parsed: list[dict], objects_after: dict[ObjectKey, ObjectAfter], device_ids_after: set[int]
) -> ConnectorPlan:
    """Check the file's connectors against the install as it will be after
    the sync and say what an apply would do. Raises ConfigImportError."""
    stored = {c.name: c for c in session.scalars(select(Connector))}
    plan = ConnectorPlan(parsed=parsed)
    for connector in parsed:
        existing = stored.get(connector["name"])
        _check_identity(connector, existing)
        _check_targets(connector, objects_after, device_ids_after)
        (plan.updated if existing is not None else plan.created).append(connector["name"])
        plan.mappings[connector["name"]] = _mapping_changes(connector, existing)
        missing = _missing_credentials(connector, existing)
        if missing:
            plan.needs_credentials[connector["name"]] = missing
    _check_after_state(parsed, stored, objects_after, device_ids_after)
    return plan


def _describe(connector_type: str, device_id: int | None) -> str:
    if connector_type == "bacnet":
        return f"a bacnet connector on device {device_id}"
    return "an http-poll connector"


def _check_identity(connector: dict, existing: Connector | None) -> None:
    """A connector's type and device can't change in place, as in the API."""
    if existing is None:
        return
    name = connector["name"]
    ending = "and a connector's type and device can't change"
    if existing.type != connector["type"]:
        raise ConfigImportError(
            f"connector {name!r} is {_describe(existing.type, existing.device_id)} here; "
            f"the file has it as {_describe(connector['type'], connector['device_id'])}, {ending}"
        )
    if connector["type"] == "bacnet" and existing.device_id != connector["device_id"]:
        raise ConfigImportError(
            f"connector {name!r} is {_describe(existing.type, existing.device_id)} here; "
            f"the file has it on device {connector['device_id']}, {ending}"
        )


def _check_targets(connector: dict, objects_after: dict, device_ids_after: set[int]) -> None:
    name = connector["name"]
    if connector["type"] == "bacnet" and connector["device_id"] not in device_ids_after:
        raise ConfigImportError(
            f"connector {name!r}: device {connector['device_id']} isn't on this install and isn't in the file"
        )
    for mapping in (*connector["mappings"], *connector["write_mappings"]):
        key = mapping["object_key"]
        if key not in objects_after:
            raise ConfigImportError(f"connector {name!r}: no object {object_label(*key)} after this import")
    for mapping in connector["mappings"]:
        key = mapping["object_key"]
        target = objects_after[key]
        try:
            check_mapping_target(f"object {object_label(*key)}", target.object_type, target.simulated)
        except ConnectorRuleError as exc:
            raise ConfigImportError(f"connector {name!r}: {exc}") from exc


def _mapping_changes(connector: dict, existing: Connector | None) -> dict[str, list[str]]:
    stored_reads = {_stored_key(m) for m in existing.mappings} if existing is not None else set()
    stored_writes = {_stored_point(m) for m in existing.write_mappings} if existing is not None else set()
    changes: dict[str, list[str]] = {"created": [], "updated": [], "deleted": []}
    file_reads = {m["object_key"] for m in connector["mappings"]}
    file_writes = {write_point(m) for m in connector["write_mappings"]}
    for mapping in connector["mappings"]:
        key = mapping["object_key"]
        changes["updated" if key in stored_reads else "created"].append(object_label(*key))
    for mapping in connector["write_mappings"]:
        point = write_point(mapping)
        changes["updated" if point in stored_writes else "created"].append(_write_label(point))
    changes["deleted"] = [object_label(*key) for key in sorted(stored_reads - file_reads)] + [
        _write_label(point) for point in sorted(stored_writes - file_writes, key=_point_sort_key)
    ]
    return changes


def _point_sort_key(point: tuple) -> tuple:
    return (point[0], point[1], point[2], -1 if point[3] is None else point[3])


def _missing_credentials(connector: dict, existing: Connector | None) -> list[str]:
    if connector["type"] != "http-poll":
        return []
    stored_headers = (existing.headers or {}) if existing is not None else {}
    return missing_credentials(
        auth_type=connector["auth_type"],
        auth_password=existing.auth_password if existing is not None else None,
        auth_token=existing.auth_token if existing is not None else None,
        headers={name: stored_headers.get(name) for name in connector["headers"]},
    )


@dataclass
class _View:
    """One connector as it will be after the import."""

    name: str
    type: str
    remote_device_instance: int | None
    reads: list[tuple[ObjectKey, str, tuple]]  # (object, direction, remote point)
    writes: list[tuple[ObjectKey, tuple]]  # (object, remote point)


def _views(parsed: list[dict], stored: dict[str, Connector], objects_after: dict, device_ids_after: set[int]) -> list[_View]:
    views = [
        _View(
            c["name"],
            c["type"],
            c["remote_device_instance"],
            [
                (m["object_key"], m["direction"], (m["remote_object_type"], m["remote_object_instance"], "present-value", None))
                for m in c["mappings"]
            ],
            [(m["object_key"], write_point(m)) for m in c["write_mappings"]],
        )
        for c in parsed
    ]
    in_file = {c["name"] for c in parsed}
    for name in sorted(stored.keys() - in_file):
        connector = stored[name]
        # A connector goes with its device, and a mapping with its object.
        if connector.type == "bacnet" and connector.device_id not in device_ids_after:
            continue
        views.append(
            _View(
                name,
                connector.type,
                connector.remote_device_instance,
                [
                    (_stored_key(m), m.direction, (m.remote_object_type, m.remote_object_instance, "present-value", None))
                    for m in connector.mappings
                    if _stored_key(m) in objects_after
                ],
                [(_stored_key(m), _stored_point(m)) for m in connector.write_mappings if _stored_key(m) in objects_after],
            )
        )
    return views


def _check_after_state(parsed: list[dict], stored: dict, objects_after: dict, device_ids_after: set[int]) -> None:
    """The rules that look across connectors, over the file's connectors and
    the ones it leaves alone."""
    views = _views(parsed, stored, objects_after, device_ids_after)
    feeds: dict[ObjectKey, str] = {}
    for view in views:
        for key, _, _ in view.reads:
            if key in feeds:
                raise ConfigImportError(
                    f"{object_label(*key)} would be fed by both {feeds[key]!r} and {view.name!r}"
                )
            feeds[key] = view.name
            if objects_after[key].simulated:
                raise ConfigImportError(
                    f"{object_label(*key)} is fed by {view.name!r} and also has a simulator"
                )

    try:
        for view in views:
            for i, (_key, point) in enumerate(view.writes):
                check_write_target_free(
                    point, [(f"write mapping from {object_label(*k)}", p) for k, p in view.writes[:i]]
                )
        by_remote: dict[int, list[_View]] = {}
        for view in views:
            if view.type == "bacnet":
                by_remote.setdefault(view.remote_device_instance, []).append(view)
        for group in by_remote.values():
            writes = [
                (f"write mapping from {object_label(*key)}", v.name, point) for v in group for key, point in v.writes
            ]
            proxies = [
                (f"read mapping on {object_label(*key)}", v.name, point)
                for v in group
                for key, direction, point in v.reads
                if direction == "proxy"
            ]
            for view in group:
                for _key, direction, point in view.reads:
                    if direction == "proxy":
                        _in_connector(view.name, check_not_written, point, writes)
                for _key, point in view.writes:
                    _in_connector(view.name, check_not_proxied, point, proxies)
    except ConnectorRuleError as exc:
        raise ConfigImportError(str(exc)) from exc


def _in_connector(name: str, check, point: tuple, others: list) -> None:
    try:
        check(point, others)
    except ConnectorRuleError as exc:
        raise ConnectorRuleError(exc.status, f"connector {name!r}: {exc}") from exc


# --- Apply -------------------------------------------------------------------

_HTTP_FIELDS = (
    "enabled", "url", "poll_interval_seconds", "timeout_seconds", "response_format", "auth_type",
    "auth_username", "verify_tls",
)
_BACNET_FIELDS = ("enabled", "remote_device_instance", "remote_address", "poll_interval_seconds", "timeout_seconds", "rewrite_interval_seconds")
_READ_FIELDS = (
    "json_path", "scale", "offset", "value_map", "remote_object_type", "remote_object_instance",
    "remote_property", "remote_array_index", "direction",
)


def apply_connector_plan(session: Session, plan: ConnectorPlan) -> None:
    """Write the plan. Call it after the sync's objects are flushed, so a
    mapping can name a new object. Stored credentials stay: a password,
    token, or header value is only ever set on the Connectors page."""
    # Rows the sync deleted (an object's mappings go with it) mustn't linger
    # in the connectors' collections.
    session.expire_all()
    stored = {c.name: c for c in session.scalars(select(Connector))}
    objects = {
        (o.device_id, o.object_type, o.instance_number): o for o in session.scalars(select(BacnetObject))
    }

    # Removals first and flushed: a feed moving from one connector to another
    # would otherwise INSERT before it DELETEs and hit the unique object_id.
    for connector in plan.parsed:
        existing = stored.get(connector["name"])
        if existing is None:
            continue
        file_reads = {m["object_key"] for m in connector["mappings"]}
        for mapping in list(existing.mappings):
            if _stored_key(mapping) not in file_reads:
                if existing.type == "http-poll":
                    release_http_mappings([mapping])
                existing.mappings.remove(mapping)
        file_writes = {write_point(m) for m in connector["write_mappings"]}
        for write in list(existing.write_mappings):
            if _stored_point(write) not in file_writes:
                existing.write_mappings.remove(write)
    session.flush()

    for connector in plan.parsed:
        row = stored.get(connector["name"])
        if row is None:
            row = _new_connector(connector)
            session.add(row)
        else:
            _update_connector(row, connector, release_http_mappings)
        _apply_mappings(row, connector, objects)
    session.flush()


def _new_connector(connector: dict) -> Connector:
    bacnet = connector["type"] == "bacnet"
    fields = _BACNET_FIELDS if bacnet else _HTTP_FIELDS
    values = {field: connector[field] for field in fields}
    if bacnet:
        # Connector.url is NOT NULL; a bacnet connector stores "".
        return Connector(name=connector["name"], type="bacnet", url="", device_id=connector["device_id"], **values)
    return Connector(
        name=connector["name"], type="http-poll", headers={n: "" for n in connector["headers"]} or None, **values
    )


def _update_connector(row: Connector, connector: dict, release_http_mappings) -> None:
    bacnet = connector["type"] == "bacnet"
    if not bacnet and row.enabled and not connector["enabled"]:
        release_http_mappings(row.mappings)
    for name in _BACNET_FIELDS if bacnet else _HTTP_FIELDS:
        setattr(row, name, connector[name])
    if not bacnet:
        stored = row.headers or {}
        row.headers = {name: stored.get(name, "") for name in connector["headers"]} or None


def _apply_mappings(row: Connector, connector: dict, objects: dict) -> None:
    reads = {_stored_key(m): m for m in row.mappings}
    for mapping in connector["mappings"]:
        values = {field: mapping[field] for field in _READ_FIELDS}
        found = reads.get(mapping["object_key"])
        if found is None:
            row.mappings.append(ConnectorMapping(object=objects[mapping["object_key"]], **values))
        else:
            for field, value in values.items():
                setattr(found, field, value)
    writes = {_stored_point(w): w for w in row.write_mappings}
    for mapping in connector["write_mappings"]:
        values = {field: mapping[field] for field in ("remote_object_type", "remote_object_instance", "remote_property", "remote_array_index", "priority")}
        found = writes.get(write_point(mapping))
        if found is None:
            row.write_mappings.append(ConnectorWriteMapping(object=objects[mapping["object_key"]], **values))
        else:
            found.priority = values["priority"]
            found.object = objects[mapping["object_key"]]


# --- Settings ----------------------------------------------------------------


def plan_settings(session: Session, settings: dict | None) -> bool:
    """Whether applying `settings` would change anything."""
    if settings is None:
        return False
    row = session.get(ServerSettings, 1)
    if row is None:
        return True
    return any(getattr(row, key) != value for key, value in settings.items())


def apply_settings(session: Session, settings: dict) -> None:
    row = get_or_create_settings(session)
    for key, value in settings.items():
        setattr(row, key, value)
