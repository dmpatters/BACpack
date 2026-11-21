"""SimulationRunner against a real in-process Application, same
testing shape as PendingWritePoller."""
from __future__ import annotations

import random
from unittest.mock import Mock

import pytest
from bacpypes3.primitivedata import ObjectIdentifier

import bacpack.simulation_runner as simulation_runner
from bacpack.bacnet import write_manual_value
from bacpack.bacnet_client import to_jsonable
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from bacpack.simulation_runner import SimulationRunner

_SQUARE = {"kind": "square", "low": 10, "high": 20, "period_s": 100, "interval_s": 1}


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _add(session_factory, **fields):
    with session_factory() as session:
        if session.get(Device, 1) is None:
            session.add(Device(id=1, name="D"))
            session.commit()
        obj = BacnetObject(device_id=1, **fields)
        session.add(obj)
        session.commit()
        return obj.id


def _set_simulation(session_factory, object_id, value):
    with session_factory() as session:
        session.get(BacnetObject, object_id).simulation = value
        session.commit()


def _app(session_factory):
    with session_factory() as session:
        device = session.get(Device, 1)
        return build_application(device, list(device.objects))


def _live(app, object_type, instance=1):
    return app.get_object_id(ObjectIdentifier((object_type, instance)))


def _runner(session_factory, app, clock):
    return SimulationRunner(session_factory, app, 1, clock=clock, rng=random.Random(7))


@pytest.mark.asyncio
async def test_writes_the_generated_value_at_priority_10(session_factory):
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    app = _app(session_factory)
    await _runner(session_factory, app, _Clock(1000.0)).tick()  # start of the cycle: high
    live = _live(app, "analog-value")
    assert to_jsonable(live.priorityArray[9]) == 20.0
    assert live.presentValue == 20.0


@pytest.mark.asyncio
async def test_writes_only_when_the_value_changes(session_factory, monkeypatch):
    calls = []
    real = simulation_runner.write_feed_value

    async def counting(obj, value, forward=True):
        calls.append(value)
        return await real(obj, value, forward)

    monkeypatch.setattr(simulation_runner, "write_feed_value", counting)
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, _app(session_factory), clock)
    for t in (1000.0, 1001.0, 1060.0, 1061.0):
        clock.t = t
        await runner.tick()
    assert calls == [20.0, 10.0]


@pytest.mark.asyncio
async def test_analog_waits_its_interval(session_factory, monkeypatch):
    calls = []
    real = simulation_runner.write_feed_value

    async def counting(obj, value, forward=True):
        calls.append(value)
        return await real(obj, value, forward)

    monkeypatch.setattr(simulation_runner, "write_feed_value", counting)
    _add(
        session_factory,
        object_type="analog-value",
        instance_number=1,
        name="AV-1",
        simulation={"kind": "noise", "center": 50, "amplitude": 5, "interval_s": 10},
    )
    clock = _Clock(1000.0)
    runner = _runner(session_factory, _app(session_factory), clock)
    for t in (1000.0, 1005.0, 1009.0, 1010.0):
        clock.t = t
        await runner.tick()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_settings_change_resets_state(session_factory):
    walk = {"kind": "random_walk", "start": 50, "max_step": 1, "low": 0, "high": 100, "interval_s": 1}
    object_id = _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=walk)
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()
    assert 49 <= _live(app, "analog-value").presentValue <= 51
    _set_simulation(session_factory, object_id, {**walk, "start": 80})
    runner.mark_changed()
    clock.t = 1001.0
    await runner.tick()
    assert 79 <= _live(app, "analog-value").presentValue <= 81


@pytest.mark.asyncio
async def test_a_renumbered_object_is_written_and_released_under_its_new_number(session_factory):
    # AV:2 stands in for the renumbered object's fresh live copy.
    av = _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    other = _add(session_factory, object_type="analog-value", instance_number=2, name="AV-2")
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()
    with session_factory() as session:
        session.delete(session.get(BacnetObject, other))
        session.get(BacnetObject, av).instance_number = 2
        session.commit()
    runner.mark_changed()
    clock.t = 1001.0  # same generated value as the last tick
    await runner.tick()
    assert to_jsonable(_live(app, "analog-value", 2).priorityArray[9]) == 20.0
    _set_simulation(session_factory, av, None)
    runner.mark_changed()
    clock.t = 1002.0
    await runner.tick()
    assert to_jsonable(_live(app, "analog-value", 2).priorityArray[9]) is None


@pytest.mark.asyncio
async def test_turning_off_releases_a_commandable_object_and_keeps_an_input(session_factory):
    av = _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    ai = _add(session_factory, object_type="analog-input", instance_number=1, name="AI-1", simulation=_SQUARE)
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()
    assert _live(app, "analog-input").presentValue == 20.0
    # Cleared through the ORM: a JSON column may store JSON null, not SQL NULL.
    _set_simulation(session_factory, av, None)
    _set_simulation(session_factory, ai, None)
    runner.mark_changed()
    clock.t = 1001.0
    await runner.tick()
    assert _live(app, "analog-value").presentValue == 0.0  # slot 10 released
    assert _live(app, "analog-input").presentValue == 20.0  # kept


@pytest.mark.asyncio
async def test_turning_off_releases_a_multistate_value_to_its_relinquish_default(session_factory):
    # MSV is commandable, so off releases slot 10 like any value
    # object instead of keeping the last state.
    msv = _add(
        session_factory, object_type="multi-state-value", instance_number=1, name="MSV-1",
        state_text=["A", "B", "C"], relinquish_default="3", simulation={"kind": "cycle", "dwell_s": 10},
    )
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()
    assert int(_live(app, "multi-state-value").presentValue) == 2  # step 100 of 3 states
    _set_simulation(session_factory, msv, None)
    runner.mark_changed()
    clock.t = 1001.0
    await runner.tick()
    assert int(_live(app, "multi-state-value").presentValue) == 3


@pytest.mark.asyncio
async def test_a_bms_write_above_10_beats_a_simulated_msv(session_factory):
    from bacpypes3.primitivedata import Unsigned

    _add(
        session_factory, object_type="multi-state-value", instance_number=1, name="MSV-1",
        state_text=["A", "B", "C"], simulation={"kind": "cycle", "dwell_s": 10},
    )
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()
    live = _live(app, "multi-state-value")
    await live.write_property("presentValue", Unsigned(1), priority=8)
    clock.t = 1010.0  # the next cycle step
    await runner.tick()
    assert int(live.presentValue) == 1


@pytest.mark.asyncio
async def test_an_out_of_service_input_is_skipped(session_factory):
    _add(
        session_factory,
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        out_of_service_default=True,
        simulation=_SQUARE,
    )
    app = _app(session_factory)
    await _runner(session_factory, app, _Clock(1000.0)).tick()
    assert _live(app, "analog-input").presentValue == 0.0


@pytest.mark.asyncio
async def test_a_manual_write_wins_and_clearing_it_reveals_the_simulation(session_factory):
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    live = _live(app, "analog-value")
    await runner.tick()
    await write_manual_value(live, 99.0)
    clock.t = 1060.0
    await runner.tick()
    assert live.presentValue == 99.0
    await write_manual_value(live, None)
    assert live.presentValue == 10.0


@pytest.mark.asyncio
async def test_follow_tracks_its_source(session_factory):
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1")
    _add(
        session_factory,
        object_type="analog-value",
        instance_number=2,
        name="AV-2",
        simulation={"kind": "follow", "source": "analog-value:1", "scale": 2, "offset": 1, "interval_s": 1},
    )
    app = _app(session_factory)
    await write_manual_value(_live(app, "analog-value", 1), 5.0)
    await _runner(session_factory, app, _Clock()).tick()
    assert _live(app, "analog-value", 2).presentValue == 11.0


@pytest.mark.asyncio
async def test_binary_and_multistate(session_factory):
    _add(
        session_factory,
        object_type="binary-value",
        instance_number=1,
        name="BV-1",
        simulation={"kind": "square", "period_s": 100},
    )
    _add(
        session_factory,
        object_type="multi-state-value",
        instance_number=1,
        name="MSV-1",
        state_text=["A", "B", "C"],
        simulation={"kind": "cycle", "dwell_s": 10},
    )
    app = _app(session_factory)
    await _runner(session_factory, app, _Clock(1000.0)).tick()
    assert to_jsonable(_live(app, "binary-value").presentValue) == "active"
    assert int(_live(app, "multi-state-value").presentValue) == 2  # step 100 of 3 states


@pytest.mark.asyncio
async def test_problems_are_logged_once_and_dont_stop_other_objects(session_factory, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(simulation_runner, "logger", fake_logger)
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    av2_id = _add(session_factory, object_type="analog-value", instance_number=2, name="AV-2")
    app = _app(session_factory)
    # Stored broken while the device runs, bypassing the API's checks (the
    # build refuses it at start).
    with session_factory() as session:
        session.get(BacnetObject, av2_id).simulation = {"kind": "sine"}
        session.commit()
    _add(session_factory, object_type="analog-value", instance_number=3, name="AV-3", simulation=_SQUARE)  # not live
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    for t in (1000.0, 1001.0, 1002.0):
        clock.t = t
        await runner.tick()
    assert _live(app, "analog-value", 1).presentValue == 20.0
    messages = [call.args[0] for call in fake_logger.warning.call_args_list]
    assert len(messages) == 2
    assert any('analog-value:2 "AV-2"' in m for m in messages)
    assert any('analog-value:3 "AV-3"' in m and "live apply failed" in m for m in messages)


class _Hook:
    """Stands in for the proxy hook a BACnet connector runner installs."""

    def __init__(self):
        self.calls = []

    async def __call__(self, value, priority):
        self.calls.append((value, priority))


@pytest.mark.asyncio
async def test_releasing_never_forwards_to_a_proxy_remote(session_factory):
    object_id = _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    live = _live(app, "analog-value")
    await runner.tick()
    assert live.presentValue == 20.0
    # The proxy mapping lands in the same tick window as the simulator's removal.
    hook = _Hook()
    live._proxy_forward = hook
    _set_simulation(session_factory, object_id, None)
    runner.mark_changed()
    clock.t = 1001.0
    await runner.tick()
    assert hook.calls == []
    assert live.presentValue == 0.0  # slot 10 still released locally


@pytest.mark.asyncio
async def test_a_proxy_object_is_not_simulated(session_factory, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(simulation_runner, "logger", fake_logger)
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    app = _app(session_factory)
    live = _live(app, "analog-value")
    hook = _Hook()
    live._proxy_forward = hook
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    for t in (1000.0, 1001.0):
        clock.t = t
        await runner.tick()
    assert hook.calls == []
    assert live.priorityArray[9] is None or to_jsonable(live.priorityArray[9]) is None
    assert live.presentValue == 0.0
    messages = [call.args[0] for call in fake_logger.warning.call_args_list]
    assert len(messages) == 1 and "BACnet proxy" in messages[0]


@pytest.mark.asyncio
async def test_a_backward_clock_step_does_not_freeze_an_analog_simulator(session_factory, monkeypatch):
    calls = []
    real = simulation_runner.write_feed_value

    async def counting(obj, value, forward=True):
        calls.append(value)
        return await real(obj, value, forward)

    monkeypatch.setattr(simulation_runner, "write_feed_value", counting)
    _add(
        session_factory,
        object_type="analog-value",
        instance_number=1,
        name="AV-1",
        simulation={"kind": "noise", "center": 50, "amplitude": 5, "interval_s": 10},
    )
    clock = _Clock(1000.0)
    runner = _runner(session_factory, _app(session_factory), clock)
    await runner.tick()
    clock.t = 500.0  # the host clock stepped back 500 s
    await runner.tick()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_backward_clock_step_restarts_a_random_dwell(session_factory):
    _add(
        session_factory,
        object_type="binary-value",
        instance_number=1,
        name="BV-1",
        simulation={"kind": "random", "min_dwell_s": 100, "max_dwell_s": 100},
    )
    app = _app(session_factory)
    clock = _Clock(1000.0)
    runner = _runner(session_factory, app, clock)
    await runner.tick()  # dwell would end at 1100
    clock.t = 500.0
    await runner.tick()  # a fresh dwell, ending at 600
    before = _live(app, "binary-value").presentValue
    clock.t = 601.0
    await runner.tick()
    assert _live(app, "binary-value").presentValue != before


# --- Cached config -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reloads_only_when_marked_changed(session_factory, monkeypatch):
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    runner = _runner(session_factory, _app(session_factory), _Clock(1000.0))
    loads = []
    real = runner._load
    monkeypatch.setattr(runner, "_load", lambda: loads.append(1) or real())

    await runner.tick()
    await runner.tick()
    assert len(loads) == 1

    runner.mark_changed()
    await runner.tick()
    assert len(loads) == 2


@pytest.mark.asyncio
async def test_a_failed_load_is_retried_on_the_next_tick(session_factory, monkeypatch):
    _add(session_factory, object_type="analog-value", instance_number=1, name="AV-1", simulation=_SQUARE)
    app = _app(session_factory)
    runner = _runner(session_factory, app, _Clock(1000.0))
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
    await runner.tick()
    assert len(calls) == 2
    assert _live(app, "analog-value").presentValue == 20.0


def test_runner_logs_under_the_simulation_name():
    from bacpack import simulation_runner

    assert simulation_runner.logger.name == "bacpack.simulation"
