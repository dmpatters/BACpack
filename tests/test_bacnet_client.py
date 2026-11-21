"""The BACnet client layer."""
from __future__ import annotations

import pytest
from bacpypes3.apdu import AbortPDU, Error, RejectPDU
from bacpypes3.basetypes import BinaryPV, PriorityArray, PriorityValue, StatusFlags
from bacpypes3.constructeddata import AnyAtomic, ArrayOf
from bacpypes3.pdu import RemoteStation
from bacpypes3.primitivedata import (
    Boolean,
    CharacterString,
    Double,
    Enumerated,
    Null,
    ObjectIdentifier,
    Real,
    Unsigned,
)

from bacpypes3.errors import PropertyError

from bacpack.bacnet_client import (
    ClientError,
    Point,
    cast_for_write,
    error_from_reply,
    error_text,
    normalize_object_type,
    normalize_property,
    parse_address,
    resolve_datatype,
    to_jsonable,
)


# --- names and points ------------------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [("analogInput", "analog-input"), ("analog-input", "analog-input"), (" device ", "device"), ("200", "200")],
)
def test_normalize_object_type(given, expected):
    assert normalize_object_type(given) == expected


@pytest.mark.parametrize(
    "given, expected",
    [("presentValue", "present-value"), ("object-name", "object-name"), ("512", "512")],
)
def test_normalize_property(given, expected):
    assert normalize_property(given) == expected


@pytest.mark.parametrize("bad", ["bogus", "", "analog inputs"])
def test_normalize_rejects_unknown_names(bad):
    with pytest.raises(ValueError):
        normalize_object_type(bad)
    with pytest.raises(ValueError):
        normalize_property(bad)


@pytest.mark.parametrize("text", ["Analog-Value", "ANALOG_VALUE", "analogValue", "analog-value"])
def test_normalize_object_type_ignores_case_and_separators(text):
    assert normalize_object_type(text) == "analog-value"


def test_normalize_property_ignores_case_and_separators():
    assert normalize_property("Present-Value") == "present-value"
    assert normalize_property("PRESENTVALUE") == "present-value"


def test_normalize_still_takes_a_proprietary_number_and_rejects_nonsense():
    assert normalize_object_type("200") == "200"
    with pytest.raises(ValueError, match="unknown object type"):
        normalize_object_type("not-a-type")


def test_point_identifiers_and_text():
    point = Point("analog-input", 3, "priority-array", 2)
    assert point.object_identifier == ObjectIdentifier("analog-input,3")
    assert str(point.property_identifier) == "priority-array"
    assert str(point) == "analog-input:3 priority-array[2]"
    assert str(Point("device", 5)) == "device:5 present-value"


def test_point_with_a_proprietary_type_number():
    assert str(Point("200", 1).object_identifier) == "200,1"


# --- addresses -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("192.168.1.5", "192.168.1.5"),
        ("192.168.1.5:47809", "192.168.1.5:47809"),
        ("2:192.168.1.5:47809", "2:192.168.1.5:47809"),
        ("65534:0x0a0b", "65534:0x0a0b"),
        ("5:18", "5:18"),
        ("5:0", "5:0"),
        ("  10.0.0.1  ", "10.0.0.1"),
    ],
)
def test_parse_address_accepts(text, expected):
    assert str(parse_address(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "", "somehost", "1.2.3", "300.1.1.1", "10.0.0.1:0", "10.0.0.1:70000", "0:10.0.0.1", "0x12", "*", "2:*",
        "18", "5:256", "0:18",
    ],
)
def test_parse_address_rejects(text):
    with pytest.raises(ValueError):
        parse_address(text)


def test_parse_address_round_trips_a_routed_one_octet_mac():
    # bacpypes3 prints a routed 1-octet MAC in decimal, and
    # that's what Who-Is results and bound_address show.
    for station in (RemoteStation(5, bytes([0x12])), RemoteStation(65534, bytes([0]))):
        assert parse_address(str(station)) == station


# --- errors ----------------------------------------------------------------


def test_error_from_reply_kinds():
    assert error_from_reply(AbortPDU(reason="noResponse")).kind == "transport"
    assert error_from_reply(AbortPDU(reason="segmentationNotSupported")).kind == "too-big"
    assert error_from_reply(AbortPDU(reason="bufferOverflow")).kind == "too-big"
    assert error_from_reply(RejectPDU(reason="unrecognizedService")).kind == "unsupported"
    assert error_from_reply(RejectPDU(reason="invalidTag")).kind == "error"
    error = error_from_reply(Error(errorClass="object", errorCode="unknownObject", service_choice=12))
    assert (error.kind, str(error)) == ("error", "object: unknown-object")
    assert error.transport is False
    assert error_from_reply(AbortPDU(reason="noResponse")).transport is True


# --- values ----------------------------------------------------------------


def test_to_jsonable_turns_a_non_finite_real_into_null():
    assert to_jsonable(Real(float("nan"))) is None
    assert to_jsonable(Real(float("inf"))) is None
    assert to_jsonable(float("-inf")) is None


def test_to_jsonable():
    array =ArrayOf(ObjectIdentifier)([ObjectIdentifier("analog-input,1"), ObjectIdentifier("device,5")])
    assert to_jsonable(Real(72.5)) == 72.5
    assert to_jsonable(Unsigned(3)) == 3
    assert to_jsonable(BinaryPV("active")) == "active"
    assert to_jsonable(CharacterString("Zone temp")) == "Zone temp"
    assert to_jsonable(ObjectIdentifier("analog-input,1")) == "analog-input:1"
    assert to_jsonable(array) == ["analog-input:1", "device:5"]
    assert to_jsonable(StatusFlags([0, 1, 0, 0])) == [0, 1, 0, 0]
    assert to_jsonable(Null(())) is None
    assert to_jsonable(None) is None


def test_to_jsonable_unwraps_a_schedule_present_value():
    # bacpypes3 types Schedule Present_Value as AnyAtomic.
    assert to_jsonable(AnyAtomic(Real(72.0))) == 72.0
    assert to_jsonable(AnyAtomic(Boolean(True))) is True
    # get_value() demotes BinaryPV to a plain Enumerated.
    assert to_jsonable(AnyAtomic(BinaryPV("active"))) == "1"


def test_to_jsonable_gives_a_choice_its_chosen_value():
    # A Choice is a bacpypes3 Sequence with an empty _order.
    priority_array = PriorityArray(
        [PriorityValue(null=Null(()))] * 15 + [PriorityValue(real=Real(17.0))]
    )
    assert to_jsonable(priority_array) == [None] * 15 + [17.0]
    assert to_jsonable(PriorityValue(enumerated=Enumerated(1))) == "1"
    assert to_jsonable(PriorityValue()) is None


def test_cast_for_write_unwraps_a_schedule_present_value():
    # A local Schedule as a write-mapping source.
    assert cast_for_write(Point("analog-value", 1), AnyAtomic(Real(72.0))) == Real(72.0)
    assert cast_for_write(Point("binary-value", 1), AnyAtomic(BinaryPV("active"))) == BinaryPV("active")
    assert cast_for_write(Point("binary-value", 1), AnyAtomic(BinaryPV("inactive"))) == BinaryPV("inactive")
    assert cast_for_write(Point("analog-value", 1), AnyAtomic(BinaryPV("active"))) == Real(1.0)
    assert cast_for_write(Point("multi-state-value", 1), AnyAtomic(Unsigned(3))) == Unsigned(3)


def test_cast_for_write_to_a_remote_schedule_present_value():
    point = Point("schedule", 1)
    assert resolve_datatype(point) is AnyAtomic
    for value, encoded_as, expected in [
        (Real(72.0), Real, 72.0),
        (BinaryPV("active"), Enumerated, 1),
        (Unsigned(2), Unsigned, 2),
        (True, Boolean, True),
        (3, Unsigned, 3),
        (72.5, Real, 72.5),
        ("72.5", Real, 72.5),
        (AnyAtomic(Real(60.0)), Real, 60.0),
    ]:
        typed = cast_for_write(point, value)
        assert isinstance(typed, AnyAtomic)
        assert typed.get_value_type() is encoded_as
        assert typed.get_value() == expected
    with pytest.raises(ClientError, match="can't write"):
        cast_for_write(point, "warm")


def test_resolve_datatype():
    assert resolve_datatype(Point("binary-output", 1)) is BinaryPV
    assert resolve_datatype(Point("analog-value", 1)) is Real
    assert resolve_datatype(Point("device", 1, "object-list", 0)) is Unsigned
    assert resolve_datatype(Point("device", 1, "object-list", 3)) is ObjectIdentifier


def test_resolve_datatype_refuses_proprietary_types():
    with pytest.raises(ClientError, match="no standard datatype"):
        resolve_datatype(Point("200", 1))
    with pytest.raises(ClientError, match="no standard datatype"):
        resolve_datatype(Point("analog-value", 1, "512"))


def test_cast_for_write_converts_between_binary_and_analog():
    # A BV feeding an AV, and an AV feeding a BO.
    assert cast_for_write(Point("analog-value", 1), BinaryPV("active")) == 1.0
    assert cast_for_write(Point("binary-output", 1), Real(1.0)) == BinaryPV("active")
    assert cast_for_write(Point("binary-output", 1), BinaryPV("inactive")) == BinaryPV("inactive")
    assert cast_for_write(Point("multi-state-value", 1), 2.0) == Unsigned(2)


def test_cast_for_write_parses_numeric_strings_but_not_for_string_properties():
    assert cast_for_write(Point("analog-value", 1), "72.5") == 72.5
    assert cast_for_write(Point("binary-value", 1), "1") == BinaryPV("active")
    assert cast_for_write(Point("binary-value", 1), "active") == BinaryPV("active")
    assert cast_for_write(Point("analog-value", 1, "description"), "42") == "42"


def test_cast_for_write_null_is_relinquish():
    assert isinstance(cast_for_write(Point("analog-value", 1), None), Null)


def test_cast_for_write_refuses_what_it_cant_convert():
    with pytest.raises(ClientError, match="can't write"):
        cast_for_write(Point("analog-value", 1), "warm")
    with pytest.raises(ClientError, match="can't write"):
        cast_for_write(Point("multi-state-value", 1), -1)


_NON_FINITE = ["nan", "NaN", "inf", "-Infinity", "1e400", float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("value", _NON_FINITE)
@pytest.mark.parametrize(
    "point",
    [
        Point("analog-value", 1),
        Point("analog-value", 1, "cov-increment"),
        Point("analog-value", 1, "min-pres-value"),
    ],
)
def test_cast_for_write_refuses_a_non_finite_number(point, value):
    with pytest.raises(ClientError, match="finite"):
        cast_for_write(point, value)


@pytest.mark.parametrize("value, expected", [("1e300", 1e300), ("-0", 0), ("72.5", 72.5)])
def test_cast_for_write_still_takes_a_finite_number(value, expected):
    assert cast_for_write(Point("analog-value", 1), value) == expected


@pytest.mark.parametrize("value", [2, -1, "2", "-1", "1_0", 0.5, 1.5, Unsigned(2)])
def test_cast_for_write_refuses_a_boolean_that_is_not_0_or_1(value):
    with pytest.raises(ClientError, match="can't write"):
        cast_for_write(Point("binary-value", 1, "out-of-service"), value)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, False), (1, True), (1.0, True), (0.0, False), ("0", False), ("1", True),
        (True, True), (False, False), ("true", True), ("False", False), ("TRUE", True),
    ],
)
def test_cast_for_write_still_takes_a_boolean_as_0_1_true_false(value, expected):
    typed = cast_for_write(Point("binary-value", 1, "out-of-service"), value)
    assert isinstance(typed, Boolean) and bool(typed) is expected


@pytest.mark.parametrize("value", ["set", "reset", "yes", "on", "", "truee"])
def test_cast_for_write_refuses_boolean_text_that_is_not_true_or_false(value):
    with pytest.raises(ClientError, match="use 0, 1, true or false"):
        cast_for_write(Point("binary-value", 1, "out-of-service"), value)


@pytest.mark.parametrize("value, expected", [(" false ", False), (" True ", True), ("FALSE", False)])
def test_cast_for_write_takes_padded_boolean_text(value, expected):
    typed = cast_for_write(Point("binary-value", 1, "out-of-service"), value)
    assert isinstance(typed, Boolean) and bool(typed) is expected


@pytest.mark.parametrize("value", [Real(float("nan")), Real(float("inf")), Double(float("-inf")), AnyAtomic(Real(float("nan")))])
def test_cast_for_write_to_an_any_atomic_refuses_a_non_finite_atomic(value):
    with pytest.raises(ClientError, match="finite"):
        cast_for_write(Point("schedule", 1), value)


def test_cast_for_write_to_an_any_atomic_keeps_a_finite_atomic():
    typed = cast_for_write(Point("schedule", 1), Real(2.5))
    assert typed.get_value_type() is Real and typed.get_value() == 2.5


@pytest.mark.asyncio
async def test_write_of_a_non_finite_number_sends_nothing():
    app = StubApp()

    with pytest.raises(ClientError, match="finite"):
        await BacnetClient(app).write(
            Binding(Address("10.0.0.7")), Point("analog-value", 1), "nan", priority=8, timeout=1
        )

    assert app.calls == []


@pytest.mark.parametrize("value", [float("nan"), "inf", "nan", "1e400"])
def test_cast_for_write_to_an_any_atomic_refuses_a_non_finite_number(value):
    with pytest.raises(ClientError, match="finite"):
        cast_for_write(Point("schedule", 1), value)


def test_cast_for_write_to_an_any_atomic_still_takes_a_finite_string():
    typed = cast_for_write(Point("schedule", 1), "1.5")
    assert typed.get_value_type() is Real and typed.get_value() == 1.5


def test_cast_for_write_non_finite_text_to_a_string_property_stays_text():
    assert cast_for_write(Point("analog-value", 1, "description"), "nan") == "nan"


@pytest.mark.parametrize("value", [2, -1, 0.5, 2.0, "2", "-1", "0x10", "1_0", "maybe", "", "10", Unsigned(2)])
def test_cast_for_write_refuses_a_binary_present_value_that_is_not_0_or_1(value):
    with pytest.raises(ClientError, match="binary-value:1"):
        cast_for_write(Point("binary-value", 1), value)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "inactive"), (1, "active"), (0.0, "inactive"), (1.0, "active"), ("0", "inactive"), ("1", "active"),
        (False, "inactive"), (True, "active"), ("inactive", "inactive"), ("Active", "active"),
        ("TRUE", "active"), ("false", "inactive"), (BinaryPV("active"), "active"),
        (AnyAtomic(BinaryPV("inactive")), "inactive"),
    ],
)
def test_cast_for_write_takes_a_binary_present_value_as_0_1_names_or_true_false(value, expected):
    assert cast_for_write(Point("binary-output", 1), value) == BinaryPV(expected)


# --- BacnetClient ------------------------------------------------------------

import asyncio
from types import SimpleNamespace

from bacpypes3.basetypes import ErrorType, Segmentation
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import PropertyIdentifier

from bacpypes3.apdu import SimpleAckPDU

from bacpack.bacnet_client import BacnetClient, Binding, has_standard_datatype


class _RecordingLog:
    def __init__(self):
        self.entries = []

    def add(self, event_type, detail):
        self.entries.append((event_type, detail))


class StubApp:
    """Stands in for LoggingApplication's client methods. Replies are set per
    test; an exception in a reply slot is raised, as bacpypes3 does."""

    def __init__(self):
        self._dcc_state = "enable"
        self.traffic_log = _RecordingLog()
        self.i_ams = []
        self.rp_values = {}
        self.rpm_replies = []
        self.write_error = None
        self.calls = []

    def who_is(self, low, high, timeout):
        self.calls.append(("who-is", low, high))
        future = asyncio.get_running_loop().create_future()
        future.set_result(
            [i for i in self.i_ams if low is None or low <= i.iAmDeviceIdentifier[1] <= high]
        )
        return future

    async def read_property(self, address, objid, prop, array_index=None):
        self.calls.append(("rp", str(objid), str(prop), array_index))
        value = self.rp_values[(str(objid), str(prop), array_index)]
        if isinstance(value, BaseException):
            raise value
        return value

    async def read_property_multiple(self, address, parameters):
        count = sum(len(refs) for refs in parameters[1::2])
        self.calls.append(("rpm", count))
        reply = self.rpm_replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(parameters) if callable(reply) else reply

    async def write_property(self, address, objid, prop, value, array_index=None, priority=None):
        self.calls.append(("wp", str(objid), str(prop), value, array_index, priority))
        if self.write_error is not None:
            raise self.write_error

    async def request(self, apdu):
        self.calls.append(("request", apdu))
        if self.write_error is not None:
            raise self.write_error
        return SimpleAckPDU()


def _i_am(instance, address):
    return SimpleNamespace(
        iAmDeviceIdentifier=ObjectIdentifier(("device", instance)),
        pduSource=Address(address),
        vendorID=999,
        maxAPDULengthAccepted=1476,
        segmentationSupported=Segmentation("segmented-both"),
    )


def _echo_rpm(values):
    """An RPM reply built from the request: `values` maps (objid text,
    property) to a value; anything missing comes back as unknown-property."""

    def reply(parameters):
        rows = []
        for objid, refs in zip(parameters[0::2], parameters[1::2]):
            for ref in refs:
                key = (str(objid), str(ref.propertyIdentifier))
                value = values.get(key, ErrorType(errorClass="property", errorCode="unknownProperty"))
                rows.append((objid, ref.propertyIdentifier, ref.propertyArrayIndex, value))
        return rows

    return reply


@pytest.mark.asyncio
async def test_bind_by_who_is_caches_the_address():
    app = StubApp()
    app.i_ams = [_i_am(1234, "10.0.0.7")]
    client = BacnetClient(app)

    binding = await client.bind(1234, timeout=1)
    again = await client.bind(1234, timeout=1)

    assert str(binding.address) == "10.0.0.7"
    assert again is binding
    assert app.calls == [("who-is", 1234, 1234)]
    assert ("client-who-is", "device 1234") in app.traffic_log.entries


@pytest.mark.asyncio
async def test_bind_with_no_i_am_is_a_transport_error():
    client = BacnetClient(StubApp())
    with pytest.raises(ClientError, match="did not answer") as info:
        await client.bind(1234, timeout=1)
    assert info.value.transport


@pytest.mark.asyncio
async def test_bind_with_a_static_address_sends_nothing():
    app = StubApp()
    client = BacnetClient(app)

    binding = await client.bind(1234, "10.0.0.9:47809", timeout=1)

    assert str(binding.address) == "10.0.0.9:47809"
    assert app.calls == []


@pytest.mark.asyncio
async def test_bad_static_address_is_a_transport_error():
    with pytest.raises(ClientError) as info:
        await BacnetClient(StubApp()).bind(1234, "somehost", timeout=1)
    assert info.value.transport


@pytest.mark.asyncio
async def test_unbind_forces_a_new_who_is():
    app = StubApp()
    app.i_ams = [_i_am(1234, "10.0.0.7")]
    client = BacnetClient(app)
    await client.bind(1234, timeout=1)

    client.unbind(1234)
    await client.bind(1234, timeout=1)

    assert [c for c in app.calls if c[0] == "who-is"] == [("who-is", 1234, 1234)] * 2
    assert client.binding(1234) is not None


@pytest.mark.asyncio
async def test_read_many_uses_one_rpm_and_maps_values_and_point_errors():
    app = StubApp()
    app.rpm_replies = [_echo_rpm({("analog-input,1", "present-value"): Real(72.5)})]
    client = BacnetClient(app)
    good, missing = Point("analog-input", 1), Point("analog-input", 2)

    values = await client.read_many(Binding(Address("10.0.0.7")), [good, missing, good], timeout=1)

    assert values[good] == 72.5
    assert isinstance(values[missing], ClientError)
    assert str(values[missing]) == "analog-input:2 present-value: property: unknown-property"
    assert app.calls == [("rpm", 2)]  # the duplicate point is asked for once


def _device_reply(values, instances):
    """An RPM reply that names `instances` (a device's real instance) for every
    device object asked about, whatever instance the request carried."""

    def reply(parameters):
        rows = []
        for objid, refs in zip(parameters[0::2], parameters[1::2]):
            for ref in refs:
                key = (str(objid), str(ref.propertyIdentifier))
                value = values.get(key, ErrorType(errorClass="property", errorCode="unknownProperty"))
                real = instances.get(int(objid[1]), int(objid[1]))
                rows.append((ObjectIdentifier(("device", real)), ref.propertyIdentifier, ref.propertyArrayIndex, value))
        return rows

    return reply


WILDCARD = 4194303


@pytest.mark.asyncio
async def test_read_many_matches_a_wildcard_device_to_the_instance_that_replied():
    app = StubApp()
    app.rpm_replies = [
        _device_reply(
            {("device,4194303", "object-name"): CharacterString("Boiler"), ("device,4194303", "vendor-name"): CharacterString("Acme")},
            {WILDCARD: 77},
        )
    ]
    name, vendor = Point("device", WILDCARD, "object-name"), Point("device", WILDCARD, "vendor-name")

    values = await BacnetClient(app).read_many(Binding(Address("10.0.0.2")), [name, vendor], timeout=1)

    assert values == {name: "Boiler", vendor: "Acme"}


@pytest.mark.asyncio
async def test_read_many_an_error_row_for_a_wildcard_point_keeps_its_error():
    app = StubApp()
    app.rpm_replies = [
        _device_reply({("device,4194303", "object-name"): CharacterString("Boiler")}, {WILDCARD: 77})
    ]
    name, vendor = Point("device", WILDCARD, "object-name"), Point("device", WILDCARD, "vendor-name")

    values = await BacnetClient(app).read_many(Binding(Address("10.0.0.2")), [name, vendor], timeout=1)

    assert values[name] == "Boiler"
    assert isinstance(values[vendor], ClientError)
    assert (values[vendor].error_class, values[vendor].error_code) == ("property", "unknown-property")


@pytest.mark.asyncio
async def test_read_many_a_wildcard_takes_the_instance_no_other_point_names():
    app = StubApp()

    def reply(parameters):
        return [
            (ObjectIdentifier(("device", 5)), PropertyIdentifier("object-name"), None, CharacterString("Five")),
            (ObjectIdentifier(("device", 77)), PropertyIdentifier("object-name"), None, CharacterString("Wild")),
        ]

    app.rpm_replies = [reply]
    wild, five = Point("device", WILDCARD, "object-name"), Point("device", 5, "object-name")

    values = await BacnetClient(app).read_many(Binding(Address("10.0.0.2")), [wild, five], timeout=1)

    assert values == {wild: "Wild", five: "Five"}


@pytest.mark.asyncio
async def test_read_many_a_wildcard_with_no_unclaimed_instance_in_the_reply_is_not_guessed():
    app = StubApp()
    app.rpm_replies = [
        lambda parameters: [
            (ObjectIdentifier(("device", 5)), PropertyIdentifier("object-name"), None, CharacterString("Five"))
        ]
    ]
    wild, five = Point("device", WILDCARD, "object-name"), Point("device", 5, "object-name")

    values = await BacnetClient(app).read_many(Binding(Address("10.0.0.2")), [wild, five], timeout=1)

    assert values[five] == "Five"
    assert isinstance(values[wild], ClientError) and "no value in reply" in str(values[wild])


@pytest.mark.asyncio
async def test_read_many_leaves_a_non_wildcard_point_to_its_own_identifier():
    app = StubApp()
    app.rpm_replies = [_echo_rpm({("analog-input,1", "present-value"): Real(72.5)})]
    point = Point("analog-input", 1)

    values = await BacnetClient(app).read_many(Binding(Address("10.0.0.2")), [point], timeout=1)

    assert values == {point: 72.5}


@pytest.mark.asyncio
async def test_read_many_batches_by_rpm_batch_size():
    app = StubApp()
    app.rpm_replies = [_echo_rpm({})] * 3
    points = [Point("analog-value", i) for i in range(45)]

    await BacnetClient(app).read_many(Binding(Address("10.0.0.7")), points, timeout=1)

    assert app.calls == [("rpm", 20), ("rpm", 20), ("rpm", 5)]


@pytest.mark.asyncio
async def test_read_many_falls_back_to_read_property_when_rpm_is_rejected():
    app = StubApp()
    app.rpm_replies = [RejectPDU(reason="unrecognizedService")]
    app.rp_values = {
        ("analog-input,1", "present-value", None): Real(1.5),
        ("analog-input,2", "present-value", None): Error(
            errorClass="object", errorCode="unknownObject", service_choice=12
        ),
    }
    binding = Binding(Address("10.0.0.7"))
    one, two = Point("analog-input", 1), Point("analog-input", 2)

    values = await BacnetClient(app).read_many(binding, [one, two], timeout=1)

    assert binding.no_rpm is True
    assert values[one] == 1.5
    assert str(values[two]) == "object: unknown-object"


@pytest.mark.asyncio
async def test_read_many_falls_back_to_read_property_for_a_decode_failure():
    # bacpypes3's read_property_multiple raises a plain
    # AttributeError decoding a reply that names a proprietary object type
    # (object_class is None, so object_class.get_property_type(...) blows
    # up) -- that must not fail every point in the batch. Only this batch's
    # points are retried one at a time, and the binding stays on RPM (unlike
    # an "unsupported" service rejection, which turns RPM off for good).
    app = StubApp()
    app.rpm_replies = [AttributeError("'NoneType' object has no attribute 'get_property_type'")]
    app.rp_values = {
        ("analog-input,1", "present-value", None): Real(1.5),
        ("200,1", "present-value", None): "-no object class-",
    }
    binding = Binding(Address("10.0.0.7"))
    good, bad = Point("analog-input", 1), Point("200", 1)

    values = await BacnetClient(app).read_many(binding, [good, bad], timeout=1)

    assert values[good] == 1.5
    assert isinstance(values[bad], ClientError)
    assert binding.no_rpm is False
    assert app.calls == [
        ("rpm", 2),
        ("rp", "analog-input,1", "present-value", None),
        ("rp", "200,1", "present-value", None),
    ]


@pytest.mark.asyncio
async def test_read_many_halves_the_batch_when_the_reply_is_too_big():
    app = StubApp()
    app.rpm_replies = [AbortPDU(reason="segmentationNotSupported"), _echo_rpm({}), _echo_rpm({})]
    binding = Binding(Address("10.0.0.7"))

    await BacnetClient(app).read_many(binding, [Point("analog-value", i) for i in range(20)], timeout=1)

    assert binding.batch_size == 10
    assert app.calls == [("rpm", 20), ("rpm", 10), ("rpm", 10)]


@pytest.mark.asyncio
async def test_read_many_errors_only_the_point_too_big_to_send_alone():
    # At batch size 1 a too-big reply used to fail the whole read,
    # so one long property failed its connector on every poll.
    app = StubApp()
    big, small = Point("analog-value", 1, "description"), Point("analog-value", 2)
    app.rpm_replies = [
        AbortPDU(reason="segmentationNotSupported"),
        _echo_rpm({("analog-value,2", "present-value"): Real(3.0)}),
    ]
    binding = Binding(Address("10.0.0.7"))
    binding.batch_size = 1

    values = await BacnetClient(app).read_many(binding, [big, small], timeout=1)

    assert isinstance(values[big], ClientError)
    assert "too big" in str(values[big])
    assert values[small] == 3.0
    assert binding.batch_size == 1


@pytest.mark.asyncio
async def test_timeout_becomes_transport_error():
    # A no-response abort is raised by bacpypes3 as a
    # BaseException; it must come out as a ClientError.
    app = StubApp()
    app.rpm_replies = [AbortPDU(reason="noResponse")]

    with pytest.raises(ClientError) as info:
        await BacnetClient(app).read_many(Binding(Address("10.0.0.7")), [Point("analog-value", 1)], timeout=1)

    assert info.value.transport


@pytest.mark.asyncio
async def test_a_hung_request_times_out():
    class HangingApp(StubApp):
        async def read_property(self, *args, **kwargs):
            await asyncio.sleep(10)

    with pytest.raises(ClientError, match="no response") as info:
        await BacnetClient(HangingApp()).read(Binding(Address("10.0.0.7")), Point("analog-value", 1), timeout=0.05)
    assert info.value.transport


@pytest.mark.asyncio
async def test_error_reply_becomes_client_error():
    app = StubApp()
    app.write_error = Error(errorClass="property", errorCode="writeAccessDenied", service_choice=15)

    with pytest.raises(ClientError, match="write-access-denied") as info:
        await BacnetClient(app).write(
            Binding(Address("10.0.0.7")), Point("analog-value", 1), 5.0, priority=8, timeout=1
        )

    assert info.value.kind == "error"


@pytest.mark.asyncio
async def test_write_relinquish_to_a_proprietary_type_is_a_client_error():
    # write_property returns (doesn't raise) a
    # "-no object class-"/"-no property type-" sentinel when it can't look
    # up the type -- nothing was sent. A relinquish (value=None) skips
    # resolve_datatype entirely, so this reply is the only thing that would
    # otherwise catch it.
    class SentinelApp(StubApp):
        async def write_property(self, address, objid, prop, value, array_index=None, priority=None):
            self.calls.append(("wp", str(objid), str(prop), value, array_index, priority))
            return "-no object class-"

    with pytest.raises(ClientError, match="no object class") as info:
        await BacnetClient(SentinelApp()).write(
            Binding(Address("10.0.0.7")), Point("200", 1), None, priority=8, timeout=1
        )

    assert info.value.kind == "error"


@pytest.mark.asyncio
async def test_write_casts_the_value_and_sends_the_priority():
    app = StubApp()

    await BacnetClient(app).write(
        Binding(Address("10.0.0.7")), Point("binary-output", 3), "active", priority=9, timeout=1
    )

    assert app.calls == [("wp", "binary-output,3", "present-value", BinaryPV("active"), None, 9)]
    assert app.traffic_log.entries[-1][0] == "client-write-property"


@pytest.mark.asyncio
async def test_write_none_relinquishes_and_needs_a_priority():
    app = StubApp()
    client = BacnetClient(app)
    binding = Binding(Address("10.0.0.7"))

    await client.write(binding, Point("analog-value", 1), None, priority=8, timeout=1)
    with pytest.raises(ClientError, match="needs a priority"):
        await client.write(binding, Point("analog-value", 1), None, priority=None, timeout=1)

    assert isinstance(app.calls[0][3], Null)
    assert len(app.calls) == 1


@pytest.mark.asyncio
async def test_read_returns_the_value():
    app = StubApp()
    app.rp_values = {("device,5", "object-name", None): CharacterString("Remote")}

    value = await BacnetClient(app).read(Binding(Address("10.0.0.7")), Point("device", 5, "object-name"), timeout=1)

    assert value == "Remote"


@pytest.mark.asyncio
async def test_read_wraps_a_decode_exception_as_a_client_error():
    # bacpypes3's cast_out can raise a plain Exception
    # decoding a value (not just return the "-no ... -" sentinel strings) --
    # that must come out of read() as a ClientError too, the same way
    # _read_batch already guards against it.
    app = StubApp()
    app.rp_values = {
        ("analog-value,1", "present-value", None): AttributeError(
            "'NoneType' object has no attribute 'cast_out'"
        ),
    }

    with pytest.raises(ClientError, match="can't decode") as info:
        await BacnetClient(app).read(Binding(Address("10.0.0.7")), Point("analog-value", 1), timeout=1)

    assert info.value.kind == "error"


@pytest.mark.asyncio
async def test_who_is_returns_sorted_devices():
    app = StubApp()
    app.i_ams = [_i_am(20, "10.0.0.20"), _i_am(3, "10.0.0.3")]

    found = await BacnetClient(app).who_is(window=0.1)

    assert found == [
        {"instance": 3, "address": "10.0.0.3", "vendor_id": 999, "max_apdu": 1476, "segmentation": "segmented-both"},
        {"instance": 20, "address": "10.0.0.20", "vendor_id": 999, "max_apdu": 1476, "segmentation": "segmented-both"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["disable", "disableInitiation"])
async def test_dcc_suppresses_every_request(state):
    app = StubApp()
    app._dcc_state = state
    app.i_ams = [_i_am(5, "10.0.0.5")]
    client = BacnetClient(app)
    binding = Binding(Address("10.0.0.7"))

    for call in (
        client.bind(5, timeout=1),
        client.who_is(window=0.1),
        client.read(binding, Point("analog-value", 1), timeout=1),
        client.read_many(binding, [Point("analog-value", 1)], timeout=1),
        client.write(binding, Point("analog-value", 1), 1.0, priority=8, timeout=1),
    ):
        with pytest.raises(ClientError) as info:
            await call
        assert info.value.kind == "dcc"

    assert app.calls == []


def test_error_text_formats_an_execution_error():
    assert error_text(PropertyError("value-out-of-range")) == "property: value-out-of-range"


def test_error_text_keeps_other_exceptions_readable():
    assert error_text(ClientError("no response within 5 s", kind="transport")) == "no response within 5 s"
    assert error_text(RuntimeError()) == "RuntimeError"


def test_error_text_survives_a_str_that_raises():
    class Bad(Exception):
        def __str__(self):
            raise ValueError("boom")

    assert error_text(Bad()) == "Bad"


@pytest.mark.asyncio
async def test_write_sends_native_as_is_to_a_proprietary_point():
    app = StubApp()

    await BacnetClient(app).write(
        Binding(Address("10.0.0.7")), Point("200", 1), 72.0, priority=12, timeout=1, native=Real(72.0)
    )

    [(kind, apdu)] = app.calls
    assert kind == "request"
    assert (int(apdu.objectIdentifier[0]), apdu.objectIdentifier[1]) == (200, 1)
    assert apdu.propertyIdentifier == PropertyIdentifier("present-value")
    assert apdu.propertyValue.cast_out(Real) == 72.0
    assert apdu.priority == 12
    assert apdu.pduDestination == Address("10.0.0.7")
    assert app.traffic_log.entries[-1][0] == "client-write-property"


@pytest.mark.asyncio
async def test_write_native_relinquish_to_a_proprietary_point():
    app = StubApp()

    await BacnetClient(app).write(
        Binding(Address("10.0.0.7")), Point("200", 1), None, priority=12, timeout=1, native=Null(())
    )

    [(kind, apdu)] = app.calls
    assert kind == "request"
    assert apdu.propertyValue.cast_out(Null) == ()  # if cast_out(Null) returns Null(()), compare to that instead
    assert apdu.priority == 12


@pytest.mark.asyncio
async def test_write_ignores_native_for_a_standard_point():
    app = StubApp()

    await BacnetClient(app).write(
        Binding(Address("10.0.0.7")), Point("analog-value", 1), 5, priority=8, timeout=1, native=Unsigned(5)
    )

    assert app.calls == [("wp", "analog-value,1", "present-value", Real(5.0), None, 8)]


def test_has_standard_datatype():
    assert has_standard_datatype(Point("analog-value", 1)) is True
    assert has_standard_datatype(Point("200", 1)) is False
    assert has_standard_datatype(Point("analog-value", 1, "512")) is False


def test_error_from_reply_keeps_what_the_remote_said():
    error = error_from_reply(Error(errorClass="property", errorCode="valueOutOfRange", service_choice=15))
    assert (error.error_class, error.error_code, error.reply) == ("property", "value-out-of-range", "error")
    assert error_from_reply(RejectPDU(reason="invalidTag")).reply == "reject"
    assert error_from_reply(AbortPDU(reason="other")).reply == "abort"
    # No response is a timeout, not an answer from the remote.
    assert error_from_reply(AbortPDU(reason="noResponse")).reply is None


@pytest.mark.asyncio
async def test_read_many_point_error_keeps_its_class_and_code():
    app = StubApp()
    app.rpm_replies = [_echo_rpm({})]
    client = BacnetClient(app)
    point = Point("analog-value", 5, "priority-array")

    values = await client.read_many(Binding(Address("10.0.0.7")), [point], timeout=1)

    assert (values[point].error_class, values[point].error_code, values[point].reply) == (
        "property", "unknown-property", "error",
    )
