"""The packaged sample building: it must keep parsing and building as the
config format moves on."""
from __future__ import annotations

import asyncio
import json
import tomllib
from importlib import resources
from pathlib import Path

from bacpack import object_config
from bacpack.config_io import parse_config_document
from bacpack.models import MAX_INSTANCE, BacnetObject, Device
from bacpack.object_builder import build_application


def _parsed():
    text = (resources.files("bacpack") / "samples" / "sample-building.json").read_text(encoding="utf-8")
    return parse_config_document(json.loads(text))


def test_the_file_parses_to_four_devices():
    parsed = _parsed()
    devices = parsed.devices
    assert [d["id"] for d in devices] == [4190001, 4190101, 4190102, 4190103]
    assert [d["name"] for d in devices] == ["Sample AHU-1", "Sample VAV-1", "Sample VAV-2", "Sample VAV-3"]
    assert [len(d["objects"]) for d in devices] == [13, 5, 5, 5]
    assert devices[0]["network_role"] == "bbmd"
    assert devices[0]["bbmd_links"] == [4190101, 4190102, 4190103]
    assert [d["network_role"] for d in devices[1:]] == ["normal"] * 3
    assert all(d["bind_address"] is None for d in devices)
    assert parsed.connectors == []
    assert parsed.settings is None


def test_every_device_passes_check_device():
    for device in _parsed().devices:
        assert object_config.check_device(device["name"], device["objects"]) is None


async def test_every_device_builds_a_bacnet_application():
    for row in _parsed().devices:
        device = Device(id=row["id"], name=row["name"], vendor_identifier=999)
        objects = [BacnetObject(device=device, **obj) for obj in row["objects"]]
        build_application(device, objects)


def test_all_ids_fit_a_bacnet_instance():
    assert all(d["id"] <= MAX_INSTANCE for d in _parsed().devices)


def test_the_package_data_ships_the_file():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert "samples/*.json" in data["tool"]["setuptools"]["package-data"]["bacpack"]


async def test_the_ahu_schedule_holds_the_occupancy_slot_and_the_filter_point_alarms():
    """The shipped AHU behaves, whatever the day or time: the schedule's default
    or its entries always put a value at priority 14, and the filter pressure
    goes high-limit above 1.2 and back to normal below 1.1."""
    row = _parsed().devices[0]
    device = Device(id=row["id"], name=row["name"], vendor_identifier=999)
    app = build_application(device, [BacnetObject(device=device, **obj) for obj in row["objects"]])

    for _ in range(20):
        await asyncio.sleep(0.02)
    occupancy = app.get_object_name("Occupancy")
    slot_14 = (await occupancy.read_property("priorityArray"))[13]
    assert slot_14 is not None and not getattr(slot_14, "null", None)

    filter_pressure = app.get_object_name("Filter Differential Pressure")
    filter_pressure.presentValue = 1.5
    for _ in range(20):
        await asyncio.sleep(0.05)
    assert str(await filter_pressure.read_property("eventState")) == "high-limit"
    filter_pressure.presentValue = 1.0
    for _ in range(20):
        await asyncio.sleep(0.05)
    assert str(await filter_pressure.read_property("eventState")) == "normal"
