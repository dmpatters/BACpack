"""Discovery, ReadProperty and ReadPropertyMultiple, against the committed
config (tests/interop/interop-config.json)."""
from __future__ import annotations

from . import parse
from .helpers import rp, rpm
from .stack import IOP1, IOP2

# What BACpack claims to clients, written out here on purpose: a change to
# one of them (objects/device.py, capabilities.py) must fail this suite.
MAX_APDU = 1024
PROTOCOL_REVISION = "22"
SEGMENTATION = "segmented-both"
MODEL_NAME = "BACpack Simulator"


def test_who_is_finds_both_devices(stack):
    found = parse.parse_whois(stack.whois_text())
    assert found == {IOP1: MAX_APDU, IOP2: MAX_APDU}


def test_who_is_limits_select_one_device(stack):
    found = parse.parse_whois(stack.tools.run("bacwi", IOP1, IOP1, "--timeout", "2000").expect_ok().out)
    assert found == {IOP1: MAX_APDU}


def test_device_object_properties(stack):
    t = stack.tools
    for device, name in ((IOP1, "IOP-1"), (IOP2, "IOP-2")):
        assert rp(t, device, "device", device, "object-name") == name
        assert rp(t, device, "device", device, "vendor-name") == "Interop Vendor"
        assert rp(t, device, "device", device, "vendor-identifier") == "999"
        assert rp(t, device, "device", device, "model-name") == MODEL_NAME
        assert rp(t, device, "device", device, "protocol-revision") == PROTOCOL_REVISION
        assert rp(t, device, "device", device, "segmentation-supported") == SEGMENTATION
        assert rp(t, device, "device", device, "max-apdu-length-accepted") == str(MAX_APDU)
    assert rp(t, IOP1, "device", IOP1, "description") == "interop device one"
    assert rp(t, IOP1, "device", IOP1, "location") == "loopback"


def test_rpm_reads_several_properties_of_several_objects(stack):
    got = rpm(
        stack.tools,
        IOP1,
        ("analog-input", 1, "object-name,present-value,units"),
        ("analog-output", 1, "present-value,relinquish-default"),
        ("binary-value", 1, "object-name,active-text,inactive-text"),
        ("multi-state-value", 1, "object-name,number-of-states,state-text"),
        ("analog-input", 2, "present-value,high-limit"),
    )
    assert got[("analog-input", 1)] == {
        "object-name": '"IOP Temp"',
        "present-value": "21.500000",
        "units": "degrees-celsius",
    }
    assert got[("analog-output", 1)]["relinquish-default"] == "10.000000"
    assert got[("analog-output", 1)]["present-value"] == "10.000000"
    assert got[("binary-value", 1)] == {
        "object-name": '"IOP Occupancy"',
        "active-text": '"Occupied"',
        "inactive-text": '"Unoccupied"',
    }
    mode = got[("multi-state-value", 1)]
    assert mode["number-of-states"] == "3"
    assert mode["state-text"] == '{"Off", "Heat", "Cool"}' or parse.parse_array(mode["state-text"]) == [
        '"Off"', '"Heat"', '"Cool"'
    ]
    assert got[("analog-input", 2)] == {"present-value": "80.000000", "high-limit": "70.000000"}


def test_object_names_on_the_second_device(stack):
    got = rpm(stack.tools, IOP2, ("analog-value", 1, "object-name,units"), ("binary-value", 1, "object-name"))
    assert got[("analog-value", 1)]["object-name"] == '"IOP2 Value"'
    assert got[("binary-value", 1)]["object-name"] == '"IOP2 Flag"'


def test_unknown_object_is_refused(stack):
    result = stack.tools.run("bacrp", IOP1, "analog-input", 99, "present-value")
    assert result.code != 0
    assert parse.parse_error(result.text) == ("object", "unknown-object")
