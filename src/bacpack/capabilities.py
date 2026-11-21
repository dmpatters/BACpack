"""What every BACpack device supports and advertises over BACnet: one source
of truth for both the wire and the About page (GET /api/capabilities).

Pure, and deliberately imports neither bacpack.application nor
bacpack.objects.device (both import this module), so the introspection
functions take the Application class as a parameter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from bacpypes3.apdu import (
    ConfirmedServiceChoice,
    UnconfirmedServiceChoice,
    confirmed_request_types,
    unconfirmed_request_types,
)
from bacpypes3.basetypes import Segmentation, ServicesSupported
from bacpypes3.local.device import DeviceObject

from .models import OBJECT_TYPES

# Receive-side handlers bacpypes3's ChangeOfValueServices mixin adds
# (bacpypes3/service/cov.py) for subscriptions *this* device started. BACpack
# never subscribes to anything, so advertising them would claim a service
# that does nothing useful.
_NOT_ADVERTISED = frozenset({"confirmed-cov-notification", "unconfirmed-cov-notification"})

# Services BACpack *sends*. Introspection can't find these -- this is the one
# hand-maintained part of the service data.
INITIATED_SERVICES = frozenset(
    {
        # Replies to Who-Is / Who-Has.
        "i-am",
        "i-have",
        "confirmed-cov-notification",
        "unconfirmed-cov-notification",
        # Intrinsic reporting.
        "confirmed-event-notification",
        "unconfirmed-event-notification",
        # bacnet_client.BacnetClient: bacnet connectors and the client tool.
        "read-property",
        "read-property-multiple",
        "write-property",
        "who-is",
    }
)

DATA_LINKS = ("BACnet/IP", "BACnet/IP BBMD", "BACnet/IP foreign device")

# Object types BACpack builds itself. network-port is advertised
# unconditionally: every running device has a bind address. file is the
# DM-BR-B config file every device carries.
_BUILT_BY_BACPACK = ("device", "network-port", "trend-log", "file")

_SERVICE_KINDS = {
    **{str(ConfirmedServiceChoice(c)): "confirmed" for c in confirmed_request_types},
    **{str(UnconfirmedServiceChoice(c)): "unconfirmed" for c in unconfirmed_request_types},
}


def service_kind(name: str) -> str:
    """"confirmed" or "unconfirmed" for a kebab-case service name."""
    return _SERVICE_KINDS[name]


def executed_services(app_cls: type) -> dict[str, str]:
    """Service name -> kind, for every service app_cls has a do_<Request>
    handler for, minus _NOT_ADVERTISED.

    Same rule as bacpypes3's Application.get_services_supported
    (bacpypes3/app.py), except unconfirmed choices are looked up in
    UnconfirmedServiceChoice -- bacpypes3 0.0.106 uses ConfirmedServiceChoice
    for both, which set the wrong bit for every unconfirmed service.
    """
    found: dict[str, str] = {}
    for request_types, choice_enum, kind in (
        (confirmed_request_types, ConfirmedServiceChoice, "confirmed"),
        (unconfirmed_request_types, UnconfirmedServiceChoice, "unconfirmed"),
    ):
        for choice, request_cls in request_types.items():
            if hasattr(app_cls, "do_" + request_cls.__name__):
                name = str(choice_enum(choice))
                if name not in _NOT_ADVERTISED:
                    found[name] = kind
    return found


def services_supported(app_cls: type) -> ServicesSupported:
    """The Protocol_Services_Supported bitstring for app_cls."""
    return ServicesSupported(sorted(executed_services(app_cls)))


def object_types_supported() -> list[str]:
    """The Protocol_Object_Types_Supported names, in display order."""
    return list(OBJECT_TYPES) + list(_BUILT_BY_BACPACK)


@dataclass(frozen=True)
class Bibb:
    """One BIBB row. `executes`/`initiates` are kebab-case service names;
    `unmet` is a non-service requirement BACpack doesn't meet yet (forces
    not-supported); rows sharing an `either_group` count as one requirement.
    """

    name: str
    title: str
    area: str
    executes: frozenset = frozenset()
    initiates: frozenset = frozenset()
    unmet: Optional[str] = None
    note: Optional[str] = None
    either_group: Optional[str] = None


def _bibb(name, title, area, executes=(), initiates=(), **kwargs) -> Bibb:
    return Bibb(name, title, area, frozenset(executes), frozenset(initiates), **kwargs)


_EVENT_NOTIFICATIONS = ("confirmed-event-notification", "unconfirmed-event-notification")
_EITHER_TIME_SYNC = "B-BC requires DM-TS-B or DM-UTC-B"

# The B-BC list is Annex L of ANSI/ASHRAE 135-2024, which IC 135-2016-11
# lets a device of an earlier protocol revision apply (BACpack claims 22). The About page words it
# "Requirements as listed in <this> of the standard". Service requirements
# per Annex K.
BIBB_PROFILE_SOURCE = "Annex L (2024 edition)"
BBC_BIBBS = (
    _bibb("DS-RP-A", "Data Sharing - ReadProperty - A", "Data Sharing",
          initiates=["read-property"]),
    _bibb("DS-RP-B", "Data Sharing - ReadProperty - B", "Data Sharing",
          executes=["read-property"]),
    _bibb("DS-RPM-A", "Data Sharing - ReadPropertyMultiple - A", "Data Sharing",
          initiates=["read-property-multiple"]),
    _bibb("DS-RPM-B", "Data Sharing - ReadPropertyMultiple - B", "Data Sharing",
          executes=["read-property-multiple"]),
    _bibb("DS-WP-A", "Data Sharing - WriteProperty - A", "Data Sharing",
          initiates=["write-property"]),
    _bibb("DS-WP-B", "Data Sharing - WriteProperty - B", "Data Sharing",
          executes=["write-property"]),
    _bibb("DS-WPM-B", "Data Sharing - WritePropertyMultiple - B", "Data Sharing",
          executes=["write-property-multiple"]),
    _bibb("AE-N-I-B", "Alarm and Event - Notification Internal - B", "Alarm and Event",
          initiates=_EVENT_NOTIFICATIONS),
    _bibb("AE-ACK-B", "Alarm and Event - ACK - B", "Alarm and Event",
          executes=["acknowledge-alarm"], initiates=_EVENT_NOTIFICATIONS),
    _bibb("AE-INFO-B", "Alarm and Event - Information - B", "Alarm and Event",
          executes=["get-event-information"]),
    _bibb("AE-CRL-B", "Alarm and Event - Configurable Recipient Lists - B", "Alarm and Event",
          executes=["add-list-element", "remove-list-element"]),
    _bibb("SCHED-E-B", "Scheduling - External - B", "Scheduling",
          # A Schedule's write targets may name another device.
          executes=["read-property", "write-property"], initiates=["write-property"]),
    _bibb("T-VMT-I-B", "Trending - Viewing and Modifying Trends Internal - B", "Trending",
          executes=["read-range"]),
    _bibb("T-ATR-B", "Trending - Automated Trend Retrieval - B", "Trending",
          executes=["read-range"], initiates=_EVENT_NOTIFICATIONS),
    _bibb("DM-DDB-A", "Device Management - Dynamic Device Binding - A", "Device Management",
          executes=["i-am"], initiates=["who-is"]),
    _bibb("DM-DDB-B", "Device Management - Dynamic Device Binding - B", "Device Management",
          executes=["who-is"], initiates=["i-am"]),
    _bibb("DM-DOB-B", "Device Management - Dynamic Object Binding - B", "Device Management",
          executes=["who-has"], initiates=["i-have"]),
    _bibb("DM-DCC-B", "Device Management - DeviceCommunicationControl - B", "Device Management",
          executes=["device-communication-control"]),
    _bibb("DM-TS-B", "Device Management - TimeSynchronization - B", "Device Management",
          executes=["time-synchronization"], note=_EITHER_TIME_SYNC, either_group="time-sync"),
    _bibb("DM-UTC-B", "Device Management - UTCTimeSynchronization - B", "Device Management",
          executes=["utc-time-synchronization"], note=_EITHER_TIME_SYNC, either_group="time-sync"),
    _bibb("DM-RD-B", "Device Management - ReinitializeDevice - B", "Device Management",
          executes=["reinitialize-device"]),
    _bibb("DM-BR-B", "Device Management - Backup and Restore - B", "Device Management",
          executes=["atomic-read-file", "atomic-write-file", "reinitialize-device"]),
)


def bibb_status(bibb: Bibb, advertised: Iterable[str]) -> tuple[bool, list[str]]:
    """(supported, missing): `missing` says why a BIBB fails -- executed
    services not advertised, initiated services not in INITIATED_SERVICES,
    then `unmet`. Supported exactly when `missing` is empty.
    """
    advertised = set(advertised)
    missing = [f"execute {s}" for s in sorted(bibb.executes - advertised)]
    missing += [f"initiate {s}" for s in sorted(bibb.initiates - INITIATED_SERVICES)]
    if bibb.unmet:
        missing.append(bibb.unmet)
    return (not missing, missing)


def capabilities_report(app_cls: type, version: str) -> dict:
    """The GET /api/capabilities payload."""
    executed = executed_services(app_cls)

    bibbs = []
    groups: dict[str, bool] = {}
    for bibb in BBC_BIBBS:
        supported, missing = bibb_status(bibb, executed)
        group = bibb.either_group or bibb.name
        groups[group] = groups.get(group, False) or supported
        bibbs.append(
            {
                "name": bibb.name,
                "title": bibb.title,
                "area": bibb.area,
                "supported": supported,
                "missing": missing,
                "note": bibb.note,
            }
        )

    services = [
        {
            "name": name,
            "kind": service_kind(name),
            "executes": name in executed,
            "initiates": name in INITIATED_SERVICES,
        }
        for name in sorted(set(executed) | INITIATED_SERVICES)
    ]

    return {
        "bacpack_version": version,
        "protocol_revision": DeviceObject.protocolRevision,
        "segmentation": str(Segmentation(DeviceObject.segmentationSupported)),
        "max_apdu_length": DeviceObject.maxApduLengthAccepted,
        "data_links": list(DATA_LINKS),
        "bibb_profile_source": BIBB_PROFILE_SOURCE,
        "bibb_summary": {"met": sum(groups.values()), "total": len(groups)},
        "services": services,
        "object_types": object_types_supported(),
        "bibbs": bibbs,
    }
