"""File-based config: a whole-server JSON export/import of every device and
its objects, plus the object-list sync shared with CSV import's `sync` mode.

Import is a declarative sync: the file is the complete desired state, so a
device/object in the DB but absent from the file is **deleted**. `dry_run`
computes the same plan without writing anything.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List

from pydantic import BaseModel, ValidationError

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bacpack_version, config_connectors, device_clock, object_config, timeutil
from .backup_restore import BACKUP_FAILURE_TIMEOUT
from .choices import CONNECTOR_TYPES, NETWORK_ROLES, canonical, canonicalize_object_fields
from .config_schemas import ConfigImportError, DeviceCreate, ObjectCreate, refuse_unknown_keys
from .device_network import LinkTarget, check_links, check_role_fields
from .models import (
    MAX_INSTANCE,
    OBJECT_TYPES,
    BacnetObject,
    Connector,
    Device,
    DeviceLink,
    ServerSettings,
    get_or_create_settings,
)
from .objects.network_port import validate_network_settings
from .objects.schedule_config import normalize_write_targets
from .unicode_text import UNSTORABLE_TEXT, has_unpaired_surrogate
from .trending import release_trend_samples_for_objects, seed_last_notify_record

# The file *format* version, not the BACpack release. Bump it only for a
# change an older importer can't read correctly (a renamed field, a changed
# meaning); a new defaulted field doesn't count. Import refuses a newer
# version.
CONFIG_VERSION = 1

# Mirrors ObjectCreate in config_schemas.py; every column must be listed here or
# export/import silently drops it.
OBJECT_FIELD_DEFAULTS = {
    "description": None,
    "units": None,
    "bacnet_writable": True,
    "out_of_service_default": False,
    "relinquish_default": None,
    "active_text": None,
    "inactive_text": None,
    "start_value": None,
    "state_text": None,
    "trended": False,
    "trend_mode": "interval",
    "trend_interval_seconds": None,
    "trend_sample_cap": None,
    "trend_log_instance": None,
    "trend_log_name": None,
    # trend_last_notify_record is runtime state (TrendState), deliberately absent.
    "trend_reporting_enabled": False,
    "trend_notification_threshold": 10,
    "trend_notification_class_instance": None,
    "trend_event_enable": None,
    "trend_notify_type": "event",
    "trend_event_detection_enable": True,
    "schedule_value_type": None,
    "schedule_default": None,
    "weekly_schedule": None,
    "exception_schedule": None,
    "schedule_effective_start": None,
    "schedule_effective_end": None,
    "schedule_priority": 14,
    "schedule_write_targets": None,
    "date_list": None,
    "alarming_enabled": False,
    "notification_class_instance": None,
    "alarm_time_delay": 0,
    "alarm_time_delay_normal": None,
    "alarm_notify_type": "alarm",
    "alarm_event_enable": None,
    "alarm_high_limit": None,
    "alarm_low_limit": None,
    "alarm_deadband": 0.0,
    "alarm_limit_enable": None,
    "alarm_value": None,
    "alarm_values": None,
    "alarm_event_detection_enable": True,
    "notification_priority": None,
    "notification_ack_required": None,
    "notification_recipient_list": None,
    "simulation": None,
}
OBJECT_FIELDS = ["object_type", "instance_number", "name", *OBJECT_FIELD_DEFAULTS]
# The device's network role; links are device
# instance numbers.
NETWORK_DEFAULTS = {
    "network_role": "normal",
    "foreign_bbmd": None,
    "foreign_ttl": 60,
    "bbmd_peers": None,
    "bbmd_links": [],
}
NETWORK_KEYS = tuple(NETWORK_DEFAULTS)
_DEVICE_KEYS = {
    "id", "name", "description", "location", "vendor_identifier", "vendor_name", "bind_address",
    "backup_failure_timeout", "objects", *NETWORK_KEYS,
}
_TOP_LEVEL_KEYS = {"version", "bacpack_version", "devices", "settings", "connectors"}


_RESTORE_CARRIES_ONLY_BACNET = "a restore file carries only this device's BACnet connectors"


def _check_types(model: type[BaseModel], data: dict, *, context: str) -> None:
    """Check `data` against the API's own request model, so a file can't
    store what the API would refuse. Strict, so "no" isn't read as False.
    Only checks: the row is stored as given."""
    try:
        model.model_validate(data, strict=True)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"])
        raise ConfigImportError(f"{context}: {where}: {error['msg']}") from None


# --- Export ------------------------------------------------------------


def export_config(
    devices: Iterable[Device],
    *,
    connectors: Iterable[Connector] | None = None,
    settings: ServerSettings | None = None,
) -> dict:
    document: dict = {"version": CONFIG_VERSION, "bacpack_version": bacpack_version()}
    if settings is not None:
        document["settings"] = config_connectors.export_settings(settings)
    document["devices"] = [_device_to_dict(d) for d in devices]
    if connectors is not None:
        document["connectors"] = config_connectors.export_connectors(connectors)
    return document


def _device_to_dict(device: Device) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "description": device.description,
        "location": device.location,
        "vendor_identifier": device.vendor_identifier,
        "vendor_name": device.vendor_name,
        "bind_address": device.bind_address,
        "backup_failure_timeout": device.backup_failure_timeout,
        "network_role": device.network_role,
        "foreign_bbmd": device.foreign_bbmd,
        "foreign_ttl": device.foreign_ttl,
        "bbmd_peers": device.bbmd_peers,
        "bbmd_links": sorted(link.linked_device_id for link in device.bbmd_links),
        "objects": [object_to_dict(o) for o in device.objects],
    }


def object_to_dict(obj: BacnetObject) -> dict:
    return {field: getattr(obj, field) for field in OBJECT_FIELDS}


def export_device_backup(device: Device) -> bytes:
    """The DM-BR-B backup file (file,1): the web UI Export format holding
    just this one device and the BACnet connectors that run through it, so
    either side's file works on the other."""
    return json.dumps(export_config([device], connectors=device.bacnet_connectors), indent=2).encode("utf-8")


# --- Parse / validate ----------------------------------------------------


def validate_object_row(row: dict, *, context: str) -> None:
    object_type = row.get("object_type")
    if object_type not in OBJECT_TYPES:
        raise ConfigImportError(f"{context}: unknown object_type {object_type!r}")
    # Types first: object_config's rules assume each value has the right
    # Python type. Everything else is the check every save path runs.
    _check_types(ObjectCreate, row, context=context)
    try:
        object_config.check_object(row)
    except ValueError as exc:
        raise ConfigImportError(f"{context}: {exc}") from exc


def _check_format_version(data: dict) -> None:
    """A missing or null version reads as this one (hand-written files);
    `bacpack_version` is informational and never checked."""
    version = data.get("version")
    if version is None:
        return
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ConfigImportError(f"'version' must be a whole number, got {version!r}")
    if version > CONFIG_VERSION:
        written_by = data.get("bacpack_version")
        source = f" (written by BACpack {written_by})" if isinstance(written_by, str) else ""
        raise ConfigImportError(
            f"config file format version {version}{source} is newer than this BACpack "
            f"({bacpack_version()}) can read (up to {CONFIG_VERSION}); upgrade BACpack to import it"
        )


@dataclass
class ParsedConfig:
    """`settings` is None when the file has no `settings` key (leave the
    current ones alone); `connectors` is [] when it has no `connectors`."""

    devices: List[dict]
    connectors: List[dict]
    settings: dict | None


def parse_config(data: dict) -> List[dict]:
    return parse_config_document(data).devices


def parse_config_document(data: dict) -> ParsedConfig:
    """Validate a decoded config document into normalized devices,
    connectors and settings for sync_config. The whole file is checked
    first, so a bad file never applies partially."""
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise ConfigImportError("config file must be an object with a top-level 'devices' list")
    if has_unpaired_surrogate(data):
        raise ConfigImportError(UNSTORABLE_TEXT)
    _check_format_version(data)
    refuse_unknown_keys(data, _TOP_LEVEL_KEYS, context="config file")
    devices = _parse_devices(data["devices"])
    return ParsedConfig(
        devices=devices,
        connectors=config_connectors.parse_connectors(data["connectors"]) if "connectors" in data else [],
        settings=config_connectors.parse_settings(data["settings"]) if "settings" in data else None,
    )


def _parse_devices(devices_raw: list) -> List[dict]:
    devices: List[dict] = []
    seen_device_ids = set()

    for i, device_raw in enumerate(devices_raw):
        context = f"devices[{i}]"
        if not isinstance(device_raw, dict):
            raise ConfigImportError(f"{context}: must be an object")

        device_id = device_raw.get("id")
        if not isinstance(device_id, int) or not (0 <= device_id <= MAX_INSTANCE):
            raise ConfigImportError(f"{context}: 'id' must be an integer 0-{MAX_INSTANCE}")
        if device_id in seen_device_ids:
            raise ConfigImportError(f"{context}: duplicate device id {device_id} in this file")
        seen_device_ids.add(device_id)

        context = f"devices[{i}] (id {device_id})"
        refuse_unknown_keys(device_raw, _DEVICE_KEYS, context=context)
        name = device_raw.get("name")
        if isinstance(name, str):
            name = name.strip()
        if not name:
            raise ConfigImportError(f"{context}: name is required")

        # Missing or null means the column default, like every other field.
        vendor_identifier = device_raw.get("vendor_identifier")
        if vendor_identifier is None:
            vendor_identifier = 999
        if not isinstance(vendor_identifier, int) or not (0 <= vendor_identifier <= 65535):
            raise ConfigImportError(f"{context}: 'vendor_identifier' must be an integer 0-65535")
        backup_failure_timeout = device_raw.get("backup_failure_timeout")
        if backup_failure_timeout is None:
            backup_failure_timeout = BACKUP_FAILURE_TIMEOUT
        # As the BACnet write: 0 would end every backup session at once.
        if (
            not isinstance(backup_failure_timeout, int)
            or isinstance(backup_failure_timeout, bool)
            or not (1 <= backup_failure_timeout <= 65535)
        ):
            raise ConfigImportError(f"{context}: 'backup_failure_timeout' must be an integer 1-65535")

        network = _parse_network(device_raw)
        _check_types(
            DeviceCreate,
            {field: device_raw.get(field) for field in ("id", "name", "description", "location", "bind_address", "vendor_name")}
            | {"vendor_identifier": vendor_identifier}
            | network,
            context=context,
        )
        problem = check_role_fields(
            network["network_role"], network["foreign_bbmd"], network["foreign_ttl"], network["bbmd_peers"]
        )
        if problem is not None:
            raise ConfigImportError(f"{context}: {problem}")
        try:
            validate_network_settings(bind_address=device_raw.get("bind_address"))
        except ValueError as exc:
            raise ConfigImportError(f"{context}: bind_address: {exc}") from exc

        objects_raw = device_raw.get("objects", [])
        if not isinstance(objects_raw, list):
            raise ConfigImportError(f"{context}: 'objects' must be a list")

        objects: List[dict] = []
        for j, obj_raw in enumerate(objects_raw):
            obj_context = f"{context}.objects[{j}]"
            if not isinstance(obj_raw, dict):
                raise ConfigImportError(f"{obj_context}: must be an object")
            refuse_unknown_keys(obj_raw, OBJECT_FIELDS, context=obj_context)

            # Null means the default, like a missing field.
            given = {
                field: value
                for field, value in obj_raw.items()
                if value is not None or OBJECT_FIELD_DEFAULTS.get(field) is None
            }
            row = canonicalize_object_fields({**OBJECT_FIELD_DEFAULTS, **given})
            row["schedule_write_targets"] = normalize_write_targets(row.get("schedule_write_targets"), device_id)
            validate_object_row(row, context=obj_context)
            objects.append({field: row[field] for field in OBJECT_FIELDS})

        problem = object_config.check_device(name, objects)
        if problem is not None:
            raise ConfigImportError(f"{context}: {problem}")

        devices.append(
            {
                "id": device_id,
                "name": name,
                "description": device_raw.get("description"),
                "location": device_raw.get("location"),
                "vendor_identifier": vendor_identifier,
                "vendor_name": device_raw.get("vendor_name"),
                "bind_address": device_raw.get("bind_address"),
                "backup_failure_timeout": backup_failure_timeout,
                **network,
                "objects": objects,
            }
        )

    return devices


def _parse_network(device_raw: dict) -> dict:
    """The device's network fields, a missing or null one at its default. The
    links are only checked for shape here: whether they name devices that can
    be linked depends on the server (sync_config)."""
    role = device_raw.get("network_role")
    ttl = device_raw.get("foreign_ttl")
    links = device_raw.get("bbmd_links")
    network = {
        "network_role": NETWORK_DEFAULTS["network_role"] if role is None else canonical(role, NETWORK_ROLES),
        "foreign_bbmd": device_raw.get("foreign_bbmd"),
        "foreign_ttl": NETWORK_DEFAULTS["foreign_ttl"] if ttl is None else ttl,
        "bbmd_peers": device_raw.get("bbmd_peers"),
        "bbmd_links": [] if links is None else links,
    }
    if isinstance(links, list) and all(isinstance(i, int) for i in links):
        network["bbmd_links"] = sorted(set(links))
    return network


# --- Sync (shared by JSON config import and CSV import's sync mode) ------


class NewObjectIds:
    """The ids new objects will get, taken in the order they are added, so a
    dry run can compare a defaulted Trend Log instance (the row id). The
    objects table has a plain rowid key (no AUTOINCREMENT), so SQLite gives
    each insert the largest id in the table plus one, after the deletes
    flushed before it: a sync deletes a device's dropped objects before it
    adds its new ones, device by device."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._known_ids: set | None = None
        self._next: int | None = None

    def _loaded(self) -> set:
        if self._known_ids is None:
            self._known_ids = set(self._session.scalars(select(BacnetObject.id)))
        return self._known_ids

    def release(self, ids: Iterable[int]) -> None:
        self._loaded().difference_update(ids)
        self._next = None

    def take(self) -> int:
        ids = self._loaded()
        if self._next is None:
            self._next = max(ids, default=0) + 1
        taken = self._next
        ids.add(taken)
        self._next += 1
        return taken


def sync_objects_for_device(
    session: Session,
    device: Device,
    object_rows: List[dict],
    *,
    dry_run: bool,
    feeds_checked: bool = False,
    new_ids: NewObjectIds | None = None,
) -> dict:
    """Sync `device`'s objects to `object_rows`, matched by (object_type,
    instance_number): create, update, and **delete** any object not listed.
    Returns a summary keyed by "object_type:instance_number".
    `feeds_checked`: the caller has planned the connectors against the
    objects as they will be (config_connectors.plan_connectors), so the
    stored mappings aren't consulted for a simulated object here.
    `new_ids`: a dry run's id plan, shared by every device a config sync
    plans, so each device's new objects get the ids left after the devices
    before it."""
    existing_by_identifier = {(o.object_type, o.instance_number): o for o in device.objects}
    seen = {(row["object_type"], row["instance_number"]) for row in object_rows}
    created, updated, deleted = [], [], []

    # One feed per object: a file can name a simulator for a connector-fed
    # object; refused up front, dry run too.
    for row in object_rows:
        existing = existing_by_identifier.get((row["object_type"], row["instance_number"]))
        if (
            not feeds_checked
            and existing is not None
            and row.get("simulation")
            and existing.connector_mapping is not None
        ):
            raise ConfigImportError(
                f"{row['object_type']}:{row['instance_number']} is fed by a connector mapping, so it can't be simulated"
            )

    # Deletions first, flushed before any create or rename: the unit of work
    # otherwise INSERTs before it DELETEs, so re-typing an object but keeping
    # its name (AI:1 "Zone Temp" -> AV:1 "Zone Temp") would hit the
    # per-device unique name.
    for identifier, existing in existing_by_identifier.items():
        if identifier not in seen:
            deleted.append(f"{identifier[0]}:{identifier[1]}")
            if not dry_run:
                # Not session.delete(): that leaves `device.objects` stale
                # within this session. Removing from the collection triggers
                # the same delete-orphan cascade.
                release_trend_samples_for_objects(session, [existing.id])
                device.objects.remove(existing)
    if deleted and not dry_run:
        session.flush()
    if dry_run:
        _plan_trend_log_identities(session, device, object_rows, existing_by_identifier, seen, new_ids)

    # Renames go through a unique placeholder name first, flushed, so two
    # kept objects can swap names (A->B, B->A) without either UPDATE hitting
    # the per-device unique name mid-flush. Done before any create, so a new
    # object can also take a name a renamed one gives up.
    renamed = []
    for row in object_rows:
        existing = existing_by_identifier.get((row["object_type"], row["instance_number"]))
        if existing is not None and existing.name != row["name"]:
            renamed.append(existing)
    if renamed and not dry_run:
        for existing in renamed:
            existing.name = f"\x00bacpack-rename-{existing.id}"
        session.flush()

    for row in object_rows:
        identifier = (row["object_type"], row["instance_number"])
        label = f"{identifier[0]}:{identifier[1]}"
        existing = existing_by_identifier.get(identifier)
        if existing is not None:
            updated.append(label)
            if not dry_run:
                was_reporting = existing.trend_reporting_enabled
                for field, value in row.items():
                    if field not in ("object_type", "instance_number"):
                        setattr(existing, field, value)
                seed_last_notify_record(existing, was_reporting)
    if renamed and not dry_run:
        session.flush()

    for row in object_rows:
        identifier = (row["object_type"], row["instance_number"])
        if identifier not in existing_by_identifier:
            created.append(f"{identifier[0]}:{identifier[1]}")
            if not dry_run:
                session.add(BacnetObject(device=device, **row))

    if not dry_run and any(row.get("trended") for row in object_rows):
        # The file's rows have no ids yet, so a defaulted Trend Log instance
        # (the row id) can only be compared once they do.
        session.flush()
        problem = object_config.trend_log_identity_problem(
            object_config.row_from_object(obj) for obj in device.objects
        )
        if problem is not None:
            raise ConfigImportError(problem)

    return {"created": created, "updated": updated, "deleted": deleted}


def _plan_trend_log_identities(
    session: Session,
    device: Device,
    object_rows: List[dict],
    existing_by_identifier: dict,
    seen: set,
    new_ids: NewObjectIds | None,
) -> None:
    """A dry run's version of the apply's after-flush check: ConfigImportError
    if two trended objects would be the same Trend Log once the new objects
    have the ids the apply will give them."""
    if new_ids is None:
        new_ids = NewObjectIds(session)
    new_ids.release(o.id for key, o in existing_by_identifier.items() if key not in seen)
    planned = []
    for row in object_rows:
        existing = existing_by_identifier.get((row["object_type"], row["instance_number"]))
        if existing is None:
            planned.append({**row, "id": new_ids.take()})
        else:
            planned.append({**object_config.row_from_object(existing), **row})
    problem = object_config.trend_log_identity_problem(planned)
    if problem is not None:
        raise ConfigImportError(problem)


def sync_config(
    session: Session,
    parsed_devices: List[dict],
    *,
    dry_run: bool,
    connectors: List[dict] | None = None,
    settings: dict | None = None,
) -> dict:
    """Apply parse_config_document's output (or, with dry_run, only plan it).
    A device absent from `parsed_devices` is deleted with everything it
    owns. Callers that must stop a device before deleting it dry-run first
    and check "devices_deleted".

    `connectors` (None = the caller doesn't handle them): the file's
    connectors, created or updated by name; others are left alone. `settings`
    (None = leave them alone). Everything is planned before the first write,
    so a refusal leaves the session untouched."""
    connector_plan = None
    if connectors is not None:
        connector_plan = config_connectors.plan_connectors(
            session,
            connectors,
            config_connectors.objects_after_sync(session, parsed_devices),
            {d["id"] for d in parsed_devices},
        )
    settings_changed = config_connectors.plan_settings(session, settings)
    existing_devices = {d.id: d for d in session.scalars(select(Device)).all()}
    _check_planned_links(session, parsed_devices, existing_devices.keys())
    seen_ids = set()
    devices_created, devices_updated, devices_deleted = [], [], []
    objects_by_device: dict[int, dict] = {}
    new_ids = NewObjectIds(session) if dry_run else None

    for device_row in parsed_devices:
        device_id = device_row["id"]
        seen_ids.add(device_id)
        existing = existing_devices.get(device_id)
        if existing is None:
            devices_created.append(device_id)
            device = _add_device(session, device_row, dry_run=dry_run)
        else:
            devices_updated.append(device_id)
            device = existing
            if not dry_run:
                device.name = device_row["name"]
                device.description = device_row["description"]
                device.location = device_row["location"]
                device.vendor_identifier = device_row["vendor_identifier"]
                device.vendor_name = device_row["vendor_name"]
                device.bind_address = device_row["bind_address"]
                device.backup_failure_timeout = device_row["backup_failure_timeout"]
                for column, value in _network_columns(device_row).items():
                    setattr(device, column, value)
                # Rows kept as they are: a replaced row with the same pair
                # would insert before its old one is deleted.
                saved = {link.linked_device_id: link for link in device.bbmd_links}
                device.bbmd_links = [
                    saved.get(i) or DeviceLink(linked_device_id=i) for i in device_row.get("bbmd_links", [])
                ]

        objects_by_device[device_id] = sync_objects_for_device(
            session,
            device,
            device_row["objects"],
            dry_run=dry_run,
            feeds_checked=connector_plan is not None,
            new_ids=new_ids,
        )

    for device_id, device in existing_devices.items():
        if device_id not in seen_ids:
            devices_deleted.append(device_id)
            if not dry_run:
                release_trend_samples_for_objects(session, [o.id for o in device.objects])
                session.delete(device)

    if not dry_run:
        if connector_plan is not None:
            session.flush()
            config_connectors.apply_connector_plan(session, connector_plan)
        if settings is not None and settings_changed:
            config_connectors.apply_settings(session, settings)

    return {
        "devices_created": devices_created,
        "devices_updated": devices_updated,
        "devices_deleted": devices_deleted,
        "objects": objects_by_device,
        "connectors_created": connector_plan.created if connector_plan else [],
        "connectors_updated": connector_plan.updated if connector_plan else [],
        "connector_mappings": connector_plan.mappings if connector_plan else {},
        "needs_credentials": connector_plan.needs_credentials if connector_plan else {},
        "settings_changed": settings_changed,
    }


def _add_device(session: Session, device_row: dict, *, dry_run: bool) -> Device:
    """The Device for a parsed row, its links attached; added to the session
    unless `dry_run`."""
    device = Device(
        id=device_row["id"],
        name=device_row["name"],
        description=device_row["description"],
        location=device_row["location"],
        vendor_identifier=device_row["vendor_identifier"],
        vendor_name=device_row["vendor_name"],
        bind_address=device_row["bind_address"],
        backup_failure_timeout=device_row["backup_failure_timeout"],
        **_network_columns(device_row),
    )
    if not dry_run:
        device.bbmd_links = [DeviceLink(linked_device_id=i) for i in device_row.get("bbmd_links", [])]
        session.add(device)
    return device


def create_devices(session: Session, parsed_devices: List[dict]) -> dict:
    """Add parse_config_document's devices and their objects, deleting and
    updating nothing (sync_config deletes every device a file omits). Same
    checks as sync_config's create path; the caller holds the locks and
    commits."""
    existing_ids = set(session.scalars(select(Device.id)))
    for row in parsed_devices:
        if row["id"] in existing_ids:
            raise ConfigImportError(f"device {row['id']} already exists")
    _check_planned_links(session, parsed_devices, existing_ids)
    objects_by_device: dict[int, dict] = {}
    for row in parsed_devices:
        device = _add_device(session, row, dry_run=False)
        objects_by_device[row["id"]] = sync_objects_for_device(session, device, row["objects"], dry_run=False)
    return {"devices_created": [row["id"] for row in parsed_devices], "objects": objects_by_device}


def _network_columns(device_row: dict) -> dict:
    return {key: device_row.get(key, NETWORK_DEFAULTS[key]) for key in NETWORK_KEYS if key != "bbmd_links"}


def _check_planned_links(session: Session, parsed_devices: List[dict], server_ids: Iterable[int]) -> None:
    """Every BBMD link in the file names a device the file keeps and that the
    link rules allow, judged on the file's devices as they will be (a device
    the file leaves out is deleted by the import)."""
    default_bind = get_or_create_settings(session).bind_address or None
    planned: Dict[int, LinkTarget] = {
        row["id"]: LinkTarget(
            row["id"], row["name"], row.get("network_role", "normal"), row["bind_address"] or default_bind
        )
        for row in parsed_devices
    }
    on_server = set(server_ids)
    for row in parsed_devices:
        own = planned[row["id"]]
        links = row.get("bbmd_links", [])
        for linked_id in links:
            if linked_id not in planned:
                where = (
                    "not in this file, so the import would delete it"
                    if linked_id in on_server
                    else "neither in this file nor on this server"
                )
                raise ConfigImportError(f"{own.name} ({own.id}): bbmd_links names device {linked_id}, which is {where}")
        problem = check_links(own, own.bind, links, planned.get)
        if problem is not None:
            raise ConfigImportError(f"{own.name} ({own.id}): bbmd_links: {problem}")


def plan_digest(plan: dict) -> str:
    """SHA-256 over a plan's sorted identifiers (not field values: an update
    writes the file's values, as previewed). Apply refuses if the dry run's
    digest no longer matches."""
    canonical = {
        "devices": [sorted(plan[key]) for key in ("devices_created", "devices_updated", "devices_deleted")],
        "objects": {
            str(device_id): [sorted(summary[key]) for key in ("created", "updated", "deleted")]
            for device_id, summary in plan["objects"].items()
        },
        # Names only: credentials and settings values aren't part of the plan
        # an apply is held to.
        "connectors": [sorted(plan.get("connectors_created", [])), sorted(plan.get("connectors_updated", []))],
        "connector_mappings": {
            name: [sorted(changes[key]) for key in ("created", "updated", "deleted")]
            for name, changes in plan.get("connector_mappings", {}).items()
        },
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def object_plan_digest(summary: dict) -> str:
    """plan_digest for one device's object plan (a CSV import's preview)."""
    canonical = [sorted(summary[key]) for key in ("created", "updated", "deleted")]
    encoded = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --- BACnet restore (DM-BR-B) --------------------------------------------


def apply_device_restore(session: Session, device: Device, data: bytes) -> dict:
    """Apply a DM-BR-B restore file onto `device` with config import's
    validation and sync. The file's `id` and `bind_address` are replaced
    with the device's own, so a restore never moves the device. Flushes but
    never commits: the caller owns the transaction."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigImportError(f"restore file is not UTF-8 JSON: {exc}") from exc
    if has_unpaired_surrogate(document):
        raise ConfigImportError(UNSTORABLE_TEXT)
    if not isinstance(document, dict) or not isinstance(document.get("devices"), list):
        raise ConfigImportError("restore file must be an object with a top-level 'devices' list")
    if len(document["devices"]) != 1:
        raise ConfigImportError(
            f"restore file must hold exactly one device, found {len(document['devices'])}"
        )
    entry = document["devices"][0]
    if not isinstance(entry, dict):
        raise ConfigImportError("devices[0]: must be an object")

    if "settings" in document:
        raise ConfigImportError("a restore file doesn't carry server settings; import them on the Config page")
    connectors = document.get("connectors", [])
    if not isinstance(connectors, list):
        raise ConfigImportError("connectors: must be a list")
    for connector in connectors:
        if isinstance(connector, dict) and canonical(connector.get("type", "http-poll"), CONNECTOR_TYPES) != "bacnet":
            raise ConfigImportError(_RESTORE_CARRIES_ONLY_BACNET)
    # Pointed at this device whatever the file says, like `id` and
    # `bind_address`: a restore never reaches another device's connectors.
    connectors = [{**c, "device": device.id} if isinstance(c, dict) else c for c in connectors]

    revision = device.database_revision or 1
    # The network role stays the device's own too: a backup from another
    # server names devices that aren't here.
    entry = {
        **entry,
        "id": device.id,
        "bind_address": device.bind_address,
        "network_role": device.network_role,
        "foreign_bbmd": device.foreign_bbmd,
        "foreign_ttl": device.foreign_ttl,
        "bbmd_peers": device.bbmd_peers,
        "bbmd_links": sorted(link.linked_device_id for link in device.bbmd_links),
    }
    # parse_config_document also refuses file,1's reserved object name,
    # which object_builder won't start with -- or endRestore would ACK and
    # the device then crash.
    document = parse_config_document({**document, "devices": [entry], "connectors": connectors})
    (parsed,) = document.devices
    # Every other device's objects stay as stored, so the connectors are
    # planned against those plus this device's new ones.
    connector_plan = config_connectors.plan_connectors(
        session,
        document.connectors,
        config_connectors.objects_after_sync(session, document.devices, only_device_id=device.id),
        {d.id for d in session.scalars(select(Device))},
    )

    device.name = parsed["name"]
    device.description = parsed["description"]
    device.location = parsed["location"]
    device.vendor_identifier = parsed["vendor_identifier"]
    device.vendor_name = parsed["vendor_name"]
    device.backup_failure_timeout = parsed["backup_failure_timeout"]
    summary = sync_objects_for_device(session, device, parsed["objects"], dry_run=False, feeds_checked=True)
    session.flush()
    config_connectors.apply_connector_plan(session, connector_plan)
    summary = {
        **summary,
        "connectors_created": connector_plan.created,
        "connectors_updated": connector_plan.updated,
        "connector_mappings": connector_plan.mappings,
        "needs_credentials": connector_plan.needs_credentials,
    }
    device.last_restore_time = timeutil.to_db(device_clock.device_utc_now())
    session.flush()
    # Exactly +1 per restore (135: Database_Revision goes up whenever a
    # restore is performed). The before_flush listener misses an identical
    # restore and counts once per flush of a changing one.
    device.database_revision = revision + 1
    session.flush()
    return summary
