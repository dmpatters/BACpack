"""The PICS (ASHRAE 135 Annex A) as Markdown, generated from the code so it
can't drift: services and BIBBs from `capabilities`, each object type's
properties and write allowlist from sample objects built the way a device
builds them. `scripts/make_pics.py` writes PICS.md.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import bacpack_version
from .application import LoggingApplication
from .bacnet import WritePropertyAllowlisted
from .capabilities import BBC_BIBBS, capabilities_report, executed_services, object_types_supported
from .models import ALARMABLE_TYPES, COMMANDABLE_TYPES, INPUT_TYPES, BacnetObject, Device
from .object_builder import OBJECT_CLASSES, build_application
from .objects.alarm_config import AlarmConfigWritable
from .objects.network_port import build_network_port_object

STANDARD_TYPES: tuple[str, ...] = tuple(object_types_supported())

_COMMANDABLE_DEFAULT = {"analog": "0.0", "binary": "inactive", "multi": "1"}

# An unsaved row reads None, not the column default, and the builders need
# these (and an id, which the Trend Log's instance comes from).
_COLUMN_DEFAULTS = dict(
    description="sample",
    bacnet_writable=True,
    out_of_service_default=False,
    schedule_priority=14,
    alarm_time_delay=0,
    alarm_deadband=0.0,
    alarm_notify_type="alarm",
    alarm_event_detection_enable=True,
    trend_mode="interval",
    trend_interval_seconds=60,
    trend_notification_threshold=10,
    trend_notify_type="event",
    trend_event_detection_enable=True,
)


@dataclass(frozen=True)
class Sample:
    """One built object's facts."""

    properties: tuple[tuple[str, str], ...]  # (kebab, camelCase), in propertyList order
    writable: frozenset[str]  # camelCase


def _row(object_type: str, row_id: int, *, full: bool) -> BacnetObject:
    values: dict = dict(_COLUMN_DEFAULTS, id=row_id)
    family = object_type.split("-")[0]
    if family == "analog":
        values["units"] = "degreesCelsius"
    if family == "multi":
        values["state_text"] = ["a", "b", "c"]
    if object_type in COMMANDABLE_TYPES:
        values["relinquish_default"] = _COMMANDABLE_DEFAULT[family]
    if object_type == "schedule":
        values.update(schedule_value_type="analog", schedule_default="0")
    if full:
        if object_type in ALARMABLE_TYPES:
            values.update(alarming_enabled=True, notification_class_instance=1)
            # Time_Delay_Normal is present only once set.
            values["alarm_time_delay_normal"] = 0
            if family == "analog":
                values.update(alarm_high_limit=90.0, alarm_low_limit=10.0)
            elif family == "binary":
                values["alarm_value"] = True
            else:
                values["alarm_values"] = [2]
        if object_type == "schedule":
            values["schedule_write_targets"] = [{"object_type": "analog-value", "instance_number": 1}]
    if object_type == "analog-value":
        values["trended"] = True
        if full:
            values.update(trend_reporting_enabled=True, trend_notification_class_instance=1)
    return BacnetObject(object_type=object_type, instance_number=1, name=f"{object_type}-1", **values)


# The four properties Property_List leaves out by definition (camelCase).
ALWAYS_PRESENT = ("objectIdentifier", "objectName", "objectType", "propertyList")
_ALWAYS_PRESENT_KEBAB = ("object-identifier", "object-name", "object-type", "property-list")


def writable_attrs(obj) -> frozenset[str]:
    """camelCase names a client may write on a built object: its allowlist,
    plus the alarm-config properties when AlarmConfigWritable sits before the
    allowlist in the class (the Trend Log, whose allowlist is empty on
    purpose; elsewhere the allowlist is first and lists them itself). Read on
    the instance: a Schedule's and a Calendar's allowlist depends on the
    object's BACnet-writable setting.
    """
    writable = set(obj._WRITABLE_ATTRS)
    mro = type(obj).__mro__
    if AlarmConfigWritable in mro and mro.index(AlarmConfigWritable) < mro.index(WritePropertyAllowlisted):
        writable |= set(obj._ALARM_CONFIG_COLUMNS)
    return frozenset(writable)


def _sample_of(obj) -> Sample:
    return Sample(
        properties=tuple((str(p), p.attr) for p in obj.propertyList),
        writable=writable_attrs(obj),
    )


async def _collect_objects(full: bool) -> dict:
    """object type -> the built object, one of each type an Application holds."""
    rows = [_row(t, i + 1, full=full) for i, t in enumerate(OBJECT_CLASSES)]
    # No bind_address: from_object_list would open a UDP socket for a Network
    # Port. The factory is a stand-in that only makes the Trend Log build;
    # reading propertyList never calls it.
    app = build_application(
        Device(id=1, name="PICS sample", vendor_identifier=999),
        rows,
        trend_session_factory=lambda: None,
    )
    return {str(obj.objectIdentifier[0]): obj for obj in app.objectIdentifier.values()}


async def _collect(full: bool) -> dict[str, Sample]:
    return {t: _sample_of(obj) for t, obj in (await _collect_objects(full)).items()}


async def _collect_all() -> dict[str, dict[str, Sample]]:
    base = await _collect(full=False)
    full = await _collect(full=True)
    samples: dict[str, dict[str, Sample]] = {t: {"base": base[t], "full": full[t]} for t in base}
    # Built alone: a Network Port in an Application opens its socket.
    roles = {"base": {}, "foreign": {"foreign_bbmd": "10.0.0.1"}, "bbmd": {"bbmd_peers": ["10.0.0.2"]}}
    samples["network-port"] = {
        label: _sample_of(build_network_port_object("10.0.0.5/24", **kwargs)) for label, kwargs in roles.items()
    }
    return samples


def build_samples() -> dict[str, dict[str, Sample]]:
    """object type -> variant label -> Sample. Runs its own event loop (the
    local objects need one), so it can't be called from inside a running one."""
    return asyncio.run(_collect_all())


_BACNET_WRITABLE = "only when the object's BACnet-writable setting is on"

# What decides whether a write to a property that is on an allowlist works.
# Hand-kept: these rules live in write_property chains that nothing exposes as
# data (probing each property could replace this table). Each key must name a
# writable property of that type (tests/test_pics.py).
WRITE_CONDITIONS: dict[tuple[str, str], str] = {
    **{(t, "presentValue"): "only while Out_Of_Service is TRUE" for t in INPUT_TYPES},
    **{
        (t, "presentValue"): f"{_BACNET_WRITABLE} (a priority write or a relinquish)"
        for t in COMMANDABLE_TYPES
    },
    **{
        ("schedule", attr): _BACNET_WRITABLE
        for attr in (
            "weeklySchedule", "exceptionSchedule", "effectivePeriod",
            "scheduleDefault", "priorityForWriting", "listOfObjectPropertyReferences",
        )
    },
    ("calendar", "dateList"): _BACNET_WRITABLE,
    ("file", "fileSize"): "only to 0, and only while a restore is in progress (a client zeroes the file first)",
    **{
        (t, "timeDelayNormal"): "(present only once set, otherwise Time_Delay applies to both transitions;"
        " a write sets it)"
        for t in ALARMABLE_TYPES
    },
}

# What turns on a property that only some variants of a type carry.
_NETWORK_ROLES = {"foreign": "the network role is foreign device", "bbmd": "the network role is BBMD"}


def _feature(object_type: str, label: str) -> str:
    if object_type == "network-port":
        return _NETWORK_ROLES[label]
    if object_type in ALARMABLE_TYPES:
        return "intrinsic reporting is configured"
    if object_type == "trend-log":
        return "the point's trend reporting is on"
    raise KeyError(f"no feature named for {object_type} variant {label}")


@dataclass(frozen=True)
class PropertyRow:
    name: str  # kebab, as in propertyList
    writable: bool
    condition: str  # "" when none


def property_table(object_type: str) -> list[PropertyRow]:
    """The four always-present properties, then every property any variant
    has, in propertyList order."""
    variants = build_samples()[object_type]
    return _property_table(object_type, variants)


def _property_table(object_type: str, variants: dict[str, Sample]) -> list[PropertyRow]:
    writable = frozenset().union(*(s.writable for s in variants.values()))
    present_in: dict[str, tuple[str, list[str]]] = {}  # kebab -> (camel, labels having it)
    for label, sample in variants.items():
        for kebab, camel in sample.properties:
            present_in.setdefault(kebab, (camel, []))[1].append(label)
    rows = [
        PropertyRow(kebab, camel in writable, _write_note(object_type, camel, camel in writable))
        for kebab, camel in zip(_ALWAYS_PRESENT_KEBAB, ALWAYS_PRESENT, strict=True)
    ]
    for kebab, (camel, labels) in present_in.items():
        notes = []
        if len(labels) < len(variants):
            notes.append("present only when " + " or ".join(_feature(object_type, label) for label in labels))
        is_writable = camel in writable
        write_note = _write_note(object_type, camel, is_writable)
        if write_note:
            notes.append(write_note)
        rows.append(PropertyRow(kebab, is_writable, "; ".join(notes)))
    return rows


def _write_note(object_type: str, camel: str, is_writable: bool) -> str:
    note = WRITE_CONDITIONS.get((object_type, camel)) if is_writable else None
    return f"writable {note}" if note else ""


async def _device_facts() -> dict[str, str]:
    app = build_application(Device(id=1, name="PICS sample", vendor_identifier=999), [])
    device = app.device_object
    assert device is not None  # from_object_list requires one
    facts = {
        name: str(getattr(device, name))
        for name in (
            "segmentationSupported", "maxApduLengthAccepted", "maxSegmentsAccepted",
            "apduSegmentTimeout", "apduTimeout", "numberOfApduRetries", "protocolRevision",
        )
    }
    facts["deviceAddressBinding"] = "non-empty" if len(device.deviceAddressBinding) else "empty"
    return facts


def sample_device_facts() -> dict[str, str]:
    """The Device object's values as built, stringified. Own event loop, as
    build_samples."""
    return asyncio.run(_device_facts())


def _report() -> dict:
    return capabilities_report(LoggingApplication, bacpack_version())


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|")


def _table(headers: tuple[str, ...], rows: list[tuple]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def render_product() -> str:
    report = _report()
    return "\n".join(
        [
            "## 1. Product",
            "",
            "- Vendor: BACpack open-source project. The vendor identifier and vendor name are set per device.",
            "- Product name: BACpack",
            f"- Product version: {report['bacpack_version']}",
            "- Description: an open-source BACnet/IP server, a device simulator and gateway for testing"
            " BAS/BMS clients and integrations.",
            "- No redundancy or high availability.",
        ]
    )


def render_profile() -> str:
    report = _report()
    rows = [
        (b["name"], b["title"], "yes" if b["supported"] else "no: " + ", ".join(b["missing"]))
        for b in report["bibbs"]
    ]
    summary = report["bibb_summary"]
    groups: dict[str, list[str]] = {}
    for bibb in BBC_BIBBS:
        if bibb.either_group:
            groups.setdefault(bibb.either_group, []).append(bibb.name)
    either = "".join(
        f" ({' and '.join(names)} are one requirement: either meets it)" for names in groups.values()
    )
    return "\n\n".join(
        [
            "## 2. BACnet standardized device profile",
            "BACnet Building Controller (B-BC). "
            f"{summary['met']} of {summary['total']} required BIBBs are supported{either}.",
            "Profile: B-BC as defined in Annex L of ANSI/ASHRAE 135-2024. BACpack devices report "
            f"Protocol_Revision {report['protocol_revision']}. ASHRAE Interpretation IC 135-2016-11"
            " lets a device claim a profile from a newer edition of the standard.",
            _table(("BIBB", "Title", "Supported"), rows),
        ]
    )


def render_services() -> str:
    rows = [
        (s["name"], s["kind"], _yes_no(s["executes"]), _yes_no(s["initiates"]))
        for s in _report()["services"]
    ]
    return "\n\n".join(
        [
            "## 3. BACnet services",
            _table(("Service", "Kind", "Executes", "Initiates"), rows),
            "AddListElement and RemoveListElement apply only to a Notification Class's Recipient_List.",
        ]
    )


def render_segmentation() -> str:
    facts = sample_device_facts()
    return "\n".join(
        [
            "## 4. Segmentation",
            "",
            "Values the Device object reports:",
            "",
            f"- Segmentation supported: {facts['segmentationSupported']} (transmit and receive)",
            f"- Maximum APDU length accepted: {facts['maxApduLengthAccepted']}",
            f"- Maximum segments accepted: {facts['maxSegmentsAccepted']}",
            f"- APDU segment timeout: {facts['apduSegmentTimeout']} ms",
            f"- APDU timeout: {facts['apduTimeout']} ms",
            f"- Number of APDU retries: {facts['numberOfApduRetries']}",
        ]
    )


def render_data_link() -> str:
    return "\n".join(
        [
            "## 6. Data link layer",
            "",
            "- BACnet/IP (Annex J), IPv4 only, one UDP port per device",
            "- BACnet/IP BBMD: a device can act as a BBMD (broadcast distribution table, foreign device table)",
            "- BACnet/IP foreign device: a device can register with a BBMD",
        ]
    )


def render_annex_a_other() -> str:
    facts = sample_device_facts()
    binding = "always empty" if facts["deviceAddressBinding"] == "empty" else "populated"
    return "\n".join(
        [
            "## 7. Other Annex A answers",
            "",
            f"- Device address binding: static binding is not supported (Device_Address_Binding is {binding})."
            " Dynamic binding uses Who-Is / I-Am and Who-Has / I-Have",
            "- Networking options: router: no; BBMD: yes; foreign device: yes",
            "- Network security: none, and BACnet/SC is not supported. An optional per-device password guards"
            " DeviceCommunicationControl and ReinitializeDevice",
            "- Character sets: ISO 10646 (UTF-8)",
            "- Gateway options: not a protocol gateway in the Annex A sense. Connectors expose values pulled"
            " from HTTP endpoints or other BACnet devices as local objects. Proxy mappings also forward writes to"
            " the remote BACnet device",
        ]
    )


_LEVEL_FAMILIES = ("analog-", "binary-", "multi-state-")


def render_object_types() -> str:
    executed = executed_services(LoggingApplication)
    created = _yes_no("create-object" in executed)
    deleted = _yes_no("delete-object" in executed)
    samples = build_samples()
    blocks = [
        "## 5. Standard object types",
        "Standard object types only: no proprietary properties and no proprietary object types.",
    ]
    for object_type in STANDARD_TYPES:
        lines = [f"### {object_type}", "", f"Can be created: {created}. Can be deleted: {deleted}."]
        if object_type.startswith(_LEVEL_FAMILIES) and object_type not in ALARMABLE_TYPES:
            lines += ["", "Intrinsic reporting: not supported."]
        rows = [
            (row.name, _yes_no(row.writable), row.condition)
            for row in _property_table(object_type, samples[object_type])
        ]
        lines += ["", _table(("Property", "Writable", "Condition"), rows)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_pics() -> str:
    """The whole PICS.md. Synchronous (it builds sample objects in their own
    event loops), so not callable from a running loop."""
    parts = [
        "# BACpack PICS",
        "Protocol Implementation Conformance Statement, laid out per ASHRAE 135 Annex A (the PICS"
        " form). Generated from the code by `scripts/make_pics.py`; do not edit by hand.",
        render_product(),
        render_profile(),
        render_services(),
        render_segmentation(),
        render_object_types(),
        render_data_link(),
        render_annex_a_other(),
    ]
    return "\n\n".join(parts) + "\n"
