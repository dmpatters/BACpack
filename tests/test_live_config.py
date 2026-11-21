"""live_config: a running device's ObjectReloader diffs the saved rows against
what it built and swaps only what changed."""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
import threading

import pytest
from bacpypes3.primitivedata import ObjectIdentifier, Real
from sqlalchemy import delete, text

from bacpack import bacpypes3_compat, gc_freeze, live_config, live_swap, trending
from bacpack.db import lock_both_files
from bacpack.device_process import _load_device_config
from bacpack.live_config import ObjectReloader, diff_rows
from bacpack.models import BacnetObject, Device, DeviceLink, DeviceRuntime, TrendSample, get_or_create_settings
from bacpack.object_builder import build_application
from bacpack.objects.calendar import LocalCalendarObject
from bacpack.trending import TrendSampler


@pytest.fixture(autouse=True)
def _a_turn_after_every_build(monkeypatch):
    # Every apply here gives the loop a turn after each object it builds, so
    # each test also shows nothing built runs before the swap.
    monkeypatch.setattr(live_config, "BUILD_TURN_SECONDS", 0.0)


# --- diff_rows (pure) --------------------------------------------------------

def _row(**fields):
    base = {"id": 1, "object_type": "analog-value", "instance_number": 1, "name": "AV-1", "description": None,
            "trended": False, "trend_mode": None, "simulation": None}
    return {**base, **fields}


def test_diff_finds_each_kind_of_change():
    old = {1: _row(), 2: _row(id=2, name="B", instance_number=2), 3: _row(id=3, name="C", instance_number=3),
           4: _row(id=4, name="D", instance_number=4), 5: _row(id=5, name="E", instance_number=5),
           6: _row(id=6, name="F", instance_number=6)}
    new = {1: _row(),  # unchanged
           2: _row(id=2, name="B2", instance_number=2),  # edited
           3: _row(id=3, name="C", instance_number=30),  # renumbered
           4: _row(id=4, name="D", instance_number=4, trended=True),  # trend only
           5: _row(id=5, name="E", instance_number=5, simulation={"kind": "sine"}),  # runner-read only
           7: _row(id=7, name="G", instance_number=7)}  # added; 6 deleted
    diff = diff_rows(old, new)
    assert sorted(diff.removed) == [3, 6]
    assert diff.rebuilt[2] == frozenset({"name"})
    assert 3 in diff.rebuilt and 7 in diff.rebuilt and 1 not in diff.rebuilt
    assert diff.trend_only == [4]
    assert diff.adopted == [5]


# --- ObjectReloader against a real DB ---------------------------------------

async def _running(session_factory, *objects):
    with session_factory() as session:
        device = Device(id=1, name="D")
        session.add(device)
        for obj in objects:
            obj.device = device
            session.add(obj)
        get_or_create_settings(session).bind_address = "127.0.0.1/8"
        session.commit()
    device, rows, *_ = _load_device_config(session_factory, 1)
    app = build_application(device, rows, trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return app, ObjectReloader(session_factory, app, device, rows)


def _edit(session_factory, fn):
    with session_factory() as session:
        fn(session)
        session.commit()


def _runtime(session_factory):
    with session_factory() as session:
        return session.get(DeviceRuntime, 1)


async def _apply(reloader, seq=1):
    await reloader.apply(seq)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _av(instance=1, name=None, **fields):
    return BacnetObject(object_type="analog-value", instance_number=instance, name=name or f"AV-{instance}",
                        relinquish_default="0", **fields)


@pytest.mark.asyncio
async def test_an_added_object_is_served(session_factory):
    app, reloader = await _running(session_factory, _av())
    _edit(session_factory, lambda s: s.add(_av(2, device_id=1)))
    await _apply(reloader, 5)
    assert app.get_object_name("AV-2") is not None
    assert any(str(oid[0]) == "analog-value" and oid[1] == 2 for oid in app.device_object.objectList)
    runtime = _runtime(session_factory)
    assert runtime.applied_seq == 5 and runtime.apply_error is None


@pytest.mark.asyncio
async def test_an_alarming_object_added_live_past_its_limit_is_in_alarm(session_factory):
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    app, reloader = await _running(session_factory, nc)
    _edit(session_factory, lambda s: s.add(BacnetObject(
        device_id=1, object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=10.0,
        alarm_deadband=1.0, alarm_time_delay=0,
    )))
    await _apply(reloader)
    for _ in range(3):
        await asyncio.sleep(0)
    assert app.get_object_name("AI-1").eventState.attr == "lowLimit"


@pytest.mark.asyncio
async def test_a_deleted_object_and_its_trend_log_are_gone(session_factory):
    app, reloader = await _running(session_factory, _av(trended=True))
    assert len([o for o in app.objectIdentifier if str(o[0]) == "trend-log"]) == 1
    _edit(session_factory, lambda s: s.query(BacnetObject).delete())
    await _apply(reloader)
    assert app.get_object_name("AV-1") is None
    assert [o for o in app.objectIdentifier if str(o[0]) == "trend-log"] == []


@pytest.mark.asyncio
async def test_an_unchanged_object_is_left_alone_and_an_edited_one_keeps_its_slots(session_factory):
    app, reloader = await _running(session_factory, _av(1), _av(2))
    untouched = app.get_object_name("AV-2")
    await app.get_object_name("AV-1").write_property("presentValue", Real(42.0), priority=8)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "name", "Renamed"))
    await _apply(reloader)
    renamed = app.get_object_name("Renamed")
    assert renamed is not None and app.get_object_name("AV-1") is None
    assert renamed.presentValue == 42.0
    assert app.get_object_name("AV-2") is untouched


@pytest.mark.asyncio
async def test_a_renumbered_object_starts_fresh(session_factory):
    app, reloader = await _running(session_factory, _av(1))
    await app.get_object_name("AV-1").write_property("presentValue", Real(42.0), priority=8)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "instance_number", 9))
    await _apply(reloader)
    obj = app.get_object_name("AV-1")
    assert obj.objectIdentifier[1] == 9
    assert obj.presentValue == 0.0  # a new BACnet identity carries nothing


@pytest.mark.asyncio
async def test_an_edited_object_keeps_its_object_list_place(session_factory):
    app, reloader = await _running(session_factory, _av(1), _av(2), _av(3))
    before = list(app.device_object.objectList)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-2").one(), "description", "e"))
    await _apply(reloader)
    assert list(app.device_object.objectList) == before


@pytest.mark.asyncio
async def test_a_renumbered_object_goes_last_and_the_rest_keep_their_places(session_factory):
    app, reloader = await _running(session_factory, _av(1), _av(2), _av(3))
    before = list(app.device_object.objectList)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "instance_number", 9))
    await _apply(reloader)
    after = list(app.device_object.objectList)
    assert after[-1] == ObjectIdentifier(("analog-value", 9))
    assert after[:-1] == [oid for oid in before if oid != ObjectIdentifier(("analog-value", 1))]


@pytest.mark.asyncio
async def test_a_trend_only_change_keeps_the_object(session_factory):
    app, reloader = await _running(session_factory, _av())
    obj = app.get_object_name("AV-1")
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trended", True))
    await _apply(reloader)
    assert app.get_object_name("AV-1") is obj
    assert len([o for o in app.objectIdentifier if str(o[0]) == "trend-log"]) == 1


@pytest.mark.asyncio
async def test_names_traded_in_one_apply(session_factory):
    app, reloader = await _running(session_factory, _av(1, "A"), _av(2, "B"))

    def trade(session):
        a = session.query(BacnetObject).filter_by(name="A").one()
        b = session.query(BacnetObject).filter_by(name="B").one()
        a.name, b.name = "tmp", "A"
        session.flush()
        a.name = "B"

    _edit(session_factory, trade)
    await _apply(reloader)
    assert app.get_object_name("A").objectIdentifier[1] == 2
    assert app.get_object_name("B").objectIdentifier[1] == 1
    assert _runtime(session_factory).apply_error is None


@pytest.mark.asyncio
async def test_a_failed_whole_set_check_applies_nothing(session_factory):
    app, reloader = await _running(session_factory, _av(1), _av(2))
    # Accepted by the DB, refused by the whole-set check: an object named
    # after its own device.
    _edit(session_factory, lambda s: s.execute(text("UPDATE main.bacnet_objects SET description = 'x'")))
    _edit(session_factory, lambda s: s.execute(text("UPDATE main.bacnet_objects SET name = 'D' WHERE name = 'AV-2'")))
    await _apply(reloader)
    assert app.get_object_name("AV-2") is not None  # still served as before
    assert str(app.get_object_name("AV-1").description) != "x"  # nothing else applied either
    assert "device's own name" in _runtime(session_factory).apply_error


@pytest.mark.asyncio
async def test_one_bad_object_keeps_its_old_version_and_reports(session_factory):
    app, reloader = await _running(session_factory, _av(1), _av(2))
    old = app.get_object_name("AV-1")
    _edit(session_factory, lambda s: s.execute(text("UPDATE main.bacnet_objects SET units = 'bogus' WHERE name = 'AV-1'")))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-2").one(), "description", "ok"))
    await _apply(reloader)
    assert app.get_object_name("AV-1") is old
    assert str(app.get_object_name("AV-2").description) == "ok"
    assert "AV-1" in _runtime(session_factory).apply_error
    _edit(session_factory, lambda s: s.execute(text("UPDATE main.bacnet_objects SET units = 'percent' WHERE name = 'AV-1'")))
    await _apply(reloader, 2)
    assert _runtime(session_factory).apply_error is None
    assert app.get_object_name("AV-1") is not old


def _attach_failing(monkeypatch, fail):
    real = live_swap.attach

    def attach(app, obj, taken):
        if fail(obj):
            raise RuntimeError("attach failed")
        real(app, obj, taken)

    monkeypatch.setattr(live_swap, "attach", attach)
    return real


@pytest.mark.asyncio
async def test_a_failed_attach_keeps_the_old_object_and_its_trend_log_and_is_retried(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av(trended=True))
    old = app.get_object_name("AV-1")
    real = _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "AV-1" and obj is not old)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))
    await _apply(reloader)
    assert app.get_object_name("AV-1") is old
    assert len([o for o in app.objectIdentifier if str(o[0]) == "trend-log"]) == 1
    assert "attach failed" in _runtime(session_factory).apply_error

    monkeypatch.setattr(live_swap, "attach", real)
    await _apply(reloader, 2)  # no new edit: the snapshot kept the old row, so it's tried again
    assert app.get_object_name("AV-1") is not old
    assert str(app.get_object_name("AV-1").description) == "edited"
    assert _runtime(session_factory).apply_error is None


def _schedule_row(**fields):
    return BacnetObject(object_type="schedule", instance_number=1, name="SCH-1", schedule_value_type="analog",
                        schedule_default="0", schedule_priority=16,
                        weekly_schedule=[[{"time": "08:00", "value": "1"}] for _ in range(7)], **fields)


def _recording_builds(monkeypatch):
    built = []
    real = live_config.build_bacnet_object

    def build(*args, **kwargs):
        obj = real(*args, **kwargs)
        built.append(obj)
        return obj

    monkeypatch.setattr(live_config, "build_bacnet_object", build)
    return built


async def _has_a_live_timer_chain(obj):
    """Gives the loop turns, then reports whether obj still has a live
    evaluation chain (a handle that is not cancelled)."""
    for _ in range(3):
        await asyncio.sleep(0)
    handle = obj._interpret_schedule_handle
    return handle is not None and not handle.cancelled()


@pytest.mark.asyncio
async def test_a_schedule_whose_attach_failed_runs_no_evaluation_timer(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _schedule_row())
    old = app.get_object_name("SCH-1")
    built = _recording_builds(monkeypatch)
    _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "SCH-1" and obj is not old)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))

    await _apply(reloader)

    (new,) = built
    assert new is not old and app.get_object_name("SCH-1") is old
    assert not await _has_a_live_timer_chain(new)


@pytest.mark.asyncio
async def test_a_schedule_whose_trend_log_build_failed_runs_no_evaluation_timer(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _schedule_row())
    built = _recording_builds(monkeypatch)

    def failing(device, obj):
        raise RuntimeError("trend build failed")

    monkeypatch.setattr(reloader, "_build_trend_log", failing)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trended", True))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))

    await _apply(reloader)

    (new,) = built
    assert "trend build failed" in _runtime(session_factory).apply_error
    assert not await _has_a_live_timer_chain(new)


@pytest.mark.asyncio
async def test_a_failed_put_back_is_reported_not_raised(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av(1), _av(2))
    _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "AV-1")
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "description", "e"))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-2").one(), "description", "ok"))
    await _apply(reloader)
    error = _runtime(session_factory).apply_error
    assert "could not put the previous version back" in error
    assert "unexpected error" not in error  # handled per object, the rest applied
    assert str(app.get_object_name("AV-2").description) == "ok"


@pytest.mark.asyncio
async def test_a_put_back_schedule_keeps_its_next_transition(session_factory, monkeypatch):
    # Retire cancelled its interpretation timer; the put-back
    # attach re-arms it (bacpypes3's add_object interprets a schedule), so
    # this pins that upstream behaviour rather than BACpack code.
    row = BacnetObject(object_type="schedule", instance_number=1, name="SCH-1", schedule_value_type="analog",
                       schedule_default="0", schedule_priority=16,
                       weekly_schedule=[[{"time": "08:00", "value": "1"}] for _ in range(7)])
    app, reloader = await _running(session_factory, row)
    old = app.get_object_name("SCH-1")
    _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "SCH-1" and obj is not old)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))
    await _apply(reloader)
    assert app.get_object_name("SCH-1") is old
    handle = old._interpret_schedule_handle
    assert handle is not None and not handle.cancelled()


@pytest.mark.asyncio
async def test_a_put_back_alarming_object_is_evaluated(session_factory, monkeypatch):
    # A value change just before the apply queued an evaluation; retire
    # cancelled it, so the put-back object must be evaluated again.
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
                      alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0,
                      alarm_low_limit=-10.0, alarm_deadband=1.0, alarm_time_delay=0)
    app, reloader = await _running(session_factory, nc, ai)
    for _ in range(3):
        await asyncio.sleep(0)
    old = app.get_object_name("AI-1")
    assert old.eventState.attr == "normal"
    _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "AI-1" and obj is not old)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AI-1").one(), "description", "e"))
    real_build = live_config.build_bacnet_object

    def build_after_a_value_change(*args, **kwargs):
        # On the loop, in the swap: the value change queues an evaluation
        # (call_soon), which the swap's retire then cancels.
        old.presentValue = -50.0
        return real_build(*args, **kwargs)

    monkeypatch.setattr(live_config, "build_bacnet_object", build_after_a_value_change)
    await reloader.apply(1)
    for _ in range(3):
        await asyncio.sleep(0)
    assert app.get_object_name("AI-1") is old
    assert old.eventState.attr == "lowLimit"


@pytest.mark.asyncio
async def test_device_description_and_revision_apply_live_and_identity_sets_restart_pending(session_factory):
    app, reloader = await _running(session_factory, _av())

    def edit(session):
        device = session.get(Device, 1)
        device.description = "new words"
        device.database_revision = 7

    _edit(session_factory, edit)
    await _apply(reloader)
    assert str(app.device_object.description) == "new words"
    assert int(app.device_object.databaseRevision) == 7
    assert _runtime(session_factory).restart_pending is False

    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "vendor_name", "Other"))
    await _apply(reloader, 2)
    assert _runtime(session_factory).restart_pending is True
    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "vendor_name", None))
    await _apply(reloader, 3)
    assert _runtime(session_factory).restart_pending is False


@pytest.mark.asyncio
async def test_device_location_applies_live_without_a_restart(session_factory):
    app, reloader = await _running(session_factory, _av())
    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "location", "Plant room"))
    await _apply(reloader)
    assert str(app.device_object.location) == "Plant room"
    assert _runtime(session_factory).restart_pending is False


@pytest.mark.asyncio
async def test_device_backup_failure_timeout_applies_live_without_a_restart(session_factory):
    app, reloader = await _running(session_factory, _av())
    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "backup_failure_timeout", 15))
    await _apply(reloader)
    assert int(app.device_object.backupFailureTimeout) == 15
    assert app.device_object._backup_restore.timeout == 15
    assert _runtime(session_factory).restart_pending is False


@pytest.mark.asyncio
async def test_a_device_name_the_device_already_serves_is_not_a_pending_restart(session_factory):
    app, reloader = await _running(session_factory, _av())
    app.device_object.objectName = "Renamed"  # what a client's Object_Name write does live
    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "name", "Renamed"))
    await _apply(reloader)
    assert _runtime(session_factory).restart_pending is False


@pytest.mark.asyncio
async def test_a_replaced_calendar_reevaluates_its_schedules(session_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(LocalCalendarObject, "_reevaluate_referencing_schedules", lambda self: calls.append(self))
    calendar = BacnetObject(object_type="calendar", instance_number=1, name="CAL-1", date_list=[{"kind": "date", "month": 12, "day": 25}])
    app, reloader = await _running(session_factory, calendar)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))
    await _apply(reloader)
    assert calls == [app.get_object_name("CAL-1")]


@pytest.mark.asyncio
async def test_a_replaced_notification_class_keeps_its_alarming_objects_linked(session_factory):
    from bacpack import bacpypes3_compat

    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(object_type="analog-input", instance_number=1, name="AI-1", alarming_enabled=True,
                      notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=0.0)
    app, reloader = await _running(session_factory, nc, ai)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="NC-1").one(), "description", "e"))
    await _apply(reloader)
    assert bacpypes3_compat.notification_class_object(app.get_object_name("AI-1")) is app.get_object_name("NC-1")


def _awaits(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Await)]


def test_the_swap_never_awaits():
    """One synchronous step: no BACnet request is served half-way through.
    Before it, _apply awaits only the worker thread's read and the build,
    and the build only gives the loop turns; neither changes anything live."""
    assert not inspect.iscoroutinefunction(ObjectReloader._apply_objects)
    awaits = _awaits(ObjectReloader._apply)
    assert len(awaits) == 2 and "to_thread" in awaits[0] and "self._build(" in awaits[1]
    assert set(_awaits(ObjectReloader._build)) == {"await asyncio.sleep(0)"}


# --- building in turns -------------------------------------------------------

@pytest.mark.asyncio
async def test_an_apply_of_many_objects_gives_the_loop_turns_between_builds(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av())
    ticks = [0]

    async def ticker():
        while True:
            ticks[0] += 1
            await asyncio.sleep(0)

    seen = []
    real = live_config.build_bacnet_object

    def spy(*args, **kwargs):
        seen.append(ticks[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(live_config, "build_bacnet_object", spy)
    _edit(session_factory, lambda s: s.add_all([_av(i, device_id=1) for i in range(2, 7)]))
    task = asyncio.ensure_future(ticker())
    try:
        await _apply(reloader)
    finally:
        task.cancel()
    assert len(seen) == 5 and all(b > a for a, b in zip(seen, seen[1:]))
    assert all(app.get_object_name(f"AV-{i}") is not None for i in range(2, 7))


@pytest.mark.asyncio
async def test_what_a_build_queues_runs_only_once_the_object_is_attached(session_factory, monkeypatch):
    # With a turn after every build, bacpypes3's _post_init would otherwise run
    # on objects not yet attached: no _app, and a notification class added in
    # the same edit not there to link to.
    app, reloader = await _running(session_factory, _av())
    apps_seen = []
    real = LocalCalendarObject._post_init

    async def recording(self):
        apps_seen.append(bacpypes3_compat.object_app(self))
        await real(self)

    monkeypatch.setattr(LocalCalendarObject, "_post_init", recording)

    def add(s):
        s.add(BacnetObject(device_id=1, object_type="notification-class", instance_number=7, name="NC-7"))
        s.add(BacnetObject(device_id=1, object_type="calendar", instance_number=1, name="CAL-1",
                           date_list=[{"kind": "date", "month": 12, "day": 25}]))
        s.add(BacnetObject(device_id=1, object_type="analog-input", instance_number=1, name="AI-1",
                           alarming_enabled=True, notification_class_instance=7,
                           alarm_high_limit=100.0, alarm_low_limit=0.0))

    _edit(session_factory, add)
    await _apply(reloader)
    assert apps_seen == [app]
    assert bacpypes3_compat.notification_class_object(app.get_object_name("AI-1")) is app.get_object_name("NC-7")


@pytest.mark.asyncio
async def test_an_apply_logs_one_summary_line(session_factory, caplog):
    # A device writes each log line to SQLite on its loop: one line per
    # object made a 100-object edit 100 commits inside the swap.
    app, reloader = await _running(session_factory, _av(1), _av(9))

    def edit(s):
        lock_both_files(s)  # the delete writes history too
        s.add_all([_av(i, device_id=1) for i in range(2, 9)])
        s.query(BacnetObject).filter_by(name="AV-1").one().description = "edited"
        s.delete(s.query(BacnetObject).filter_by(name="AV-9").one())

    _edit(session_factory, edit)
    with caplog.at_level(logging.INFO, logger="bacpack.live_config"):
        await _apply(reloader)
    lines = [r.getMessage() for r in caplog.records if r.name == "bacpack.live_config"]
    assert lines == [
        "device 1: added 7 (analog-value:2 \"AV-2\", analog-value:3 \"AV-3\", analog-value:4 \"AV-4\", "
        "analog-value:5 \"AV-5\", analog-value:6 \"AV-6\" and 2 more), replaced 1 (analog-value:1 \"AV-1\"), "
        "removed 1 (analog-value:9 \"AV-9\")"
    ]


@pytest.mark.asyncio
async def test_a_stop_during_the_build_swaps_nothing(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av())
    built = asyncio.Event()
    real = live_config.build_bacnet_object

    def spy(*args, **kwargs):
        built.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(live_config, "build_bacnet_object", spy)
    works = []

    class Recording(bacpypes3_compat.DeferredLoopWork):
        def __init__(self):
            super().__init__()
            works.append(self)

    monkeypatch.setattr(bacpypes3_compat, "DeferredLoopWork", Recording)
    _edit(session_factory, lambda s: s.add_all([_av(i, device_id=1) for i in range(2, 5)]))
    task = asyncio.ensure_future(reloader.apply(1))
    await built.wait()
    (work,) = works
    held = [fn for kind, fn, *_ in work._calls if kind == "task"]
    assert held  # else the test proves nothing
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [app.get_object_name(f"AV-{i}") for i in range(2, 5)] == [None, None, None]
    assert sorted(reloader._rows) == [1]
    # Closed, not left to be garbage-collected as "never awaited".
    assert work._calls == [] and all(inspect.getcoroutinestate(c) == inspect.CORO_CLOSED for c in held)
    assert await reloader.apply(2) is True  # the lock was released
    assert app.get_object_name("AV-4") is not None


# --- trend sampling follows the swap -----------------------------------------
# TrendSampler resolved each trended object once at start: after a swap an
# interval trend kept sampling the retired object's frozen value and a COV
# trend's hook sat on the retired object, so nothing was recorded.

async def _sampling(session_factory, monkeypatch, *objects):
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)
    app, _ = await _running(session_factory, *objects)
    device, rows, *_ = _load_device_config(session_factory, 1)
    sampler = TrendSampler(session_factory, app, 1)
    sampler.start()
    reloader = ObjectReloader(session_factory, app, device, rows, trend_sampler=sampler)
    return app, reloader, sampler


def _sample_values(session_factory, name):
    with session_factory() as session:
        object_id = session.query(BacnetObject).filter_by(name=name).one().id
        return [s.value for s in session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id)]


async def _until_sampled(session_factory, name, value, timeout=10.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if value in _sample_values(session_factory, name):
            return True
        await asyncio.sleep(0.02)
    return False


def _due_now(sampler):
    # The next scheduler tick records every interval object, rather than
    # after its real interval, which a loaded parallel run can stretch past
    # the wait.
    for object_id in sampler._due:
        sampler._due[object_id] = 0.0


def _rename(session_factory, old, new):
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name=old).one(), "name", new))


@pytest.mark.asyncio
async def test_an_interval_trend_samples_the_swapped_object(session_factory, monkeypatch):
    app, reloader, sampler = await _sampling(
        session_factory, monkeypatch, _av(trended=True, trend_mode="interval", trend_interval_seconds=1))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))
    await _apply(reloader)
    await app.get_object_name("AV-1").write_property("presentValue", Real(55.0), priority=8)
    _due_now(sampler)
    try:
        assert await _until_sampled(session_factory, "AV-1", 55.0)
    finally:
        sampler.stop()


@pytest.mark.asyncio
async def test_a_cov_trend_follows_the_swapped_object_and_leaves_the_old_one(session_factory, monkeypatch):
    app, reloader, sampler = await _sampling(session_factory, monkeypatch, _av(trended=True, trend_mode="cov"))
    old = app.get_object_name("AV-1")
    (old_hook,) = [hook for _, hook in sampler._cov_hooks.values()]
    _rename(session_factory, "AV-1", "AV-R")
    await _apply(reloader)
    new = app.get_object_name("AV-R")
    assert new is not old
    try:
        assert old_hook not in bacpypes3_compat.property_monitors(old, "presentValue")
        await new.write_property("presentValue", Real(77.0), priority=8)
        assert await _until_sampled(session_factory, "AV-R", 77.0)
    finally:
        sampler.stop()


@pytest.mark.asyncio
async def test_an_object_trended_or_added_live_is_sampled(session_factory, monkeypatch):
    app, reloader, sampler = await _sampling(session_factory, monkeypatch, _av(1))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trended", True))
    _edit(session_factory, lambda s: s.add(_av(2, device_id=1, trended=True, trend_mode="cov")))
    await _apply(reloader)
    try:
        await app.get_object_name("AV-1").write_property("presentValue", Real(11.0), priority=8)
        await app.get_object_name("AV-2").write_property("presentValue", Real(22.0), priority=8)
        assert await _until_sampled(session_factory, "AV-2", 22.0)
        # AV-1 is an interval trend (default 60 s): it is scheduled, on the live object.
        assert sampler._interval_objects.get(1) is app.get_object_name("AV-1")
    finally:
        sampler.stop()


@pytest.mark.asyncio
async def test_a_trend_interval_change_and_a_delete_reach_the_sampler(session_factory, monkeypatch):
    app, reloader, sampler = await _sampling(
        session_factory, monkeypatch,
        _av(1, trended=True, trend_mode="interval", trend_interval_seconds=600), _av(2, trended=True, trend_mode="cov"))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "trend_interval_seconds", 1))
    _edit(session_factory, lambda s: s.query(BacnetObject).filter_by(name="AV-2").delete())
    await _apply(reloader)
    try:
        assert sampler._intervals[1] == 1
        assert 2 not in sampler._cov_hooks
        await app.get_object_name("AV-1").write_property("presentValue", Real(33.0), priority=8)
        _due_now(sampler)
        assert await _until_sampled(session_factory, "AV-1", 33.0)
    finally:
        sampler.stop()


# --- schedules release the slots they stop using -----------------------------
# A restart empties every priority array; a live edit doesn't, so a schedule
# releases the slots it stops using itself.

def _sched(instance=1, name="Sched-1", **fields):
    base = dict(object_type="schedule", instance_number=instance, name=name, schedule_value_type="analog",
                schedule_default="10.0", weekly_schedule=[[{"time": "00:00", "value": "10.0"}] for _ in range(7)],
                schedule_priority=8, schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1}])
    return BacnetObject(**{**base, **fields})


def _slot(obj, priority):
    pv = obj.priorityArray[priority - 1]
    return None if pv.null is not None else getattr(pv, pv._choice)


async def _turns():
    for _ in range(10):
        await asyncio.sleep(0.02)


async def _scheduled(session_factory, *extra):
    app, reloader = await _running(session_factory, _av(1), _sched(), *extra)
    await _turns()
    assert _slot(app.get_object_name("AV-1"), 8) == 10.0
    return app, reloader


def _edit_sched(session_factory, **fields):
    def fn(s):
        row = s.query(BacnetObject).filter_by(name="Sched-1").one()
        for key, value in fields.items():
            setattr(row, key, value)
    _edit(session_factory, fn)


@pytest.mark.asyncio
async def test_a_deleted_schedule_releases_its_slot(session_factory):
    app, reloader = await _scheduled(session_factory)
    _edit(session_factory, lambda s: s.query(BacnetObject).filter_by(name="Sched-1").delete())
    await _apply(reloader)
    await _turns()
    assert _slot(app.get_object_name("AV-1"), 8) is None


@pytest.mark.asyncio
async def test_a_priority_edit_moves_the_slot(session_factory):
    app, reloader = await _scheduled(session_factory)
    target = app.get_object_name("AV-1")
    seen = []
    bacpypes3_compat.property_monitors(target, "presentValue").append(lambda old, new: seen.append(new))
    _edit_sched(session_factory, schedule_priority=12)
    await _apply(reloader)
    await _turns()
    assert _slot(target, 12) == 10.0
    assert _slot(target, 8) is None
    assert 0.0 not in seen  # the new slot is written before the old is released


@pytest.mark.asyncio
async def test_a_removed_target_is_released_by_a_live_edit(session_factory):
    app, reloader = await _scheduled(session_factory)
    _edit_sched(session_factory, schedule_write_targets=[])
    await _apply(reloader)
    await _turns()
    assert _slot(app.get_object_name("AV-1"), 8) is None


@pytest.mark.asyncio
async def test_a_renumbered_schedule_keeps_commanding_its_target(session_factory):
    app, reloader = await _scheduled(session_factory)
    target = app.get_object_name("AV-1")
    seen = []
    bacpypes3_compat.property_monitors(target, "presentValue").append(lambda old, new: seen.append(new))
    _edit_sched(session_factory, instance_number=7)
    await _apply(reloader)
    await _turns()
    assert _slot(target, 8) == 10.0
    assert 0.0 not in seen  # no gap: the slot was never left empty


@pytest.mark.asyncio
async def test_a_description_edit_on_a_schedule_writes_nothing(session_factory, monkeypatch):
    from bacpack.objects.schedule import WritingScheduleObject

    app, reloader = await _scheduled(session_factory)
    calls = []
    real = WritingScheduleObject._apply_schedule_writes

    async def spy(self, atomic_value):
        calls.append(atomic_value)
        await real(self, atomic_value)

    monkeypatch.setattr(WritingScheduleObject, "_apply_schedule_writes", spy)
    _edit_sched(session_factory, description="edited")
    await _apply(reloader)
    await _turns()
    assert calls == []
    assert _slot(app.get_object_name("AV-1"), 8) == 10.0


@pytest.mark.asyncio
async def test_a_weekly_edit_that_changes_the_value_still_writes(session_factory):
    app, reloader = await _scheduled(session_factory)
    _edit_sched(session_factory, weekly_schedule=[[{"time": "00:00", "value": "30.0"}] for _ in range(7)])
    await _apply(reloader)
    await _turns()
    assert _slot(app.get_object_name("AV-1"), 8) == 30.0


@pytest.mark.asyncio
async def test_a_description_edit_on_a_schedule_away_from_its_default_writes_nothing(session_factory, monkeypatch):
    # Its rebuilt object starts at Schedule_Default (5.0), not the 10.0 it
    # serves: without the carried Present_Value its first evaluation would
    # "change" and write again.
    from bacpack.objects.schedule import WritingScheduleObject

    app, reloader = await _running(session_factory, _av(1), _sched(schedule_default="5.0"))
    await _turns()
    assert _slot(app.get_object_name("AV-1"), 8) == 10.0
    calls = []
    real = WritingScheduleObject._apply_schedule_writes

    async def spy(self, atomic_value):
        calls.append(atomic_value)
        await real(self, atomic_value)

    monkeypatch.setattr(WritingScheduleObject, "_apply_schedule_writes", spy)
    _edit_sched(session_factory, description="edited")
    await _apply(reloader)
    await _turns()
    assert calls == []


@pytest.mark.asyncio
async def test_an_apply_that_raises_after_attaching_is_retried_without_duplicates(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av())
    real = live_swap.restore_order
    monkeypatch.setattr(live_swap, "restore_order", lambda app, order: (_ for _ in ()).throw(RuntimeError("boom")))
    _edit(session_factory, lambda s: s.add(_av(2, device_id=1, trended=True)))
    assert await reloader.apply(1) is False  # not handled: DeviceWatch tries again
    assert "boom" in _runtime(session_factory).apply_error

    monkeypatch.setattr(live_swap, "restore_order", real)
    assert await reloader.apply(2) is True
    assert _runtime(session_factory).apply_error is None
    assert app.get_object_name("AV-2") is not None
    listed = [oid for oid in app.device_object.objectList if str(oid[0]) == "analog-value" and oid[1] == 2]
    assert len(listed) == 1
    assert len([o for o in app.objectIdentifier if str(o[0]) == "trend-log"]) == 1


class _FakeRefreezer:
    def __init__(self):
        self.calls = []

    def changed(self, count, device_objects):
        self.calls.append((count, device_objects))
        return False


async def _running_with_refreezer(session_factory, *objects):
    app, reloader = await _running(session_factory, *objects)
    reloader.refreezer = _FakeRefreezer()
    return app, reloader


def _describe(session_factory, **by_name):
    for name, text_ in by_name.items():
        _edit(session_factory, lambda s, n=name, t=text_: setattr(s.query(BacnetObject).filter_by(name=n.replace("_", "-")).one(), "description", t))


@pytest.mark.asyncio
async def test_a_swap_reports_what_it_retired_to_the_refreezer(session_factory):
    app, reloader = await _running_with_refreezer(session_factory, _av(1), _av(2))
    _describe(session_factory, AV_1="a", AV_2="b")
    await _apply(reloader)
    assert reloader.refreezer.calls == [(2, 2)]


@pytest.mark.asyncio
async def test_an_apply_that_retires_nothing_does_not_call_the_refreezer(session_factory):
    app, reloader = await _running_with_refreezer(session_factory, _av(1))
    await _apply(reloader, 3)
    assert reloader.refreezer.calls == []


@pytest.mark.asyncio
async def test_a_trended_objects_log_counts_too(session_factory):
    app, reloader = await _running_with_refreezer(session_factory, _av(trended=True))
    _describe(session_factory, AV_1="a")
    await _apply(reloader)
    assert reloader.refreezer.calls == [(2, 1)]


@pytest.mark.asyncio
async def test_retirements_from_an_apply_that_raised_still_count(session_factory, monkeypatch):
    app, reloader = await _running_with_refreezer(session_factory, _av(1))
    monkeypatch.setattr(live_swap, "restore_order", lambda app, order: (_ for _ in ()).throw(RuntimeError("boom")))
    _describe(session_factory, AV_1="a")
    assert await reloader.apply(1) is False
    assert len(reloader.refreezer.calls) == 1 and reloader.refreezer.calls[0][0] >= 1


@pytest.mark.asyncio
async def test_an_object_added_live_counts_toward_the_refreeze(session_factory):
    # Built after the freeze, it would stay in every full collection.
    app, reloader = await _running_with_refreezer(session_factory, _av(1))
    _edit(session_factory, lambda s: s.add(_av(2, device_id=1)))
    await _apply(reloader)
    assert reloader.refreezer.calls == [(1, 2)]


@pytest.mark.asyncio
async def test_a_real_refreezer_refreezes_once_an_apply_crosses_the_floor(session_factory):
    calls = []
    app, reloader = await _running(session_factory, _av(1))
    reloader.refreezer = gc_freeze.Refreezer(
        collect=lambda: calls.append("collect"), freeze=lambda: calls.append("freeze"),
        unfreeze=lambda: calls.append("unfreeze"),
    )
    reloader.refreezer.pending = gc_freeze.MIN_REFREEZE_OBJECTS - 1
    _describe(session_factory, AV_1="a")
    await _apply(reloader)
    assert calls == ["unfreeze", "collect", "freeze"]
    assert reloader.refreezer.pending == 0


@pytest.mark.asyncio
async def test_no_refreezer_is_fine(session_factory):
    app, reloader = await _running(session_factory, _av(1))
    assert reloader.refreezer is None
    _describe(session_factory, AV_1="a")
    await _apply(reloader)
    assert str(app.get_object_name("AV-1").description) == "a"


@pytest.mark.asyncio
async def test_a_repeating_apply_failure_is_logged_once(session_factory, monkeypatch, caplog):
    app, reloader = await _running(session_factory, _av())
    monkeypatch.setattr(live_swap, "restore_order", lambda app, order: (_ for _ in ()).throw(RuntimeError("boom")))
    _edit(session_factory, lambda s: s.add(_av(2, device_id=1)))
    for seq in (1, 2, 3):
        assert await reloader.apply(seq) is False
    failures = [r for r in caplog.records if r.getMessage() == "device 1: applying config changes failed"]
    assert len(failures) == 1


# --- reading and comparing off the device's loop ----------------------------

@pytest.mark.asyncio
async def test_an_apply_reads_and_compares_off_the_loop(session_factory, monkeypatch):
    # On a big device the read, check and diff block the loop for a quarter
    # second.
    app, reloader = await _running(session_factory, _av())
    loop_thread = threading.get_ident()
    seen = []

    def factory():
        seen.append(("session", threading.get_ident()))
        return session_factory()

    def spying(name, real):
        def spy(*args, **kwargs):
            seen.append((name, threading.get_ident()))
            return real(*args, **kwargs)
        return spy

    reloader.session_factory = factory
    monkeypatch.setattr(live_config.object_config, "check_device",
                        spying("check", live_config.object_config.check_device))
    monkeypatch.setattr(live_config, "diff_rows", spying("diff", live_config.diff_rows))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "description", "edited"))
    await _apply(reloader)
    assert app.get_object_name("AV-1").description == "edited"
    assert {name for name, _ in seen} == {"session", "check", "diff"}
    assert [name for name, thread in seen if thread == loop_thread] == []


@pytest.mark.asyncio
async def test_applies_do_not_overlap(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av())
    started, release = threading.Event(), threading.Event()
    loads = []
    real = reloader._load

    def slow_load():
        loads.append("start")
        if len(loads) == 1:
            started.set()
            release.wait(5)
        result = real()
        loads.append("end")
        return result

    monkeypatch.setattr(reloader, "_load", slow_load)
    first = asyncio.ensure_future(reloader.apply(1))
    await asyncio.to_thread(started.wait, 5)
    second = asyncio.ensure_future(reloader.apply(2))
    for _ in range(5):
        await asyncio.sleep(0)
    assert loads == ["start"]
    release.set()
    assert await first is True and await second is True
    assert loads == ["start", "end", "start", "end"]


# --- network role and links --------------------------------------------------

async def _bbmd_linked_to_another_device(session_factory):
    """Device 1 a BBMD on port 47810 linked to device 2 (47811); device 3 unlinked."""
    with session_factory() as session:
        session.add_all([
            Device(id=1, name="D", bind_address="127.0.0.1/8:47810", network_role="bbmd"),
            Device(id=2, name="E", bind_address="127.0.0.1/8:47811"),
            Device(id=3, name="F", bind_address="127.0.0.1/8:47812"),
        ])
        session.flush()
        session.get(Device, 1).bbmd_links = [DeviceLink(linked_device_id=2)]
        session.commit()
    device, rows, _bind, network = _load_device_config(session_factory, 1)
    app = build_application(device, rows, trend_session_factory=session_factory)
    await asyncio.sleep(0)
    return ObjectReloader(session_factory, app, device, rows, network=network)


@pytest.mark.asyncio
async def test_a_linked_devices_port_change_makes_its_bbmd_pending_a_restart(session_factory):
    reloader = await _bbmd_linked_to_another_device(session_factory)
    await _apply(reloader, 1)
    assert _runtime(session_factory).restart_pending is False

    _edit(session_factory, lambda s: setattr(s.get(Device, 2), "bind_address", "127.0.0.1/8:47813"))
    await _apply(reloader, 2)
    assert _runtime(session_factory).restart_pending is True


@pytest.mark.asyncio
async def test_a_role_change_on_the_device_itself_is_pending_a_restart(session_factory):
    reloader = await _bbmd_linked_to_another_device(session_factory)
    _edit(session_factory, lambda s: setattr(s.get(Device, 1), "network_role", "normal"))
    await _apply(reloader, 1)
    assert _runtime(session_factory).restart_pending is True


@pytest.mark.asyncio
async def test_an_unrelated_devices_port_change_is_not(session_factory):
    reloader = await _bbmd_linked_to_another_device(session_factory)
    _edit(session_factory, lambda s: setattr(s.get(Device, 3), "bind_address", "127.0.0.1/8:47814"))
    await _apply(reloader, 1)
    assert _runtime(session_factory).restart_pending is False


@pytest.mark.asyncio
async def test_a_deleted_link_is_pending_a_restart(session_factory):
    reloader = await _bbmd_linked_to_another_device(session_factory)
    _edit(session_factory, lambda s: s.execute(delete(DeviceLink)))
    await _apply(reloader, 1)
    assert _runtime(session_factory).restart_pending is True


def test_changing_active_text_rebuilds_the_object():
    old = {1: _row(object_type="binary-value", active_text=None)}
    new = {1: _row(object_type="binary-value", active_text="Running")}
    diff = diff_rows(old, new)
    assert diff.rebuilt == {1: frozenset({"active_text"})}
    assert diff.trend_only == []


def test_changing_the_trend_log_name_rebuilds_only_the_trend_log():
    old = {1: _row(trended=True, trend_log_name=None, trend_log_instance=None)}
    new = {1: _row(trended=True, trend_log_name="History", trend_log_instance=None)}
    diff = diff_rows(old, new)
    assert diff.trend_only == [1] and diff.rebuilt == {} and diff.removed == []
    new = {1: _row(trended=True, trend_log_name=None, trend_log_instance=9)}
    assert diff_rows(old, new).trend_only == [1]


def _logs(app):
    return sorted(int(o[1]) for o in app.objectIdentifier if str(o[0]) == "trend-log")


@pytest.mark.asyncio
async def test_changing_the_trend_log_instance_replaces_the_old_log(session_factory):
    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77))
    assert _logs(app) == [77]
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trend_log_instance", 78))
    await _apply(reloader)
    assert _logs(app) == [78]
    assert len([o for o in app.device_object.objectList if str(o[0]) == "trend-log"]) == 1


@pytest.mark.asyncio
async def test_changing_a_default_log_to_a_fixed_instance_moves_it(session_factory):
    app, reloader = await _running(session_factory, _av(trended=True))
    [default] = _logs(app)
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trend_log_instance", default + 50))
    await _apply(reloader)
    assert _logs(app) == [default + 50]


@pytest.mark.asyncio
async def test_untrending_a_point_with_a_fixed_instance_removes_its_log(session_factory):
    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trended", False))
    await _apply(reloader)
    assert _logs(app) == []


@pytest.mark.asyncio
async def test_removing_a_point_with_a_fixed_instance_removes_its_log(session_factory):
    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77), _av(2))
    _edit(session_factory, lambda s: s.delete(s.query(BacnetObject).filter_by(name="AV-1").one()))
    await _apply(reloader)
    assert _logs(app) == [] and app.get_object_name("AV-1") is None


@pytest.mark.asyncio
async def test_two_logs_can_swap_instances_in_one_apply(session_factory):
    app, reloader = await _running(
        session_factory, _av(1, trended=True, trend_log_instance=77), _av(2, trended=True, trend_log_instance=78)
    )

    def swap(session):
        session.query(BacnetObject).filter_by(name="AV-1").one().trend_log_instance = 78
        session.query(BacnetObject).filter_by(name="AV-2").one().trend_log_instance = 77

    _edit(session_factory, swap)
    await _apply(reloader)
    assert _logs(app) == [77, 78]
    assert app.get_object_id(ObjectIdentifier(("trend-log", 78)))._trend_object_id == app.get_object_name("AV-1")._row_id


@pytest.mark.asyncio
async def test_a_failed_attach_puts_the_old_log_back_at_its_old_instance(session_factory, monkeypatch):
    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77))
    old = app.get_object_name("AV-1")
    _attach_failing(monkeypatch, lambda obj: str(obj.objectName) == "AV-1" and obj is not old)

    def edit(session):
        row = session.query(BacnetObject).one()
        row.description = "edited"
        row.trend_log_instance = 78

    _edit(session_factory, edit)
    await _apply(reloader)
    assert app.get_object_name("AV-1") is old
    assert _logs(app) == [77]


class _NoScan(dict):
    """An object dict that fails when anything walks it."""

    def values(self):
        raise AssertionError("scanned every object")

    items = keys = __iter__ = values

    def plain(self):
        return dict(dict.items(self))


@pytest.mark.asyncio
async def test_finding_a_fixed_instance_log_by_row_id_scans_no_objects(session_factory):
    app, reloader = await _running(
        session_factory, _av(1, trended=True, trend_log_instance=77), _av(2, trended=True)
    )
    ids = [app.get_object_name("AV-1")._row_id, app.get_object_name("AV-2")._row_id]
    expected = {id(app.get_object_id(ObjectIdentifier(("trend-log", n)))) for n in (77, ids[1])}
    app.objectIdentifier = _NoScan(app.objectIdentifier)
    assert {id(log) for log in app._trend_logs_of(ids)} == expected
    app.objectIdentifier = app.objectIdentifier.plain()

    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "trend_log_instance", 78))
    await _apply(reloader)
    moved = app.get_object_id(ObjectIdentifier(("trend-log", 78)))
    app.objectIdentifier = _NoScan(app.objectIdentifier)
    assert app._trend_logs_of([ids[0]]) == [moved]
    app.objectIdentifier = app.objectIdentifier.plain()

    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).filter_by(name="AV-1").one(), "trended", False))
    await _apply(reloader)
    app.objectIdentifier = _NoScan(app.objectIdentifier)
    assert app._trend_logs_of([ids[0]]) == []


@pytest.mark.asyncio
async def test_cov_subscriptions_do_not_follow_a_trend_log_to_a_new_instance(session_factory):
    from bacpypes3.apdu import SubscribeCOVRequest
    from bacpypes3.pdu import Address

    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77))
    app.response = lambda apdu: asyncio.sleep(0)
    app.cov_notification = lambda cov, apdu: None
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=3, monitoredObjectIdentifier=("trend-log", 77),
        issueConfirmedNotifications=False, lifetime=300,
    )
    request.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(request)
    assert len(app._cov_detections[ObjectIdentifier(("trend-log", 77))].cov_subscriptions) == 1

    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trend_log_instance", 78))
    await _apply(reloader)

    assert _logs(app) == [78]
    assert ObjectIdentifier(("trend-log", 78)) not in app._cov_detections


@pytest.mark.asyncio
async def test_cov_subscriptions_stay_with_a_trend_log_that_keeps_its_instance(session_factory):
    from bacpypes3.apdu import SubscribeCOVRequest
    from bacpypes3.pdu import Address

    app, reloader = await _running(session_factory, _av(trended=True, trend_log_instance=77))
    app.response = lambda apdu: asyncio.sleep(0)
    app.cov_notification = lambda cov, apdu: None
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=3, monitoredObjectIdentifier=("trend-log", 77),
        issueConfirmedNotifications=False, lifetime=300,
    )
    request.pduSource = Address("192.168.1.99")
    await app.do_SubscribeCOVRequest(request)

    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trend_interval_seconds", 30))
    _edit(session_factory, lambda s: setattr(s.query(BacnetObject).one(), "trend_log_name", "Renamed"))
    await _apply(reloader)

    assert len(app._cov_detections[ObjectIdentifier(("trend-log", 77))].cov_subscriptions) == 1
