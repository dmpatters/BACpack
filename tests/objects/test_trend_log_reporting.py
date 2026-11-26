"""BUFFER_READY reporting on Trend Log objects (T-ATR-B). Devices are built
from saved rows with a real session factory, the way a running device builds
them, and outbound notifications are captured at app.request."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.apdu import AcknowledgeAlarmRequest, GetEventInformationRequest
from bacpypes3.basetypes import TimeStamp
from bacpypes3.primitivedata import ObjectIdentifier, Unsigned

from bacpack.models import BacnetObject, Device, TrendSample, TrendState
from bacpack.object_builder import build_application
from bacpack.objects.trend_log import LocalTrendLogObject
from bacpack.objects.trend_log_reporting import ReportingTrendLogObject
from bacpack.trending import _MAX_SEQUENCE, record_sample
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked

_RECIPIENT = {
    "recipient": {"kind": "address", "address": "192.168.1.77"},
    "valid_days": [True] * 7,
    "from_time": "00:00",
    "to_time": "23:59",
    "transitions": [True, True, True],
    "confirmed": False,
    "process_identifier": 5,
}


async def _build(session_factory, *, ack_required=False, nc_priority=None, **av_overrides):
    """Device 599001 with NC-1 (one unconfirmed recipient) and a trended,
    reporting AV-1 (threshold 3 unless overridden). Returns (app, log,
    av_id, sent) where `sent` collects every outbound APDU."""
    fields = dict(
        object_type="analog-value",
        instance_number=1,
        name="AV-1",
        units="percent",
        trended=True,
        trend_reporting_enabled=True,
        trend_notification_threshold=3,
        trend_notification_class_instance=1,
    )
    fields.update(av_overrides)
    with session_factory() as session:
        device = Device(id=599001, name="Test Device", vendor_identifier=999)
        session.add(device)
        nc = dict(
            device=device,
            object_type="notification-class",
            instance_number=1,
            name="NC-1",
            notification_ack_required=[ack_required] * 3,
            notification_recipient_list=[_RECIPIENT],
        )
        if nc_priority is not None:
            nc["notification_priority"] = nc_priority
        session.add(BacnetObject(**nc))
        state = {
            key: fields.pop(f"trend_{key}")
            for key in ("next_sequence", "last_notify_record")
            if f"trend_{key}" in fields
        }
        if state:
            fields["trend_state"] = TrendState(**state)
        av = BacnetObject(device=device, **fields)
        session.add(av)
        session.commit()
        av_id = av.id
        app = build_application(device, list(device.objects), trend_session_factory=session_factory)
    await asyncio.sleep(0)  # Object._post_init links the log to its notification class
    sent = []

    def _capture(apdu):
        sent.append(apdu)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    app.request = _capture
    log = app.get_object_id(ObjectIdentifier(("trend-log", av_id)))
    return app, log, av_id, sent


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _until(condition, timeout=10.0):
    # In a thread: a condition that reads the database on the loop would add
    # its own stalls to a loop-gap measurement it runs inside.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await asyncio.to_thread(condition):
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _sample_count(session_factory, av_id):
    with session_factory() as session:
        return session.query(TrendSample).filter_by(object_id=av_id).count()


async def _add_records(session_factory, log, av_id, n):
    for i in range(n):
        record_sample(session_factory, av_id, float(i))
    log.records_added()
    await _settle()


def _saved_last_notify(session_factory, av_id):
    with session_factory() as session:
        return session.get(BacnetObject, av_id).trend_state.last_notify_record


@pytest.mark.asyncio
async def test_reporting_log_carries_the_reporting_properties(session_factory):
    app, log, av_id, _ = await _build(session_factory)
    assert isinstance(log, ReportingTrendLogObject)
    assert log.eventState.attr == "normal"
    assert int(log.notificationThreshold) == 3
    assert int(log.lastNotifyRecord) == 0
    assert int(log.recordsSinceNotification) == 0
    assert int(log.notificationClass) == 1
    assert log.notifyType.attr == "event"
    assert list(log.ackedTransitions) == [1, 1, 1]
    assert log._notification_class_object is not None


@pytest.mark.asyncio
async def test_non_reporting_trended_object_gets_a_plain_trend_log(session_factory):
    app, log, av_id, _ = await _build(session_factory, trend_reporting_enabled=False)
    assert type(log) is LocalTrendLogObject
    assert getattr(log, "_event_algorithm", None) is None


@pytest.mark.asyncio
async def test_nothing_is_sent_below_the_threshold(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    await _add_records(session_factory, log, av_id, 2)
    assert sent == []
    assert int(log.recordsSinceNotification) == 2


@pytest.mark.asyncio
async def test_one_notification_at_the_threshold(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    await _add_records(session_factory, log, av_id, 3)

    assert len(sent) == 1
    n = sent[0]
    assert type(n).__name__ == "UnconfirmedEventNotificationRequest"
    assert n.eventType.attr == "bufferReady"
    assert n.fromState.attr == "normal" and n.toState.attr == "normal"
    assert n.notifyType.attr == "event"
    assert int(n.processIdentifier) == 5
    values = n.eventValues.bufferReady
    assert int(values.previousNotification) == 0
    assert int(values.currentNotification) == 3
    assert values.bufferProperty.objectIdentifier == ObjectIdentifier(("trend-log", av_id))
    assert values.bufferProperty.propertyIdentifier.attr == "logBuffer"
    assert values.bufferProperty.deviceIdentifier == ObjectIdentifier(("device", 599001))
    assert n.encode() is not None
    assert int(log.lastNotifyRecord) == 3
    assert int(log.recordsSinceNotification) == 0
    await _until(lambda: _saved_last_notify(session_factory, av_id) == 3)


@pytest.mark.asyncio
async def test_buffer_ready_carries_the_to_normal_priority(session_factory):
    # bacpypes3 read the NC's slot 0 (to-offnormal) for a
    # NORMAL-to-NORMAL transition.
    app, log, av_id, sent = await _build(session_factory, nc_priority=[10, 20, 30])
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 1
    assert int(sent[0].priority) == 30


@pytest.mark.asyncio
async def test_a_burst_over_the_threshold_sends_one_notification(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    await _add_records(session_factory, log, av_id, 8)
    assert len(sent) == 1
    assert int(sent[0].eventValues.bufferReady.currentNotification) == 8
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 2
    assert int(sent[1].eventValues.bufferReady.previousNotification) == 8
    assert int(sent[1].eventValues.bufferReady.currentNotification) == 11


@pytest.mark.asyncio
async def test_threshold_zero_never_notifies(session_factory):
    app, log, av_id, sent = await _build(session_factory, trend_notification_threshold=0)
    await _add_records(session_factory, log, av_id, 5)
    assert sent == []


@pytest.mark.asyncio
async def test_lowering_the_threshold_below_pending_fires_at_once(session_factory):
    app, log, av_id, sent = await _build(session_factory, trend_notification_threshold=10)
    await _add_records(session_factory, log, av_id, 4)
    assert sent == []
    log.notificationThreshold = Unsigned(4)  # monitored binding re-runs the algorithm
    await _settle()
    assert len(sent) == 1
    assert int(sent[0].eventValues.bufferReady.currentNotification) == 4


@pytest.mark.asyncio
async def test_detection_disabled_suppresses_and_reenabling_fires(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    log.eventDetectionEnable = False
    await _settle()
    await _add_records(session_factory, log, av_id, 5)
    assert sent == []
    log.eventDetectionEnable = True
    await _settle()
    assert len(sent) == 1
    assert int(sent[0].eventValues.bufferReady.currentNotification) == 5


@pytest.mark.asyncio
async def test_restart_reports_the_gap_on_the_next_record(session_factory):
    # A previous run notified at 3 and then recorded 4 more without
    # reaching the threshold of 5; this run picks up from the saved value.
    app, log, av_id, sent = await _build(
        session_factory, trend_notification_threshold=5, trend_last_notify_record=3, trend_next_sequence=8
    )
    assert int(log.recordsSinceNotification) == 4
    await _add_records(session_factory, log, av_id, 1)
    assert len(sent) == 1
    values = sent[0].eventValues.bufferReady
    assert (int(values.previousNotification), int(values.currentNotification)) == (3, 8)


@pytest.mark.asyncio
async def test_the_counter_wrap_is_handled(session_factory):
    app, log, av_id, sent = await _build(
        session_factory, trend_next_sequence=_MAX_SEQUENCE - 1, trend_last_notify_record=_MAX_SEQUENCE - 2
    )
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 1
    values = sent[0].eventValues.bufferReady
    assert (int(values.previousNotification), int(values.currentNotification)) == (_MAX_SEQUENCE - 2, 1)


@pytest.mark.asyncio
async def test_no_ack_required_leaves_acked_transitions_alone(session_factory):
    app, log, av_id, sent = await _build(session_factory, ack_required=False)
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 1
    assert list(log.ackedTransitions) == [1, 1, 1]


async def _send(app, req):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await getattr(app, f"do_{type(req).__name__}")(req)
    return captured[0] if captured else None


@pytest.mark.asyncio
async def test_ack_required_notification_is_listed_until_acknowledged(session_factory):
    app, log, av_id, sent = await _build(session_factory, ack_required=True)
    await _add_records(session_factory, log, av_id, 3)
    assert sent[0].ackRequired
    assert list(log.ackedTransitions) == [1, 1, 0]

    info = await _send(app, GetEventInformationRequest())
    listed = [s.objectIdentifier for s in info.listOfEventSummaries]
    assert ObjectIdentifier(("trend-log", av_id)) in listed

    await _send(
        app,
        AcknowledgeAlarmRequest(
            acknowledgingProcessIdentifier=5,
            eventObjectIdentifier=("trend-log", av_id),
            eventStateAcknowledged="normal",
            timeStamp=log.eventTimeStamps[2],
            acknowledgmentSource="test",
            timeOfAcknowledgment=TimeStamp(sequenceNumber=0),
        ),
    )
    assert list(log.ackedTransitions) == [1, 1, 1]
    info = await _send(app, GetEventInformationRequest())
    assert ObjectIdentifier(("trend-log", av_id)) not in [s.objectIdentifier for s in info.listOfEventSummaries]


@pytest.mark.asyncio
async def test_disable_initiation_drops_the_send_but_still_advances(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    # Put the real request path back behind a spy, so the DCC gate in
    # LoggingApplication.request is what decides.
    del app.request
    app._dcc_state = "disableInitiation"
    spied, futures = [], []
    real_request = app.request

    def _spy(apdu):
        spied.append(apdu)
        future = real_request(apdu)
        futures.append(future)
        return future

    app.request = _spy
    await _add_records(session_factory, log, av_id, 3)
    assert len(spied) == 1
    assert await asyncio.wait_for(futures[0], timeout=1.0) is None  # suppressed, never sent
    assert int(log.lastNotifyRecord) == 3
    await _until(lambda: _saved_last_notify(session_factory, av_id) == 3)


@pytest.mark.asyncio
async def test_the_last_notify_record_save_waits_for_a_held_lock_off_the_loop(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    for i in range(3):
        record_sample(session_factory, av_id, float(i))

    async def notify_and_wait_for_the_save():
        log.records_added()
        await _until(lambda: _saved_last_notify(session_factory, av_id) == 3)

    run = await run_while_locked(session_factory, notify_and_wait_for_the_save, history=True)

    assert_waited_off_the_loop(run)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_failed_save_is_logged_not_raised(session_factory, monkeypatch, caplog):
    app, log, av_id, sent = await _build(session_factory)

    def _boom():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(log, "_trend_session_factory_for_save", _boom, raising=False)
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 1
    assert int(log.lastNotifyRecord) == 3
    await _until(lambda: "could not save Last_Notify_Record" in caplog.text)


# --- BACnet writes ------------------------------------------------------------

from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.errors import PropertyError


def _typed(obj, attr, value):
    element = obj._elements[attr]
    return element(element.cast(value))


def _column(session_factory, av_id, column):
    with session_factory() as session:
        return getattr(session.get(BacnetObject, av_id), column)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value, column, saved",
    [
        ("notificationThreshold", 7, "trend_notification_threshold", 7),
        ("eventEnable", [True, False, True], "trend_event_enable", [True, False, True]),
        ("notifyType", "alarm", "trend_notify_type", "alarm"),
        ("eventDetectionEnable", False, "trend_event_detection_enable", False),
    ],
)
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_reporting_write_is_applied_and_saved(session_factory, attr, value, column, saved, as_identifier):
    app, log, av_id, _ = await _build(session_factory)
    await log.write_property(PropertyIdentifier(attr) if as_identifier else attr, _typed(log, attr, value))
    assert getattr(log, attr) == _typed(log, attr, value)
    assert _column(session_factory, av_id, column) == saved


@pytest.mark.asyncio
async def test_notification_class_write_relinks_and_saves(session_factory):
    app, log, av_id, _ = await _build(session_factory)
    with session_factory() as session:
        device = session.get(Device, 599001)
        session.add(BacnetObject(device=device, object_type="notification-class", instance_number=2, name="NC-2"))
        session.commit()
    from bacpack.objects.notification_class import LocalNotificationClassObject

    nc2 = LocalNotificationClassObject(
        objectIdentifier=("notification-class", 2), objectName="NC-2", notificationClass=2,
        priority=[1, 2, 3], ackRequired=[False, False, False], recipientList=[],
    )
    app.add_object(nc2)
    await log.write_property("notificationClass", _typed(log, "notificationClass", 2))
    assert log._notification_class_object is nc2
    assert _column(session_factory, av_id, "trend_notification_class_instance") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value, column",
    [
        ("notificationThreshold", 2**32, "trend_notification_threshold"),
        ("notificationThreshold", -1, "trend_notification_threshold"),
        ("eventEnable", [True, True, True, True], "trend_event_enable"),
        ("notifyType", "ackNotification", "trend_notify_type"),
        ("notificationClass", 9, "trend_notification_class_instance"),
    ],
)
async def test_bad_reporting_write_is_rejected_and_nothing_changes(session_factory, attr, value, column):
    app, log, av_id, _ = await _build(session_factory)
    before_live = getattr(log, attr)
    before_saved = _column(session_factory, av_id, column)
    with pytest.raises(PropertyError):
        await log.write_property(attr, value)
    assert getattr(log, attr) == before_live
    assert _column(session_factory, av_id, column) == before_saved


@pytest.mark.asyncio
async def test_indexed_reporting_write_is_rejected(session_factory):
    app, log, av_id, _ = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await log.write_property("eventEnable", [True, True, True], index=1)
    assert excinfo.value.errorCode == "propertyIsNotAnArray"


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, value", [("objectName", "x"), ("lastNotifyRecord", 0), ("enable", False)])
async def test_other_properties_stay_denied_on_a_reporting_log(session_factory, attr, value):
    app, log, av_id, _ = await _build(session_factory)
    with pytest.raises(PropertyError) as excinfo:
        await log.write_property(attr, value)
    assert excinfo.value.errorCode == "writeAccessDenied"


@pytest.mark.asyncio
async def test_threshold_write_takes_effect_live(session_factory):
    app, log, av_id, sent = await _build(session_factory, trend_notification_threshold=10)
    await _add_records(session_factory, log, av_id, 4)
    await log.write_property("notificationThreshold", _typed(log, "notificationThreshold", 4))
    await _settle()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_written_threshold_survives_a_rebuild(session_factory):
    app, log, av_id, _ = await _build(session_factory)
    await log.write_property("notificationThreshold", _typed(log, "notificationThreshold", 42))
    with session_factory() as session:
        device = session.get(Device, 599001)
        rebuilt = build_application(device, list(device.objects), trend_session_factory=session_factory)
    assert int(rebuilt.get_object_id(ObjectIdentifier(("trend-log", av_id))).notificationThreshold) == 42


# --- Triggers -----------------------------------------------------------

from bacpack import trending
from bacpack.bacnet import write_feed_value
from bacpack.clock_sync import apply_clock_offset
from bacpack.trending import TrendSampler

# The clock jump test moves the process-wide device clock; conftest.py's
# autouse _reset_device_clock puts it back after each test.


@pytest.mark.asyncio
async def test_trend_records_added_skips_missing_and_plain_logs(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    for i in range(3):
        record_sample(session_factory, av_id, float(i))
    app.trend_records_added([av_id, 99999])  # 99999: no such trend log
    await _settle()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_interval_sampling_triggers_a_notification(session_factory, monkeypatch):
    monkeypatch.setattr(trending, "SCHEDULER_TICK_SECONDS", 0.01)
    app, log, av_id, sent = await _build(
        session_factory, trend_mode="interval", trend_interval_seconds=1, trend_notification_threshold=2
    )
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    loop = asyncio.get_event_loop()
    try:
        # Samples are written in a worker thread: wait for each, as in the COV test.
        for count in (1, 2):
            sampler._due[av_id] = loop.time()  # due now
            await _until(lambda: _sample_count(session_factory, av_id) == count)
        await _until(lambda: len(sent) == 1)
    finally:
        sampler.stop()
    await _settle()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_cov_sampling_triggers_a_notification(session_factory):
    app, log, av_id, sent = await _build(session_factory, trend_mode="cov", trend_notification_threshold=2)
    sampler = TrendSampler(session_factory, app, device_id=599001)
    sampler.start()
    av = app.get_object_name("AV-1")
    try:
        # A COV sample is written in a worker thread: wait for each one, not a
        # fixed time, so a loaded machine can't leave the second one pending.
        for count, value in enumerate((10.0, 20.0), start=1):  # well over covIncrement 1.0
            await write_feed_value(av, value)
            await _until(lambda: _sample_count(session_factory, av_id) == count)
        await _until(lambda: len(sent) == 1)
    finally:
        sampler.stop()
    await _settle()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_clock_jump_record_counts_and_triggers(session_factory):
    app, log, av_id, sent = await _build(session_factory, trend_notification_threshold=1)
    await apply_clock_offset(session_factory, app, 599001, 3600.0, "test")
    await _settle()
    assert len(sent) == 1


# --- Event_Enable's to-normal bit gates the send ---------------------------


@pytest.mark.asyncio
async def test_event_enable_to_normal_off_suppresses_the_send_but_advances(session_factory):
    app, log, av_id, sent = await _build(session_factory, ack_required=True, trend_event_enable=[True, True, False])
    await _add_records(session_factory, log, av_id, 3)
    assert sent == []
    assert int(log.lastNotifyRecord) == 3
    await _until(lambda: _saved_last_notify(session_factory, av_id) == 3)
    assert list(log.ackedTransitions) == [1, 1, 1]  # nothing was sent, so nothing to acknowledge


@pytest.mark.asyncio
async def test_event_enable_write_takes_effect_live(session_factory):
    app, log, av_id, sent = await _build(session_factory)
    await log.write_property("eventEnable", _typed(log, "eventEnable", [True, True, False]))
    await _add_records(session_factory, log, av_id, 3)
    assert sent == []
    await log.write_property("eventEnable", _typed(log, "eventEnable", [True, True, True]))
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == 1
    assert int(sent[0].eventValues.bufferReady.previousNotification) == 3


@pytest.mark.asyncio
async def test_trend_records_added_reaches_a_log_with_a_fixed_instance(session_factory):
    app, _, av_id, sent = await _build(session_factory, trend_log_instance=77)
    assert app.get_object_id(ObjectIdentifier(("trend-log", av_id))) is None
    for i in range(3):
        record_sample(session_factory, av_id, float(i))
    app.trend_records_added([av_id])
    await _settle()
    assert len(sent) == 1


# Valid_Days follows the device clock's weekday, not bacpypes3's fixed
# Thursday: BUFFER_READY goes through the same send_notifications.
@pytest.mark.asyncio
@pytest.mark.parametrize("valid_only_today", [True, False])
async def test_buffer_ready_honours_valid_days_on_the_device_clock(session_factory, valid_only_today):
    from bacpack import device_clock

    device_clock.install()
    device_clock.set_offset(device_clock.offset_for_local(2026, 11, 15, 12, 0, 0))  # a Sunday

    sunday = [False] * 6 + [True]
    days = sunday if valid_only_today else [not d for d in sunday]
    app, log, av_id, sent = await _build(session_factory)
    nc = app.get_object_name("NC-1")
    for destination in nc.recipientList:
        destination.validDays = days
    await _add_records(session_factory, log, av_id, 3)
    assert len(sent) == (1 if valid_only_today else 0)
