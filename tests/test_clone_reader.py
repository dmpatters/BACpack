"""clone_reader: what a clone reads from a controller, against a fake client.
The real wire is tests/test_clone_reader_network.py."""
from __future__ import annotations

import pytest
from bacpypes3.primitivedata import ObjectIdentifier

from bacpack import clone_reader
from bacpack.bacnet_client import Binding, ClientError
from bacpack.clone_reader import PROPERTIES_BY_TYPE, read_controller
from bacpypes3.pdu import Address


def _unknown(prop="x"):
    return ClientError(f"{prop}: property: unknown-property", error_class="property", error_code="unknown-property", reply="error")


def _oid(object_type, instance):
    return ObjectIdentifier((object_type, instance))


class FakeClone:
    """A controller as a dict of (object type, instance, property, array index) -> value or ClientError."""

    def __init__(self, values, *, object_list=None):
        self.values = dict(values)
        self.object_list = object_list
        self.many_calls = []
        self.single_calls = []
        self.fail_many_after = None

    def _lookup(self, point):
        return self.values.get(point.key, _unknown(str(point)))

    async def read(self, binding, point, *, timeout):
        self.single_calls.append(point)
        if point.prop == "object-list" and point.array_index is None and self.object_list == "too-big":
            raise ClientError("abort: segmentation-not-supported", kind="too-big")
        value = self._lookup(point)
        if isinstance(value, ClientError):
            raise value
        return value

    async def read_many(self, binding, points, *, timeout):
        self.many_calls.append(list(points))
        if self.fail_many_after is not None and len(self.many_calls) > self.fail_many_after:
            raise ClientError("no response within 1 s", kind="transport")
        return {point: self._lookup(point) for point in points}


def _controller(*objects, device_name="Boiler Room", extra=None):
    """objects: (type, instance, {property: value}) triples."""
    values = {
        ("device", 4001, "object-name", None): device_name,
        ("device", 4001, "description", None): "Plant controller",
        ("device", 4001, "location", None): "Basement",
        ("device", 4001, "vendor-name", None): "Acme Controls",
        ("device", 4001, "vendor-identifier", None): 42,
        ("device", 4001, "object-list", None): [_oid("device", 4001), *[_oid(t, i) for t, i, _ in objects]],
    }
    for object_type, instance, props in objects:
        for prop, value in props.items():
            values[(object_type, instance, prop, None)] = value
    values.update(extra or {})
    return FakeClone(values)


async def _read(client, report=None, cancelled=None, instance=4001):
    reports = []
    result = await read_controller(
        client, Binding(Address("10.0.0.5")), instance, 5.0,
        report or (lambda done, total, phase: reports.append((done, total))), cancelled or (lambda: False),
    )
    return result, reports


@pytest.mark.asyncio
async def test_reads_device_and_every_object():
    client = _controller(
        ("analog-input", 1, {"object-name": "Zone Temp", "description": "North zone", "units": "degrees-fahrenheit", "present-value": 71.5}),
        ("binary-value", 2, {"object-name": "Fan", "active-text": "Running", "inactive-text": "Stopped"}),
    )
    result, _ = await _read(client)

    assert result["device"] == {
        "instance": 4001, "name": "Boiler Room", "description": "Plant controller", "location": "Basement",
        "vendor_name": "Acme Controls", "vendor_identifier": 42, "object_list_count": 3,
    }
    ai, bv = result["objects"]
    assert (ai["object_type"], ai["instance"], ai["error"]) == ("analog-input", 1, None)
    assert ai["props"] == {
        "object-name": "Zone Temp", "description": "North zone", "units": "degrees-fahrenheit", "present-value": 71.5,
    }
    assert bv["props"]["active-text"] == "Running" and bv["props"]["inactive-text"] == "Stopped"
    assert result["read_at"].endswith("Z") or "+" in result["read_at"] or "T" in result["read_at"]


def _as_wildcard(client):
    """The same controller addressed by the wildcard instance: it answers
    its device properties under whatever instance was asked."""
    client.values = {
        (("device", 4194303, *key[2:]) if key[0] == "device" else key): value for key, value in client.values.items()
    }
    return client


@pytest.mark.asyncio
async def test_a_wildcard_read_records_the_controllers_real_instance():
    client = _as_wildcard(_controller(("analog-input", 1, {"object-name": "Zone Temp"})))

    result, _ = await _read(client, instance=4194303)

    assert result["device"]["instance"] == 4001
    assert result["device"]["name"] == "Boiler Room"
    assert [o["instance"] for o in result["objects"]] == [1]


@pytest.mark.asyncio
async def test_a_wildcard_read_with_no_single_device_in_the_list_keeps_the_wildcard():
    client = _as_wildcard(_controller(("analog-input", 1, {"object-name": "Zone Temp"})))
    client.values[("device", 4194303, "object-list", None)] = [_oid("analog-input", 1)]

    result, _ = await _read(client, instance=4194303)

    assert result["device"]["instance"] == 4194303


@pytest.mark.asyncio
async def test_the_device_object_is_not_an_object_entry():
    result, _ = await _read(_controller(("analog-input", 1, {"object-name": "AI"})))
    assert [(o["object_type"], o["instance"]) for o in result["objects"]] == [("analog-input", 1)]


@pytest.mark.asyncio
async def test_unknown_property_is_missing_not_an_error():
    client = _controller(("analog-input", 1, {"object-name": "AI-1", "present-value": 3.0}))
    [obj], _ = (await _read(client))[0]["objects"], None

    assert obj["error"] is None
    assert "description" not in obj["props"] and "units" not in obj["props"]
    assert {"description", "units"} <= set(obj["missing"])
    assert "object-name" not in obj["missing"]


@pytest.mark.asyncio
async def test_object_without_a_name_is_an_error_entry():
    client = _controller(
        ("analog-input", 1, {"object-name": _unknown("name"), "units": "percent"}),
        ("analog-input", 2, {"object-name": "AI-2"}),
    )
    result, _ = await _read(client)
    broken, fine = result["objects"]
    assert broken["error"] is not None and "unknown-property" in broken["error"]
    assert fine["error"] is None


@pytest.mark.asyncio
async def test_unsupported_type_reads_only_its_name():
    client = _controller(("accumulator", 3, {"object-name": "kWh", "present-value": 5}), ("loop", 1, {"object-name": "PID"}))
    result, _ = await _read(client)

    assert [(o["object_type"], o["props"]) for o in result["objects"]] == [
        ("accumulator", {"object-name": "kWh"}), ("loop", {"object-name": "PID"}),
    ]
    asked = {(p.object_type, p.prop) for call in client.many_calls for p in call if p.object_type in ("accumulator", "loop")}
    assert asked == {("accumulator", "object-name"), ("loop", "object-name")}


@pytest.mark.asyncio
async def test_priority_array_answer_marks_commandable():
    client = _controller(
        ("analog-value", 1, {"object-name": "Setpoint", "relinquish-default": 70.0}),
        ("analog-value", 2, {"object-name": "Readout"}),
        extra={("analog-value", 1, "priority-array", 0): 16, ("analog-value", 2, "priority-array", 0): ClientError(
            "property: property-is-not-an-array", error_class="property", error_code="property-is-not-an-array", reply="error")},
    )
    result, _ = await _read(client)
    commandable, readonly = result["objects"]
    assert commandable["props"]["commandable"] is True and readonly["props"]["commandable"] is False
    assert "priority-array" not in commandable["props"] and "priority-array" not in commandable["missing"]
    assert commandable["props"]["relinquish-default"] == 70.0


@pytest.mark.asyncio
async def test_inputs_are_not_asked_for_a_priority_array_or_outputs_for_a_present_value():
    client = _controller(("analog-input", 1, {"object-name": "AI"}), ("analog-output", 1, {"object-name": "AO"}))
    await _read(client)
    asked = {(p.object_type, p.prop) for call in client.many_calls for p in call}
    assert ("analog-input", "priority-array") not in asked and ("analog-input", "present-value") in asked
    assert ("analog-output", "present-value") not in asked and ("analog-output", "priority-array") in asked


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type", sorted(PROPERTIES_BY_TYPE))
async def test_every_listed_property_is_a_valid_bacnet_property(object_type):
    from bacpack.bacnet_client import normalize_object_type, normalize_property

    assert normalize_object_type(object_type) == object_type
    for prop in PROPERTIES_BY_TYPE[object_type]:
        assert normalize_property(prop) == prop
    assert {"object-name", "description"} <= set(PROPERTIES_BY_TYPE[object_type])


@pytest.mark.asyncio
async def test_progress_reaches_the_total():
    client = _controller(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 251)])
    result, reports = await _read(client)

    assert len(result["objects"]) == 250
    assert reports[0] == (0, 250) and reports[-1] == (250, 250)
    assert [done for done, _ in reports] == sorted(done for done, _ in reports)
    assert len(reports) > 2


@pytest.mark.asyncio
async def test_reads_go_out_in_chunks_of_about_a_hundred_points():
    client = _controller(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 301)])
    await _read(client)
    sizes = [len(call) for call in client.many_calls]
    assert max(sizes) <= 100 + len(PROPERTIES_BY_TYPE["analog-input"])
    assert len(client.many_calls) >= 3


@pytest.mark.asyncio
async def test_a_failed_object_does_not_end_a_big_read():
    # One chunk where every property of one object errors, as when a remote
    # falls back to reading one at a time.
    objects = [("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 6)]
    objects[2] = ("analog-input", 3, {"object-name": ClientError("analog-input:3 object-name: can't decode")})
    result, reports = await _read(_controller(*objects))
    assert [o["error"] is not None for o in result["objects"]] == [False, False, True, False, False]
    assert reports[-1] == (5, 5)


@pytest.mark.asyncio
async def test_cancel_stops_between_batches():
    client = _controller(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 301)])
    state = {"cancel": False}

    def report(done, total, phase):
        if done >= 100:
            state["cancel"] = True

    with pytest.raises(ClientError, match="cancelled"):
        await _read(client, report=report, cancelled=lambda: state["cancel"])
    # 300 objects of 14 points each take about 42 chunks; the read stopped well short of that.
    assert len(client.many_calls) < 20


@pytest.mark.asyncio
async def test_a_transport_error_fails_the_whole_read():
    client = _controller(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 301)])
    client.fail_many_after = 2
    with pytest.raises(ClientError) as excinfo:
        await _read(client)
    assert excinfo.value.transport


@pytest.mark.asyncio
async def test_an_object_list_too_big_for_one_reply_is_read_by_index():
    client = _controller(("analog-input", 1, {"object-name": "AI-1"}), ("analog-input", 2, {"object-name": "AI-2"}))
    ids = client.values[("device", 4001, "object-list", None)]
    client.object_list = "too-big"
    client.values[("device", 4001, "object-list", 0)] = len(ids)
    for index, oid in enumerate(ids, start=1):
        client.values[("device", 4001, "object-list", index)] = oid

    result, _ = await _read(client)

    assert [o["instance"] for o in result["objects"]] == [1, 2]
    assert result["device"]["object_list_count"] == 3


@pytest.mark.asyncio
async def test_a_device_without_a_description_still_reads():
    client = _controller(("analog-input", 1, {"object-name": "AI"}))
    del client.values[("device", 4001, "description", None)]
    del client.values[("device", 4001, "location", None)]
    result, _ = await _read(client)
    assert result["device"]["description"] is None and result["device"]["location"] is None


def _too_big_list(*objects):
    client = _controller(*objects)
    ids = client.values[("device", 4001, "object-list", None)]
    client.object_list = "too-big"
    client.values[("device", 4001, "object-list", 0)] = len(ids)
    for index, oid in enumerate(ids, start=1):
        client.values[("device", 4001, "object-list", index)] = oid
    return client


@pytest.mark.asyncio
async def test_reading_a_long_object_list_by_index_reports_progress():
    client = _too_big_list(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 6)])
    _, reports = await _read(client)
    assert reports[:6] == [(1, 6), (2, 6), (3, 6), (4, 6), (5, 6), (6, 6)]


@pytest.mark.asyncio
async def test_cancel_stops_the_object_list_read_between_entries():
    client = _too_big_list(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 30)])
    state = {"cancel": False}

    def report(done, total, phase):
        state["cancel"] = done >= 3

    with pytest.raises(ClientError, match="cancelled"):
        await _read(client, report=report, cancelled=lambda: state["cancel"])
    entry_reads = [p for p in client.single_calls if p.prop == "object-list" and p.array_index not in (None, 0)]
    assert len(entry_reads) == 3 and len(client.many_calls) == 1  # only the device's own read


def _refused(code, cls="device"):
    return ClientError(f"x: {cls}: {code}", error_class=cls, error_code=code, reply="error")


@pytest.mark.asyncio
async def test_a_property_refused_for_another_reason_is_failed_not_missing():
    client = _controller(
        ("analog-input", 1, {"object-name": "AI", "units": _refused("operational-problem")}),
    )
    [obj], _ = (await _read(client))[0]["objects"], None
    assert obj["failed"] == {"units": "operational-problem"}
    assert "units" not in obj["missing"] and "description" in obj["missing"]


@pytest.mark.asyncio
async def test_a_priority_array_that_fails_for_another_reason_is_not_called_uncommandable():
    client = _controller(
        ("analog-value", 1, {"object-name": "AV"}),
        extra={("analog-value", 1, "priority-array", 0): _refused("busy")},
    )
    [obj], _ = (await _read(client))[0]["objects"], None
    assert "commandable" not in obj["props"]
    assert obj["failed"] == {"priority-array": "busy"}


@pytest.mark.asyncio
async def test_a_too_big_array_is_read_one_index_at_a_time():
    too_big = ClientError("abort: segmentation-not-supported", kind="too-big")
    client = _controller(
        ("multi-state-value", 1, {"object-name": "MSV", "state-text": too_big, "number-of-states": 2}),
        extra={("multi-state-value", 1, "state-text", 0): 2, ("multi-state-value", 1, "state-text", 1): "Off",
               ("multi-state-value", 1, "state-text", 2): "On"},
    )
    [obj], _ = (await _read(client))[0]["objects"], None
    assert obj["props"]["state-text"] == ["Off", "On"]
    assert "state-text" not in obj["missing"] and "state-text" not in obj.get("failed", {})


@pytest.mark.asyncio
async def test_a_too_big_property_that_cannot_be_read_by_index_is_failed():
    too_big = ClientError("abort: segmentation-not-supported", kind="too-big")
    client = _controller(("calendar", 1, {"object-name": "Cal", "date-list": too_big}))
    [obj], _ = (await _read(client))[0]["objects"], None
    assert "date-list" not in obj["props"] and obj["failed"] == {"date-list": "too-big"}


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["many", None, [1, 2]])
async def test_a_by_index_count_that_is_not_a_number_fails_the_property_not_the_job(count):
    too_big = ClientError("abort: segmentation-not-supported", kind="too-big")
    client = _controller(
        ("multi-state-value", 1, {"object-name": "MSV", "state-text": too_big}),
        extra={("multi-state-value", 1, "state-text", 0): count},
    )
    [obj] = (await _read(client))[0]["objects"]
    assert "state-text" not in obj["props"] and obj["failed"] == {"state-text": "bad-array-length"}


@pytest.mark.asyncio
async def test_a_dcc_during_a_by_index_read_ends_the_read():
    too_big = ClientError("abort: segmentation-not-supported", kind="too-big")
    client = _controller(
        ("multi-state-value", 1, {"object-name": "MSV", "state-text": too_big}),
        extra={
            ("multi-state-value", 1, "state-text", 0): 2,
            ("multi-state-value", 1, "state-text", 1): ClientError("under DCC", kind="dcc"),
        },
    )
    with pytest.raises(ClientError) as excinfo:
        await _read(client)
    assert excinfo.value.kind == "dcc"


@pytest.mark.asyncio
async def test_cancel_stops_a_by_index_array_read():
    too_big = ClientError("abort: segmentation-not-supported", kind="too-big")
    extra = {("multi-state-value", 1, "state-text", 0): 50}
    extra.update({("multi-state-value", 1, "state-text", n): f"S{n}" for n in range(1, 51)})
    client = _controller(("multi-state-value", 1, {"object-name": "MSV", "state-text": too_big}), extra=extra)
    seen = []

    def cancelled():
        seen.append(1)
        return len(client.single_calls) >= 5

    with pytest.raises(ClientError, match="cancelled"):
        await _read(client, cancelled=cancelled)
    assert len(client.single_calls) < 10


@pytest.mark.asyncio
async def test_a_recipient_address_is_read_as_hex_text():
    from bacpack.objects.notification_class import build_recipient_list

    recipients = build_recipient_list([
        {"recipient": {"kind": "device", "instance_number": 99}, "valid_days": [True] * 7, "from_time": "00:00:00.00",
         "to_time": "23:59:59.99", "transitions": [True] * 3, "confirmed": False, "process_identifier": 1},
        {"recipient": {"kind": "address", "address": "192.168.1.77:47808"}, "valid_days": [True] * 7,
         "from_time": "00:00:00.00", "to_time": "23:59:59.99", "transitions": [True] * 3, "confirmed": True,
         "process_identifier": 2},
    ])
    client = _controller(("notification-class", 5, {"object-name": "NC", "recipient-list": recipients}))
    [obj], _ = (await _read(client))[0]["objects"], None
    first, second = obj["props"]["recipient-list"]
    assert first["recipient"] == "device:99"
    assert second["recipient"]["macAddress"] == "c0a8014dbac0"


@pytest.mark.asyncio
async def test_progress_names_the_phase_object_list_then_objects():
    client = _too_big_list(*[("analog-input", i, {"object-name": f"AI-{i}"}) for i in range(1, 4)])
    seen = []
    await _read(client, report=lambda done, total, phase: seen.append((phase, done, total)))
    assert seen[:4] == [("object-list", 1, 4), ("object-list", 2, 4), ("object-list", 3, 4), ("object-list", 4, 4)]
    assert seen[4:] == [("objects", 0, 3), ("objects", 3, 3)]
