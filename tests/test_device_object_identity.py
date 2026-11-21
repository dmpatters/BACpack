"""Device object identity properties. A real client reads these off the device
object; bacpypes3's own class defaults would make BACpack introduce itself as
"BACpypes".
"""
from __future__ import annotations

import pytest

from bacpack.models import Device
from bacpack.objects.device import build_device_object


@pytest.mark.asyncio
async def test_configured_vendor_name_reaches_the_device_object():
    # DeviceObject.__init__ calls asyncio.ensure_future, so this needs a
    # running loop -- hence the asyncio mark rather than a plain test.
    device = Device(id=599001, name="D", vendor_identifier=999, vendor_name="Example Controls")
    obj = build_device_object(device)
    assert obj.vendorName == "Example Controls"


@pytest.mark.asyncio
async def test_vendor_name_falls_back_to_bacpack_not_bacpypes():
    device = Device(id=599001, name="D", vendor_identifier=999, vendor_name=None)
    obj = build_device_object(device)
    assert obj.vendorName == "BACpack"


@pytest.mark.asyncio
async def test_model_and_firmware_are_bacpack_specific_not_placeholders():
    device = Device(id=599001, name="D", vendor_identifier=999)
    obj = build_device_object(device)
    assert obj.modelName != "N/A"
    assert obj.firmwareRevision != "N/A"
    assert obj.applicationSoftwareVersion != "1.0"
    assert "BACpack" in obj.modelName


@pytest.mark.asyncio
async def test_firmware_reports_the_same_version_as_the_api_without_package_metadata(monkeypatch):
    import bacpack
    from importlib.metadata import PackageNotFoundError

    def not_installed(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(bacpack, "_pkg_version", not_installed)
    obj = build_device_object(Device(id=599001, name="D", vendor_identifier=999))
    assert obj.firmwareRevision == bacpack.bacpack_version() == "0.0.0-dev"
    assert obj.applicationSoftwareVersion == "0.0.0-dev"
