"""ExternalScheduleWriter -- a Schedule's writes to another
device's properties, retried until delivered. The BACnet client is faked; the
Schedule objects are real, from build_application."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Enumerated, Null, ObjectIdentifier, Real, Unsigned

from bacpack.bacnet_client import Binding, ClientError, Point, to_jsonable
from bacpack.device_process import _load_device_config
from bacpack.live_config import ObjectReloader
from bacpack.models import BacnetObject, Device, ObjectRuntime, get_or_create_settings
from bacpack.object_builder import build_application
from bacpack.objects.schedule_config import build_schedule_write_targets
import bacpack.schedule_external as schedule_external
from bacpack.schedule_external import ExternalScheduleWriter

AV3 = Point("analog-value", 3)
KEY_5 = "599005:analog-value:3:present-value"
KEY_6 = "599006:analog-value:3:present-value"


class FakeClient:
    def __init__(self):
        self.bind_fail = {}      # instance -> ClientError
        self.write_fail = {}     # (instance, point) -> ClientError
        self.writes = []         # (instance, point, jsonable value, priority)
        self.unbound = []
        self.natives = []        # native= passed to each write
        self.gate = {}           # instance -> asyncio.Event bind waits on

    async def bind(self, instance, static_address=None, *, timeout):
        if instance in self.gate:
            await self.gate[instance].wait()
        if instance in self.bind_fail:
            raise self.bind_fail[instance]
        return Binding(Address(f"10.0.0.{instance % 250}"))

    def unbind(self, instance, static_address=None):
        self.unbound.append(instance)

    async def write(self, binding, point, value, *, priority, timeout, native=None):
        instance = next(i for i in (599005, 599006, 599001) if binding.address == Address(f"10.0.0.{i % 250}"))
        error = self.write_fail.get((instance, point))
        if error is not None:
            raise error
        self.natives.append(native)
        self.writes.append((instance, point, None if value is None else to_jsonable(value), priority))


def _remote(device_instance=599005, **overrides):
    target = {
        "device_instance": device_instance,
        "object_type": "analog-value",
        "instance_number": 3,
        "property_identifier": "present-value",
    }
    target.update(overrides)
    return target


def _setup(session_factory, targets, *, value_type="analog", default="10.0", value="60.0", extra=(), **sched_extra):
    with session_factory() as session:
        device = Device(id=599001, name="D")
        sched = BacnetObject(
            device=device,
            object_type="schedule",
            instance_number=1,
            name="Sched-1",
            schedule_value_type=value_type,
            schedule_default=default,
            weekly_schedule=[[{"time": "00:00", "value": value}] for _ in range(7)],
            schedule_priority=12,
            schedule_write_targets=targets,
            **sched_extra,
        )
        session.add_all([device, sched, *[BacnetObject(device=device, **e) for e in extra]])
        session.commit()
        return sched.id


async def _app(session_factory):
    with session_factory() as session:
        device = session.get(Device, 599001)
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    for _ in range(10):  # let interpret_schedule run
        await asyncio.sleep(0.02)
    return app


def _sched(app, instance=1):
    return app.get_object_id(ObjectIdentifier(("schedule", instance)))


def _set_pv(obj, value):
    obj.presentValue = AnyAtomic(Real(value))


async def _run(writer):
    await writer.tick()
    await writer.idle()


def _status(session_factory, object_id):
    with session_factory() as session:
        return session.get(BacnetObject, object_id).runtime.schedule_write_status


@pytest.fixture
def clock(monkeypatch):
    """Controls the writer's notion of now (loop time)."""
    now = {"t": 1000.0}
    monkeypatch.setattr(schedule_external, "_now", lambda: now["t"])
    return now


@pytest.mark.asyncio
async def test_sends_every_external_target_on_the_first_tick(session_factory, clock):
    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    assert client.writes == [(599005, AV3, 60.0, 12)]
    entry = _status(session_factory, object_id)[KEY_5]
    assert entry["error"] is None
    assert entry["last_sent_value"] == 60.0
    assert entry["last_sent_at"]
    assert entry["suppressed_by_dcc"] is False
    app.close()


@pytest.mark.asyncio
async def test_nothing_resent_while_value_unchanged_then_transition_sent(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)
    await _run(writer)
    assert len(client.writes) == 1

    _set_pv(_sched(app), 70.0)
    await _run(writer)

    assert client.writes[-1] == (599005, AV3, 70.0, 12)
    assert len(client.writes) == 2
    app.close()


@pytest.mark.asyncio
async def test_priority_change_resends(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    _sched(app).priorityForWriting = Unsigned(9)
    await _run(writer)

    assert (599005, AV3, 60.0, 9) in client.writes  # and slot 12 released after it
    app.close()


@pytest.mark.asyncio
async def test_transport_failure_unbinds_backs_off_and_recovers(session_factory, clock):
    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = ClientError("device 599005 did not answer Who-Is", kind="transport")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)
    assert _status(session_factory, object_id)[KEY_5]["error"] == "device 599005 did not answer Who-Is"
    assert 599005 in client.unbound

    del client.bind_fail[599005]
    clock["t"] += 4.9
    await _run(writer)
    assert client.writes == []  # still backing off (5 s)

    clock["t"] += 0.2
    await _run(writer)
    assert client.writes == [(599005, AV3, 60.0, 12)]
    assert _status(session_factory, object_id)[KEY_5]["error"] is None
    app.close()


@pytest.mark.asyncio
async def test_backoff_doubles_and_caps(session_factory, clock, monkeypatch):
    monkeypatch.setattr(schedule_external, "MAX_BACKOFF_SECONDS", 12.0)
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = ClientError("timeout", kind="transport")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    attempts = []
    real_bind = client.bind

    async def counting_bind(instance, static_address=None, *, timeout):
        attempts.append(clock["t"])
        return await real_bind(instance, static_address, timeout=timeout)

    client.bind = counting_bind
    for _ in range(40):
        await _run(writer)
        clock["t"] += 1.0

    gaps = [round(b - a) for a, b in zip(attempts, attempts[1:])]
    assert gaps[:4] == [5, 10, 12, 12]
    app.close()


@pytest.mark.asyncio
async def test_value_change_resets_backoff(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = ClientError("timeout", kind="transport")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    del client.bind_fail[599005]
    _set_pv(_sched(app), 70.0)
    clock["t"] += 1.0  # well inside the 5 s backoff
    await _run(writer)

    assert client.writes == [(599005, AV3, 70.0, 12)]
    app.close()


@pytest.mark.asyncio
async def test_error_reply_backs_off_only_that_target(session_factory, clock):
    object_id = _setup(
        session_factory, [_remote(), _remote(instance_number=4)]
    )
    app = await _app(session_factory)
    client = FakeClient()
    client.write_fail[(599005, AV3)] = ClientError("object: unknown-object")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    status = _status(session_factory, object_id)
    assert status[KEY_5]["error"] == "object: unknown-object"
    assert status["599005:analog-value:4:present-value"]["error"] is None
    assert client.writes == [(599005, Point("analog-value", 4), 60.0, 12)]
    assert client.unbound == []

    clock["t"] += 1.0
    await _run(writer)
    assert len(client.writes) == 1  # the failed one waits out its backoff; the good one is delivered
    app.close()


@pytest.mark.asyncio
async def test_dcc_suppression_is_not_a_failure(session_factory, clock):
    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = ClientError("suppressed by DCC (disableInitiation)", kind="dcc")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)
    assert _status(session_factory, object_id)[KEY_5]["suppressed_by_dcc"] is True
    assert client.unbound == []

    del client.bind_fail[599005]
    clock["t"] += 1.0  # next tick, no backoff
    await _run(writer)
    assert client.writes == [(599005, AV3, 60.0, 12)]
    assert _status(session_factory, object_id)[KEY_5]["suppressed_by_dcc"] is False
    app.close()


@pytest.mark.asyncio
async def test_a_slow_device_does_not_block_another(session_factory, clock):
    _setup(session_factory, [_remote(599005), _remote(599006)])
    app = await _app(session_factory)
    client = FakeClient()
    client.gate[599006] = asyncio.Event()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await writer.tick()
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.writes == [(599005, AV3, 60.0, 12)]

    await writer.tick()  # 599006 still in flight: not started twice
    client.gate[599006].set()
    await writer.idle()
    assert client.writes.count((599006, AV3, 60.0, 12)) == 1
    app.close()


@pytest.mark.asyncio
async def test_null_present_value_relinquishes_at_the_priority(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    _sched(app).presentValue = AnyAtomic(Null(()))
    await _run(writer)

    assert client.writes[-1] == (599005, AV3, None, 12)
    app.close()


@pytest.mark.asyncio
async def test_binary_schedule_sends_its_integer(session_factory, clock):
    _setup(
        session_factory,
        [_remote(object_type="binary-value")],
        value_type="binary",
        default="inactive",
        value="active",
    )
    app = await _app(session_factory)
    client = FakeClient()
    sent = []
    real_write = client.write

    async def capturing_write(binding, point, value, *, priority, timeout, native=None):
        sent.append(value)
        await real_write(binding, point, value, priority=priority, timeout=timeout, native=native)

    client.write = capturing_write
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    assert sent == [1]
    assert type(sent[0]) is int
    app.close()


@pytest.mark.asyncio
async def test_replaced_reference_list_drops_old_target_and_sends_new(session_factory, clock):
    object_id = _setup(session_factory, [_remote(599005)])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    _sched(app).listOfObjectPropertyReferences = build_schedule_write_targets([_remote(599006)])
    await _run(writer)

    assert (599006, AV3, 60.0, 12) in client.writes  # and 599005 released
    assert set(_status(session_factory, object_id)) == {KEY_6}
    app.close()


@pytest.mark.asyncio
async def test_a_send_in_flight_when_its_target_is_removed_leaves_no_status(session_factory, clock):
    # The late result used to re-create the entry.
    object_id = _setup(session_factory, [_remote(599005)])
    app = await _app(session_factory)
    client = FakeClient()
    client.gate[599005] = asyncio.Event()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await writer.tick()  # send to 599005 starts and waits on the gate
    _sched(app).listOfObjectPropertyReferences = build_schedule_write_targets([])
    await writer.tick()  # target gone: status and state pruned
    client.gate[599005].set()
    await writer.idle()

    assert KEY_5 not in (_status(session_factory, object_id) or {})
    app.close()


@pytest.mark.asyncio
async def test_two_schedules_on_the_same_remote_point_are_tracked_separately(session_factory, clock):
    _setup(
        session_factory,
        [_remote()],
        extra=[
            dict(
                object_type="schedule",
                instance_number=2,
                name="Sched-2",
                schedule_value_type="analog",
                schedule_default="1.0",
                weekly_schedule=[[{"time": "00:00", "value": "20.0"}] for _ in range(7)],
                schedule_priority=13,
                schedule_write_targets=[_remote()],
            )
        ],
    )
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    assert sorted(client.writes, key=lambda w: w[3]) == [(599005, AV3, 60.0, 12), (599005, AV3, 20.0, 13)]
    _set_pv(_sched(app, 2), 25.0)
    await _run(writer)
    assert client.writes[-1] == (599005, AV3, 25.0, 13)
    assert len(client.writes) == 3
    app.close()


@pytest.mark.asyncio
async def test_own_device_reference_is_left_to_the_local_path(session_factory, clock):
    _setup(
        session_factory,
        [_remote(599001)],
        extra=[dict(object_type="analog-value", instance_number=3, name="AV-3")],
    )
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)
    assert client.writes == []
    app.close()


@pytest.mark.asyncio
async def test_status_saved_only_on_change_and_stale_status_cleared(session_factory, clock, monkeypatch):
    object_id = _setup(session_factory, [_remote()])
    with session_factory() as session:
        other = BacnetObject(
            device_id=599001,
            object_type="schedule",
            instance_number=2,
            name="Sched-2",
            schedule_value_type="analog",
            schedule_default="1.0",
            weekly_schedule=[[{"time": "00:00", "value": "2.0"}] for _ in range(7)],
            runtime=ObjectRuntime(schedule_write_status={"old:analog-value:1:present-value": {"error": "x"}}),
        )
        session.add(other)
        session.commit()
        other_id = other.id
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    saves = []
    real_save = writer._save_status

    async def counting_save(oid):
        saves.append(oid)
        await real_save(oid)

    monkeypatch.setattr(writer, "_save_status", counting_save)

    await _run(writer)
    assert _status(session_factory, other_id) is None  # no external targets now: cleared
    first = list(saves)
    assert object_id in first and other_id in first

    saves.clear()
    await _run(writer)
    await _run(writer)
    assert saves == []
    app.close()


@pytest.mark.asyncio
async def test_a_restart_keeps_the_last_delivery_in_the_status(session_factory, clock):
    # Found live: restarting the device with the remote down
    # replaced "sent 55 at ..." with a bare error, forgetting what the
    # remote was last given.
    object_id = _setup(session_factory, [_remote()])
    earlier = {"last_sent_at": "2026-09-29T12:00:00+00:00", "last_sent_value": 55.0, "error": None, "suppressed_by_dcc": False}
    with session_factory() as session:
        session.get(BacnetObject, object_id).runtime.schedule_write_status = {KEY_5: earlier}
        session.commit()
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = ClientError("device 599005 did not answer Who-Is", kind="transport")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    entry = _status(session_factory, object_id)[KEY_5]
    assert entry["error"] == "device 599005 did not answer Who-Is"
    assert entry["last_sent_value"] == 55.0
    assert entry["last_sent_at"] == "2026-09-29T12:00:00+00:00"
    app.close()


@pytest.mark.asyncio
async def test_an_unexpected_write_exception_fails_only_that_target(session_factory, clock):
    # e.g. bacpypes3 can't build the request for a constructed-type property
    # and raises AttributeError, not ClientError.
    object_id = _setup(session_factory, [_remote(), _remote(instance_number=4)])
    app = await _app(session_factory)
    client = FakeClient()
    client.write_fail[(599005, AV3)] = AttributeError("objectIdentifier is a required element")
    attempts = []
    real_write = client.write

    async def counting_write(binding, point, value, *, priority, timeout, native=None):
        attempts.append(point)
        await real_write(binding, point, value, priority=priority, timeout=timeout, native=native)

    client.write = counting_write
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    assert client.writes == [(599005, Point("analog-value", 4), 60.0, 12)]
    assert "objectIdentifier is a required element" in _status(session_factory, object_id)[KEY_5]["error"]
    clock["t"] += 1.0
    await _run(writer)
    assert attempts.count(AV3) == 1  # backing off, not retried every tick
    app.close()


@pytest.mark.asyncio
async def test_an_unexpected_bind_exception_backs_off_the_group(session_factory, clock):
    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.bind_fail[599005] = RuntimeError("boom")
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    assert _status(session_factory, object_id)[KEY_5]["error"] == "boom"
    del client.bind_fail[599005]
    clock["t"] += 1.0
    await _run(writer)
    assert client.writes == []  # still inside the 5 s backoff
    app.close()


@pytest.mark.asyncio
async def test_a_failed_older_send_does_not_back_off_a_newer_value(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    client.gate[599005] = asyncio.Event()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await writer.tick()  # 60 in flight, waiting on bind
    _set_pv(_sched(app), 70.0)
    await writer.tick()  # device busy: 70 not started, but due at once
    client.bind_fail[599005] = ClientError("timeout", kind="transport")
    client.gate[599005].set()
    await writer.idle()  # the 60 send fails

    del client.bind_fail[599005]
    clock["t"] += 1.0
    await _run(writer)
    assert client.writes == [(599005, AV3, 70.0, 12)]
    app.close()


@pytest.mark.asyncio
async def test_writer_passes_the_schedules_own_type_as_native(session_factory, clock):
    # Used only when the remote point has no standard datatype.
    _setup(session_factory, [_remote(object_type="200")])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    [native] = client.natives
    assert isinstance(native, Real) and float(native) == 60.0
    app.close()


@pytest.mark.asyncio
async def test_binary_schedule_native_is_enumerated(session_factory, clock):
    _setup(session_factory, [_remote(object_type="200")], value_type="binary", default="inactive", value="active")
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    [native] = client.natives
    assert isinstance(native, Enumerated) and int(native) == 1
    app.close()


@pytest.mark.asyncio
async def test_an_out_of_season_schedule_sends_nothing_until_it_comes_into_season(session_factory, clock):
    # Its held Schedule_Default could win a remote slot over
    # an in-season sibling's value. Local targets get the same check.
    _setup(
        session_factory,
        [_remote()],
        schedule_effective_start="2099-01-01",
        schedule_effective_end="2099-12-31",
    )
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)
    assert client.writes == []

    _sched(app)._in_effective_period = True  # the season starts
    await _run(writer)
    assert client.writes == [(599005, AV3, 10.0, 12)]
    app.close()


@pytest.mark.asyncio
async def test_coming_back_into_season_resends_the_same_value(session_factory, clock):
    # While it was out of season a sibling may have
    # written the shared remote slot, so coming back with the value it
    # delivered before must still send it (local targets do the same).
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)
    assert client.writes == [(599005, AV3, 60.0, 12)]

    _sched(app)._in_effective_period = False  # the season ends
    await _run(writer)
    _sched(app)._in_effective_period = True  # and starts again
    await _run(writer)

    assert client.writes == [(599005, AV3, 60.0, 12), (599005, AV3, 60.0, 12)]
    app.close()


# --- releasing the slots a schedule stops using, best effort ----------


@pytest.mark.asyncio
async def test_a_priority_change_releases_the_old_remote_slot(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    _sched(app).priorityForWriting = Unsigned(9)
    await _run(writer)

    assert (599005, AV3, 60.0, 9) in client.writes
    assert (599005, AV3, None, 12) in client.writes
    await _run(writer)
    assert len(client.writes) == 3  # released once, not every tick
    app.close()


@pytest.mark.asyncio
async def test_a_removed_remote_target_is_released(session_factory, clock):
    _setup(session_factory, [_remote(599005)])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    _sched(app).listOfObjectPropertyReferences = build_schedule_write_targets([_remote(599006)])
    await _run(writer)

    assert (599005, AV3, None, 12) in client.writes
    assert (599006, AV3, 60.0, 12) in client.writes
    app.close()


@pytest.mark.asyncio
async def test_a_deleted_schedule_releases_its_remote_slots(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    app.delete_object(_sched(app))
    await _run(writer)

    assert client.writes == [(599005, AV3, 60.0, 12), (599005, AV3, None, 12)]
    app.close()


@pytest.mark.asyncio
async def test_a_failed_release_is_retried_after_the_backoff(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    client.write_fail[(599005, AV3)] = ClientError("no answer", kind="transport")
    app.delete_object(_sched(app))
    await _run(writer)
    del client.write_fail[(599005, AV3)]
    clock["t"] += 4.9
    await _run(writer)
    assert client.writes == [(599005, AV3, 60.0, 12)]  # still backing off

    clock["t"] += 0.2
    await _run(writer)
    assert client.writes[-1] == (599005, AV3, None, 12)
    app.close()


@pytest.mark.asyncio
async def test_a_sibling_on_the_same_remote_slot_resends_instead_of_a_release(session_factory, clock):
    sibling = dict(
        object_type="schedule", instance_number=2, name="Sched-2", schedule_value_type="analog",
        schedule_default="1.0", weekly_schedule=[[{"time": "00:00", "value": "20.0"}] for _ in range(7)],
        schedule_priority=12, schedule_write_targets=[_remote()],
    )
    _setup(session_factory, [_remote()], extra=[sibling])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)
    sent = len(client.writes)

    app.delete_object(_sched(app, 1))
    await _run(writer)  # the release is dropped; Sched-2 re-sends next tick
    await _run(writer)

    assert client.writes[sent:] == [(599005, AV3, 20.0, 12)]
    app.close()


@pytest.mark.asyncio
async def test_a_schedule_that_never_delivered_releases_nothing(session_factory, clock):
    _setup(session_factory, [_remote()], schedule_effective_start="2099-01-01", schedule_effective_end="2099-12-31")
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    app.delete_object(_sched(app))
    await _run(writer)

    assert client.writes == []
    app.close()


# --- Release follow-ups ------------------------------------------------------

_SIBLING = dict(
    object_type="schedule", instance_number=2, name="Sched-2", schedule_value_type="analog",
    schedule_default="1.0", weekly_schedule=[[{"time": "00:00", "value": "20.0"}] for _ in range(7)],
    schedule_priority=12, schedule_write_targets=[_remote()],
)


@pytest.mark.asyncio
async def test_a_refused_release_is_dropped_after_one_warning(session_factory, clock, caplog):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    client.write_fail[(599005, AV3)] = ClientError("object: unknown-object")
    app.delete_object(_sched(app))
    await _run(writer)
    assert writer._releases == {}
    del client.write_fail[(599005, AV3)]
    clock["t"] += 600.0
    await _run(writer)
    assert client.writes == [(599005, AV3, 60.0, 12)]  # never retried
    assert caplog.text.count("not retried") == 1
    app.close()


@pytest.mark.asyncio
async def test_a_claimant_whose_write_is_refused_lets_the_release_go(session_factory, clock):
    _setup(session_factory, [_remote()], extra=[_SIBLING])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)
    sent = len(client.writes)

    app.delete_object(_sched(app, 1))
    await _run(writer)  # the release is held; Sched-2 re-sends next tick
    client.write_fail[(599005, AV3)] = ClientError("property: value-out-of-range")
    await _run(writer)  # Sched-2's write is refused
    del client.write_fail[(599005, AV3)]
    await _run(writer)  # so the held release goes out
    assert client.writes[sent:] == [(599005, AV3, None, 12)]
    app.close()


@pytest.mark.asyncio
async def test_a_dcc_suppressed_release_is_retried_every_tick(session_factory, clock):
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)

    client.bind_fail[599005] = ClientError("suppressed by DCC (disableInitiation)", kind="dcc")
    app.delete_object(_sched(app))
    await _run(writer)
    (release,) = writer._releases.values()
    assert release.next_attempt_at == 0.0 and release.backoff == 0.0  # no backoff

    del client.bind_fail[599005]
    clock["t"] += 1.0
    await _run(writer)
    assert client.writes[-1] == (599005, AV3, None, 12)
    app.close()


@pytest.mark.asyncio
async def test_a_stopped_writer_sends_no_release(session_factory, clock, monkeypatch):
    monkeypatch.setattr(schedule_external, "WRITER_TICK_SECONDS", 0.01)
    _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    writer.start()
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert client.writes == [(599005, AV3, 60.0, 12)]
    writer.stop()  # what a device stop does
    app.delete_object(_sched(app))
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert client.writes == [(599005, AV3, 60.0, 12)]
    app.close()


@pytest.mark.asyncio
async def test_a_claimant_takes_the_slot_over_through_a_live_apply(session_factory, clock):
    _setup(session_factory, [_remote()], extra=[_SIBLING])
    with session_factory() as session:
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()
    app = await _app(session_factory)
    device, rows, *_ = _load_device_config(session_factory, 599001)
    reloader = ObjectReloader(session_factory, app, device, rows)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)
    await _run(writer)
    sent = len(client.writes)

    with session_factory() as session:
        session.query(BacnetObject).filter_by(name="Sched-1").delete()
        session.commit()
    await reloader.apply(1)
    await _run(writer)
    await _run(writer)
    assert client.writes[sent:] == [(599005, AV3, 20.0, 12)]
    assert writer._releases == {}
    app.close()


@pytest.mark.asyncio
async def test_schedule_status_saves_off_the_loop(session_factory, clock):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=FakeClient())

    run = await run_while_locked(session_factory, lambda: _run(writer))

    assert_waited_off_the_loop(run)
    assert _status(session_factory, object_id)[KEY_5]["last_sent_value"] == 60.0
    app.close()


@pytest.mark.asyncio
async def test_a_failed_schedule_status_save_is_retried(session_factory, clock):
    from tests.session_fakes import CommitFailingSessionFactory

    object_id = _setup(session_factory, [_remote()])
    app = await _app(session_factory)
    writer = ExternalScheduleWriter(CommitFailingSessionFactory(session_factory), app, 599001, client=FakeClient())

    await _run(writer)
    assert _status(session_factory, object_id) is None  # the save failed

    writer.session_factory = session_factory
    await _run(writer)  # nothing changed, but the last save never landed

    assert _status(session_factory, object_id)[KEY_5]["last_sent_value"] == 60.0
    app.close()


@pytest.mark.asyncio
async def test_a_null_default_outside_the_scheduled_times_relinquishes_the_remote_target(session_factory, clock):
    object_id = _setup(session_factory, [_remote()], default=None)
    with session_factory() as session:
        session.get(BacnetObject, object_id).weekly_schedule = [[] for _ in range(7)]
        session.commit()
    app = await _app(session_factory)
    assert isinstance(_sched(app).presentValue.get_value(), Null)
    client = FakeClient()
    writer = ExternalScheduleWriter(session_factory, app, 599001, client=client)

    await _run(writer)

    assert client.writes == [(599005, AV3, None, 12)]
    app.close()
