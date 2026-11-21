"""The one format for naming an object, device or connector in a log line:
the id, then the quoted name, `analog-value:9 "Old Chiller"`,
`device 599001 "AHU-1"`, `connector 3 "Weather feed"`, with `\\` and `"` inside
the name backslash-escaped.

The diagnostics file's text scan finds a name by this shape, so it can replace
the name of an object that has since been renamed or deleted.
Every log call that names one of these
goes through here; tests/test_log_names_guard.py fails on one that doesn't.
"""
from __future__ import annotations

import re


def quote(name) -> str:
    escaped = str(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unescape_name(escaped: str) -> str:
    return re.sub(r"\\(.)", r"\1", escaped, flags=re.DOTALL)


def object_ref(object_type, instance, name) -> str:
    return f"{object_type}:{instance} {quote(name)}"


def device_ref(device_id, name) -> str:
    return f"device {device_id} {quote(name)}"


def connector_ref(connector_id, name) -> str:
    return f"connector {connector_id} {quote(name)}"


def live_object_ref(obj) -> str:
    """A bacpypes3 object of the running device."""
    object_type, instance = obj.objectIdentifier
    return object_ref(object_type, instance, obj.objectName)
