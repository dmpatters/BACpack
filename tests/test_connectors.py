"""Covers pull connectors (HTTP poll).
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest

from bacpack import connectors, timeutil
from bacpack.connectors import (
    ConnectorPoller,
    coerce_mapped_value,
    extract_json_path,
    flatten_json_fields,
)
from bacpack.models import BacnetObject, Connector, ConnectorMapping, Device
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked


def _make_connector(session_factory, *, url="http://example.invalid/status", **connector_kwargs):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        device.runtime.process_status = "running"
        obj = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1"
        )
        connector = Connector(name="Test Connector", url=url, **connector_kwargs)
        mapping = ConnectorMapping(connector=connector, object=obj, json_path="data.temp")
        session.add_all([device, obj, connector, mapping])
        session.commit()
        return connector.id, obj.id, mapping.id


def _make_connector_for_input(session_factory, *, url="http://example.invalid/status", **connector_kwargs):
    # Same shape as _make_connector, but the mapping targets an Input object
    # (no priority array) rather than an analog-value -- create_mapping
    # (web/api/connectors.py) has always allowed this; it's ordinary config, not an
    # exotic one. See the relinquish-against-a-non-commandable-target tests
    # below.
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        device.runtime.process_status = "running"
        obj = BacnetObject(
            device=device, object_type="analog-input", instance_number=1, name="AI-1"
        )
        connector = Connector(name="Test Connector", url=url, **connector_kwargs)
        mapping = ConnectorMapping(connector=connector, object=obj, json_path="data.temp")
        session.add_all([device, obj, connector, mapping])
        session.commit()
        return connector.id, obj.id, mapping.id


def _feed(session_factory, object_id):
    with session_factory() as session:
        runtime = session.get(BacnetObject, object_id).runtime
        return runtime.pending_feed_value, runtime.pending_feed_queued


async def _until(condition, timeout=3.0):
    # A polled value is queued from a worker thread: wait for the row
    # rather than for a fixed time.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


# --- pure helpers ------------------------------------------------------


def test_extract_json_path_nested():
    assert extract_json_path({"data": {"temp": 72.5}}, "data.temp") == 72.5


def test_extract_json_path_missing_key_raises():
    with pytest.raises(KeyError):
        extract_json_path({"data": {}}, "data.temp")


def test_extract_json_path_non_dict_raises():
    with pytest.raises(KeyError):
        extract_json_path({"data": 5}, "data.temp")


def test_coerce_mapped_value_scale_offset():
    assert coerce_mapped_value(10, scale=2.0, offset=1.0, value_map=None) == 21.0


def test_coerce_mapped_value_uses_value_map_before_scale():
    value = coerce_mapped_value("open", scale=1.0, offset=0.0, value_map={"open": 1, "closed": 0})
    assert value == 1.0


def test_coerce_mapped_value_json_booleans_match_lowercase_keys():
    # JSON true/false arrive as Python bools; str(True) is "True", so a map
    # written the way the JSON reads ("true") never matched.
    value_map = {"true": 1, "false": 0}
    assert coerce_mapped_value(True, scale=1.0, offset=0.0, value_map=value_map) == 1.0
    assert coerce_mapped_value(False, scale=1.0, offset=0.0, value_map=value_map) == 0.0


def test_coerce_mapped_value_boolean_still_matches_a_capitalized_key():
    # A map someone already wrote against the old behaviour keeps working.
    assert coerce_mapped_value(True, scale=1.0, offset=0.0, value_map={"True": 5}) == 5.0


def test_coerce_mapped_value_unknown_value_map_key_raises():
    with pytest.raises(ValueError):
        coerce_mapped_value("ajar", scale=1.0, offset=0.0, value_map={"open": 1})


# --- flatten_json_fields (connector-mapping JSON path picker) ------------


def test_flatten_json_fields_lists_leaves_as_dot_paths():
    fields = flatten_json_fields({"data": {"temp": 72.5, "humidity": 41}})
    assert {"path": "data.temp", "value": 72.5, "is_array": False} in fields
    assert {"path": "data.humidity", "value": 41, "is_array": False} in fields
    assert len(fields) == 2


def test_flatten_json_fields_marks_arrays_non_selectable_without_recursing():
    fields = flatten_json_fields({"readings": [1, 2, 3], "name": "sensor"})
    assert {"path": "readings", "value": None, "is_array": True} in fields
    assert {"path": "name", "value": "sensor", "is_array": False} in fields
    assert len(fields) == 2


def test_flatten_json_fields_handles_deep_nesting_and_empty_dict():
    fields = flatten_json_fields({"a": {"b": {"c": 1}}, "empty": {}})
    assert fields == [{"path": "a.b.c", "value": 1, "is_array": False}]


# --- ConnectorPoller -----------------------------------------------------


@pytest.mark.asyncio
async def test_successful_poll_writes_mapped_value(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector(
        session_factory, poll_interval_seconds=0.05
    )

    def handler(request):
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await _until(lambda: _feed(session_factory, object_id) == (72.5, True))
    status = poller.status[connector_id]
    poller.stop()

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_value == 72.5
        assert obj.runtime.pending_feed_queued is True
    assert status.last_success is True
    assert status.consecutive_failures == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_last_poll_at_is_a_utc_iso_time_like_a_bacnet_connectors(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector(session_factory, poll_interval_seconds=0.05)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"temp": 1}})))
    poller = ConnectorPoller(session_factory, client=client)
    before = timeutil.utc_now().replace(microsecond=0)
    poller.start()
    await _until(lambda: _feed(session_factory, object_id) == (1, True))
    polled = poller.status[connector_id].to_dict()["last_poll_at"]
    poller.stop()
    await client.aclose()

    assert isinstance(polled, str)
    parsed = dt.datetime.fromisoformat(polled)
    assert parsed.utcoffset() == dt.timedelta(0)
    assert parsed.microsecond == 0
    assert before <= parsed <= timeutil.utc_now()


@pytest.mark.asyncio
async def test_queueing_a_polled_value_waits_for_a_held_lock_off_the_loop(session_factory):
    # One commit per mapping per poll, each of which can wait out another
    # process's write lock.
    _, object_id, mapping_id = _make_connector(session_factory)
    poller = ConnectorPoller(session_factory)

    run = await run_while_locked(
        session_factory, lambda: poller._queue_pending_write(mapping_id, object_id, 599001, 5.0)
    )

    assert_waited_off_the_loop(run)
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_feed_value == 5.0


@pytest.mark.asyncio
async def test_stop_clears_schedule_and_status(session_factory, monkeypatch):
    # The three clears must not sit after _client_for's return, or a stop/start
    # in one process keeps the old schedule and status.
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=0.05)

    def handler(request):
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    assert connector_id in poller.status
    poller.stop()

    assert poller._due == {}
    assert poller._settings == {}
    assert poller.status == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_failure_relinquishes_and_backs_off(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector(
        session_factory, poll_interval_seconds=0.05
    )
    with session_factory() as session:
        # a prior value that should get relinquished
        obj = session.get(BacnetObject, object_id)
        obj.runtime.pending_feed_value = 42.0
        obj.runtime.pending_feed_queued = True
        session.commit()

    def handler(request):
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    # Poll for the outcome instead of a fixed 0.09 s, which a busy machine
    # could miss.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while True:
        status = poller.status.get(connector_id)
        with session_factory() as session:
            relinquished = session.get(BacnetObject, object_id).runtime.pending_feed_value is None
        if (status is not None and status.last_success is False and relinquished) or loop.time() > deadline:
            break
        await asyncio.sleep(0.01)
    poller.stop()

    assert status is not None
    assert status.last_success is False
    assert status.consecutive_failures >= 1
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        # relinquished (queued as None), not left queued at 42
        assert obj.runtime.pending_feed_value is None
        assert obj.runtime.pending_feed_queued is True
    await client.aclose()


@pytest.mark.asyncio
async def test_parse_failure_relinquishes_only_that_mapping(session_factory, monkeypatch, caplog):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector(
        session_factory, poll_interval_seconds=0.05
    )
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.runtime.pending_feed_value = 42.0
        obj.runtime.pending_feed_queued = True
        session.commit()

    def handler(request):
        # valid JSON, but missing the mapped path -- a parse-level failure
        return httpx.Response(200, json={"data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await _until(lambda: _feed(session_factory, object_id) == (None, True))
    status = poller.status[connector_id]
    poller.stop()

    # A successful HTTP fetch with a per-mapping extraction failure is still
    # a successful poll at the transport level -- no backoff.
    assert status.last_success is True
    assert status.consecutive_failures == 0
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_value is None
        assert obj.runtime.pending_feed_queued is True
    await client.aclose()

    matching = [r for r in caplog.records if "mapping" in r.message and "connector" in r.message]
    assert matching and matching[0].device_id == 599001


# --- ConnectorPoller.preview (JSON path picker) ---------------------------


@pytest.mark.asyncio
async def test_preview_returns_flattened_fields_for_json_connector(session_factory):
    connector_id, _, _ = _make_connector(session_factory)

    def handler(request):
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)

    result = await poller.preview(connector_id)

    assert result == {
        "ok": True,
        "response_format": "json",
        "fields": [{"path": "data.temp", "value": 72.5, "is_array": False}],
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_returns_raw_text_for_plain_text_connector(session_factory):
    connector_id, _, _ = _make_connector(session_factory, response_format="plain-text")

    def handler(request):
        return httpx.Response(200, text="72.5")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)

    result = await poller.preview(connector_id)

    assert result == {"ok": True, "response_format": "plain-text", "raw_text": "72.5"}
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_transport_failure_returns_ok_false(session_factory):
    connector_id, _, _ = _make_connector(session_factory)

    def handler(request):
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)

    result = await poller.preview(connector_id)

    assert result["ok"] is False
    assert "error" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_non_dict_json_top_level_returns_ok_false(session_factory):
    connector_id, _, _ = _make_connector(session_factory)

    def handler(request):
        return httpx.Response(200, json=[1, 2, 3])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)

    result = await poller.preview(connector_id)

    assert result == {"ok": False, "error": "top-level response is not a JSON object"}
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_missing_connector_returns_ok_false(session_factory):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    poller = ConnectorPoller(session_factory, client=client)

    result = await poller.preview(999999)

    assert result == {"ok": False, "error": "connector not found"}
    await client.aclose()


@pytest.mark.asyncio
async def test_basic_auth_sent_on_request(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    _make_connector(
        session_factory,
        poll_interval_seconds=0.05,
        auth_type="basic",
        auth_username="user",
        auth_password="pass",
    )

    seen_headers = {}

    def handler(request):
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"temp": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    poller.stop()

    assert seen_headers["authorization"] is not None
    assert seen_headers["authorization"].startswith("Basic ")
    await client.aclose()


@pytest.mark.asyncio
async def test_bearer_token_sent_as_header(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    _make_connector(
        session_factory,
        poll_interval_seconds=0.05,
        auth_type="bearer",
        auth_token="secret-token",
    )

    seen_headers = {}

    def handler(request):
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"temp": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    poller.stop()

    assert seen_headers["authorization"] == "Bearer secret-token"
    await client.aclose()


@pytest.mark.asyncio
async def test_disabled_connector_is_not_polled(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector(
        session_factory, poll_interval_seconds=0.05, enabled=False
    )

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"temp": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    poller.stop()

    assert calls == []
    assert connector_id not in poller.status
    await client.aclose()


@pytest.mark.asyncio
async def test_scheduler_task_is_created_even_with_zero_connectors_at_boot(session_factory):
    """`if self._due: self._scheduler_task = ...` used to mean NO scheduler
    task at all was created when there were zero enabled connectors at
    process boot -- fatal now that ConnectorPoller is started exactly once
    per process lifetime (not reconstructed per EngineManager start()/stop()
    cycle), since a connector created later would then never poll until a
    full process restart.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    try:
        assert poller._scheduler_task is not None
        assert not poller._scheduler_task.done()
    finally:
        poller.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_new_connector_created_after_start_is_polled_without_restart(
    session_factory, monkeypatch
):
    """The other half of Fix 3: _scheduler_loop must re-scan enabled
    connectors each tick and pick up one created after start() ran, not just
    keep polling whatever was enabled at the moment start() snapshotted.
    """
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)

    def handler(request):
        return httpx.Response(200, json={"data": {"temp": 9}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    try:
        assert poller._due == {}  # nothing enabled yet

        connector_id, object_id, _ = _make_connector(
            session_factory, poll_interval_seconds=0.02
        )

        await _until(lambda: _feed(session_factory, object_id) == (9.0, True))

        assert connector_id in poller._due
        with session_factory() as session:
            obj = session.get(BacnetObject, object_id)
            assert obj.runtime.pending_feed_value == 9.0
            assert obj.runtime.pending_feed_queued is True
    finally:
        poller.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_stop_cancels_scheduler_task(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    _make_connector(session_factory, poll_interval_seconds=0.05)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    task = poller._scheduler_task
    poller.stop()
    await asyncio.sleep(0)

    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_poll_does_not_queue_a_write_for_a_non_running_device(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=1, name="D")  # process_status defaults to "stopped"
        session.add(device)
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        session.add(obj)
        connector = Connector(name="C", url="http://example.test/data")
        session.add(connector)
        session.flush()
        session.add(ConnectorMapping(connector_id=connector.id, object_id=obj.id, json_path="v"))
        session.commit()
        object_id = obj.id

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"v": 42.0}))
    client = httpx.AsyncClient(transport=transport)
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    poller.stop()

    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_queued is False


# --- Relinquish against an Input target, and scheduler survival -
# `_poll_one`'s transport-failure branch relinquishes every mapping
# unconditionally, and create_mapping (web/api/connectors.py) has always
# allowed a mapping to target an Input object. Before this session's fix,
# write_feed_value(obj, None) on such an object raised TypeError
# (typed_present_value casting None), which escaped _poll_one and
# _scheduler_loop uncaught -- the scheduler task died silently and no
# connector polled again until an engine restart. See
# test_write_paths.py's test_write_feed_value_none_on_*_reverts_to_class_default
# for the write_feed_value-level fix this depends on.


@pytest.mark.asyncio
async def test_transport_failure_against_input_mapping_does_not_crash_scheduler(
    session_factory, monkeypatch
):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, _ = _make_connector_for_input(
        session_factory, poll_interval_seconds=0.05
    )

    def handler(request):
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await _until(lambda: _feed(session_factory, object_id) == (None, True))
    status = poller.status[connector_id]
    task = poller._scheduler_task

    assert status.last_success is False
    assert status.consecutive_failures >= 1
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        # relinquished (queued as None), not left stale -- and queuing this
        # against an Input mapping doesn't crash the scheduler.
        assert obj.runtime.pending_feed_value is None
        assert obj.runtime.pending_feed_queued is True
    # The scheduler must still be alive (not dead from an uncaught
    # exception) -- checked before stop() deliberately cancels it.
    assert not task.done()

    poller.stop()
    await client.aclose()


@pytest.mark.asyncio
async def test_scheduler_survives_an_unexpected_exception_in_poll_one(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    _, object_id, _ = _make_connector(session_factory, poll_interval_seconds=0.02)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"temp": 5}})))
    poller = ConnectorPoller(session_factory, client=client)

    call_count = 0
    real_poll_one = poller._poll_one

    async def flaky_poll_one(connector_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom -- something unrelated to any per-connector failure path")
        await real_poll_one(connector_id)

    poller._poll_one = flaky_poll_one
    poller.start()

    def _later_tick_landed() -> bool:
        with session_factory() as session:
            return call_count >= 2 and session.get(BacnetObject, object_id).runtime.pending_feed_queued is True

    # Polled, not a fixed sleep -- a fixed 0.09 s flaked under full-suite load.
    deadline = asyncio.get_running_loop().time() + 5
    while not _later_tick_landed() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    task = poller._scheduler_task

    # The first tick's exception must not have killed the loop -- a later
    # tick still ran (and succeeded, proving the loop kept ticking, not
    # just that the task object happens to still exist).
    assert call_count >= 2
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_value == 5.0
        assert obj.runtime.pending_feed_queued is True
    assert not task.done()

    poller.stop()
    await client.aclose()


@pytest.mark.asyncio
async def test_bacnet_connectors_are_ignored_by_the_http_poller(session_factory, monkeypatch):
    # bacnet connectors run inside their device's process
    # (bacnet_connectors.py); the parent's HTTP poller must never pick one up.
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    with session_factory() as session:
        device = Device(id=599001, name="D")
        connector = Connector(
            name="Remote", type="bacnet", url="", device=device,
            remote_device_instance=2, poll_interval_seconds=0.05,
        )
        session.add_all([device, connector])
        session.commit()
        connector_id = connector.id

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.09)
    poller.stop()

    assert calls == []
    assert connector_id not in poller.status
    await client.aclose()


# --- Connectors are polled concurrently ------------------------------------


def _make_two_connectors(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        device.runtime.process_status = "running"
        slow_obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1")
        fast_obj = BacnetObject(device=device, object_type="analog-value", instance_number=2, name="AV-2")
        slow = Connector(name="Slow", url="http://slow.invalid/", poll_interval_seconds=0.02, timeout_seconds=5)
        fast = Connector(name="Fast", url="http://fast.invalid/", poll_interval_seconds=0.02)
        session.add_all([
            device, slow_obj, fast_obj, slow, fast,
            ConnectorMapping(connector=slow, object=slow_obj, json_path="data.temp"),
            ConnectorMapping(connector=fast, object=fast_obj, json_path="data.temp"),
        ])
        session.commit()
        return fast_obj.id


@pytest.mark.asyncio
async def test_a_slow_connector_does_not_hold_up_the_others(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    fast_object_id = _make_two_connectors(session_factory)
    slow_calls = []

    async def handler(request):
        if request.url.host == "slow.invalid":
            slow_calls.append(request)
            await asyncio.sleep(2)
        return httpx.Response(200, json={"data": {"temp": 7}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.3)  # several intervals, for the in-flight check below
    await _until(lambda: _feed(session_factory, fast_object_id)[0] == 7.0)
    poller.stop()

    with session_factory() as session:
        assert session.get(BacnetObject, fast_object_id).runtime.pending_feed_value == 7.0
    # A connector whose poll is still in flight isn't started again.
    assert len(slow_calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_stop_cancels_polls_in_flight(session_factory, monkeypatch):
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    _make_two_connectors(session_factory)
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(request.url.host)
            raise
        return httpx.Response(200, json={"data": {"temp": 7}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.1)
    poller.stop()
    await asyncio.sleep(0.05)

    assert sorted(cancelled) == ["fast.invalid", "slow.invalid"]
    await client.aclose()


@pytest.mark.parametrize("raw", ["nan", "inf", "-Infinity", "1e999", float("nan")])
def test_coerce_mapped_value_refuses_non_finite(raw):
    # A non-finite value is a parse-level failure for that mapping.
    with pytest.raises(ValueError):
        connectors.coerce_mapped_value(raw, scale=1.0, offset=0.0, value_map=None)


def test_coerce_mapped_value_refuses_an_overflowing_scale():
    with pytest.raises(ValueError):
        connectors.coerce_mapped_value("1e308", scale=10.0, offset=0.0, value_map=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete-mapping", "disable-connector"])
async def test_a_poll_finishing_after_a_delete_or_disable_writes_nothing(session_factory, monkeypatch, change):
    # A poll in flight when the user deleted the mapping or disabled
    # the connector still wrote its value afterwards.
    monkeypatch.setattr(connectors, "SCHEDULER_TICK_SECONDS", 0.01)
    connector_id, object_id, mapping_id = _make_connector(session_factory, poll_interval_seconds=0.05)
    calls = []

    def handler(request):
        calls.append(request)
        with session_factory() as session:
            if change == "delete-mapping":
                session.delete(session.get(ConnectorMapping, mapping_id))
            else:
                session.get(Connector, connector_id).enabled = False
            session.commit()
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = ConnectorPoller(session_factory, client=client)
    poller.start()
    await asyncio.sleep(0.15)
    poller.stop()
    await client.aclose()

    assert calls  # the poll really ran
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert obj.runtime.pending_feed_queued is False


# --- Fetch deadline, size cap, redirects, per-mapping status ----------------


class _NeverEndingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{"
        await asyncio.sleep(30)
        yield b"}"


def _poller(session_factory, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ConnectorPoller(session_factory, client=client), client


@pytest.mark.asyncio
async def test_a_body_over_the_cap_is_refused(session_factory):
    connector_id, _, _ = _make_connector(session_factory)
    big = b"x" * (connectors.MAX_RESPONSE_BYTES + 1)
    poller, client = _poller(session_factory, lambda request: httpx.Response(200, content=big))

    assert await poller.preview(connector_id) == {"ok": False, "error": "response larger than 1 MiB"}
    await client.aclose()


@pytest.mark.asyncio
async def test_a_response_that_never_finishes_fails_at_the_timeout(session_factory):
    connector_id, _, _ = _make_connector(session_factory, timeout_seconds=1)
    poller, client = _poller(session_factory, lambda request: httpx.Response(200, stream=_NeverEndingStream()))

    result = await asyncio.wait_for(poller.preview(connector_id), 5)

    assert result == {"ok": False, "error": "no complete response within 1 s"}
    await client.aclose()


@pytest.mark.asyncio
async def test_a_redirect_is_followed(session_factory):
    connector_id, object_id, _ = _make_connector(session_factory, url="http://example.invalid/old")

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "http://example.invalid/new"})
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    poller, client = _poller(session_factory, handler)
    await poller._poll_one(connector_id)

    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_feed_value == 72.5
    await client.aclose()


# --- Custom headers follow a redirect only where Authorization would -----------


def _redirect_capture(location):
    """A handler redirecting /old to `location` and recording the headers of
    every request that reaches anything else."""
    seen = []

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": location})
        seen.append(request.headers)
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    return handler, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "kept"),
    [
        ("http://example.invalid/new", True),
        ("https://example.invalid/new", True),
        ("http://other.invalid/new", False),
        ("http://example.invalid:8080/new", False),
        ("https://example.invalid:8443/new", False),
    ],
)
async def test_custom_headers_are_dropped_on_a_redirect_to_another_origin(session_factory, location, kept):
    connector_id, _, _ = _make_connector(
        session_factory,
        url="http://example.invalid/old",
        headers={"X-Api-Key": "secret"},
        auth_type="bearer",
        auth_token="tok",
    )
    handler, seen = _redirect_capture(location)
    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(connector_id))["ok"] is True

    assert len(seen) == 1
    assert ("x-api-key" in seen[0]) is kept
    assert ("authorization" in seen[0]) is kept
    await client.aclose()


@pytest.mark.asyncio
async def test_basic_auth_is_not_resent_after_a_cross_origin_redirect(session_factory):
    connector_id, _, _ = _make_connector(
        session_factory,
        url="http://example.invalid/old",
        auth_type="basic",
        auth_username="user",
        auth_password="pw",
    )
    handler, seen = _redirect_capture("http://other.invalid/new")
    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(connector_id))["ok"] is True

    assert "authorization" not in seen[0]
    await client.aclose()


@pytest.mark.asyncio
async def test_custom_headers_stay_dropped_after_redirecting_back(session_factory):
    connector_id, _, _ = _make_connector(
        session_factory, url="http://example.invalid/old", headers={"X-Api-Key": "secret"}
    )
    seen = []

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "http://other.invalid/hop"})
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://example.invalid/new"})
        seen.append(request.headers)
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(connector_id))["ok"] is True

    assert "x-api-key" not in seen[0]
    await client.aclose()


@pytest.mark.asyncio
async def test_a_redirect_loop_fails_the_fetch(session_factory):
    connector_id, _, _ = _make_connector(session_factory, url="http://example.invalid/old")
    poller, client = _poller(
        session_factory,
        lambda request: httpx.Response(302, headers={"location": "http://example.invalid/old"}),
    )

    result = await poller.preview(connector_id)

    assert result["ok"] is False
    assert "redirect" in result["error"].lower()
    await client.aclose()


def _add_second_connector(session_factory, url):
    with session_factory() as session:
        connector = Connector(name="Second Connector", url=url)
        session.add(connector)
        session.commit()
        return connector.id


@pytest.mark.asyncio
async def test_a_cookie_one_connector_receives_is_not_sent_on_another(session_factory):
    first_id, _, _ = _make_connector(session_factory, url="http://example.invalid/first")
    second_id = _add_second_connector(session_factory, "http://example.invalid/second")
    cookies_seen = []

    def handler(request):
        cookies_seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "sid=A; Path=/"}, json={"data": {"temp": 1}})

    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(first_id))["ok"] is True
    assert (await poller.preview(second_id))["ok"] is True
    assert (await poller.preview(first_id))["ok"] is True

    assert cookies_seen == [None, None, None]
    assert len(client.cookies.jar) == 0
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://example.invalid/new", "http://other.invalid/new"])
async def test_a_cookie_set_on_a_redirect_hop_is_not_sent_on_the_next(session_factory, location):
    connector_id, _, _ = _make_connector(session_factory, url="http://example.invalid/old")
    cookies_seen = []

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": location, "set-cookie": "s=2; Path=/"})
        cookies_seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(connector_id))["ok"] is True

    assert cookies_seen == [None]
    await client.aclose()


@pytest.mark.asyncio
async def test_a_cookie_header_typed_into_a_connector_is_still_sent(session_factory):
    connector_id, _, _ = _make_connector(session_factory, headers={"Cookie": "mine=1"})
    cookies_seen = []

    def handler(request):
        cookies_seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    poller, client = _poller(session_factory, handler)

    assert (await poller.preview(connector_id))["ok"] is True

    assert cookies_seen == ["mine=1"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_tls", [True, False])
async def test_the_clients_the_poller_makes_keep_no_cookies(session_factory, verify_tls):
    poller = ConnectorPoller(session_factory)
    client = poller._client_for(verify_tls)
    request = httpx.Request("GET", "https://example.invalid/")
    response = httpx.Response(200, headers={"set-cookie": "sid=A; Path=/"}, request=request)

    client.cookies.extract_cookies(response)

    assert len(client.cookies.jar) == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_a_missing_path_is_reported_per_mapping(session_factory):
    connector_id, _, mapping_id = _make_connector(session_factory)
    poller, client = _poller(session_factory, lambda request: httpx.Response(200, json={"data": {}}))

    await poller._poll_one(connector_id)

    status = poller.status[connector_id].to_dict()
    assert status["last_success"] is True
    assert status["read_errors"] == {str(mapping_id): "no value at 'data.temp'"}
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_relinquishes_without_backoff(session_factory):
    connector_id, object_id, mapping_id = _make_connector(session_factory, poll_interval_seconds=60)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        obj.runtime.pending_feed_value, obj.runtime.pending_feed_queued = 42.0, True
        session.commit()
    poller, client = _poller(session_factory, lambda request: httpx.Response(200, text="<html>oops</html>"))

    await poller._poll_one(connector_id)

    status = poller.status[connector_id]
    assert status.last_success is False
    assert status.last_error.startswith("response isn't valid JSON")
    assert status.consecutive_failures == 0
    assert status.read_errors == {str(mapping_id): status.last_error}
    assert poller._due[connector_id] == pytest.approx(asyncio.get_running_loop().time() + 60, abs=1)
    with session_factory() as session:
        obj = session.get(BacnetObject, object_id)
        assert (obj.runtime.pending_feed_value, obj.runtime.pending_feed_queued) == (None, True)
    await client.aclose()


@pytest.mark.asyncio
async def test_a_server_error_still_backs_off(session_factory):
    connector_id, _, mapping_id = _make_connector(session_factory, poll_interval_seconds=60)
    poller, client = _poller(session_factory, lambda request: httpx.Response(500))

    await poller._poll_one(connector_id)

    status = poller.status[connector_id]
    assert status.consecutive_failures == 1
    assert poller._due[connector_id] >= asyncio.get_running_loop().time() + 110
    assert status.read_errors[str(mapping_id)] == status.last_error
    await client.aclose()


@pytest.mark.asyncio
async def test_plain_text_body_is_not_parsed_as_json(session_factory):
    connector_id, object_id, _ = _make_connector(session_factory, response_format="plain-text")
    poller, client = _poller(session_factory, lambda request: httpx.Response(200, text="72.5"))

    await poller._poll_one(connector_id)

    assert poller.status[connector_id].last_success is True
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_feed_value == 72.5
    await client.aclose()


# --- An edit clears the connector's backoff ---------------------------------


@pytest.mark.asyncio
async def test_editing_a_connector_clears_its_backoff(session_factory):
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=60)
    poller, client = _poller(session_factory, lambda request: httpx.Response(500))
    poller._track_new_connectors()
    await poller._poll_one(connector_id)
    loop = asyncio.get_running_loop()
    assert poller._due[connector_id] > loop.time() + 100

    with session_factory() as session:
        session.get(Connector, connector_id).url = "http://example.invalid/fixed"
        session.commit()
    poller._track_new_connectors()

    assert poller.status[connector_id].consecutive_failures == 0
    assert poller._due[connector_id] <= loop.time()
    await client.aclose()


@pytest.mark.asyncio
async def test_a_poll_in_flight_across_an_edit_sets_no_backoff(session_factory):
    # A poll still using the old settings when the
    # edit lands mustn't put back the backoff the edit cleared.
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=60)
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return httpx.Response(500)

    poller, client = _poller(session_factory, handler)
    poller._track_new_connectors()
    task = asyncio.ensure_future(poller._poll_one(connector_id))
    await asyncio.sleep(0.01)  # the poll waits on the response
    with session_factory() as session:
        session.get(Connector, connector_id).url = "http://example.invalid/fixed"
        session.commit()
    poller._track_new_connectors()
    release.set()
    await task

    assert poller.status[connector_id].consecutive_failures == 0
    assert poller._due[connector_id] <= asyncio.get_running_loop().time()
    await client.aclose()


@pytest.mark.asyncio
async def test_an_unchanged_connector_keeps_its_backoff(session_factory):
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=60)
    poller, client = _poller(session_factory, lambda request: httpx.Response(500))
    poller._track_new_connectors()
    await poller._poll_one(connector_id)
    due = poller._due[connector_id]

    poller._track_new_connectors()

    assert poller._due[connector_id] == due
    await client.aclose()


@pytest.mark.asyncio
async def test_a_reenabled_connector_starts_fresh(session_factory):
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=60)
    poller, client = _poller(session_factory, lambda request: httpx.Response(500))
    poller._track_new_connectors()
    with session_factory() as session:
        session.get(Connector, connector_id).enabled = False
        session.commit()
    await poller._poll_one(connector_id)  # sees it disabled, forgets it
    assert connector_id not in poller._settings

    with session_factory() as session:
        session.get(Connector, connector_id).enabled = True
        session.commit()
    poller._track_new_connectors()

    assert poller._due[connector_id] >= asyncio.get_running_loop().time() + 59
    await client.aclose()


# --- Per-connector TLS certificate check switch -----------------------------


def _two_client_poller(session_factory, handler=None):
    """A poller with both clients injected, recording which one each request
    went through."""
    used = []

    def recording(name):
        def respond(request):
            used.append((name, request.url.path))
            if handler is not None:
                return handler(request)
            return httpx.Response(200, json={"data": {"temp": 72.5}})
        return respond

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording("verified")))
    insecure = httpx.AsyncClient(transport=httpx.MockTransport(recording("insecure")))
    poller = ConnectorPoller(session_factory, client=client, insecure_client=insecure)
    return poller, used, (client, insecure)


async def _close(clients):
    for client in clients:
        await client.aclose()


def test_a_new_connector_verifies_tls_by_default(session_factory):
    connector_id, _, _ = _make_connector(session_factory)
    with session_factory() as session:
        assert session.get(Connector, connector_id).verify_tls is True


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_tls, expected", [(True, "verified"), (False, "insecure")])
async def test_poll_and_preview_use_the_client_matching_verify_tls(session_factory, verify_tls, expected):
    connector_id, _, _ = _make_connector(session_factory, verify_tls=verify_tls)
    poller, used, clients = _two_client_poller(session_factory)

    await poller._poll_one(connector_id)
    assert (await poller.preview(connector_id))["ok"] is True

    assert [name for name, _ in used] == [expected, expected]
    await _close(clients)


@pytest.mark.asyncio
async def test_a_redirect_stays_on_the_unverified_client(session_factory):
    connector_id, object_id, _ = _make_connector(
        session_factory, url="https://example.invalid/old", verify_tls=False
    )

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://example.invalid/new"})
        return httpx.Response(200, json={"data": {"temp": 72.5}})

    poller, used, clients = _two_client_poller(session_factory, handler)
    await poller._poll_one(connector_id)

    assert used == [("insecure", "/old"), ("insecure", "/new")]
    with session_factory() as session:
        assert session.get(BacnetObject, object_id).runtime.pending_feed_value == 72.5
    await _close(clients)


@pytest.mark.asyncio
async def test_the_unverified_client_is_made_only_when_needed(session_factory, monkeypatch):
    made = []
    real = httpx.AsyncClient

    def recording_client(*args, **kwargs):
        made.append(kwargs)
        return real(*args, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), **kwargs)

    monkeypatch.setattr(connectors.httpx, "AsyncClient", recording_client)
    connector_id, _, _ = _make_connector(session_factory)
    poller = ConnectorPoller(session_factory)
    poller.start()
    await poller.preview(connector_id)
    assert made == [{}]

    with session_factory() as session:
        session.get(Connector, connector_id).verify_tls = False
        session.commit()
    await poller.preview(connector_id)
    await poller.preview(connector_id)
    assert made == [{}, {"verify": False}]

    insecure = poller._insecure_client
    poller.stop()
    await asyncio.sleep(0)
    assert insecure.is_closed
    assert poller._insecure_client is None


@pytest.mark.asyncio
async def test_the_verifying_client_is_made_only_when_needed(session_factory, monkeypatch):
    # Building one loads the CA bundle (a quarter second on Windows), at
    # every app start even with no http-poll connector.
    made = []
    real = httpx.AsyncClient

    def recording_client(*args, **kwargs):
        made.append(kwargs)
        return real(*args, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), **kwargs)

    monkeypatch.setattr(connectors.httpx, "AsyncClient", recording_client)
    poller = ConnectorPoller(session_factory)
    poller.start()
    assert made == []

    connector_id, _, _ = _make_connector(session_factory)
    await poller.preview(connector_id)
    await poller.preview(connector_id)
    assert made == [{}]

    verifying = poller._client
    poller.stop()
    await asyncio.sleep(0)
    assert verifying.is_closed
    assert poller._client is None


@pytest.mark.asyncio
async def test_turning_verify_tls_off_clears_the_backoff(session_factory):
    connector_id, _, _ = _make_connector(session_factory, poll_interval_seconds=60)
    poller, _, clients = _two_client_poller(session_factory, lambda request: httpx.Response(500))
    poller._track_new_connectors()
    await poller._poll_one(connector_id)
    loop = asyncio.get_running_loop()
    assert poller._due[connector_id] > loop.time() + 100

    with session_factory() as session:
        session.get(Connector, connector_id).verify_tls = False
        session.commit()
    poller._track_new_connectors()

    assert poller.status[connector_id].consecutive_failures == 0
    assert poller._due[connector_id] <= loop.time()
    await _close(clients)


_CERT_FAILURE = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "self-signed certificate (_ssl.c:1000)"
)


@pytest.mark.asyncio
async def test_a_certificate_failure_hints_at_the_switch(session_factory):
    connector_id, _, _ = _make_connector(session_factory, url="https://example.invalid/status")

    def handler(request):
        raise httpx.ConnectError(_CERT_FAILURE, request=request)

    poller, client = _poller(session_factory, handler)
    result = await poller.preview(connector_id)
    await poller._poll_one(connector_id)

    assert result["ok"] is False
    assert result["error"].startswith(_CERT_FAILURE)
    assert "turn off Verify TLS certificate" in result["error"]
    assert "turn off Verify TLS certificate" in poller.status[connector_id].last_error
    await client.aclose()


@pytest.mark.asyncio
async def test_other_connect_errors_get_no_hint(session_factory):
    connector_id, _, _ = _make_connector(session_factory)

    def handler(request):
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    poller, client = _poller(session_factory, handler)
    result = await poller.preview(connector_id)

    assert result == {"ok": False, "error": "[Errno 111] Connection refused"}
    await client.aclose()


# --- Missing credentials --------------------------------------------------


def _counting_poller(session_factory):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"temp": 1.0}})

    poller, client = _poller(session_factory, handler)
    return poller, client, requests


@pytest.mark.asyncio
async def test_a_connector_missing_a_credential_does_not_poll(session_factory):
    connector_id, object_id, mapping_id = _make_connector(
        session_factory, auth_type="basic", auth_username="u", headers={"X-Api-Key": ""}
    )
    poller, client, requests = _counting_poller(session_factory)

    await poller._poll_one(connector_id)

    assert requests == []
    status = poller.status[connector_id]
    assert status.last_success is False
    assert status.last_error == "Missing credentials: password, header X-Api-Key"
    assert status.read_errors == {str(mapping_id): "Missing credentials: password, header X-Api-Key"}
    assert status.consecutive_failures == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_a_connector_missing_a_credential_releases_its_values_once(session_factory):
    connector_id, object_id, _ = _make_connector(session_factory, headers={"X-Api-Key": ""})
    poller, client, _ = _counting_poller(session_factory)

    await poller._poll_one(connector_id)
    await _until(lambda: _feed(session_factory, object_id) == (None, True))
    with session_factory() as session:
        session.get(BacnetObject, object_id).runtime.pending_feed_queued = False  # the device took it
        session.commit()
    await poller._poll_one(connector_id)
    await poller._poll_one(connector_id)

    assert _feed(session_factory, object_id) == (None, False)
    await client.aclose()


@pytest.mark.asyncio
async def test_a_connector_polls_once_its_credentials_are_filled_in(session_factory):
    connector_id, object_id, _ = _make_connector(session_factory, headers={"X-Api-Key": ""})
    poller, client, requests = _counting_poller(session_factory)
    await poller._poll_one(connector_id)
    with session_factory() as session:
        session.get(Connector, connector_id).headers = {"X-Api-Key": "k"}
        session.commit()

    await poller._poll_one(connector_id)

    assert len(requests) == 1 and requests[0].headers["X-Api-Key"] == "k"
    assert poller.status[connector_id].last_success is True
    await _until(lambda: _feed(session_factory, object_id) == (1.0, True))
    # Missing again: released again.
    with session_factory() as session:
        session.get(Connector, connector_id).headers = {"X-Api-Key": ""}
        session.commit()
    await poller._poll_one(connector_id)
    await _until(lambda: _feed(session_factory, object_id) == (None, True))
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_of_a_connector_missing_a_credential_does_not_fetch(session_factory):
    connector_id, _, _ = _make_connector(session_factory, auth_type="bearer")
    poller, client, requests = _counting_poller(session_factory)

    result = await poller.preview(connector_id)

    assert result == {"ok": False, "error": "Missing credentials: token"}
    assert requests == []
    await client.aclose()
