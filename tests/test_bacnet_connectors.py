"""BacnetConnectorRunner -- read and write mappings to one remote
device, run inside the owning device's process. The BACnet client is faked
(FakeClient); the local objects are real, from build_application."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import BinaryPV, PriorityValue, Reliability
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.errors import ExecutionError, PropertyError
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Boolean, ObjectIdentifier, Real, Unsigned

from bacpack.bacnet import write_manual_value
from bacpack.bacnet_client import Binding, ClientError, Point, cast_for_write, to_jsonable
from bacpack.bacnet_connector_types import NOT_LIVE, ReadMapping, read_value_for_feed
from bacpack.bacnet_connectors import BacnetConnectorRunner
from bacpack.models import BacnetObject, Connector, ConnectorMapping, ConnectorWriteMapping, Device
from bacpack.object_builder import build_application

REMOTE_AI = Point("analog-input", 1)
REMOTE_BI = Point("binary-input", 2)
REMOTE_AV = Point("analog-value", 5)


class FakeClient:
    def __init__(self):
        self.values = {}
        self.fail = None
        self.write_fail = None
        self.reads = 0
        self.writes = []
        self.write_attempts = 0
        self.unbound = []
        self.write_delay = 0.0
        self.read_gate = None  # an asyncio.Event read_many waits on, when set
        self.bound = {}

    async def bind(self, instance, static_address=None, *, timeout):
        if self.fail is not None:
            raise self.fail
        binding = Binding(Address(static_address or "10.0.0.2"))
        self.bound[instance] = binding
        return binding

    def binding(self, instance, static_address=None):
        return self.bound.get(instance)

    def unbind(self, instance, static_address=None):
        self.unbound.append(instance)
        self.bound.pop(instance, None)

    async def read_many(self, binding, points, *, timeout):
        self.reads += 1
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.fail is not None:
            raise self.fail
        return {p: self.values.get(p, ClientError(f"{p}: object: unknown-object")) for p in points}

    async def write(self, binding, point, value, *, priority, timeout):
        self.write_attempts += 1
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        if self.fail is not None:
            raise self.fail
        if self.write_fail is not None:
            raise self.write_fail
        cast_for_write(point, value)  # the ClientError the real write raises for an unconvertible value
        self.writes.append((point, to_jsonable(value), priority))


def _setup(session_factory, *, reads=(REMOTE_AI,), writes=(), **connector_kwargs):
    """Device 1 with AV-1 and BV-1, and a bacnet connector to device 2.
    `reads` feed AV-1 (then BV-1); `writes` send AV-1. Returns ids."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        av = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        bv = BacnetObject(device=device, object_type="binary-value", instance_number=1, name="BV-1")
        settings = dict(poll_interval_seconds=60, timeout_seconds=1, remote_address="10.0.0.2")
        settings.update(connector_kwargs)
        connector = Connector(
            name="Remote", type="bacnet", url="", device=device, remote_device_instance=2, **settings
        )
        session.add_all([device, av, bv, connector])
        read_rows = [
            ConnectorMapping(
                connector=connector,
                object=target,
                remote_object_type=point.object_type,
                remote_object_instance=point.instance,
                remote_property=point.prop,
            )
            for point, target in zip(reads, (av, bv))
        ]
        write_rows = [
            ConnectorWriteMapping(
                connector=connector,
                object=av,
                remote_object_type=point.object_type,
                remote_object_instance=point.instance,
                remote_property=point.prop,
                priority=9,
            )
            for point in writes
        ]
        session.add_all(read_rows + write_rows)
        session.commit()
        return {
            "connector": connector.id,
            "reads": [m.id for m in read_rows],
            "writes": [m.id for m in write_rows],
        }


def _app(session_factory):
    with session_factory() as session:
        device = session.get(Device, 1)
        return build_application(device, list(device.objects))


def _live(app, object_type="analog-value"):
    return app.get_object_id(ObjectIdentifier((object_type, 1)))


async def _run(runner):
    await runner.tick()
    await runner.idle()


def _status(session_factory, connector_id):
    with session_factory() as session:
        return session.get(Connector, connector_id).runtime.client_status


REMOTE_AV_ARRAY = Point("analog-value", 5, "priority-array")
NO_ARRAY = ClientError(
    "analog-value,5 priority-array: property: unknown-property",
    error_class="property", error_code="unknown-property", reply="error",
)


def _setup_proxy(session_factory, *, direction="proxy"):
    """Device 1 with SP (analog-value 1), proxying REMOTE_AV on device 2."""
    with session_factory() as session:
        device = Device(id=1, name="D")
        local = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="SP")
        connector = Connector(
            name="Remote", type="bacnet", url="", device=device, remote_device_instance=2,
            poll_interval_seconds=60, timeout_seconds=1, remote_address="10.0.0.2",
        )
        mapping = ConnectorMapping(
            connector=connector, object=local, remote_object_type="analog-value",
            remote_object_instance=5, remote_property="present-value", direction=direction,
        )
        session.add_all([device, local, connector, mapping])
        session.commit()
        return {"connector": connector.id, "mapping": mapping.id}


def _remote_array(**slots):
    """16 remote slots; p8=72.0 puts Real(72.0) at priority 8."""
    return [
        PriorityValue(Real(slots[f"p{i}"])) if f"p{i}" in slots else PriorityValue(null=())
        for i in range(1, 17)
    ]


def _slot(obj, priority):
    value = obj.priorityArray[priority - 1]
    return None if value._choice in (None, "null") else getattr(value, value._choice)


def _set_direction(session_factory, mapping_id, direction):
    with session_factory() as session:
        session.get(ConnectorMapping, mapping_id).direction = direction
        session.commit()


# --- read_value_for_feed -----------------------------------------------------


def _mapping(**overrides):
    fields = dict(id=1, object_type="analog-value", instance=1, point=REMOTE_AI, scale=1.0, offset=0.0, value_map=None)
    fields.update(overrides)
    return ReadMapping(**fields)


def test_read_value_for_feed_scales_and_offsets():
    assert read_value_for_feed(Real(10.0), _mapping(scale=2.0, offset=1.0)) == 21.0


def test_enumerated_read_needs_no_value_map():
    assert read_value_for_feed(BinaryPV("active"), _mapping()) == 1.0


def test_value_map_looks_up_by_name():
    assert read_value_for_feed(BinaryPV("active"), _mapping(value_map={"active": 5})) == 5.0


def test_value_map_looks_up_a_boolean_as_true_false():
    # str(Boolean(True)) is "1" -- a "true" key never matched.
    assert read_value_for_feed(Boolean(True), _mapping(value_map={"true": 5, "false": 2})) == 5.0
    assert read_value_for_feed(Boolean(False), _mapping(value_map={"true": 5, "false": 2})) == 2.0


# --- reads -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_feeds_the_remote_value_and_saves_status(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)

    assert _live(app).presentValue == 72.5
    status = _status(session_factory, ids["connector"])
    assert status["last_success"] is True
    assert status["consecutive_failures"] == 0
    assert status["bound_address"] == "10.0.0.2"
    assert status["read_errors"] == {str(ids["reads"][0]): None}
    assert status["last_poll_at"] is not None


@pytest.mark.asyncio
async def test_poll_waits_for_its_interval(session_factory):
    _setup(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(1.0)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)

    await _run(runner)
    await _run(runner)

    assert client.reads == 1


@pytest.mark.asyncio
async def test_binary_remote_feeds_a_binary_local_object(session_factory):
    _setup(session_factory, reads=(REMOTE_AI, REMOTE_BI))
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AI: Real(1.0), REMOTE_BI: BinaryPV("active")}

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert _live(app, "binary-value").presentValue == BinaryPV("active")


@pytest.mark.asyncio
async def test_transport_failure_relinquishes_unbinds_and_backs_off(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    default = _live(app).relinquishDefault

    client.fail = ClientError("no response within 1 s", kind="transport")
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _live(app).presentValue == default
    assert client.unbound == [2]
    status = _status(session_factory, ids["connector"])
    assert status["last_success"] is False
    assert status["consecutive_failures"] == 1
    assert status["last_error"] == "no response within 1 s"
    # Backed off: 60 s interval doubled.
    assert runner._due[ids["connector"]] >= asyncio.get_running_loop().time() + 110


@pytest.mark.asyncio
async def test_transport_failure_on_read_clears_the_bound_address(session_factory):
    # Found in a live check: the connector page kept saying "bound
    # to <old address>" next to a "did not answer Who-Is" failure.
    ids = _setup(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)
    assert _status(session_factory, ids["connector"])["bound_address"] == "10.0.0.2"

    client.fail = ClientError("no response within 1 s", kind="transport")
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _status(session_factory, ids["connector"])["bound_address"] is None


@pytest.mark.asyncio
async def test_editing_the_remote_address_clears_the_backoff(session_factory):
    # A fixed connector used to wait out up to 5 minutes of backoff.
    ids = _setup(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(1.0)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    client.fail = ClientError("no response within 1 s", kind="transport")
    await _run(runner)
    assert runner._due[ids["connector"]] > asyncio.get_running_loop().time() + 100

    client.fail = None
    with session_factory() as session:
        session.get(Connector, ids["connector"]).remote_address = "10.0.0.3"
        session.commit()
    runner.mark_changed()
    reads = client.reads
    await _run(runner)

    assert client.reads == reads + 1
    assert client.unbound == [2, 2]  # the failure, then the edit
    status = _status(session_factory, ids["connector"])
    assert (status["last_success"], status["consecutive_failures"]) == (True, 0)


@pytest.mark.asyncio
async def test_a_run_in_flight_across_an_edit_sets_no_backoff(session_factory):
    # A run still using the old settings when the
    # edit lands mustn't put back the backoff the edit cleared.
    ids = _setup(session_factory)
    release = asyncio.Event()

    class SlowClient(FakeClient):
        async def bind(self, instance, static_address=None, *, timeout):
            await release.wait()
            raise ClientError("no response within 1 s", kind="transport")

    client = SlowClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await runner.tick()  # the run starts and waits in bind
    with session_factory() as session:
        session.get(Connector, ids["connector"]).remote_address = "10.0.0.3"
        session.commit()
    runner.mark_changed()
    await runner.tick()  # notes the edit; the run is still in flight
    release.set()
    await runner.idle()

    assert ids["connector"] not in runner._due
    assert runner._status[ids["connector"]]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_a_mapping_edit_keeps_the_backoff(session_factory):
    ids = _setup(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    client.fail = ClientError("no response within 1 s", kind="transport")
    await _run(runner)
    due = runner._due[ids["connector"]]

    with session_factory() as session:
        session.get(ConnectorMapping, ids["reads"][0]).scale = 2.0
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert runner._due[ids["connector"]] == due


@pytest.mark.asyncio
async def test_point_error_relinquishes_only_that_mapping(session_factory):
    ids = _setup(session_factory, reads=(REMOTE_AI, REMOTE_BI))
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AI: Real(72.5), REMOTE_BI: BinaryPV("active")}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    del client.values[REMOTE_BI]
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _live(app).presentValue == 72.5
    assert _live(app, "binary-value").presentValue == _live(app, "binary-value").relinquishDefault
    status = _status(session_factory, ids["connector"])
    assert status["last_success"] is True
    assert status["read_errors"][str(ids["reads"][1])] == "binary-input:2 present-value: object: unknown-object"
    assert client.unbound == []
    # Normal interval, not backoff.
    assert runner._due[ids["connector"]] < asyncio.get_running_loop().time() + 61


@pytest.mark.asyncio
async def test_value_that_wont_coerce_is_a_point_error(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = "warm"

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert str(ids["reads"][0]) in _status(session_factory, ids["connector"])["read_errors"]
    assert _status(session_factory, ids["connector"])["read_errors"][str(ids["reads"][0])] is not None


@pytest.mark.asyncio
async def test_disabling_the_connector_relinquishes_its_read_mappings(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = False
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert _live(app).presentValue == _live(app).relinquishDefault
    assert client.reads == 1


@pytest.mark.asyncio
async def test_deleting_a_read_mapping_relinquishes_its_feed(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.delete(session.get(ConnectorMapping, ids["reads"][0]))
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert _live(app).presentValue == _live(app).relinquishDefault


@pytest.mark.asyncio
async def test_dcc_suppression_is_not_a_failure(session_factory):
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    client.fail = ClientError("suppressed by DCC (disableInitiation)", kind="dcc")
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _live(app).presentValue == 72.5  # not relinquished
    status = _status(session_factory, ids["connector"])
    assert status["suppressed_by_dcc"] is True
    assert status["consecutive_failures"] == 0
    assert client.unbound == []

    client.fail = None
    await _run(runner)
    assert _status(session_factory, ids["connector"])["suppressed_by_dcc"] is False


@pytest.mark.asyncio
async def test_only_this_devices_enabled_bacnet_connectors_run(session_factory):
    _setup(session_factory)
    with session_factory() as session:
        other = Device(id=3, name="Other")
        session.add_all(
            [
                other,
                Connector(name="Theirs", type="bacnet", url="", device=other, remote_device_instance=2),
                Connector(name="Http", url="http://example.invalid"),
            ]
        )
        session.commit()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=FakeClient())

    assert [c.remote_instance for c in runner._load()] == [2]
    assert len(runner._load()) == 1


@pytest.mark.asyncio
async def test_runner_survives_an_unexpected_exception(session_factory, monkeypatch):
    # Nothing escaping one tick may end the loop.
    import bacpack.bacnet_connectors as module

    monkeypatch.setattr(module, "RUNNER_TICK_SECONDS", 0.01)
    _setup(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(1.0)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    calls = []
    real_load = runner._load

    def flaky_load():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real_load()

    runner._load = flaky_load
    runner.start()
    # Poll with a deadline: a fixed 0.1 s failed once under full-suite load.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while (len(calls) < 2 or client.reads < 1) and loop.time() < deadline:
        await asyncio.sleep(0.01)
    runner.stop()

    assert len(calls) > 1
    assert client.reads >= 1


# --- writes ------------------------------------------------------------------


async def _set_local(app, value):
    await write_manual_value(_live(app), value)


@pytest.mark.asyncio
async def test_write_mapping_sends_the_local_value_at_its_priority(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert client.writes == [(REMOTE_AV, 17.0, 9)]
    entry = _status(session_factory, ids["connector"])["write_status"][str(ids["writes"][0])]
    assert entry["error"] is None
    assert entry["last_sent_at"] is not None


@pytest.mark.asyncio
async def test_a_value_the_remote_type_cannot_take_is_refused_and_reported(session_factory):
    # 5.0 is no binary Present_Value: the cast raises the ClientError the real write raises.
    ids = _setup(session_factory, reads=(), writes=(Point("binary-value", 7),))
    app = _app(session_factory)
    await _set_local(app, 5.0)
    client = FakeClient()

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert client.writes == []
    entry = _status(session_factory, ids["connector"])["write_status"][str(ids["writes"][0])]
    assert "can't write" in entry["error"]


@pytest.mark.asyncio
async def test_unchanged_value_waits_for_the_rewrite_interval(session_factory):
    # A long interval, passed by backdating the last send rather than by
    # sleeping: a slow CI runner can take longer than a short interval
    # between two ticks.
    _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=60)
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)
    await _run(runner)
    assert len(client.writes) == 1

    for commanded in runner._commanded.values():
        commanded.last_sent_at -= 61
    await _run(runner)
    assert len(client.writes) == 2


@pytest.mark.asyncio
async def test_rewrite_interval_zero_means_on_change_only(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)
    await asyncio.sleep(0.02)
    await _run(runner)

    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_changed_local_value_is_sent_on_the_next_tick(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    await _set_local(app, 55.0)
    await _run(runner)

    assert client.writes[-1] == (REMOTE_AV, 55.0, 9)


@pytest.mark.asyncio
async def test_a_binary_local_value_is_sent_as_itself(session_factory):
    # The runner hands the raw Present_Value to the client, which converts it
    # to the remote type (BacnetClient.write / cast_for_write).
    with session_factory() as session:
        device = Device(id=1, name="D")
        bv = BacnetObject(device=device, object_type="binary-value", instance_number=1, name="BV-1")
        connector = Connector(
            name="Remote", type="bacnet", url="", device=device,
            remote_device_instance=2, remote_address="10.0.0.2",
        )
        session.add_all(
            [
                device, bv, connector,
                ConnectorWriteMapping(
                    connector=connector, object=bv,
                    remote_object_type="binary-output", remote_object_instance=4,
                ),
            ]
        )
        session.commit()
    app = _app(session_factory)
    await write_manual_value(_live(app, "binary-value"), "active")
    client = FakeClient()

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert client.writes == [(Point("binary-output", 4), "active", 8)]


@pytest.mark.asyncio
async def test_deleting_a_write_mapping_relinquishes_the_remote_point(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert client.writes[-1] == (REMOTE_AV, None, 9)


@pytest.mark.asyncio
async def test_disabling_the_connector_relinquishes_its_write_mappings(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = False
        session.commit()
    runner.mark_changed()
    await _run(runner)
    await _run(runner)

    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, None, 9)]


@pytest.mark.asyncio
async def test_priority_change_relinquishes_the_old_priority_first(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.get(ConnectorWriteMapping, ids["writes"][0]).priority = 12
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert client.writes == [(REMOTE_AV, 17.0, 9), (REMOTE_AV, None, 9), (REMOTE_AV, 17.0, 12)]


@pytest.mark.asyncio
async def test_failed_write_is_not_retried_every_tick(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    client.write_fail = ClientError("property: write-access-denied")
    attempts = []
    real_write = client.write

    async def counting_write(*args, **kwargs):
        attempts.append(args)
        await real_write(*args, **kwargs)

    client.write = counting_write
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)
    await _run(runner)
    assert len(attempts) == 1
    entry = _status(session_factory, ids["connector"])["write_status"][str(ids["writes"][0])]
    assert entry["error"] == "property: write-access-denied"

    await _set_local(app, 5.0)
    await _run(runner)
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_transport_failure_on_write_unbinds(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    client.write_fail = ClientError("no response within 1 s", kind="transport")

    await _run(BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client))

    assert client.unbound == [2]


@pytest.mark.asyncio
async def test_transport_failure_on_write_clears_the_bound_address(session_factory):
    ids = _setup(session_factory, writes=(REMOTE_AV,))
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    client.write_fail = ClientError("no response within 1 s", kind="transport")

    await _run(BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client))

    # The read bound it, then the write's transport failure dropped it.
    assert client.unbound == [2]
    assert _status(session_factory, ids["connector"])["bound_address"] is None


UNREACHABLE = ClientError("no response within 1 s", kind="transport")


@pytest.mark.asyncio
async def test_a_write_lost_to_the_transport_is_resent_once_a_read_rebinds(session_factory):
    ids = _setup(session_factory, writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    client.fail = UNREACHABLE
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)
    assert client.writes == []
    assert runner._commanded, "kept, so the point is still relinquished later"

    client.fail = None
    await _run(runner)  # the read is backed off, nothing has rebound
    assert client.writes == []

    runner._due.clear()
    await _run(runner)  # the read rebinds; the write goes out the same tick
    assert client.writes == [(REMOTE_AV, 17.0, 9)]
    entry = _status(session_factory, ids["connector"])["write_status"][str(ids["writes"][0])]
    assert entry["error"] is None

    await _run(runner)
    assert len(client.writes) == 1, "delivered: on change only again"


def _make_write_retries_due(runner):
    for commanded in runner._commanded.values():
        commanded.retry_at = 0.0


@pytest.mark.asyncio
async def test_a_write_only_connector_retries_an_undelivered_write_with_backoff(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    client = FakeClient()
    client.write_fail = UNREACHABLE
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)

    await _run(runner)
    await _run(runner)
    assert client.write_attempts == 1, "not every tick"
    [commanded] = runner._commanded.values()
    assert commanded.retry_at - asyncio.get_running_loop().time() == pytest.approx(10, abs=1)

    _make_write_retries_due(runner)
    await _run(runner)
    assert client.write_attempts == 2
    [commanded] = runner._commanded.values()
    assert commanded.retry_at - asyncio.get_running_loop().time() == pytest.approx(20, abs=1)

    client.write_fail = None
    _make_write_retries_due(runner)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]
    entry = _status(session_factory, ids["connector"])["write_status"][str(ids["writes"][0])]
    assert entry["error"] is None

    _make_write_retries_due(runner)
    await _run(runner)
    assert client.write_attempts == 3


@pytest.mark.asyncio
async def test_a_retried_write_the_remote_refuses_is_not_retried_again(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    client = FakeClient()
    client.write_fail = UNREACHABLE
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)

    client.write_fail = ClientError("property: write-access-denied")
    _make_write_retries_due(runner)
    await _run(runner)
    _make_write_retries_due(runner)
    await _run(runner)

    assert client.write_attempts == 2


@pytest.mark.asyncio
async def test_write_under_dcc_is_sent_once_dcc_lifts(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    client.fail = ClientError("suppressed by DCC (disable)", kind="dcc")
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)

    await _run(runner)
    assert client.writes == []
    assert _status(session_factory, ids["connector"])["suppressed_by_dcc"] is True

    client.fail = None
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]


@pytest.mark.asyncio
async def test_shutdown_relinquishes_every_commanded_point(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)

    await runner.shutdown()

    assert client.writes[-1] == (REMOTE_AV, None, 9)


@pytest.mark.asyncio
async def test_shutdown_gives_up_on_a_remote_that_doesnt_answer(session_factory):
    _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)

    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    client.write = hang
    loop = asyncio.get_running_loop()
    started = loop.time()
    await runner.shutdown(timeout=0.05)

    assert loop.time() - started < 1


# --- the connector runner survives errors and DCC refusals --------------------


@pytest.mark.asyncio
async def test_runner_survives_a_raw_error_reject_abort_nack(session_factory):
    # Fix 1: bacpypes3 raises ErrorRejectAbortNack as a BaseException, not an
    # Exception. Every real client call goes through BacnetClient._call,
    # which turns these into ClientError -- but a raw one must not end this
    # connector's task for good if it ever got past that.
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.fail = ErrorRejectAbortNack()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)

    status = _status(session_factory, ids["connector"])
    assert status["last_error"]
    now = asyncio.get_running_loop().time()
    assert runner._due[ids["connector"]] > now

    # A later tick still runs the connector -- the task wasn't ended.
    client.fail = None
    client.values[REMOTE_AI] = Real(1.0)
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _live(app).presentValue == 1.0


@pytest.mark.asyncio
async def test_runner_survives_a_non_dcc_client_error_escaping_poll(session_factory):
    # `except ClientError as exc: if exc.kind != "dcc": raise` used to
    # re-raise past its own except clause -- the sibling `except Exception`
    # never sees a raise from inside another except block of the same try,
    # so it skipped logging/_save_status and ended the task. _poll/_push
    # handle their own ClientErrors, so force one to escape another way.
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(1.0)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    real_poll = runner._poll

    async def broken_poll(config, status, now):
        raise ClientError("boom", kind="error")

    runner._poll = broken_poll

    await _run(runner)

    status = _status(session_factory, ids["connector"])
    assert status["last_error"] == "boom"
    now = asyncio.get_running_loop().time()
    assert runner._due[ids["connector"]] > now

    # A later tick still runs -- the bad re-raise didn't end the task.
    runner._poll = real_poll
    runner._due[ids["connector"]] = 0.0
    await _run(runner)

    assert _live(app).presentValue == 1.0


@pytest.mark.asyncio
async def test_disabling_mid_poll_cancels_the_in_flight_run_and_clears_due(session_factory):
    # Fix 3: a run still waiting on a slow bind when its connector is
    # disabled used to be left running; if it later finished on its own it
    # could re-feed, re-write and re-set `_due`, which `_release_removed`
    # would then never clean up (it only pruned `_due` via `_status`'s keys,
    # and `_status` had already been popped) -- so a re-enabled connector
    # would wait out a stale backoff before polling again.
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    real_bind = client.bind

    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    client.bind = hang
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()
    task = runner._running[ids["connector"]]
    await asyncio.sleep(0)  # let the run actually start awaiting bind

    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = False
        session.commit()
    runner.mark_changed()
    await runner.tick()
    await runner.idle()

    assert task.cancelled()
    assert ids["connector"] not in runner._due
    assert ids["connector"] not in runner._running

    # Re-enabling polls on the next tick rather than waiting on a stale due
    # time the cancelled run never actually got to set.
    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = True
        session.commit()
    runner.mark_changed()
    client.bind = real_bind
    client.values[REMOTE_AI] = Real(9.0)
    await _run(runner)

    assert _live(app).presentValue == 9.0


# --- relinquish, stop and status pruning --------------------------------------


@pytest.mark.asyncio
async def test_disabling_mid_write_cancels_the_run_but_still_relinquishes(session_factory):
    # The relinquish still happens even though the in-flight run gets
    # cancelled -- needs its own write-mapping coverage (the earlier mid-poll
    # cancellation test has no write mapping, and the
    # disabling-relinquishes-writes test has no in-flight task).
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    real_write = client.write
    hung = asyncio.Event()

    async def hang_once(*args, **kwargs):
        hung.set()
        await asyncio.sleep(10)

    client.write = hang_once
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()
    task = runner._running[ids["connector"]]
    await hung.wait()  # the run is now stuck inside its own write() call

    client.write = real_write  # doesn't affect the already-started hung call
    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = False
        session.commit()
    runner.mark_changed()
    await runner.tick()
    await runner.idle()

    assert task.cancelled()
    assert client.writes[-1] == (REMOTE_AV, None, 9)


@pytest.mark.asyncio
async def test_dcc_suppressed_relinquish_is_retried_until_it_lands(session_factory):
    # A relinquish suppressed by DCC must stay pending and be retried on later
    # ticks, not dropped.
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]

    client.fail = ClientError("suppressed by DCC (disable)", kind="dcc")
    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]  # nothing sent -- DCC suppressed

    await _run(runner)  # still under DCC: retried, still suppressed
    assert client.writes == [(REMOTE_AV, 0.0, 9)]

    client.fail = None
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, None, 9)]

    await _run(runner)  # already relinquished -- not sent again
    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, None, 9)]


@pytest.mark.asyncio
async def test_dcc_suppressed_priority_change_relinquishes_old_then_writes_new(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 17.0, 9)]

    client.fail = ClientError("suppressed by DCC (disable)", kind="dcc")
    with session_factory() as session:
        session.get(ConnectorWriteMapping, ids["writes"][0]).priority = 12
        session.commit()
    runner.mark_changed()
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 17.0, 9)]  # nothing more -- suppressed

    client.fail = None
    await _run(runner)

    assert client.writes == [(REMOTE_AV, 17.0, 9), (REMOTE_AV, None, 9), (REMOTE_AV, 17.0, 12)]


@pytest.mark.asyncio
async def test_shutdown_resends_a_relinquish_stop_would_otherwise_cancel(session_factory):
    # stop() cancels every _background task,
    # including a relinquish _release_removed just spawned for a removed
    # write mapping -- shutdown() must still get it out rather than losing
    # it when stop() cancels the in-flight background attempt.
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]

    real_write = client.write
    hung = asyncio.Event()

    async def hang(*args, **kwargs):
        hung.set()
        await asyncio.sleep(10)

    client.write = hang
    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()
    await runner.tick()  # spawns the background relinquish; it hangs on write
    await hung.wait()

    client.write = real_write  # restored before shutdown's own attempt
    await runner.shutdown()

    assert client.writes[-1] == (REMOTE_AV, None, 9)


@pytest.mark.asyncio
async def test_relinquish_survives_a_raw_error_reject_abort_nack(session_factory):
    # _relinquish previously caught only ClientError;
    # a raw ErrorRejectAbortNack must not escape and leave the background
    # relinquish task's exception unretrieved (or the entry stuck pending
    # forever even though it's not a DCC suppression).
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    async def raising_write(*args, **kwargs):
        raise ErrorRejectAbortNack()

    client.write = raising_write
    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()

    await _run(runner)  # must not raise

    # Not DCC, so treated as done (best effort once) -- not retried forever.
    assert not runner._pending_relinquish


async def _delete_write_mapping_while_unreachable(session_factory):
    """A write mapping sent once, then deleted while the remote doesn't
    answer. Returns (runner, client)."""
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9)]
    client.fail = ClientError("no response within 1 s", kind="transport")
    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()
    await _run(runner)
    return runner, client


def _make_retries_due(runner):
    for key, (failures, _) in list(runner._relinquish_retry.items()):
        runner._relinquish_retry[key] = (failures, 0.0)


@pytest.mark.asyncio
async def test_unreachable_relinquish_is_kept_and_retried_after_a_delay(session_factory):
    # A relinquish lost to a transport failure used to be dropped,
    # leaving BACpack's command in the remote's priority array for good.
    runner, client = await _delete_write_mapping_while_unreachable(session_factory)
    assert len(runner._pending_relinquish) == 1
    assert client.unbound == [2]
    [(failures, due)] = runner._relinquish_retry.values()
    assert failures == 1
    assert due >= asyncio.get_running_loop().time() + 9

    client.fail = None
    await _run(runner)  # not due yet
    assert client.writes == [(REMOTE_AV, 0.0, 9)]

    _make_retries_due(runner)
    await _run(runner)
    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, None, 9)]
    assert not runner._pending_relinquish
    assert not runner._relinquish_retry


@pytest.mark.asyncio
async def test_relinquish_retry_delay_doubles_and_caps(session_factory):
    runner, client = await _delete_write_mapping_while_unreachable(session_factory)
    for expected in (20, 40, 80, 160, 300, 300):
        _make_retries_due(runner)
        await _run(runner)
        [(_, due)] = runner._relinquish_retry.values()
        assert due - asyncio.get_running_loop().time() == pytest.approx(expected, abs=1)


@pytest.mark.asyncio
async def test_unreachable_relinquish_warns_first_and_at_the_cap_only(session_factory, caplog):
    # A remote that never answers (an address typo,
    # a decommissioned device) mustn't log a warning every 5 minutes for good.
    caplog.set_level("WARNING", logger="bacpack.bacnet_connectors")
    runner, client = await _delete_write_mapping_while_unreachable(session_factory)
    for _ in range(8):  # 20, 40, 80, 160, 300, 300, 300, 300 s
        _make_retries_due(runner)
        await _run(runner)

    warnings = [r.getMessage() for r in caplog.records if "relinquish" in r.getMessage()]
    assert len(warnings) == 2
    assert warnings[0].endswith("will retry")
    assert "every 300 s" in warnings[1]


@pytest.mark.asyncio
async def test_relinquish_refused_by_the_remote_is_not_retried(session_factory):
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)
    client.write_fail = ClientError("property: write-access-denied")
    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()

    await _run(runner)

    assert not runner._pending_relinquish
    assert not runner._relinquish_retry


@pytest.mark.asyncio
async def test_unreachable_relinquish_never_nulls_a_mapping_recreated_on_its_target(session_factory):
    # The retry must not undo a live mapping's write.
    runner, client = await _delete_write_mapping_while_unreachable(session_factory)
    with session_factory() as session:
        connector = session.query(Connector).one()
        av = session.query(BacnetObject).filter_by(name="AV-1").one()
        session.add(ConnectorWriteMapping(
            connector=connector, object=av, remote_object_type="analog-value",
            remote_object_instance=5, remote_property="present-value", priority=9,
        ))
        session.commit()
    runner.mark_changed()
    client.fail = None
    await _run(runner)  # the new mapping writes
    _make_retries_due(runner)
    await _run(runner)  # the retry finds the target owned and drops itself

    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, 0.0, 9)]
    assert not runner._pending_relinquish


@pytest.mark.asyncio
async def test_unreachable_relinquish_never_nulls_a_mapping_recreated_via_another_address(session_factory):
    # The address is only the route to the remote, so
    # a mapping re-created on the same device, point and priority after the
    # connector moved to a static address still owns the target.
    runner, client = await _delete_write_mapping_while_unreachable(session_factory)
    with session_factory() as session:
        connector = session.query(Connector).one()
        connector.remote_address = "10.0.0.3"
        av = session.query(BacnetObject).filter_by(name="AV-1").one()
        session.add(ConnectorWriteMapping(
            connector=connector, object=av, remote_object_type="analog-value",
            remote_object_instance=5, remote_property="present-value", priority=9,
        ))
        session.commit()
    runner.mark_changed()
    client.fail = None
    await _run(runner)  # the new mapping writes through the new address
    _make_retries_due(runner)
    await _run(runner)  # the retry finds the target owned and drops itself

    assert client.writes == [(REMOTE_AV, 0.0, 9), (REMOTE_AV, 0.0, 9)]
    assert not runner._pending_relinquish


@pytest.mark.asyncio
async def test_deleting_the_last_write_mapping_prunes_its_status_entry(session_factory):
    # write_status pruning used to run only inside
    # _push, which only runs when config.writes is non-empty -- so deleting
    # a connector's last write mapping left its entry behind.
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,))
    app = _app(session_factory)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    assert str(ids["writes"][0]) in _status(session_factory, ids["connector"])["write_status"]

    with session_factory() as session:
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert _status(session_factory, ids["connector"])["write_status"] == {}


# --- Schedule values, deleted mappings and objects added at run time ---------


def test_read_value_for_feed_unwraps_a_schedule_present_value():
    # bacpypes3 types Schedule Present_Value as AnyAtomic.
    assert read_value_for_feed(AnyAtomic(Real(72.0)), _mapping()) == 72.0
    assert read_value_for_feed(AnyAtomic(BinaryPV("active")), _mapping()) == 1.0
    assert read_value_for_feed(AnyAtomic(Unsigned(3)), _mapping()) == 3.0


@pytest.mark.asyncio
async def test_remote_schedule_present_value_feeds_a_local_object(session_factory):
    remote_schedule = Point("schedule", 3)
    ids = _setup(session_factory, reads=(remote_schedule,))
    app = _app(session_factory)
    client = FakeClient()
    client.values[remote_schedule] = AnyAtomic(Real(55.0))

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert _live(app).presentValue == 55.0
    assert _status(session_factory, ids["connector"])["read_errors"] == {str(ids["reads"][0]): None}


@pytest.mark.asyncio
async def test_local_schedule_as_a_write_mapping_source(session_factory):
    # A local Schedule's Present_Value is AnyAtomic-wrapped
    # (interpret_schedule stores eval()'s result as-is).
    with session_factory() as session:
        device = Device(id=1, name="D")
        schedule = BacnetObject(
            device=device, object_type="schedule", instance_number=1, name="Sched-1",
            schedule_value_type="analog", schedule_default="10.0", weekly_schedule=[[] for _ in range(7)],
        )
        connector = Connector(
            name="Remote", type="bacnet", url="", device=device,
            remote_device_instance=2, remote_address="10.0.0.2", rewrite_interval_seconds=0,
        )
        mapping = ConnectorWriteMapping(
            connector=connector, object=schedule,
            remote_object_type="analog-value", remote_object_instance=5, priority=9,
        )
        session.add_all([device, schedule, connector, mapping])
        session.commit()
        connector_id, mapping_id = connector.id, mapping.id
    app = _app(session_factory)
    live = app.get_object_id(ObjectIdentifier(("schedule", 1)))
    client = FakeClient()
    typed = []
    real_write = client.write

    async def casting_write(binding, point, value, *, priority, timeout):
        typed.append(cast_for_write(point, value))  # what BacnetClient.write sends
        await real_write(binding, point, value, priority=priority, timeout=timeout)

    client.write = casting_write
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)  # the schedule's own value: its default
    await _run(runner)  # unchanged: not sent again
    assert isinstance(live.presentValue, AnyAtomic)
    live.presentValue = AnyAtomic(Real(60.0))
    await _run(runner)

    assert client.writes == [(REMOTE_AV, 10.0, 9), (REMOTE_AV, 60.0, 9)]
    assert typed == [Real(10.0), Real(60.0)]
    assert _status(session_factory, connector_id)["write_status"][str(mapping_id)]["error"] is None


class _Gate:
    """Holds the first call that waits on it until released."""

    def __init__(self):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.used = False

    async def wait(self):
        if self.used:
            return
        self.used = True
        self.reached.set()
        await self.release.wait()


def _readd_write_mapping(session_factory, ids):
    """The UI's only way to "edit" a write mapping: delete, add it again.
    Added before the delete so SQLite can't hand the new row the old id."""
    with session_factory() as session:
        av = session.query(BacnetObject).filter_by(object_type="analog-value", instance_number=1).one()
        session.add(
            ConnectorWriteMapping(
                connector_id=ids["connector"], object=av,
                remote_object_type=REMOTE_AV.object_type, remote_object_instance=REMOTE_AV.instance,
                remote_property=REMOTE_AV.prop, priority=9,
            )
        )
        session.flush()
        session.delete(session.get(ConnectorWriteMapping, ids["writes"][0]))
        session.commit()


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pending_relinquish_is_dropped_when_a_live_mapping_owns_the_target(session_factory):
    # A write mapping deleted and re-added with the same
    # target while its relinquish is still pending (DCC) -- the NULL must
    # not land after the new mapping's write.
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    client.fail = ClientError("suppressed by DCC (disable)", kind="dcc")
    _readd_write_mapping(session_factory, ids)
    runner.mark_changed()
    await _run(runner)  # the relinquish and the new write are both suppressed
    assert client.writes == [(REMOTE_AV, 17.0, 9)]

    # DCC lifts; the pending relinquish is still binding when the new write goes.
    client.fail = None
    gate = _Gate()
    real_bind = client.bind

    async def gated_bind(*args, **kwargs):
        await gate.wait()
        return await real_bind(*args, **kwargs)

    client.bind = gated_bind
    await runner.tick()
    await asyncio.wait_for(gate.reached.wait(), 2)
    await _settle()
    assert client.writes == [(REMOTE_AV, 17.0, 9), (REMOTE_AV, 17.0, 9)]  # the new write went first
    gate.release.set()
    await asyncio.wait_for(runner.idle(), 2)

    assert client.writes == [(REMOTE_AV, 17.0, 9), (REMOTE_AV, 17.0, 9)]
    assert not runner._pending_relinquish


@pytest.mark.asyncio
async def test_a_relinquish_that_lands_after_a_new_write_triggers_a_resend(session_factory):
    # The NULL was already on its way when the new mapping
    # wrote -- the value is re-sent on the next tick, even at re-write 0.
    ids = _setup(session_factory, reads=(), writes=(REMOTE_AV,), rewrite_interval_seconds=0)
    app = _app(session_factory)
    await _set_local(app, 17.0)
    client = FakeClient()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    client.fail = ClientError("suppressed by DCC (disable)", kind="dcc")
    _readd_write_mapping(session_factory, ids)
    runner.mark_changed()
    await _run(runner)
    client.fail = None

    gate = _Gate()
    real_write = client.write

    async def gated_write(binding, point, value, *, priority, timeout):
        if value is None:
            await gate.wait()
        await real_write(binding, point, value, priority=priority, timeout=timeout)

    client.write = gated_write
    await runner.tick()
    await asyncio.wait_for(gate.reached.wait(), 2)  # the NULL is past its check, held in flight
    await _settle()
    gate.release.set()
    await asyncio.wait_for(runner.idle(), 2)
    assert client.writes[-2:] == [(REMOTE_AV, 17.0, 9), (REMOTE_AV, None, 9)]

    await _run(runner)
    assert client.writes[-1] == (REMOTE_AV, 17.0, 9)
    await _run(runner)  # and only once
    assert client.writes[-2:] == [(REMOTE_AV, None, 9), (REMOTE_AV, 17.0, 9)]


@pytest.mark.asyncio
async def test_mappings_on_an_object_that_isnt_live_say_so(session_factory):
    # An object added since the device started.
    ids = _setup(session_factory)
    app = _app(session_factory)
    with session_factory() as session:
        av2 = BacnetObject(device_id=1, object_type="analog-value", instance_number=2, name="AV-2")
        read = ConnectorMapping(
            connector_id=ids["connector"], object=av2,
            remote_object_type=REMOTE_BI.object_type, remote_object_instance=REMOTE_BI.instance,
            remote_property=REMOTE_BI.prop,
        )
        write = ConnectorWriteMapping(
            connector_id=ids["connector"], object=av2,
            remote_object_type="analog-value", remote_object_instance=6, priority=9,
        )
        session.add_all([av2, read, write])
        session.commit()
        read_id, write_id = read.id, write.id
    client = FakeClient()
    client.values = {REMOTE_AI: Real(72.5), REMOTE_BI: BinaryPV("active")}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await _run(runner)
    await _run(runner)

    status = _status(session_factory, ids["connector"])
    assert status["read_errors"][str(ids["reads"][0])] is None
    assert status["read_errors"][str(read_id)] == NOT_LIVE
    assert status["write_status"][str(write_id)] == {
        "error": NOT_LIVE,
        "last_sent_at": None,
    }
    assert client.writes == []


@pytest.mark.asyncio
async def test_local_feed_refusal_says_so(session_factory):
    # The remote read worked, the local object refused.
    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values[REMOTE_AI] = Real(72.5)
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    async def refuse(object_type, instance, value):
        if value is not None:
            raise PropertyError("value-out-of-range")

    runner._feed = refuse
    await _run(runner)

    errors = _status(session_factory, ids["connector"])["read_errors"]
    assert errors[str(ids["reads"][0])] == "local object refused the value (property: value-out-of-range)"


@pytest.mark.asyncio
async def test_remote_point_error_text_names_the_point(session_factory):
    ids = _setup(session_factory)
    client = FakeClient()  # no value for REMOTE_AI: FakeClient answers unknown-object
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=client)
    await _run(runner)
    errors = _status(session_factory, ids["connector"])["read_errors"]
    assert errors[str(ids["reads"][0])] == "analog-input:1 present-value: object: unknown-object"


# --- proxies ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_mirrors_present_value_and_priority_array(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(68.0), REMOTE_AV_ARRAY: _remote_array(p5=68.0, p8=72.0)}

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    obj = _live(app)
    assert obj.presentValue == 68.0
    assert (_slot(obj, 5), _slot(obj, 8), _slot(obj, 10)) == (68.0, 72.0, None)
    assert obj.reliability == Reliability("no-fault-detected")
    assert _status(session_factory, ids["connector"])["read_errors"] == {str(ids["mapping"]): None}
    assert client.writes == []


@pytest.mark.asyncio
async def test_proxy_is_faulted_until_its_first_good_read(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    client.read_gate = asyncio.Event()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()
    assert _live(app).reliability == Reliability("communication-failure")

    client.read_gate.set()
    await runner.idle()
    assert _live(app).reliability == Reliability("no-fault-detected")


@pytest.mark.asyncio
async def test_a_write_to_the_proxy_is_forwarded_at_its_priority_and_reread(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(68.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)

    await obj.write_property("presentValue", Real(70.0), priority=8)

    assert client.writes == [(REMOTE_AV, 70.0, 8)]
    assert _slot(obj, 8) == 70.0
    forward = _status(session_factory, ids["connector"])["last_forward"][str(ids["mapping"])]
    assert (forward["priority"], forward["value"], forward["error"]) == (8, 70.0, None)
    client.values = {REMOTE_AV: Real(70.0), REMOTE_AV_ARRAY: _remote_array(p8=70.0)}
    await _run(runner)  # due again at once, not after the 60 s interval
    assert client.reads == 2 and obj.presentValue == 70.0


@pytest.mark.asyncio
async def test_proxy_passes_the_remotes_error_back_and_changes_nothing(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    client.write_fail = ClientError(
        "property: value-out-of-range", error_class="property", error_code="value-out-of-range", reply="error"
    )

    with pytest.raises(ExecutionError) as caught:
        await obj.write_property("presentValue", Real(999.0), priority=8)

    assert (caught.value.errorClass, caught.value.errorCode) == ("property", "value-out-of-range")
    assert _slot(obj, 8) is None
    forward = _status(session_factory, ids["connector"])["last_forward"][str(ids["mapping"])]
    assert (forward["value"], forward["error"]) == (999.0, "property: value-out-of-range")


@pytest.mark.asyncio
async def test_proxy_forward_gives_up_at_the_cap(session_factory, monkeypatch):
    monkeypatch.setattr("bacpack.bacnet_connector_proxy.FORWARD_CAP_SECONDS", 0.05)
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    client.write_delay = 1.0

    with pytest.raises(ExecutionError) as caught:
        await obj.write_property("presentValue", Real(5.0), priority=8)

    assert (caught.value.errorClass, caught.value.errorCode) == ("communication", "timeout")
    assert _slot(obj, 8) is None
    assert 2 in client.unbound


@pytest.mark.asyncio
async def test_after_a_forward_times_out_the_next_ones_fail_at_once(session_factory, monkeypatch):
    # n writes to proxies on a dead remote took n x the cap.
    monkeypatch.setattr("bacpack.bacnet_connector_proxy.FORWARD_CAP_SECONDS", 0.05)
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    client.write_delay = 1.0

    with pytest.raises(ExecutionError):
        await obj.write_property("presentValue", Real(5.0), priority=8)
    assert client.write_attempts == 1
    with pytest.raises(ExecutionError) as caught:
        await obj.write_property("presentValue", Real(6.0), priority=8)
    assert (caught.value.errorClass, caught.value.errorCode) == ("communication", "timeout")
    assert client.write_attempts == 1  # not sent
    forward = _status(session_factory, ids["connector"])["last_forward"][str(ids["mapping"])]
    assert forward["value"] == 6.0 and "not sent" in forward["error"]

    # A successful poll ends the window early.
    client.write_delay = 0.0
    runner._due.clear()
    await _run(runner)
    await obj.write_property("presentValue", Real(7.0), priority=8)
    assert client.write_attempts == 2 and client.writes == [(REMOTE_AV, 7.0, 8)]


@pytest.mark.asyncio
async def test_the_fast_fail_window_ends_on_its_own(session_factory, monkeypatch):
    monkeypatch.setattr("bacpack.bacnet_connector_proxy.FORWARD_CAP_SECONDS", 0.05)
    monkeypatch.setattr("bacpack.bacnet_connector_proxy.FORWARD_UNREACHABLE_SECONDS", 0.1)
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    client.write_delay = 1.0
    with pytest.raises(ExecutionError):
        await obj.write_property("presentValue", Real(5.0), priority=8)

    client.write_delay = 0.0
    await asyncio.sleep(0.15)
    await obj.write_property("presentValue", Real(6.0), priority=8)

    assert client.write_attempts == 2 and client.writes == [(REMOTE_AV, 6.0, 8)]


@pytest.mark.asyncio
async def test_a_remote_error_answer_opens_no_fast_fail_window(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    client.write_fail = ClientError(
        "property: value-out-of-range", error_class="property", error_code="value-out-of-range", reply="error"
    )
    with pytest.raises(ExecutionError):
        await obj.write_property("presentValue", Real(999.0), priority=8)

    client.write_fail = None
    await obj.write_property("presentValue", Real(5.0), priority=8)

    assert client.write_attempts == 2


@pytest.mark.asyncio
async def test_proxy_with_an_unreachable_remote_faults_and_refuses_writes(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(40.0), REMOTE_AV_ARRAY: _remote_array(p8=40.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)

    client.fail = ClientError("no response within 1 s", kind="transport")
    runner._due.clear()
    await _run(runner)

    assert (obj.presentValue, _slot(obj, 8)) == (40.0, 40.0)  # last values kept, not relinquished
    assert obj.reliability == Reliability("communication-failure")
    with pytest.raises(ExecutionError) as caught:
        await obj.write_property("presentValue", Real(41.0), priority=8)
    assert (caught.value.errorClass, caught.value.errorCode) == ("communication", "timeout")
    assert _slot(obj, 8) == 40.0

    client.fail = None
    runner._due.clear()
    await _run(runner)
    assert obj.reliability == Reliability("no-fault-detected")


@pytest.mark.asyncio
async def test_an_alarming_proxy_returns_to_normal_when_the_remote_recovers_with_a_new_value(session_factory):
    # The new value queues the alarm evaluation; a Reliability change made
    # after it in the same step went unrecorded, so the object stayed FAULT.
    _setup_proxy(session_factory)
    with session_factory() as session:
        sp = session.query(BacnetObject).filter_by(name="SP").one()
        sp.alarming_enabled = True
        sp.notification_class_instance = 1
        sp.alarm_high_limit = 100.0
        sp.alarm_low_limit = 0.0
        sp.alarm_deadband = 1.0
        session.add(BacnetObject(device_id=1, object_type="notification-class", instance_number=1, name="NC-1"))
        session.commit()
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(40.0), REMOTE_AV_ARRAY: _remote_array(p8=40.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)

    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    await settle()
    assert obj.eventState.attr == "normal"

    client.fail = ClientError("no response within 1 s", kind="transport")
    runner._due.clear()
    await _run(runner)
    await settle()
    assert obj.eventState.attr == "fault"

    client.fail = None
    client.values = {REMOTE_AV: Real(41.0), REMOTE_AV_ARRAY: _remote_array(p8=41.0)}
    runner._due.clear()
    await _run(runner)
    await settle()
    assert obj.presentValue == 41.0
    assert obj.reliability == Reliability("no-fault-detected")
    assert obj.eventState.attr == "normal"


@pytest.mark.asyncio
async def test_proxy_without_a_remote_priority_array_mirrors_the_value_only(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(21.0), REMOTE_AV_ARRAY: NO_ARRAY}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    assert obj.presentValue == 21.0
    assert all(_slot(obj, p) is None for p in range(1, 17))
    assert obj.reliability == Reliability("no-fault-detected")

    await obj.write_property("presentValue", Real(23.0), priority=8)
    assert client.writes == [(REMOTE_AV, 23.0, 8)] and _slot(obj, 8) == 23.0

    client.values[REMOTE_AV] = Real(23.0)
    await _run(runner)
    assert obj.presentValue == 23.0 and _slot(obj, 8) is None


@pytest.mark.asyncio
async def test_any_property_error_on_the_remote_array_means_no_array(session_factory):
    # Read-access-denied used to fault the proxy for good.
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    denied = ClientError(
        "analog-value,5 priority-array: property: read-access-denied",
        error_class="property", error_code="read-access-denied", reply="error",
    )
    client.values = {REMOTE_AV: Real(21.0), REMOTE_AV_ARRAY: denied}

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    obj = _live(app)
    assert obj.presentValue == 21.0
    assert all(_slot(obj, p) is None for p in range(1, 17))
    assert obj.reliability == Reliability("no-fault-detected")


@pytest.mark.asyncio
async def test_another_error_class_on_the_remote_array_is_a_configuration_fault(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    refused = ClientError(
        "analog-value,5 priority-array: security: authentication-failed",
        error_class="security", error_code="authentication-failed", reply="error",
    )
    client.values = {REMOTE_AV: Real(21.0), REMOTE_AV_ARRAY: refused}

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert _live(app).reliability == Reliability("configuration-error")
    assert "authentication-failed" in _status(session_factory, ids["connector"])["read_errors"][str(ids["mapping"])]


@pytest.mark.asyncio
async def test_proxy_point_error_is_a_configuration_fault(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()  # no values: every point is unknown-object

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert _live(app).reliability == Reliability("configuration-error")
    assert "unknown-object" in _status(session_factory, ids["connector"])["read_errors"][str(ids["mapping"])]


@pytest.mark.asyncio
async def test_a_read_in_flight_during_a_forward_is_dropped_and_read_again(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(10.0), REMOTE_AV_ARRAY: _remote_array()}
    client.read_gate = asyncio.Event()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()  # hook attached; the poll is now waiting in read_many
    await asyncio.sleep(0)
    obj = _live(app)
    await obj.write_property("presentValue", Real(70.0), priority=8)
    client.read_gate.set()
    await runner.idle()

    assert _slot(obj, 8) == 70.0  # the read that started before the write didn't wipe it
    client.values = {REMOTE_AV: Real(70.0), REMOTE_AV_ARRAY: _remote_array(p8=70.0)}
    await _run(runner)
    assert client.reads == 2 and obj.presentValue == 70.0


@pytest.mark.asyncio
async def test_removing_a_proxy_clears_the_mirror_and_sends_nothing(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(72.0), REMOTE_AV_ARRAY: _remote_array(p8=72.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)

    with session_factory() as session:
        session.delete(session.get(ConnectorMapping, ids["mapping"]))
        session.commit()
    runner.mark_changed()
    await _run(runner)

    assert getattr(obj, "_proxy_forward", None) is None
    assert all(_slot(obj, p) is None for p in range(1, 17))
    assert obj.reliability == Reliability("no-fault-detected")
    await obj.write_property("presentValue", Real(3.0), priority=8)  # a plain local object again
    assert client.writes == [] and obj.presentValue == 3.0


@pytest.mark.asyncio
async def test_disabling_the_connector_clears_its_proxies(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(72.0), REMOTE_AV_ARRAY: _remote_array(p8=72.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)

    with session_factory() as session:
        session.get(Connector, ids["connector"]).enabled = False
        session.commit()
    runner.mark_changed()
    await _run(runner)

    obj = _live(app)
    assert getattr(obj, "_proxy_forward", None) is None and _slot(obj, 8) is None
    assert client.writes == []


@pytest.mark.asyncio
async def test_switching_between_read_and_proxy(session_factory):
    ids = _setup_proxy(session_factory, direction="read")
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(50.0), REMOTE_AV_ARRAY: _remote_array(p8=72.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    obj = _live(app)
    assert _slot(obj, 10) == 50.0 and getattr(obj, "_proxy_forward", None) is None

    _set_direction(session_factory, ids["mapping"], "proxy")
    runner.mark_changed()
    runner._due.clear()
    await _run(runner)
    assert (_slot(obj, 8), _slot(obj, 10), obj.presentValue) == (72.0, None, 50.0)
    assert obj._proxy_forward is not None
    assert client.writes == []  # releasing the old feed sends nothing

    _set_direction(session_factory, ids["mapping"], "read")
    runner.mark_changed()
    runner._due.clear()
    await _run(runner)
    assert (_slot(obj, 8), _slot(obj, 10)) == (None, 50.0)
    assert getattr(obj, "_proxy_forward", None) is None


@pytest.mark.asyncio
async def test_proxy_on_an_object_not_live_reports_it(session_factory):
    ids = _setup_proxy(session_factory)
    with session_factory() as session:
        device = session.get(Device, 1)
        app = build_application(device, [o for o in device.objects if o.name != "SP"])
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}

    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))

    assert _status(session_factory, ids["connector"])["read_errors"] == {str(ids["mapping"]): NOT_LIVE}


@pytest.mark.asyncio
async def test_shutdown_sends_nothing_for_proxies(session_factory):
    _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(1.0), REMOTE_AV_ARRAY: _remote_array()}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)
    await _run(runner)
    await _live(app).write_property("presentValue", Real(2.0), priority=8)
    sent = list(client.writes)

    await runner.shutdown()

    assert client.writes == sent  # the forwarded write only, no relinquish


@pytest.mark.asyncio
async def test_a_read_in_flight_across_a_switch_to_proxy_forwards_nothing(session_factory):
    ids = _setup_proxy(session_factory, direction="read")
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(50.0), REMOTE_AV_ARRAY: _remote_array()}
    client.read_gate = asyncio.Event()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()  # a plain read poll, waiting in read_many
    await asyncio.sleep(0)
    _set_direction(session_factory, ids["mapping"], "proxy")
    runner.mark_changed()
    await runner.tick()  # the hook is attached while the old poll is in flight
    client.read_gate.set()
    await runner.idle()

    assert client.writes == []


@pytest.mark.asyncio
async def test_a_proxy_poll_in_flight_across_a_delete_does_not_mirror_again(session_factory):
    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(72.0), REMOTE_AV_ARRAY: _remote_array(p8=72.0)}
    client.read_gate = asyncio.Event()
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    await runner.tick()
    await asyncio.sleep(0)
    with session_factory() as session:
        session.delete(session.get(ConnectorMapping, ids["mapping"]))
        session.commit()
    runner.mark_changed()
    await runner.tick()  # clears the mirror while the poll is in flight
    client.read_gate.set()
    await runner.idle()

    obj = _live(app)
    assert all(_slot(obj, p) is None for p in range(1, 17))
    assert obj.reliability == Reliability("no-fault-detected")


# --- Cached config -----------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_reloads_only_when_marked_changed(session_factory, monkeypatch):
    _setup(session_factory)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=FakeClient())
    loads = []
    real = runner._load
    monkeypatch.setattr(runner, "_load", lambda: loads.append(1) or real())

    await _run(runner)
    await _run(runner)
    assert len(loads) == 1

    runner.mark_changed()
    await _run(runner)
    assert len(loads) == 2


@pytest.mark.asyncio
async def test_runner_retries_a_failed_load_on_the_next_tick(session_factory, monkeypatch):
    _setup(session_factory)
    runner = BacnetConnectorRunner(session_factory, _app(session_factory), 1, client=FakeClient())
    calls = []
    real = runner._load

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(runner, "_load", flaky)
    with pytest.raises(RuntimeError):
        await runner.tick()
    await _run(runner)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_connector_status_saves_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    ids = _setup(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AI: Real(5.0)}
    runner = BacnetConnectorRunner(session_factory, app, 1, client=client)

    run = await run_while_locked(session_factory, lambda: _run(runner))

    assert_waited_off_the_loop(run)
    assert _status(session_factory, ids["connector"])["read_errors"] == {str(ids["reads"][0]): None}


@pytest.mark.asyncio
async def test_a_proxy_write_saves_its_status_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    ids = _setup_proxy(session_factory)
    app = _app(session_factory)
    client = FakeClient()
    client.values = {REMOTE_AV: Real(68.0), REMOTE_AV_ARRAY: _remote_array()}
    await _run(BacnetConnectorRunner(session_factory, app, 1, client=client))
    obj = _live(app)

    run = await run_while_locked(session_factory, lambda: obj.write_property("presentValue", Real(70.0), priority=8))

    assert_waited_off_the_loop(run)
    forward = _status(session_factory, ids["connector"])["last_forward"][str(ids["mapping"])]
    assert (forward["value"], forward["error"]) == (70.0, None)
