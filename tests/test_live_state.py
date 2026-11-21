"""live_state.read_live_state: a device's own objects as the live-state API
reports them. Against a real in-process Application, no socket."""
from __future__ import annotations

import json

import pytest
from bacpypes3.primitivedata import ObjectIdentifier

from bacpack.bacnet import write_manual_value
from bacpack.live_state import read_live_state
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application


def _setup(session_factory, *objects):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        rows = [BacnetObject(device=device, **fields) for fields in objects]
        session.add_all(rows)
        session.commit()
        ids = [row.id for row in rows]
        device = session.get(Device, 1)
        app = build_application(device, list(device.objects))
    return app, ids


@pytest.mark.asyncio
async def test_a_commanded_analog_value(session_factory):
    app, (av,) = _setup(
        session_factory,
        dict(object_type="analog-value", instance_number=1, name="AV-1", relinquish_default="70"),
    )
    await write_manual_value(app.get_object_id(ObjectIdentifier(("analog-value", 1))), 72.5)

    result = await read_live_state(session_factory, app, 1)

    assert result["read_at"].endswith("+00:00")
    (entry,) = result["objects"]
    assert entry["object_id"] == av
    assert entry["object_type"] == "analog-value"
    assert entry["instance"] == 1
    assert entry["present_value"] == 72.5
    assert len(entry["priority_array"]) == 16
    assert entry["priority_array"][7] == 72.5
    assert [v for i, v in enumerate(entry["priority_array"]) if i != 7] == [None] * 15
    assert entry["controlling_priority"] == 8
    assert entry["relinquish_default"] == 70.0
    assert entry["status_flags"] == []
    assert entry["event_state"] == "normal"
    assert entry["out_of_service"] is False


@pytest.mark.asyncio
async def test_an_uncommanded_binary_value_runs_on_relinquish_default(session_factory):
    app, _ = _setup(session_factory, dict(object_type="binary-value", instance_number=2, name="BV-2"))

    (entry,) = (await read_live_state(session_factory, app, 1))["objects"]

    assert entry["present_value"] == "inactive"
    assert entry["priority_array"] == [None] * 16
    assert entry["controlling_priority"] is None


@pytest.mark.asyncio
async def test_out_of_service_shows_in_the_status_flags(session_factory):
    app, _ = _setup(
        session_factory,
        dict(object_type="analog-value", instance_number=3, name="AV-3", out_of_service_default=True),
    )

    (entry,) = (await read_live_state(session_factory, app, 1))["objects"]

    assert entry["out_of_service"] is True
    assert entry["status_flags"] == ["out-of-service"]


@pytest.mark.asyncio
async def test_properties_an_object_type_lacks_are_null(session_factory):
    app, _ = _setup(session_factory, dict(object_type="notification-class", instance_number=4, name="NC-4"))

    (entry,) = (await read_live_state(session_factory, app, 1))["objects"]

    for key in ("present_value", "priority_array", "controlling_priority", "relinquish_default", "out_of_service"):
        assert entry[key] is None, key


@pytest.mark.asyncio
async def test_object_ids_pick_a_subset_and_unknown_ids_are_left_out(session_factory):
    app, (first, second) = _setup(
        session_factory,
        dict(object_type="analog-value", instance_number=1, name="AV-1"),
        dict(object_type="analog-value", instance_number=2, name="AV-2"),
    )

    result = await read_live_state(session_factory, app, 1, [second, 9999])

    assert [e["object_id"] for e in result["objects"]] == [second]


@pytest.mark.asyncio
async def test_a_row_saved_after_the_build_is_not_live_yet(session_factory):
    app, _ = _setup(session_factory, dict(object_type="analog-value", instance_number=1, name="AV-1"))
    with session_factory() as session:
        late = BacnetObject(device_id=1, object_type="analog-value", instance_number=2, name="AV-2")
        session.add(late)
        session.commit()
        late_id = late.id

    result = await read_live_state(session_factory, app, 1, [late_id])

    assert result["objects"] == []


@pytest.mark.asyncio
async def test_a_binary_schedule_reads_active_or_inactive(session_factory):
    # AnyAtomic.get_value() demotes a schedule's BinaryPV to an Enumerated
    # whose text is "0"/"1".
    app, _ = _setup(
        session_factory,
        dict(object_type="schedule", instance_number=5, name="SCH-5", schedule_value_type="binary", schedule_default="active"),
    )

    (entry,) = (await read_live_state(session_factory, app, 1))["objects"]

    assert entry["present_value"] == "active"


@pytest.mark.asyncio
async def test_a_non_finite_present_value_reads_as_null(session_factory):
    # A client can write NaN or Inf over the wire; JSON has no such number.
    app, _ = _setup(
        session_factory,
        dict(object_type="analog-value", instance_number=1, name="AV-1", relinquish_default="70"),
    )
    await write_manual_value(app.get_object_id(ObjectIdentifier(("analog-value", 1))), float("nan"))

    (entry,) = (await read_live_state(session_factory, app, 1))["objects"]

    assert entry["present_value"] is None
    assert entry["priority_array"][7] is None
    json.dumps(entry, allow_nan=False)
