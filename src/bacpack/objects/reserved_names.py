"""Names a device gives objects it builds itself (its network port, file,1,
each trended object's Trend Log), and the clashes that would stop it
starting: bacpypes3 refuses a duplicate Object_Name.

A clash function takes check_object-shaped row dicts (name, trended, and
optionally id, trend_log_instance, trend_log_name), so it sees each Trend
Log's effective name and instance."""
from __future__ import annotations

from typing import Optional


# Reserved: no object or device may take it (find_reserved_name_clash).
NETWORK_PORT_NAME = "NetworkPort-1"


def trend_log_name(object_name: str) -> str:
    return f"{object_name}-Trend"


def effective_trend_log_name(row: dict) -> str:
    """The Trend Log's Object_Name: the point's fixed one, else the default."""
    return row.get("trend_log_name") or trend_log_name(row["name"])


def effective_trend_log_instance(row: dict) -> Optional[int]:
    """The Trend Log's instance: the point's fixed one, else its row id (None
    for a row not saved yet, whose id isn't known)."""
    fixed = row.get("trend_log_instance")
    return row.get("id") if fixed is None else fixed


def find_trend_log_name_clash(rows) -> Optional[str]:
    """An error if a trended object's Trend_Log name is another object's
    name, or two Trend Logs share one, else None: bacpypes3 refuses a
    duplicate objectName, so the device wouldn't start."""
    rows = list(rows)
    names = {row["name"] for row in rows}
    log_owners: dict = {}
    for row in rows:
        if not row.get("trended"):
            continue
        log_name = effective_trend_log_name(row)
        if log_name in names:
            return (
                f"object {log_name!r} has the same name as the trend log of "
                f"trended object {row['name']!r}"
            )
        if log_name in log_owners:
            return (
                f"trended objects {log_owners[log_name]!r} and {row['name']!r} "
                f"have the same trend log name {log_name!r}"
            )
        log_owners[log_name] = row["name"]
    return None


def find_trend_log_identity_clash(rows) -> Optional[str]:
    """An error if two trended objects would be the same Trend Log instance.
    Every trend log shares one object type, so the instance is the whole
    identity. A defaulted instance (the row id) of a row not saved yet is
    unknown here: the save paths re-check after the flush."""
    owners: dict = {}
    for row in rows:
        if not row.get("trended"):
            continue
        instance = effective_trend_log_instance(row)
        if instance is None:
            continue
        if instance in owners:
            return (
                f"trended objects {owners[instance]!r} and {row['name']!r} would both be "
                f"Trend Log {instance}; set a Trend Log instance on the Trending tab of one of them"
            )
        owners[instance] = row["name"]
    return None


def config_file_name(device_name: str) -> str:
    return f"{device_name}-config.json"


def find_reserved_name_clash(device_name: str, rows) -> Optional[str]:
    """A message if two objects would share a name once the device builds
    the objects it names itself, else None. `rows`: the device's whole
    object list.

    Reserved: the device's own name (Object_Name is unique within a device,
    Device object included), file,1's and the network port's names, and no
    trend log may take the device's name. bacpypes3 refuses a duplicate
    name, so the device wouldn't start."""
    rows = list(rows)
    names = {row["name"] for row in rows}
    if device_name in names:
        return f"object name {device_name!r} is the device's own name"
    if device_name == NETWORK_PORT_NAME:
        return f"device name {NETWORK_PORT_NAME!r} is reserved for the device's network port"
    reserved = config_file_name(device_name)
    if reserved in names:
        return f"object name {reserved!r} is reserved for the device's backup/restore file (file,1)"
    if NETWORK_PORT_NAME in names:
        return f"object name {NETWORK_PORT_NAME!r} is reserved for the device's network port"
    for row in rows:
        if not row.get("trended"):
            continue
        log_name = effective_trend_log_name(row)
        if log_name == device_name:
            return f"device name {device_name!r} is the trend log name of trended object {row['name']!r}"
        if log_name == NETWORK_PORT_NAME:
            return (
                f"trend log name {NETWORK_PORT_NAME!r} of trended object {row['name']!r} "
                "is reserved for the device's network port"
            )
        if log_name == reserved:
            return (
                f"trend log name {reserved!r} of trended object {row['name']!r} "
                "is reserved for the device's backup/restore file (file,1)"
            )
    return None
