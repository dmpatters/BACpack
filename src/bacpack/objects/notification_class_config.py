"""Shape and range rules for a Notification Class's saved config (priority,
ack_required, Recipient_List). No object classes, so object_config can import
it."""
from __future__ import annotations

import re
from typing import Optional

from bacpypes3.basetypes import DeviceAddress, Time

from ..choices import RECIPIENT_KINDS
from ..models import MAX_INSTANCE


_VALID_RECIPIENT_KINDS = set(RECIPIENT_KINDS)


def validate_notification_class_config(
    priority: Optional[list],
    ack_required: Optional[list],
    recipient_list: Optional[list],
) -> None:
    """Shape/range validation only, no DB access (whether a referenced
    object exists is object_config.check_device's job)."""
    if priority is not None:
        if len(priority) != 3:
            raise ValueError("priority must have exactly 3 entries")
        for value in priority:
            if not (0 <= int(value) <= 255):
                raise ValueError("priority entries must be 0-255")

    if ack_required is not None and len(ack_required) != 3:
        raise ValueError("ack_required must have exactly 3 entries")

    for entry in recipient_list or []:
        recipient = entry.get("recipient") or {}
        if recipient.get("kind") not in _VALID_RECIPIENT_KINDS:
            raise ValueError(
                f"recipient kind must be one of {sorted(_VALID_RECIPIENT_KINDS)}"
            )
        if recipient["kind"] == "device":
            if "instance_number" not in recipient:
                raise ValueError("a device recipient requires instance_number")
            _validate_instance_number(recipient["instance_number"])
        if recipient["kind"] == "address":
            if not recipient.get("address"):
                raise ValueError("an address recipient requires address")
            _validate_address(recipient["address"])
        for field in ("from_time", "to_time"):
            _validate_time(field, entry.get(field))

        valid_days = entry.get("valid_days")
        if valid_days is None or len(valid_days) != 7:
            raise ValueError("valid_days must have exactly 7 entries")

        transitions = entry.get("transitions")
        if transitions is None or len(transitions) != 3:
            raise ValueError("transitions must have exactly 3 entries")

        if not isinstance(entry.get("process_identifier"), int):
            raise ValueError("process_identifier must be an integer")
        if not 0 <= entry["process_identifier"] <= 4294967295:
            raise ValueError("process_identifier must be 0-4294967295")


# The forms address_string stores: IPv4 with optional port, 0x hex or "*",
# each optionally behind "network:", or "network:" plus a decimal MAC.
# Checked first: DeviceAddress would look anything else up as a hostname at
# every device start.
_STATION = r"(?:\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?|0x[0-9a-fA-F]+|\*)"
_ADDRESS_FORM = re.compile(rf"(?:\d+:)?{_STATION}|\d+:\d+")


def _validate_instance_number(value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INSTANCE:
        raise ValueError(f"a device recipient's instance_number must be an integer 0-{MAX_INSTANCE}")


def _validate_address(text) -> None:
    if not isinstance(text, str) or not _ADDRESS_FORM.fullmatch(text):
        raise ValueError(f"recipient address {text!r} isn't a BACnet address")
    try:
        DeviceAddress(text)
    except Exception as exc:
        raise ValueError(f"recipient address {text!r} isn't a BACnet address") from exc


def _validate_time(field: str, text) -> None:
    """The same rule the BACnet path applies (_time_string in
    notification_class.py): a real time,
    no unspecified (*) field."""
    try:
        hour, minute, second, hundredth = tuple(Time(text))
    except Exception as exc:
        raise ValueError(f"{field} must be a time like 06:00:00") from exc
    if not (hour <= 23 and minute <= 59 and second <= 59 and hundredth <= 99):
        raise ValueError(f"{field} must be a time like 06:00:00, with no * field")
