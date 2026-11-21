"""The generated PICS: sample objects, property tables,
and that PICS.md is what the generator writes now."""
from __future__ import annotations

import asyncio
import logging

from bacpack import capabilities
from bacpack.pics import STANDARD_TYPES, build_samples


def test_every_standard_type_has_a_sample():
    samples = build_samples()
    assert set(samples) == set(capabilities.object_types_supported())
    assert list(STANDARD_TYPES) == capabilities.object_types_supported()


def test_every_type_has_base_and_full_and_full_is_a_superset():
    for object_type, variants in build_samples().items():
        if object_type == "network-port":
            assert set(variants) == {"base", "foreign", "bbmd"}
            continue
        assert set(variants) == {"base", "full"}, object_type
        base = {kebab for kebab, _ in variants["base"].properties}
        full = {kebab for kebab, _ in variants["full"].properties}
        assert base <= full, object_type


def _names(sample) -> set:
    return {kebab for kebab, _ in sample.properties}


def test_optional_features_add_properties_only_where_a_type_has_them():
    samples = build_samples()
    ai = samples["analog-input"]
    assert {"high-limit", "event-enable"} <= _names(ai["full"]) - _names(ai["base"])
    for never_alarms in ("binary-output", "multi-state-output"):
        assert _names(samples[never_alarms]["full"]) == _names(samples[never_alarms]["base"])
    trend = samples["trend-log"]
    assert {"notification-threshold", "records-since-notification"} <= _names(trend["full"]) - _names(
        trend["base"]
    )
    port = samples["network-port"]
    assert "bbmd-broadcast-distribution-table" in _names(port["bbmd"])
    assert "fd-bbmd-address" in _names(port["foreign"])
    assert not {"bbmd-broadcast-distribution-table", "fd-bbmd-address"} & _names(port["base"])


def test_property_list_leaves_out_the_four_by_definition():
    for variants in build_samples().values():
        for sample in variants.values():
            assert not {"object-identifier", "object-name", "object-type", "property-list"} & _names(sample)


def test_building_opens_no_socket_and_leaves_no_orphan_task(monkeypatch, capfd, caplog):
    def boom(*args, **kwargs):
        raise AssertionError("a sample build must not open a socket")

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_datagram_endpoint", boom)
    build_samples()
    assert capfd.readouterr().err == ""
    # asyncio reports "Task exception was never retrieved" through logging.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


def _camel(sample) -> set:
    return {camel for _, camel in sample.properties}


_FOUR = {"objectIdentifier", "objectName", "objectType", "propertyList"}


def _writable(object_type: str, variant: str) -> set:
    return set(build_samples()[object_type][variant].writable)


def test_writable_is_a_subset_of_present_plus_the_four():
    for object_type, variants in build_samples().items():
        for label, sample in variants.items():
            assert sample.writable <= _camel(sample) | _FOUR, (object_type, label)


_ALARM_ANALOG = {
    "highLimit", "lowLimit", "deadband", "limitEnable", "timeDelay", "timeDelayNormal",
    "eventEnable", "notifyType", "notificationClass", "eventDetectionEnable",
}


def test_exact_writable_sets():
    assert _writable("device", "full") == {"objectName", "description", "location", "backupFailureTimeout"}
    assert _writable("file", "full") == {"fileSize"}
    for label in ("base", "foreign", "bbmd"):
        assert _writable("network-port", label) == set()
    assert _writable("notification-class", "full") == {"recipientList", "priority", "ackRequired"}
    assert _writable("calendar", "full") == {"dateList"}
    assert _writable("schedule", "full") == {
        "weeklySchedule", "exceptionSchedule", "effectivePeriod", "scheduleDefault",
        "priorityForWriting", "listOfObjectPropertyReferences",
    }
    assert _writable("trend-log", "full") == {
        "notificationThreshold", "notificationClass", "eventEnable", "notifyType", "eventDetectionEnable",
    }
    assert _writable("trend-log", "base") == set()
    assert _writable("analog-input", "base") == {"presentValue", "outOfService"}
    assert _writable("analog-value", "base") == {"presentValue", "relinquishDefault", "outOfService"}
    assert _writable("analog-value", "full") == {"presentValue", "relinquishDefault", "outOfService"} | _ALARM_ANALOG
    assert "stateText" in _writable("multi-state-input", "base")
    assert {"alarmValue", "timeDelayNormal"} <= _writable("binary-value", "full")
    assert {"alarmValues", "timeDelayNormal"} <= _writable("multi-state-value", "full")


def test_every_allowlist_entry_of_a_full_variant_is_a_present_property():
    # writable_attrs must not filter: a gap (an allowlisted name the object
    # lacks) has to show up here, not vanish from the document.
    import asyncio

    from bacpack.pics import ALWAYS_PRESENT, _collect_objects

    async def check() -> None:
        for object_type, obj in (await _collect_objects(full=True)).items():
            present = {p.attr for p in obj.propertyList} | set(ALWAYS_PRESENT)
            assert set(obj._WRITABLE_ATTRS) <= present, (object_type, set(obj._WRITABLE_ATTRS) - present)

    asyncio.run(check())


async def test_schedule_and_calendar_writable_follow_the_writable_setting():
    from bacpack.models import BacnetObject, Device
    from bacpack.object_builder import build_application
    from bacpack.pics import writable_attrs

    rows = [
        BacnetObject(
            id=1, object_type="schedule", instance_number=1, name="S", bacnet_writable=False,
            schedule_value_type="analog", schedule_default="0", schedule_priority=14,
            out_of_service_default=False,
        ),
        BacnetObject(id=2, object_type="calendar", instance_number=1, name="C", bacnet_writable=False),
    ]
    app = build_application(Device(id=1, name="D", vendor_identifier=999), rows)
    assert writable_attrs(app.get_object_name("S")) == frozenset()
    assert writable_attrs(app.get_object_name("C")) == frozenset()


def _union_writable(object_type: str) -> set:
    return set().union(*(s.writable for s in build_samples()[object_type].values()))


def test_every_write_condition_names_a_writable_property():
    from bacpack.pics import WRITE_CONDITIONS

    assert WRITE_CONDITIONS
    for object_type, attr in WRITE_CONDITIONS:
        assert object_type in STANDARD_TYPES, object_type
        assert attr in _union_writable(object_type), f"stale write condition: {object_type} {attr}"


def _rows(object_type: str) -> dict:
    from bacpack.pics import property_table

    return {row.name: row for row in property_table(object_type)}


def test_property_table_rows():
    from bacpack.pics import property_table

    assert [r.name for r in property_table("device")[:4]] == [
        "object-identifier", "object-name", "object-type", "property-list",
    ]
    assert _rows("device")["object-name"].writable
    assert not _rows("analog-input")["object-name"].writable

    av = _rows("analog-value")
    assert av["present-value"].writable and "BACnet-writable" in av["present-value"].condition
    assert av["high-limit"].writable and "intrinsic reporting" in av["high-limit"].condition
    assert av["relinquish-default"].writable and av["relinquish-default"].condition == ""
    assert "Out_Of_Service" in _rows("analog-input")["present-value"].condition
    assert "alarm-value" not in _rows("binary-output")
    assert "restore" in _rows("file")["file-size"].condition
    assert "BACnet-writable" in _rows("schedule")["weekly-schedule"].condition

    port = _rows("network-port")
    assert "foreign" in port["fd-bbmd-address"].condition
    assert "BBMD" in port["bbmd-foreign-device-table"].condition
    assert not any(row.writable for row in port.values())

    trend = _rows("trend-log")
    assert trend["notification-threshold"].writable and "trend reporting" in trend["notification-threshold"].condition
    assert not trend["record-count"].writable and trend["record-count"].condition == ""


def test_a_property_in_some_variants_only_always_says_why():
    from bacpack.pics import property_table

    for object_type, variants in build_samples().items():
        for row in property_table(object_type):
            in_all = all(row.name in {k for k, _ in v.properties} or row.name in (
                "object-identifier", "object-name", "object-type", "property-list") for v in variants.values())
            if not in_all:
                assert row.condition.startswith("present only when"), (object_type, row.name)


def test_built_device_reports_the_default_segmentation_values():
    from bacpack.pics import sample_device_facts

    facts = sample_device_facts()
    assert facts["segmentationSupported"] == "segmented-both"
    assert facts["maxApduLengthAccepted"] == "1024"
    assert facts["maxSegmentsAccepted"] == "16"
    assert facts["apduSegmentTimeout"] == "1000"
    assert facts["apduTimeout"] == "3000"
    assert facts["numberOfApduRetries"] == "3"
    assert facts["protocolRevision"] == "22"
    assert facts["deviceAddressBinding"] == "empty"


def test_product_section():
    import re

    from bacpack import bacpack_version
    from bacpack.pics import render_product

    text = render_product()
    assert "BACpack open-source project" in text
    assert bacpack_version() in text
    assert "ISO 16484" not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)


def test_profile_section():
    from bacpack.pics import render_profile

    text = render_profile()
    assert (
        "Profile: B-BC as defined in Annex L of ANSI/ASHRAE 135-2024. BACpack devices report"
        " Protocol_Revision 22. ASHRAE Interpretation IC 135-2016-11 lets a device claim a profile"
        " from a newer edition of the standard."
    ) in text
    for bibb in capabilities.BBC_BIBBS:
        assert f"| {bibb.name} |" in text


def test_services_section():
    from bacpack.pics import render_services

    text = render_services()
    assert "| read-property | confirmed | yes | yes |" in text
    assert "create-object" not in text
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    from bacpack.application import LoggingApplication

    assert len(rows) - 1 == len(capabilities.capabilities_report(LoggingApplication, "x")["services"])


def test_segmentation_data_link_and_other_sections():
    from bacpack.pics import render_annex_a_other, render_data_link, render_segmentation

    seg = render_segmentation()
    for fragment in ("segmented-both", "1024", "16", "1000 ms", "3000 ms", "retries: 3"):
        assert fragment in seg
    link = render_data_link()
    assert "BACnet/IP" in link and "BBMD" in link and "foreign device" in link
    other = render_annex_a_other()
    for fragment in ("UTF-8", "BBMD", "BACnet/SC", "Device_Address_Binding", "gateway"):
        assert fragment in other


def test_render_is_deterministic_and_ends_with_one_newline():
    from bacpack.pics import render_pics

    text = render_pics()
    assert text == render_pics()
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_every_standard_type_has_a_heading_and_a_table():
    from bacpack.pics import render_pics

    text = render_pics()
    for object_type in STANDARD_TYPES:
        assert f"### {object_type}\n" in text
    assert "| present-value | yes |" in text
    assert "| object-name | yes |" in text  # the Device's
    assert "Intrinsic reporting: not supported." in text


def test_objects_are_neither_creatable_nor_deletable():
    from bacpack.application import LoggingApplication
    from bacpack.pics import render_pics

    executed = capabilities.executed_services(LoggingApplication)
    assert "create-object" not in executed and "delete-object" not in executed
    assert render_pics().count("Can be created: no. Can be deleted: no.") == len(STANDARD_TYPES)


def test_sections_in_annex_a_order():
    from bacpack.pics import render_pics

    text = render_pics()
    titles = [
        "## 1. Product", "## 2. BACnet standardized device profile", "## 3. BACnet services",
        "## 4. Segmentation", "## 5. Standard object types", "## 6. Data link layer",
        "## 7. Other Annex A answers",
    ]
    positions = [text.index(title) for title in titles]
    assert positions == sorted(positions)


def test_pics_md_matches_the_generator():
    import difflib
    from pathlib import Path

    from bacpack.pics import render_pics

    actual = (Path(__file__).resolve().parents[1] / "PICS.md").read_text(encoding="utf-8")
    expected = render_pics()
    if actual != expected:
        diff = list(difflib.unified_diff(actual.splitlines(), expected.splitlines(), "PICS.md", "generated", lineterm=""))
        raise AssertionError(
            "PICS.md is out of date: run `python scripts/make_pics.py` and commit it\n"
            + "\n".join(diff[:20])
        )


def test_the_windows_bundle_ships_pics():
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "build_bundle.ps1"
    assert any(
        "Copy-Item" in line and "PICS.md" in line for line in script.read_text(encoding="utf-8").splitlines()
    )


def test_make_pics_writes_the_generated_text(tmp_path):
    import importlib.util
    from pathlib import Path

    from bacpack.pics import render_pics

    path = Path(__file__).resolve().parents[1] / "scripts" / "make_pics.py"
    spec = importlib.util.spec_from_file_location("make_pics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "PICS.md"
    module.write_pics(target)
    assert target.read_bytes() == render_pics().encode("utf-8")


def test_the_installed_version_is_the_one_in_pyproject():
    import tomllib
    from pathlib import Path

    from bacpack import bacpack_version

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert bacpack_version() == declared, (
        f"installed metadata says {bacpack_version()}, pyproject says {declared}: "
        'reinstall with `pip install -e ".[dev]"` (PICS.md carries the installed version)'
    )


def test_profile_explains_the_either_or_requirement():
    from bacpack.pics import render_profile

    assert "DM-TS-B and DM-UTC-B are one requirement: either meets it" in render_profile()


def test_pics_wording():
    from bacpack.pics import render_annex_a_other, render_object_types, render_services

    assert "writable only to 0, and only while a restore is in progress" in render_object_types()
    assert "forward writes to the remote BACnet device" in render_annex_a_other()
    assert "AddListElement and RemoveListElement apply only to a Notification Class's Recipient_List" in render_services()
