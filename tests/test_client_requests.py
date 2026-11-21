"""ClientRequestPoller -- the web UI's discovery/ad-hoc BACnet
client requests, answered inside the device's own process."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from bacpypes3.basetypes import PriorityArray, PriorityValue
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import CharacterString, Null, ObjectIdentifier, Real, Unsigned

from bacpack import client_requests
from bacpack.bacnet_client import Binding, ClientError, Point
from bacpack.client_requests import ClientRequestPoller, prune_old_client_requests
from bacpack.models import BacnetObject, ClientRequest, Device
from bacpack.object_builder import build_application


class FakeClient:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.unbound = []
        self.found = []
        self.bind_error = None

    async def who_is(self, low=None, high=None, *, window):
        self.who_is_args = (low, high, window)
        return self.found

    async def bind(self, instance, static_address=None, *, timeout):
        if self.bind_error is not None:
            raise self.bind_error
        return Binding(Address(static_address or "10.0.0.2"))

    def unbind(self, instance, static_address=None):
        self.unbound.append(instance)

    async def read(self, binding, point, *, timeout):
        value = self.values[point]
        if isinstance(value, ClientError):
            raise value
        return value

    async def read_many(self, binding, points, *, timeout):
        return {p: self.values.get(p, ClientError("unknown")) for p in points}

    async def write(self, binding, point, value, *, priority, timeout):
        self.writes.append((point, value, priority))


def _add_request(session_factory, kind, params, status="pending"):
    with session_factory() as session:
        if session.get(Device, 1) is None:
            session.add(Device(id=1, name="D"))
        row = ClientRequest(device_id=1, kind=kind, params=params, status=status)
        session.add(row)
        session.commit()
        return row.id


def _row(session_factory, request_id):
    with session_factory() as session:
        row = session.get(ClientRequest, request_id)
        return {"status": row.status, "result": row.result, "error": row.error, "finished_at": row.finished_at}


async def _answer(poller):
    tasks = await poller.claim_and_start()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_who_is_request_is_answered(session_factory):
    client = FakeClient()
    client.found = [{"instance": 5, "address": "10.0.0.5", "vendor_id": 1, "max_apdu": 1476, "segmentation": "no-segmentation"}]
    request_id = _add_request(session_factory, "who-is", {"low": 1, "high": 10, "window_seconds": 2.0})

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    row = _row(session_factory, request_id)
    assert row["status"] == "done"
    assert row["result"] == client.found
    assert row["finished_at"] is not None
    assert client.who_is_args == (1, 10, 2.0)


@pytest.mark.asyncio
async def test_read_request_returns_a_json_value(session_factory):
    client = FakeClient()
    client.values[Point("analog-input", 3)] = Real(72.5)
    request_id = _add_request(
        session_factory,
        "read",
        {"instance": 2, "timeout_seconds": 1, "object_type": "analog-input", "object_instance": 3,
         "property": "present-value", "array_index": None},
    )

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, request_id)["result"] == {"value": 72.5}


@pytest.mark.asyncio
async def test_read_of_a_schedule_present_value_and_a_priority_array(session_factory):
    # A Schedule's Present_Value arrives AnyAtomic-wrapped,
    # and each priority-array slot is a Choice (PriorityValue).
    client = FakeClient()
    client.values[Point("schedule", 1)] = AnyAtomic(Real(21.5))
    client.values[Point("analog-value", 4, "priority-array")] = PriorityArray(
        [PriorityValue(null=Null(()))] * 8 + [PriorityValue(real=Real(17.0))] + [PriorityValue(null=Null(()))] * 7
    )
    params = {"instance": 2, "timeout_seconds": 1, "array_index": None}
    schedule_id = _add_request(
        session_factory, "read",
        {**params, "object_type": "schedule", "object_instance": 1, "property": "present-value"},
    )
    array_id = _add_request(
        session_factory, "read",
        {**params, "object_type": "analog-value", "object_instance": 4, "property": "priority-array"},
    )

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, schedule_id)["result"] == {"value": 21.5}
    assert _row(session_factory, array_id)["result"] == {"value": [None] * 8 + [17.0] + [None] * 7}


@pytest.mark.asyncio
async def test_write_request_sends_value_and_priority(session_factory):
    client = FakeClient()
    request_id = _add_request(
        session_factory,
        "write",
        {"instance": 2, "address": "10.0.0.9", "timeout_seconds": 1, "object_type": "analog-value",
         "object_instance": 1, "property": "present-value", "array_index": None, "value": 5, "priority": 8},
    )

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, request_id)["result"] == {"ok": True}
    assert client.writes == [(Point("analog-value", 1), 5, 8)]


@pytest.mark.asyncio
async def test_object_list_names_each_object(session_factory):
    client = FakeClient()
    client.values = {
        Point("device", 2, "object-list"): [ObjectIdentifier("device,2"), ObjectIdentifier("analog-input,1")],
        Point("device", 2, "object-name"): CharacterString("Remote"),
        Point("analog-input", 1, "object-name"): CharacterString("Zone temp"),
    }
    request_id = _add_request(session_factory, "object-list", {"instance": 2, "timeout_seconds": 1})

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, request_id)["result"] == [
        {"object_type": "device", "instance": 2, "name": "Remote"},
        {"object_type": "analog-input", "instance": 1, "name": "Zone temp"},
    ]


@pytest.mark.asyncio
async def test_object_list_too_big_is_read_one_index_at_a_time(session_factory):
    client = FakeClient()
    client.values = {
        Point("device", 2, "object-list"): ClientError("abort: segmentation-not-supported", kind="too-big"),
        Point("device", 2, "object-list", 0): Unsigned(1),
        Point("device", 2, "object-list", 1): ObjectIdentifier("device,2"),
    }
    request_id = _add_request(session_factory, "object-list", {"instance": 2, "timeout_seconds": 1})

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, request_id)["result"] == [{"object_type": "device", "instance": 2, "name": None}]


@pytest.mark.asyncio
async def test_client_error_is_saved_and_transport_errors_unbind(session_factory):
    client = FakeClient()
    client.values[Point("analog-input", 3)] = ClientError("no response within 1 s", kind="transport")
    request_id = _add_request(
        session_factory,
        "read",
        {"instance": 2, "timeout_seconds": 1, "object_type": "analog-input", "object_instance": 3,
         "property": "present-value", "array_index": None},
    )

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    row = _row(session_factory, request_id)
    assert (row["status"], row["error"]) == ("error", "no response within 1 s")
    assert client.unbound == [2]


@pytest.mark.asyncio
async def test_each_request_is_claimed_once(session_factory):
    _add_request(session_factory, "who-is", {"window_seconds": 1})
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())

    first = await poller.claim_and_start()
    second = await poller.claim_and_start()
    await asyncio.gather(*first)

    assert (len(first), len(second)) == (1, 0)


@pytest.mark.asyncio
async def test_a_late_answer_does_not_overwrite_the_apis_timeout(session_factory):
    client = FakeClient()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_who_is(low=None, high=None, *, window):
        started.set()
        await release.wait()
        return []

    client.who_is = slow_who_is
    request_id = _add_request(session_factory, "who-is", {"window_seconds": 1})
    tasks = await ClientRequestPoller(session_factory, 1, client=client).claim_and_start()
    await started.wait()

    with session_factory() as session:
        row = session.get(ClientRequest, request_id)
        row.status, row.error = "error", "timed out"
        session.commit()
    release.set()
    await asyncio.gather(*tasks)

    assert _row(session_factory, request_id)["error"] == "timed out"


def _bind_while_the_api_gives_up(client, session_factory, request_id):
    # The API's 504 marks the row while the device process is still binding.
    real_bind = client.bind

    async def bind(instance, static_address=None, *, timeout):
        with session_factory() as session:
            row = session.get(ClientRequest, request_id)
            row.status, row.error = "error", "timed out"
            session.commit()
        return await real_bind(instance, static_address, timeout=timeout)

    client.bind = bind


@pytest.mark.asyncio
async def test_a_write_the_api_gave_up_on_during_the_bind_is_not_sent(session_factory):
    # A slow bind used to be followed by the write anyway, after the
    # API had already answered 504.
    client = FakeClient()
    request_id = _add_request(
        session_factory,
        "write",
        {"instance": 2, "timeout_seconds": 1, "object_type": "analog-value", "object_instance": 1,
         "property": "present-value", "array_index": None, "value": 5, "priority": 8},
    )
    _bind_while_the_api_gives_up(client, session_factory, request_id)

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert client.writes == []
    assert _row(session_factory, request_id)["error"] == "timed out"


@pytest.mark.asyncio
async def test_a_read_the_api_gave_up_on_during_the_bind_still_reads(session_factory):
    # Only writes are held back: a late read changes nothing remote.
    client = FakeClient()
    point = Point("analog-value", 1, "present-value", None)
    client.values[point] = Real(1.5)
    reads = []
    real_read = client.read

    async def read(binding, p, *, timeout):
        reads.append(p)
        return await real_read(binding, p, timeout=timeout)

    client.read = read
    request_id = _add_request(
        session_factory,
        "read",
        {"instance": 2, "timeout_seconds": 1, "object_type": "analog-value", "object_instance": 1,
         "property": "present-value", "array_index": None},
    )
    _bind_while_the_api_gives_up(client, session_factory, request_id)

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert reads == [point]
    assert _row(session_factory, request_id)["error"] == "timed out"


@pytest.mark.asyncio
async def test_other_devices_requests_are_left_alone(session_factory):
    with session_factory() as session:
        session.add_all([Device(id=1, name="D"), Device(id=2, name="E")])
        session.add(ClientRequest(device_id=2, kind="who-is", params={"window_seconds": 1}))
        session.commit()

    assert await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start() == []


def test_prune_old_client_requests(session_factory):
    old = _add_request(session_factory, "who-is", {})
    new = _add_request(session_factory, "who-is", {})
    with session_factory() as session:
        session.get(ClientRequest, old).created_at = dt.datetime(2020, 1, 1)
        session.commit()

    assert prune_old_client_requests(session_factory) == 1
    with session_factory() as session:
        assert [r.id for r in session.query(ClientRequest)] == [new]


@pytest.mark.asyncio
async def test_live_request_is_answered_from_the_devices_own_objects(session_factory):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        session.commit()
        device = session.get(Device, 1)
        app = build_application(device, list(device.objects))
    client = FakeClient()
    poller = ClientRequestPoller(session_factory, 1, client=client, app=app)
    request_id = _add_request(session_factory, "live", {"object_ids": None})

    await _answer(poller)

    row = _row(session_factory, request_id)
    assert row["status"] == "done", row["error"]
    assert [e["object_type"] for e in row["result"]["objects"]] == ["analog-value"]
    assert client.writes == [] and client.unbound == []


@pytest.mark.asyncio
async def test_a_kind_without_an_instance_fails_the_way_older_device_processes_answer_live(session_factory):
    # Before the "live" kind existed, execute() read params["instance"]
    # first; the live API matches this exact text (web/api/live.py).
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())
    request_id = _add_request(session_factory, "not-a-kind", {"object_ids": None})

    await _answer(poller)

    assert _row(session_factory, request_id)["error"] == "internal error: 'instance'"


@pytest.mark.asyncio
async def test_identify_reads_the_remote_devices_name_and_vendor(session_factory):
    client = FakeClient()
    client.values = {
        Point("device", 2, "object-name"): CharacterString("AHU-1 controller"),
        Point("device", 2, "vendor-name"): CharacterString("Example Controls"),
    }
    request_id = _add_request(session_factory, "identify", {"instance": 2, "address": "10.0.0.2", "timeout_seconds": 1})

    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    row = _row(session_factory, request_id)
    assert row["status"] == "done"
    assert row["result"] == {"name": "AHU-1 controller", "vendor_name": "Example Controls"}


@pytest.mark.asyncio
async def test_identify_leaves_out_a_property_the_device_refuses(session_factory):
    client = FakeClient()
    client.values = {Point("device", 2, "object-name"): CharacterString("Router")}  # vendor-name -> ClientError

    request_id = _add_request(session_factory, "identify", {"instance": 2, "timeout_seconds": 1})
    await _answer(ClientRequestPoller(session_factory, 1, client=client))

    assert _row(session_factory, request_id)["result"] == {"name": "Router", "vendor_name": None}


@pytest.mark.asyncio
async def test_a_claim_waits_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    request_id = _add_request(session_factory, "who-is", {"window_seconds": 1})
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())

    run = await run_while_locked(session_factory, poller.claim_and_start, history=True)

    assert_waited_off_the_loop(run)
    assert len(run.result) == 1
    await asyncio.gather(*run.result)
    assert _row(session_factory, request_id)["status"] == "done"


@pytest.mark.asyncio
async def test_an_answer_saves_off_the_loop(session_factory):
    from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

    client = FakeClient()
    gate = asyncio.Event()

    async def gated_who_is(low=None, high=None, *, window):
        await gate.wait()  # the lock is taken before the request finishes
        return []

    client.who_is = gated_who_is
    request_id = _add_request(session_factory, "who-is", {"window_seconds": 1})
    tasks = await ClientRequestPoller(session_factory, 1, client=client).claim_and_start()

    async def finish():
        gate.set()
        await asyncio.gather(*tasks)

    run = await run_while_locked(session_factory, finish, history=True)

    assert_waited_off_the_loop(run)
    assert _row(session_factory, request_id)["status"] == "done"


# --- The clone read job -----------------------------------------------------

_CLONE_PARAMS = {"instance": 2, "timeout_seconds": 1}


async def _until_row(session_factory, request_id, condition, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        row = _row_with_progress(session_factory, request_id)
        if condition(row):
            return row
        assert loop.time() < deadline, f"timed out waiting; row is {row}"
        await asyncio.sleep(0.01)


def _row_with_progress(session_factory, request_id):
    with session_factory() as session:
        row = session.get(ClientRequest, request_id)
        return {
            "status": row.status, "result": row.result, "error": row.error,
            "progress": row.progress, "cancel_requested": row.cancel_requested,
        }


@pytest.mark.asyncio
async def test_clone_job_reports_progress_and_finishes(session_factory, monkeypatch):
    release = asyncio.Event()

    async def fake(client, binding, instance, timeout, report, cancelled):
        assert instance == 2
        report(1, 3)
        await release.wait()
        report(3, 3)
        return {"objects": []}

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()

    row = await _until_row(session_factory, request_id, lambda r: r["progress"] == {"phase": "objects", "done": 1, "total": 3})
    assert row["status"] == "running"
    release.set()
    await asyncio.gather(*tasks)

    row = _row_with_progress(session_factory, request_id)
    assert row["status"] == "done" and row["result"] == {"objects": []}
    assert row["progress"] == {"phase": "objects", "done": 3, "total": 3}


@pytest.mark.asyncio
async def test_progress_is_saved_at_most_every_half_second(session_factory, monkeypatch):
    release = asyncio.Event()
    saved = []
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())
    real = poller._save_progress

    def counting(request_id, done, total, phase="objects"):
        saved.append((done, total))
        return real(request_id, done, total, phase)

    monkeypatch.setattr(poller, "_save_progress", counting)

    async def fake(client, binding, instance, timeout, report, cancelled):
        for done in range(1, 101):
            report(done, 100)
        await release.wait()
        return {}

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await poller.claim_and_start()
    await asyncio.sleep(0.1)
    release.set()
    await asyncio.gather(*tasks)

    assert len(saved) <= 3  # the first report, the last one at the end
    assert saved[-1] == (100, 100)


@pytest.mark.asyncio
async def test_cancel_requested_ends_the_job_as_cancelled(session_factory, monkeypatch):
    monkeypatch.setattr(client_requests, "CLONE_PROGRESS_SECONDS", 0.0)

    async def fake(client, binding, instance, timeout, report, cancelled):
        done = 0
        while not cancelled():
            done += 1
            report(done, 1000)
            await asyncio.sleep(0.01)
        raise ClientError("cancelled")

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await _until_row(session_factory, request_id, lambda r: r["progress"] is not None)

    with session_factory() as session:
        session.get(ClientRequest, request_id).cancel_requested = True
        session.commit()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)

    row = _row_with_progress(session_factory, request_id)
    assert (row["status"], row["error"]) == ("error", "cancelled")


@pytest.mark.asyncio
async def test_a_job_whose_row_was_deleted_is_cancelled(session_factory, monkeypatch):
    monkeypatch.setattr(client_requests, "CLONE_PROGRESS_SECONDS", 0.0)

    async def fake(client, binding, instance, timeout, report, cancelled):
        while not cancelled():
            report(1, 2)
            await asyncio.sleep(0.01)
        raise ClientError("cancelled")

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await _until_row(session_factory, request_id, lambda r: r["progress"] is not None)
    with session_factory() as session:
        session.delete(session.get(ClientRequest, request_id))
        session.commit()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)


@pytest.mark.asyncio
async def test_a_failed_read_ends_the_job_with_the_error(session_factory, monkeypatch):
    async def fake(client, binding, instance, timeout, report, cancelled):
        raise ClientError("no answer", kind="transport")

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await asyncio.gather(*tasks)
    row = _row_with_progress(session_factory, request_id)
    assert (row["status"], row["error"]) == ("error", "no answer")


@pytest.mark.asyncio
async def test_stale_clone_jobs_are_failed_at_poller_start(session_factory):
    stale = _add_request(session_factory, "clone-read", _CLONE_PARAMS, status="running")
    other_kind = _add_request(session_factory, "read", {}, status="running")
    finished = _add_request(session_factory, "clone-read", _CLONE_PARAMS, status="done")
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())

    await poller.claim_and_start()

    row = _row_with_progress(session_factory, stale)
    assert (row["status"], row["error"]) == ("error", "the device restarted before the read finished")
    assert _row_with_progress(session_factory, other_kind)["status"] == "running"
    assert _row_with_progress(session_factory, finished)["status"] == "done"

    # Only the first tick sweeps: a job claimed since is left alone.
    running = _add_request(session_factory, "clone-read", _CLONE_PARAMS, status="running")
    await poller.claim_and_start()
    assert _row_with_progress(session_factory, running)["status"] == "running"


@pytest.mark.asyncio
async def test_a_progress_save_does_not_overwrite_a_finished_row(session_factory):
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS, status="done")
    poller = ClientRequestPoller(session_factory, 1, client=FakeClient())

    # A row no longer running has no one waiting: the save reads as cancelled.
    assert poller._save_progress(request_id, 5, 9) is True
    assert _row_with_progress(session_factory, request_id)["progress"] is None


@pytest.mark.asyncio
async def test_a_clone_job_is_not_held_to_a_request_budget(session_factory, monkeypatch):
    async def fake(client, binding, instance, timeout, report, cancelled):
        await asyncio.sleep(0.05)
        return {"objects": []}

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", {"instance": 2, "timeout_seconds": 1})
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await asyncio.gather(*tasks)
    assert _row_with_progress(session_factory, request_id)["status"] == "done"


@pytest.mark.asyncio
async def test_a_clone_job_cancelled_before_it_was_claimed_never_starts_reading(session_factory, monkeypatch):
    calls = []

    async def fake(client, binding, instance, timeout, report, cancelled):
        calls.append(instance)
        return {}

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    with session_factory() as session:
        session.get(ClientRequest, request_id).cancel_requested = True
        session.commit()

    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await asyncio.gather(*tasks)

    assert calls == []
    row = _row(session_factory, request_id)
    assert row["status"] == "error" and row["error"] == "cancelled" and row["finished_at"] is not None


@pytest.mark.asyncio
async def test_the_saved_progress_carries_the_phase(session_factory, monkeypatch):
    release = asyncio.Event()

    async def fake(client, binding, instance, timeout, report, cancelled):
        report(2, 9, "object-list")
        await release.wait()
        return {}

    monkeypatch.setattr(client_requests.clone_reader, "read_controller", fake)
    request_id = _add_request(session_factory, "clone-read", _CLONE_PARAMS)
    tasks = await ClientRequestPoller(session_factory, 1, client=FakeClient()).claim_and_start()
    await _until_row(
        session_factory, request_id, lambda r: r["progress"] == {"phase": "object-list", "done": 2, "total": 9}
    )
    release.set()
    await asyncio.gather(*tasks)
