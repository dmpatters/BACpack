"""Notification Class object, for alarm recipient/priority routing.
bacpypes3 ships only the abstract property definitions, so the local class is
composed here.
"""

from __future__ import annotations

from typing import Optional

from bacpypes3.basetypes import Destination, DeviceAddress, Recipient, Time
from bacpypes3.constructeddata import ListOf
from bacpypes3.errors import PropertyError
from bacpypes3.local.object import Object as _Object
from bacpypes3.object import NotificationClassObject as _NotificationClassObject

from .. import bacpypes3_compat, config_save, object_config
from ..bacnet import WritePropertyAllowlisted, normalized_attr
from ..models import BacnetObject
from .notification_class_config import validate_notification_class_config

_DEFAULT_BACNET_PORT = 47808

_ROUTING_COLUMNS = {"priority": "notification_priority", "ackRequired": "notification_ack_required"}


def build_recipient_list(recipient_list: Optional[list]) -> "ListOf(Destination)":
    """Stored JSON to ListOf(Destination). A device recipient is resolved
    to an address by bacpypes3 at send time, not here."""
    destinations = []
    for entry in recipient_list or []:
        recipient_cfg = entry["recipient"]
        if recipient_cfg["kind"] == "device":
            recipient = Recipient(device=("device", recipient_cfg["instance_number"]))
        else:
            recipient = Recipient(address=DeviceAddress(recipient_cfg["address"]))

        destinations.append(
            Destination(
                validDays=entry["valid_days"],
                fromTime=Time(entry["from_time"]),
                toTime=Time(entry["to_time"]),
                recipient=recipient,
                processIdentifier=entry["process_identifier"],
                issueConfirmedNotifications=entry["confirmed"],
                transitions=entry["transitions"],
            )
        )
    return ListOf(Destination)(destinations)


# --- AE-CRL-B: editing Recipient_List over the wire --------------------------


class ListElementError(Exception):
    """An AddListElement/RemoveListElement failure, with the 1-based element
    position (0: not about one element), for a ChangeListError."""

    def __init__(self, error_class: str, error_code: str, position: int = 0) -> None:
        super().__init__(f"{error_class}/{error_code} at element {position}")
        self.error_class = error_class
        self.error_code = error_code
        self.position = position


def address_string(network: int, mac: bytes) -> str:
    """The stored text of a BACnet address, parseable by DeviceAddress.
    bacpypes3 prints a BACnet/IP MAC as 0xc0a801c8bac0, unreadable in the UI."""
    mac = bytes(mac)
    if not mac:
        station = "*"
    elif len(mac) == 6:
        ip = ".".join(str(octet) for octet in mac[:4])
        port = int.from_bytes(mac[4:], "big")
        station = ip if port == _DEFAULT_BACNET_PORT else f"{ip}:{port}"
    else:
        station = "0x" + mac.hex()
    return f"{network}:{station}" if network else station


def _time_string(value) -> str:
    if 255 in tuple(value):
        raise ValueError("times with unspecified fields aren't supported")
    return str(value)


def destination_to_entry(destination: Destination) -> dict:
    """A wire Destination in BACpack's stored JSON form (the inverse of
    build_recipient_list). Raises ValueError for anything that form can't
    hold."""
    recipient = destination.recipient
    if recipient.device is not None:
        if recipient.device[0].attr != "device":
            raise ValueError("a recipient object must be a device")
        recipient_cfg = {"kind": "device", "instance_number": int(recipient.device[1])}
    elif recipient.address is not None:
        address = recipient.address
        text = address_string(address.networkNumber, address.macAddress)
        # Only what parses back to the same address: e.g. "65535:*" (global
        # broadcast) is refused by DeviceAddress.
        try:
            parsed = DeviceAddress(text)
        except Exception as exc:
            raise ValueError(f"address {text!r} can't be stored") from exc
        if (parsed.networkNumber, bytes(parsed.macAddress)) != (address.networkNumber, bytes(address.macAddress)):
            raise ValueError(f"address {text!r} can't be stored")
        recipient_cfg = {"kind": "address", "address": text}
    else:
        raise ValueError("a recipient needs a device or an address")
    return {
        "recipient": recipient_cfg,
        "valid_days": [bool(bit) for bit in destination.validDays],
        "from_time": _time_string(destination.fromTime),
        "to_time": _time_string(destination.toTime),
        "transitions": [bool(bit) for bit in destination.transitions],
        "confirmed": bool(destination.issueConfirmedNotifications),
        "process_identifier": int(destination.processIdentifier),
    }


def _normalized_address(text: str) -> str:
    try:
        parsed = DeviceAddress(text)
    except Exception:
        # Typed by hand in the UI and never checked: it just never matches.
        return text
    return address_string(parsed.networkNumber, parsed.macAddress)


def recipient_key(entry: dict) -> tuple:
    """What identifies a Recipient_List entry for Add/Remove: the recipient
    plus the process identifier, so one device can register several
    processes."""
    recipient = entry["recipient"]
    if recipient["kind"] == "device":
        who = ("device", int(recipient["instance_number"]))
    else:
        who = ("address", _normalized_address(recipient["address"]))
    return who + (int(entry["process_identifier"]),)


def add_entries(current: list, new: list) -> list:
    """AddListElement: an element with a present key overwrites the first
    match (dropping further duplicates), else is appended; a later element
    with the same key wins."""
    result = list(current)
    for entry in new:
        key = recipient_key(entry)
        matches = [i for i, existing in enumerate(result) if recipient_key(existing) == key]
        if not matches:
            result.append(entry)
            continue
        result[matches[0]] = entry
        for i in reversed(matches[1:]):
            del result[i]
    return result


def remove_entries(current: list, entries: list) -> list:
    """RemoveListElement: removes every entry with each element's key. One
    with nothing to remove fails the whole request at its position."""
    result = list(current)
    for position, entry in enumerate(entries, start=1):
        key = recipient_key(entry)
        kept = [existing for existing in result if recipient_key(existing) != key]
        if len(kept) == len(result):
            raise ListElementError("services", "listElementNotFound", position)
        result = kept
    return result


class RecipientListEditable:
    """Recipient_List editable over BACnet: Add/RemoveListElement and a
    whole-list WriteProperty. The list is also kept in stored JSON form
    (_recipient_entries), so edits never re-derive it from the live value.
    Each change is validated in full, applied live, then saved."""

    _recipient_entries: tuple | list = ()  # replaced per instance, never mutated in place

    async def add_recipients(self, destinations) -> None:
        await self._apply_list_change(add_entries(self._recipient_entries, self._entries_from(destinations)))

    async def remove_recipients(self, destinations) -> None:
        await self._apply_list_change(remove_entries(self._recipient_entries, self._entries_from(destinations)))

    async def _apply_list_change(self, entries: list) -> None:
        try:
            self._check_saved_entries(entries)
        except ValueError as exc:
            raise ListElementError("property", "valueOutOfRange") from exc
        await self._apply_recipient_entries(entries)

    def _check_saved_entries(self, entries: list) -> None:
        """ValueError if the saved row with these entries wouldn't build."""
        object_config.check_saved_row(
            getattr(self, "_recipient_list_session_factory", None),
            getattr(self, "_recipient_list_object_id", None),
            {"notification_recipient_list": entries},
        )

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        name = normalized_attr(self, attr)
        if name in _ROUTING_COLUMNS:
            await self._write_routing(attr, name, value, index, priority)
            return
        if name != "recipientList":
            await super().write_property(attr, value, index, priority)
            return
        # A BACnetLIST, not an array.
        if index is not None:
            raise PropertyError("propertyIsNotAnArray")
        element = bacpypes3_compat.required_element_class(self, "recipientList")
        try:
            entries = self._entries_from(element(element.cast(value)))
        except (ListElementError, TypeError, ValueError, AttributeError) as exc:
            raise PropertyError("value-out-of-range") from exc
        try:
            self._check_saved_entries(entries)
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc
        # Stored as sent, duplicates and all -- same as a UI save.
        await self._apply_recipient_entries(entries)

    async def _write_routing(self, attr, name: str, value, index, priority) -> None:
        """Priority / Ack_Required: checked as the saved row, applied live,
        then saved."""
        column = _ROUTING_COLUMNS[name]
        if name == "priority":
            if index is None:
                new = self._whole_list(value, int)
            else:
                # As State_Text: index 0 (the length) is never written.
                current = [int(p) for p in self.priority]  # pyright: ignore[reportAttributeAccessIssue]  # a bacpypes3 property of the composed class
                if index == 0:
                    raise PropertyError("writeAccessDenied")
                if not (1 <= index <= len(current)):
                    raise PropertyError("invalidArrayIndex")
                new = current
                new[index - 1] = self._as_int(value)
        else:
            if index is not None:
                raise PropertyError("propertyIsNotAnArray")
            new = self._whole_list(value, bool)
        try:
            validate_notification_class_config(
                new if name == "priority" else None, new if name == "ackRequired" else None, None
            )
            object_config.check_saved_row(
                getattr(self, "_recipient_list_session_factory", None),
                getattr(self, "_recipient_list_object_id", None),
                {column: new},
            )
        except (TypeError, ValueError) as exc:
            raise PropertyError("value-out-of-range") from exc
        await super().write_property(attr, value, index, priority)  # pyright: ignore[reportAttributeAccessIssue]  # mixin: the next base in the MRO has it
        await self._save_columns({column: new})

    @staticmethod
    def _as_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise PropertyError("value-out-of-range") from exc

    @staticmethod
    def _whole_list(value, kind) -> list:
        try:
            return [kind(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise PropertyError("value-out-of-range") from exc

    @staticmethod
    def _entries_from(destinations) -> list:
        entries = []
        for position, destination in enumerate(destinations, start=1):
            try:
                entry = destination_to_entry(destination)
                validate_notification_class_config(None, None, [entry])
            except (TypeError, ValueError, AttributeError) as exc:
                raise ListElementError("property", "valueOutOfRange", position) from exc
            entries.append(entry)
        return entries

    async def _apply_recipient_entries(self, entries: list) -> None:
        self.recipientList = build_recipient_list(entries)
        self._recipient_entries = entries
        await self._save_recipient_entries(entries)

    async def _save_recipient_entries(self, entries: list) -> None:
        await self._save_columns({"notification_recipient_list": entries})

    async def _save_columns(self, columns: dict) -> None:
        session_factory = getattr(self, "_recipient_list_session_factory", None)
        object_id = getattr(self, "_recipient_list_object_id", None)
        if session_factory is None or object_id is None:
            return
        what = ", ".join(columns)
        await config_save.save_columns(
            session_factory, BacnetObject, object_id, columns, f"{what} for object id {object_id}"
        )


class LocalNotificationClassObject(
    WritePropertyAllowlisted, RecipientListEditable, _Object, _NotificationClassObject
):
    """Notification Class object. Recipient_List, Priority and Ack_Required
    are BACnet-writable and saved."""

    _WRITABLE_ATTRS: frozenset = frozenset({"recipientList", "priority", "ackRequired"})
