"""Covers native BACnet Trend_Log objects and the from-scratch ReadRange
implementation. bacpypes3's own ReadRangeServices.do_ReadRangeRequest is
`raise NotImplementedError()`, so this is exercising BACpack's own protocol
code, not just wiring.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from bacpypes3.apdu import ReadPropertyMultipleRequest, ReadPropertyRequest, ReadRangeRequest
from bacpypes3.basetypes import (
    LogRecord,
    PropertyReference,
    Range,
    RangeByPosition,
    RangeBySequenceNumber,
    RangeByTime,
    ReadAccessSpecification,
)
from bacpypes3.constructeddata import ListOf
from bacpypes3.errors import ExecutionError
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier, Unsigned

from bacpack.object_builder import build_application
from bacpack.objects import trend_log as trend_log_module
from bacpack.objects.trend_log import _log_record_from_sample, device_local_timestamp, read_trend_window
from bacpack import readrange_cursors
from bacpack.readrange_cursors import ReadRangeCursors
from bacpack.timeutil import python_datetime
from bacpack.models import BacnetObject, Device, TrendSample
from bacpack.models import get_or_create_history_counters, trend_state_of, TrendState
from bacpack.trending import _MAX_SEQUENCE, record_sample


def _build_trended_app(session_factory, object_id, *, object_type="analog-value", **extra):
    device = Device(id=599001, name="Test Device")
    obj = BacnetObject(
        object_type=object_type,
        instance_number=1,
        name="AV-1",
        units="percent",
        trended=True,
        trend_mode="interval",
        trend_interval_seconds=30,
        id=object_id,
        **extra,
    )
    return build_application(device, [obj], trend_session_factory=session_factory)


def _make_object_row(session_factory, **kwargs):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device,
            object_type="analog-value",
            instance_number=1,
            name="AV-1",
            units="percent",
            trended=True,
            trend_mode="interval",
            trend_interval_seconds=30,
            **kwargs,
        )
        session.add(device)
        session.add(obj)
        session.commit()
        return obj.id


async def _read_range(app, range_choice=None, property_identifier="logBuffer", instance=1, source=None):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    req = ReadRangeRequest(
        objectIdentifier=("trend-log", instance),
        propertyIdentifier=property_identifier,
        range=Range(**{range_choice[0]: range_choice[1]}) if range_choice else None,
    )
    if source is not None:
        req.pduSource = source
    await app.do_ReadRangeRequest(req)
    return captured[0]


def _decode(resp):
    return list(resp.itemData.cast_out(ListOf(LogRecord)))


def _local(naive_utc: dt.datetime, offset_seconds: float = 0.0) -> dt.datetime:
    """What a client should see for a row stored at `naive_utc` with this
    clock offset: host-local wall time, naive."""
    shifted = naive_utc.replace(tzinfo=dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return shifted.astimezone().replace(tzinfo=None)


def _add_rows(session_factory, object_id, rows):
    """rows: (utc_timestamp, value, clock_offset, kind) in insertion order."""
    with session_factory() as session:
        for i, (stamp, value, clock_offset, kind) in enumerate(rows):
            session.add(
                TrendSample(
                    object_id=object_id, sequence=i + 1, value=value, timestamp=stamp,
                    clock_offset=clock_offset, kind=kind,
                )
            )
        trend_state_of(session.get(BacnetObject, object_id)).next_sequence = len(rows) + 1
        session.commit()


@pytest.mark.asyncio
async def test_trend_log_object_built_for_trended_objects_only(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)

    assert app.get_object_name("AV-1-Trend") is not None
    assert app.get_object_id(ObjectIdentifier(("trend-log", 1))) is not None


@pytest.mark.asyncio
async def test_trend_logs_dont_collide_across_object_types_at_same_instance(session_factory):
    """Found live: two trended objects of different types sharing an
    instance_number (e.g. analog-output:2 and multi-state-value:2, both
    trended) used to both build a Trend_Log at ("trend-log", 2) -- a real
    BACnet identifier collision -- since the old scheme keyed the trend-log
    off instance_number alone. Trend-logs are now keyed off each row's own
    `id`, which is unique regardless of the source objects' own types/
    instance numbers.
    """
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj_a = BacnetObject(
            device=device,
            object_type="analog-output",
            instance_number=2,
            name="AO-2",
            units="percent",
            trended=True,
            trend_mode="interval",
            trend_interval_seconds=30,
        )
        obj_b = BacnetObject(
            device=device,
            object_type="multi-state-value",
            instance_number=2,
            name="MSV-2",
            state_text=["Off", "On"],
            trended=True,
            trend_mode="interval",
            trend_interval_seconds=30,
        )
        session.add_all([device, obj_a, obj_b])
        session.commit()
        rows = [
            BacnetObject(
                object_type=o.object_type,
                instance_number=o.instance_number,
                name=o.name,
                units=o.units,
                state_text=o.state_text,
                trended=o.trended,
                trend_mode=o.trend_mode,
                trend_interval_seconds=o.trend_interval_seconds,
                id=o.id,
            )
            for o in (obj_a, obj_b)
        ]

    device = Device(id=599001, name="Test Device")
    app = build_application(device, rows, trend_session_factory=session_factory)

    assert app.get_object_name("AO-2-Trend") is not None
    assert app.get_object_name("MSV-2-Trend") is not None
    assert app.get_object_name("AO-2-Trend") is not app.get_object_name("MSV-2-Trend")


@pytest.mark.asyncio
async def test_no_trend_log_object_for_untrended_object(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device, object_type="analog-value", instance_number=1, name="AV-1"
        )
        session.add(device)
        session.add(obj)
        session.commit()
        object_id = obj.id

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device,
        [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1", id=object_id)],
        trend_session_factory=session_factory,
    )
    assert app.get_object_name("AV-1-Trend") is None


@pytest.mark.asyncio
async def test_record_count_and_total_record_count_read_live(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [1.0, 2.0, 3.0]:
        record_sample(session_factory, object_id, v)

    app = _build_trended_app(session_factory, object_id)
    trend_log = app.get_object_name("AV-1-Trend")

    assert await trend_log.read_property("recordCount") == 3
    assert await trend_log.read_property("totalRecordCount") == 3

    record_sample(session_factory, object_id, 4.0)
    # live, not a snapshot from construction time
    assert await trend_log.read_property("recordCount") == 4


@pytest.mark.asyncio
async def test_read_range_no_range_returns_everything(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0, 30.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(app, None)
    records = _decode(resp)

    assert resp.itemCount == 3
    assert [float(r.logDatum.realValue) for r in records] == [10.0, 20.0, 30.0]
    assert resp.firstSequenceNumber is None  # no range: none sent
    assert list(resp.resultFlags) == [1, 1, 0]  # firstItem, lastItem, not moreItems


@pytest.mark.asyncio
async def test_read_range_empty_log(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(app, None)
    assert resp.itemCount == 0
    assert _decode(resp) == []


@pytest.mark.asyncio
async def test_read_range_by_position_forward(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(app, ("byPosition", RangeByPosition(referenceIndex=2, count=2)))
    records = _decode(resp)

    assert resp.itemCount == 2
    assert [float(r.logDatum.realValue) for r in records] == [20.0, 30.0]
    assert resp.firstSequenceNumber is None  # by position: none sent


@pytest.mark.asyncio
async def test_read_range_by_position_backward(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id)

    # referenceIndex=4 (value 40.0), count=-2 -> the 2 records ending at
    # position 4 inclusive, going backward: positions 3 and 4 (30.0, 40.0)
    resp = await _read_range(app, ("byPosition", RangeByPosition(referenceIndex=4, count=-2)))
    records = _decode(resp)

    assert [float(r.logDatum.realValue) for r in records] == [30.0, 40.0]


@pytest.mark.asyncio
async def test_read_range_by_sequence_number(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0, 30.0, 40.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(
        app, ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=2, count=2))
    )
    records = _decode(resp)
    assert [float(r.logDatum.realValue) for r in records] == [20.0, 30.0]

    # backward
    resp = await _read_range(
        app, ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=3, count=-2))
    )
    records = _decode(resp)
    assert [float(r.logDatum.realValue) for r in records] == [20.0, 30.0]


@pytest.mark.asyncio
async def test_read_range_by_sequence_number_across_wraparound(session_factory):
    """Regression test for a bug found in testing: `sequence` wraps back to 1 at
    trending._MAX_SEQUENCE, so a plain bisect over raw sequence values
    used to misbehave right at that boundary (a decrease in an otherwise-
    sorted list breaks bisect's precondition). Inserted directly (like
    test_read_range_by_time above) to construct a wraparound in true
    chronological order without needing ~4.3 billion real samples --
    `_MAX_SEQUENCE - 1`/`_MAX_SEQUENCE` are the last two pre-wrap samples,
    `1`/`2` the first two post-wrap ones, inserted in that (chronological)
    order.
    """
    from bacpack.models import TrendSample

    object_id = _make_object_row(session_factory)
    with session_factory() as session:
        rows = [
            (_MAX_SEQUENCE - 1, 10.0),
            (_MAX_SEQUENCE, 20.0),
            (1, 30.0),
            (2, 40.0),
        ]
        for seq, value in rows:
            session.add(TrendSample(object_id=object_id, sequence=seq, value=value))
        obj = session.get(BacnetObject, object_id)
        trend_state_of(obj).next_sequence = 3
        session.commit()

    app = _build_trended_app(session_factory, object_id)

    # Forward from just after the wrap -- must not accidentally match the
    # pre-wrap record that happens to share a small-looking raw value.
    resp = await _read_range(
        app, ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=1, count=2))
    )
    records = _decode(resp)
    assert [float(r.logDatum.realValue) for r in records] == [30.0, 40.0]

    # Backward from just before the wrap.
    resp = await _read_range(
        app,
        (
            "bySequenceNumber",
            RangeBySequenceNumber(referenceSequenceNumber=_MAX_SEQUENCE, count=-2),
        ),
    )
    records = _decode(resp)
    assert [float(r.logDatum.realValue) for r in records] == [10.0, 20.0]


@pytest.mark.asyncio
async def test_read_range_by_time(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(
        session_factory, object_id,
        [(base + dt.timedelta(minutes=i), v, 0.0, "value") for i, v in enumerate([10.0, 20.0, 30.0])],
    )
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(
        app, ("byTime", RangeByTime(referenceTime=_local(base + dt.timedelta(seconds=30)), count=10))
    )
    records = _decode(resp)
    assert [float(r.logDatum.realValue) for r in records] == [20.0, 30.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("count, expected", [(10, [30.0]), (-10, [10.0])])
async def test_read_range_by_time_excludes_a_record_stamped_at_the_reference(session_factory, count, expected):
    # 135 clause 15.8: the first record "newer than" Reference_Time going
    # forward, the last "older than" it going back. A client paging by its
    # last timestamp would otherwise get that record twice.
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(
        session_factory, object_id,
        [(base + dt.timedelta(minutes=i), v, 0.0, "value") for i, v in enumerate([10.0, 20.0, 30.0])],
    )
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(
        app, ("byTime", RangeByTime(referenceTime=_local(base + dt.timedelta(minutes=1)), count=count))
    )
    assert [float(r.logDatum.realValue) for r in _decode(resp)] == expected


# --- Time sync -----------------------------------------------------------


@pytest.mark.asyncio
async def test_log_record_timestamps_are_device_local_not_utc(session_factory):
    """Regression for the naive-UTC wire bug: records went out in UTC."""
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, [(base, 1.0, 0.0, "value"), (base, 2.0, 3600.0, "value")])
    app = _build_trended_app(session_factory, object_id)

    records = _decode(await _read_range(app))

    assert python_datetime(records[0].timestamp) == _local(base)
    assert python_datetime(records[1].timestamp) == _local(base, 3600.0)


def test_device_local_timestamp_helper():
    sample = TrendSample(timestamp=dt.datetime(2026, 1, 1, 12, 0, 0), clock_offset=-90.0)
    assert device_local_timestamp(sample) == _local(dt.datetime(2026, 1, 1, 12, 0, 0), -90.0)


@pytest.mark.asyncio
async def test_time_change_rows_encode_as_time_change_datum(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(
        session_factory, object_id,
        [(base, 1.0, 0.0, "value"), (base, 3600.0, 3600.0, "time-change")],
    )
    app = _build_trended_app(session_factory, object_id)

    records = _decode(await _read_range(app))

    assert float(records[0].logDatum.realValue) == 1.0
    assert float(records[1].logDatum.timeChange) == 3600.0
    assert int(await app.get_object_id(ObjectIdentifier(("trend-log", object_id))).read_property("recordCount")) == 2


def _backwards_jump_rows(base):
    """Clock set back an hour after the second sample. Device-local times:
    12:00, 12:01, 11:02 (time-change), 11:03, 11:04 -- no longer sorted."""
    m = lambda n: base + dt.timedelta(minutes=n)
    return [
        (m(0), 1.0, 0.0, "value"),
        (m(1), 2.0, 0.0, "value"),
        (m(2), -3600.0, -3600.0, "time-change"),
        (m(3), 4.0, -3600.0, "value"),
        (m(4), 5.0, -3600.0, "value"),
    ]


def _values(records):
    return [
        float(r.logDatum.timeChange) if r.logDatum.timeChange is not None else float(r.logDatum.realValue)
        for r in records
    ]


@pytest.mark.asyncio
async def test_read_range_by_time_forward_over_a_backwards_clock_jump(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, _backwards_jump_rows(base))
    app = _build_trended_app(session_factory, object_id)

    # Oldest record after device-local 12:00:30 is the 12:01 sample; the
    # window then runs forward in buffer order. bisect on this unsorted list
    # would return an empty window here.
    reference = _local(base + dt.timedelta(seconds=30))
    records = _decode(await _read_range(app, ("byTime", RangeByTime(referenceTime=reference, count=10))))

    assert _values(records) == [2.0, -3600.0, 4.0, 5.0]


@pytest.mark.asyncio
async def test_read_range_by_time_backward_over_a_backwards_clock_jump(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, _backwards_jump_rows(base))
    app = _build_trended_app(session_factory, object_id)

    # Newest record before device-local 11:03:30 is the 11:03 sample.
    reference = _local(base + dt.timedelta(minutes=3, seconds=30), -3600.0)
    records = _decode(await _read_range(app, ("byTime", RangeByTime(referenceTime=reference, count=-2))))

    assert _values(records) == [-3600.0, 4.0]


@pytest.mark.asyncio
async def test_read_range_wrong_property_rejected(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)

    with pytest.raises(ExecutionError):
        await _read_range(app, None, property_identifier="recordCount")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "obj_id, prop, index, error",
    [
        (("trend-log", 1), "presentValue", None, ("property", "unknownProperty")),
        (("trend-log", 1), "recordCount", None, ("services", "propertyIsNotAList")),
        (("analog-value", 1), "presentValue", None, ("services", "propertyIsNotAList")),
        (("trend-log", 1), "logBuffer", 1, ("property", "propertyIsNotAnArray")),
    ],
)
async def test_read_range_error_codes(session_factory, obj_id, prop, index, error):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)
    req = ReadRangeRequest(objectIdentifier=obj_id, propertyIdentifier=prop, propertyArrayIndex=index)

    with pytest.raises(ExecutionError) as exc:
        await app.do_ReadRangeRequest(req)

    assert (exc.value.errorClass, exc.value.errorCode) == error


@pytest.mark.asyncio
async def test_read_range_unknown_object_rejected(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)

    captured = []
    app.response = lambda resp: captured.append(resp)
    req = ReadRangeRequest(
        objectIdentifier=("trend-log", 99), propertyIdentifier="logBuffer"
    )
    with pytest.raises(ExecutionError):
        await app.do_ReadRangeRequest(req)


@pytest.mark.asyncio
async def test_read_range_on_non_trend_log_object_rejected(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)

    captured = []
    app.response = lambda resp: captured.append(resp)
    req = ReadRangeRequest(
        objectIdentifier=("analog-value", 1), propertyIdentifier="presentValue"
    )
    with pytest.raises(ExecutionError):
        await app.do_ReadRangeRequest(req)


@pytest.mark.asyncio
async def test_log_datum_variant_matches_source_object_type(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        obj = BacnetObject(
            device=device,
            object_type="binary-value",
            instance_number=1,
            name="BV-1",
            trended=True,
            trend_mode="interval",
            trend_interval_seconds=30,
        )
        session.add(device)
        session.add(obj)
        session.commit()
        object_id = obj.id

    record_sample(session_factory, object_id, 1.0)

    device = Device(id=599001, name="Test Device")
    app = build_application(
        device,
        [
            BacnetObject(
                object_type="binary-value",
                instance_number=1,
                name="BV-1",
                trended=True,
                trend_mode="interval",
                trend_interval_seconds=30,
                id=object_id,
            )
        ],
        trend_session_factory=session_factory,
    )

    resp = await _read_range(app, None)
    records = _decode(resp)
    assert int(records[0].logDatum.enumValue) == 1


async def _seed_one_sample(session_factory, object_id):
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=1, value=1.5))
        trend_state_of(session.get(BacnetObject, object_id)).next_sequence = 2
        session.commit()


async def _rpm(app, prop):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    await app.do_ReadPropertyMultipleRequest(
        ReadPropertyMultipleRequest(
            listOfReadAccessSpecs=[
                ReadAccessSpecification(
                    objectIdentifier=("trend-log", 1),
                    listOfPropertyReferences=[PropertyReference(propertyIdentifier=prop)],
                )
            ]
        )
    )
    return list(captured[0].listOfReadAccessResults[0].listOfResults)


@pytest.mark.asyncio
async def test_read_property_of_log_buffer_is_read_access_denied(session_factory):
    # A plain list can't be encoded, so the client got operational-problem.
    # Log_Buffer is read with ReadRange; ReadProperty gets a clean BACnet error
    # instead.
    object_id = _make_object_row(session_factory)
    await _seed_one_sample(session_factory, object_id)
    app = _build_trended_app(session_factory, object_id)
    with pytest.raises(ExecutionError) as excinfo:
        await app.do_ReadPropertyRequest(
            ReadPropertyRequest(objectIdentifier=("trend-log", 1), propertyIdentifier="logBuffer")
        )
    assert excinfo.value.errorCode == "readAccessDenied"


@pytest.mark.asyncio
@pytest.mark.parametrize("prop", ["all", "required"])
async def test_rpm_all_and_required_on_a_trend_log_leave_log_buffer_out(session_factory, prop):
    object_id = _make_object_row(session_factory)
    await _seed_one_sample(session_factory, object_id)
    app = _build_trended_app(session_factory, object_id)
    results = await _rpm(app, prop)
    names = {r.propertyIdentifier.attr for r in results}
    assert "recordCount" in names
    assert "objectName" in names
    assert "logBuffer" not in names


@pytest.mark.asyncio
async def test_rpm_naming_log_buffer_gets_an_error_element(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_one_sample(session_factory, object_id)
    app = _build_trended_app(session_factory, object_id)
    results = await _rpm(app, "logBuffer")
    assert len(results) == 1
    assert results[0].readResult.propertyAccessError.errorCode.attr == "readAccessDenied"


@pytest.mark.asyncio
async def test_read_range_still_reads_the_buffer_after_the_refusal(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_one_sample(session_factory, object_id)
    app = _build_trended_app(session_factory, object_id)
    records = _decode(await _read_range(app))
    assert [float(r.logDatum.realValue) for r in records] == [1.5]


# --- ReadRange replies fit the client ------------------------------------
# bacpypes3 aborts a reply bigger than the client accepts instead of
# shortening it, so do_ReadRangeRequest trims and sets MORE_ITEMS.


async def _seed_samples(session_factory, object_id, count):
    with session_factory() as session:
        for seq in range(1, count + 1):
            session.add(TrendSample(object_id=object_id, sequence=seq, value=float(seq)))
        trend_state_of(session.get(BacnetObject, object_id)).next_sequence = count + 1
        session.commit()


async def _read_range_from(app, range_choice, *, max_resp, segmented=False, max_segs=0):
    captured = []

    async def _capture(resp):
        captured.append(resp)

    app.response = _capture
    req = ReadRangeRequest(
        objectIdentifier=("trend-log", 1),
        propertyIdentifier="logBuffer",
        range=Range(**{range_choice[0]: range_choice[1]}) if range_choice else None,
    )
    req.apduMaxResp = max_resp
    req.apduSA = segmented
    req.apduMaxSegs = max_segs
    await app.do_ReadRangeRequest(req)
    return captured[0]


def _flags(resp):
    return [int(resp.resultFlags[i]) for i in range(3)]  # first, last, more


@pytest.mark.asyncio
async def test_read_range_forward_is_trimmed_to_a_480_octet_client(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 100)
    app = _build_trended_app(session_factory, object_id)
    resp = await _read_range_from(app, None, max_resp=3)  # 480 octets, unsegmented
    records = _decode(resp)
    assert 0 < len(records) < 100
    assert int(resp.itemCount) == len(records)
    assert resp.firstSequenceNumber is None
    assert [float(r.logDatum.realValue) for r in records][0] == 1.0
    assert _flags(resp) == [1, 0, 1]


@pytest.mark.asyncio
async def test_read_range_backward_keeps_the_newest(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 100)
    app = _build_trended_app(session_factory, object_id)
    resp = await _read_range_from(
        app, ("byPosition", RangeByPosition(referenceIndex=100, count=-100)), max_resp=3
    )
    records = _decode(resp)
    assert 0 < len(records) < 100
    assert float(records[-1].logDatum.realValue) == 100.0
    assert _flags(resp) == [0, 1, 1]


@pytest.mark.asyncio
async def test_read_range_to_a_50_octet_client_returns_one_record(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 5)
    app = _build_trended_app(session_factory, object_id)
    resp = await _read_range_from(app, None, max_resp=0)  # 50 octets, the minimum
    assert len(_decode(resp)) == 1
    assert _flags(resp) == [1, 0, 1]


@pytest.mark.asyncio
async def test_read_range_segmented_client_gets_everything_that_fits(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 100)
    app = _build_trended_app(session_factory, object_id)
    resp = await _read_range_from(app, None, max_resp=3, segmented=True, max_segs=0)
    assert len(_decode(resp)) == 100
    assert _flags(resp) == [1, 1, 0]


# --- Spec rules -----------------------------------------------------------
# A reference that doesn't exist matches nothing (135 clause 15.8; positions
# count from 1); count 0 matches nothing; MORE_ITEMS only when the reply was
# trimmed to fit; firstSequenceNumber only for by-sequence-number/by-time.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "range_choice",
    [
        ("byPosition", RangeByPosition(referenceIndex=0, count=2)),
        ("byPosition", RangeByPosition(referenceIndex=6, count=-2)),
        ("byPosition", RangeByPosition(referenceIndex=100, count=5)),
        ("byPosition", RangeByPosition(referenceIndex=2, count=0)),
        ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=99, count=2)),
        ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=99, count=-2)),
        ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=2, count=0)),
        ("byTime", RangeByTime(referenceTime=_local(dt.datetime(2000, 1, 1)), count=0)),
    ],
)
async def test_read_range_reference_that_does_not_exist_matches_nothing(session_factory, range_choice):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(app, range_choice)

    assert resp.itemCount == 0
    assert _decode(resp) == []
    assert _flags(resp) == [0, 0, 0]
    assert resp.firstSequenceNumber is None


@pytest.mark.asyncio
async def test_read_range_more_items_only_when_the_reply_was_trimmed(session_factory):
    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 100)
    app = _build_trended_app(session_factory, object_id)

    resp = await _read_range(app, ("byPosition", RangeByPosition(referenceIndex=10, count=5)))

    assert resp.itemCount == 5
    assert _flags(resp) == [0, 0, 0]  # got all 5 asked for: not first, not last, no more


@pytest.mark.asyncio
async def test_read_range_first_sequence_number_only_for_sequence_and_time_reads(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(
        session_factory, object_id,
        [(base + dt.timedelta(minutes=i), v, 0.0, "value") for i, v in enumerate([10.0, 20.0, 30.0])],
    )
    app = _build_trended_app(session_factory, object_id)

    assert (await _read_range(app, None)).firstSequenceNumber is None
    resp = await _read_range(app, ("byPosition", RangeByPosition(referenceIndex=2, count=1)))
    assert resp.firstSequenceNumber is None
    resp = await _read_range(
        app, ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=2, count=1))
    )
    assert resp.firstSequenceNumber == 2
    resp = await _read_range(
        app, ("byTime", RangeByTime(referenceTime=_local(base + dt.timedelta(seconds=30)), count=1))
    )
    assert resp.firstSequenceNumber == 2


# --- Write allowlist: a Trend Log refused no writes before ----------------

from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.errors import PropertyError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value",
    [("objectName", "hacked"), ("enable", False), ("logInterval", 5), ("stopWhenFull", True)],
)
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_plain_trend_log_refuses_writes(session_factory, attr, value, as_identifier):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)
    log = app.get_object_id(ObjectIdentifier(("trend-log", object_id)))
    before = getattr(log, attr)

    with pytest.raises(PropertyError) as excinfo:
        await log.write_property(PropertyIdentifier(attr) if as_identifier else attr, value)

    assert excinfo.value.errorCode == "writeAccessDenied"
    assert getattr(log, attr) == before


# --- LogDatum variant per value family -------------------------------------
# Binary Present_Value is Enumerated (enumValue), multi-state is Unsigned
# (unsignedValue), a calendar's is Boolean (booleanValue). Binary used to go
# out as unsignedValue.


@pytest.mark.parametrize(
    "family, variant",
    [
        ("binary-input", "enumValue"),
        ("binary-value", "enumValue"),
        ("binary", "enumValue"),  # a binary schedule
        ("multi-state-value", "unsignedValue"),
        ("multi-state", "unsignedValue"),  # a multi-state schedule
        ("boolean", "booleanValue"),  # a calendar
        ("analog-value", "realValue"),
        ("analog", "realValue"),  # an analog schedule
    ],
)
def test_log_datum_variant_per_value_family(family, variant):
    sample = TrendSample(object_id=1, value=1.0, timestamp=dt.datetime(2026, 10, 1, 12, 0), kind="sample")
    datum = _log_record_from_sample(sample, family).logDatum
    assert getattr(datum, variant) is not None
    assert [name for name in ("booleanValue", "realValue", "enumValue", "unsignedValue") if getattr(datum, name) is not None] == [variant]


@pytest.mark.asyncio
async def test_calendar_trend_log_uses_the_boolean_family(session_factory):
    with session_factory() as session:
        device = Device(id=599001, name="Test Device")
        cal = BacnetObject(
            device=device, object_type="calendar", instance_number=1, name="CAL-1",
            date_list=[{"kind": "date", "year": 2026, "month": 12, "day": 25}], trended=True, trend_mode="cov",
        )
        session.add_all([device, cal])
        session.commit()
        objects = list(device.objects)
    app = build_application(device, objects, trend_session_factory=session_factory)
    trend_log = app.get_object_id(ObjectIdentifier(("trend-log", 1)))
    assert trend_log._trend_object_type == "boolean"


from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Date, Time


@pytest.mark.asyncio
async def test_read_range_by_time_accepts_an_unspecified_day_of_week(session_factory):
    """Day of week 255 is legal in a referenceTime; bacpypes3's Date.date
    refused it, so the device answered operational-problem."""
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, [(base, 1.0, 0.0, "value"), (base + dt.timedelta(minutes=2), 2.0, 0.0, "value")])
    app = _build_trended_app(session_factory, object_id)
    local = _local(base + dt.timedelta(minutes=1))
    reference = DateTime(
        date=Date((local.year - 1900, local.month, local.day, 255)),
        time=Time((local.hour, local.minute, local.second, 255)),
    )
    records = _decode(await _read_range(app, ("byTime", RangeByTime(referenceTime=reference, count=10))))
    assert _values(records) == [2.0]


@pytest.mark.asyncio
async def test_read_range_by_time_refuses_a_wildcarded_reference_cleanly(session_factory):
    object_id = _make_object_row(session_factory)
    _add_rows(session_factory, object_id, [(dt.datetime(2026, 1, 1, 12, 0, 0), 1.0, 0.0, "value")])
    app = _build_trended_app(session_factory, object_id)
    reference = DateTime(date=Date((255, 1, 1, 255)), time=Time((12, 0, 0, 0)))
    with pytest.raises(ExecutionError) as excinfo:
        await _read_range(app, ("byTime", RangeByTime(referenceTime=reference, count=10)))
    assert excinfo.value.errorCode == "parameterOutOfRange"


@pytest.mark.asyncio
async def test_read_range_by_time_pages_by_wire_timestamps_with_a_fractional_clock_offset(session_factory):
    # A record goes out at hundredths of a second; a client paging by the
    # timestamps it received must not get the reference record again. Only
    # forward paging could: truncation never puts the wire time after the
    # local time.
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, [(base + dt.timedelta(minutes=i), float(i), -1.937, "value") for i in range(4)])
    app = _build_trended_app(session_factory, object_id)
    records = _decode(await _read_range(app))
    assert _values(records) == [0.0, 1.0, 2.0, 3.0]

    for i, record in enumerate(records):
        forward = _decode(await _read_range(app, ("byTime", RangeByTime(referenceTime=record.timestamp, count=1))))
        assert _values(forward) == ([] if i == 3 else [float(i + 1)])
        backward = _decode(await _read_range(app, ("byTime", RangeByTime(referenceTime=record.timestamp, count=-1))))
        assert _values(backward) == ([] if i == 0 else [float(i - 1)])


# --- ReadRange reads only the window it needs -----------------------------
# The reply must match selecting the window from the whole buffer in memory,
# which _reference_window does.


def _reference_window(all_samples, range_choice):
    if range_choice is None:
        return list(all_samples)
    if range_choice.byPosition is not None:
        count = int(range_choice.byPosition.count)
        reference = int(range_choice.byPosition.referenceIndex)
        if count == 0 or not 1 <= reference <= len(all_samples):
            return []
        if count > 0:
            return all_samples[reference - 1 : reference - 1 + count]
        return all_samples[max(0, reference + count) : reference]
    if range_choice.bySequenceNumber is not None:
        count = int(range_choice.bySequenceNumber.count)
        reference = int(range_choice.bySequenceNumber.referenceSequenceNumber)
        raw = [s.sequence for s in all_samples]
        if count == 0 or reference not in raw:
            return []
        position = raw.index(reference)
        if count > 0:
            return all_samples[position : position + count]
        return all_samples[max(0, position + 1 + count) : position + 1]
    count = int(range_choice.byTime.count)
    if count == 0:
        return []
    reference = python_datetime(range_choice.byTime.referenceTime)
    local = [device_local_timestamp(s) for s in all_samples]
    if count > 0:
        start = next((i for i, t in enumerate(local) if t > reference), len(local))
        return all_samples[start : start + count]
    end = next((i + 1 for i in range(len(local) - 1, -1, -1) if local[i] < reference), 0)
    return all_samples[max(0, end + count) : end]


def _jumpy_wrapping_rows(base):
    """Twelve rows; the clock is set back three hours at the fifth and
    forward six at the ninth, so device-local times run 12:00-12:03,
    09:04-09:07, 15:08-15:11. Values are unique, so a record names its row."""
    rows = []
    offsets = [0.0] * 4 + [-10800.0] * 4 + [10800.0] * 4
    for i in range(12):
        kind, value = "value", float(i)
        if i == 4:
            kind, value = "time-change", -10800.0
        elif i == 8:
            kind, value = "time-change", 21600.0
        rows.append((base + dt.timedelta(minutes=i), value, offsets[i], kind))
    return rows


def _range_grid(samples):
    counts = [-20, -3, -1, 0, 1, 3, 20]
    grid = [None]
    for reference in [0, 1, 2, 6, 12, 13]:
        grid += [("byPosition", RangeByPosition(referenceIndex=reference, count=c)) for c in counts]
    for reference in [s.sequence for s in samples] + [10, 99]:
        grid += [("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=reference, count=c)) for c in counts]
    times = [dt.datetime(1901, 1, 2), dt.datetime(2150, 1, 1)]
    for s in samples:
        local = device_local_timestamp(s)
        times += [local - dt.timedelta(seconds=30), local, local + dt.timedelta(seconds=30)]
    for reference in times:
        grid += [("byTime", RangeByTime(referenceTime=reference, count=c)) for c in counts]
    return grid


@pytest.mark.asyncio
async def test_read_range_matches_selecting_from_the_whole_buffer(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    # Across a sequence wrap.
    sequences = [_MAX_SEQUENCE - 2, _MAX_SEQUENCE - 1, _MAX_SEQUENCE] + list(range(1, 10))
    with session_factory() as session:
        for i, (stamp, value, clock_offset, kind) in enumerate(_jumpy_wrapping_rows(base)):
            session.add(
                TrendSample(
                    object_id=object_id, sequence=sequences[i], value=value, timestamp=stamp,
                    clock_offset=clock_offset, kind=kind,
                )
            )
            # Another log's rows in between, which must never show up.
            session.add(TrendSample(object_id=object_id + 1000, sequence=i + 1, value=500.0 + i, timestamp=stamp))
            session.flush()
        trend_state_of(session.get(BacnetObject, object_id)).next_sequence = 10
        session.commit()
        samples = session.query(TrendSample).filter_by(object_id=object_id).order_by(TrendSample.id).all()
    app = _build_trended_app(session_factory, object_id)

    for range_choice in _range_grid(samples):
        window = _reference_window(samples, Range(**{range_choice[0]: range_choice[1]}) if range_choice else None)
        # A real pager gets the cursor's answer whenever a reference equals
        # the previous reply's end, so each grid read starts with an empty
        # map to test the time lookup on its own.
        app._read_range_cursors = ReadRangeCursors()
        resp = await _read_range_from(app, range_choice, max_resp=5, segmented=True)
        expected = (
            [s.value for s in window],
            [int(bool(window) and window[0] is samples[0]), int(bool(window) and window[-1] is samples[-1]), 0],
            window[0].sequence if window and range_choice and range_choice[0] != "byPosition" else None,
        )
        got = (_values(_decode(resp)), _flags(resp), resp.firstSequenceNumber)
        assert got == expected, range_choice
        assert int(resp.itemCount) == len(window)


def _count_local_timestamps(monkeypatch):
    calls = []
    original = trend_log_module.device_local_timestamp

    def counting(sample):
        calls.append(sample)
        return original(sample)

    monkeypatch.setattr(trend_log_module, "device_local_timestamp", counting)
    return calls


def _seed_minutely(session_factory, object_id, count, base):
    _add_rows(
        session_factory, object_id,
        [(base + dt.timedelta(minutes=i), float(i + 1), 0.0, "value") for i in range(count)],
    )


@pytest.mark.asyncio
async def test_read_range_by_position_builds_only_the_records_that_fit(session_factory, monkeypatch):
    object_id = _make_object_row(session_factory)
    _seed_minutely(session_factory, object_id, 2000, dt.datetime(2026, 1, 1))
    app = _build_trended_app(session_factory, object_id)
    calls = _count_local_timestamps(monkeypatch)

    resp = await _read_range_from(
        app, ("byPosition", RangeByPosition(referenceIndex=1000, count=2000)), max_resp=3
    )

    records = _decode(resp)
    assert float(records[0].logDatum.realValue) == 1000.0
    assert _flags(resp) == [0, 0, 1]
    # The records sent plus the one that didn't fit, not all 1001 matched.
    assert len(calls) <= len(records) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2000, -2000])
async def test_read_range_by_time_doesnt_check_every_record(session_factory, monkeypatch, count):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1)
    _seed_minutely(session_factory, object_id, 2000, base)
    app = _build_trended_app(session_factory, object_id)
    calls = _count_local_timestamps(monkeypatch)
    # Record 1000 is stamped base + 999 minutes.
    reference = _local(base + dt.timedelta(minutes=999))

    resp = await _read_range_from(app, ("byTime", RangeByTime(referenceTime=reference, count=count)), max_resp=3)

    records = _decode(resp)
    assert float(records[0 if count > 0 else -1].logDatum.realValue) == (1001.0 if count > 0 else 999.0)
    # Only rows stamped within a few hours of the reference get a local time.
    assert len(calls) < 500


@pytest.mark.asyncio
async def test_read_range_reads_the_database_off_the_event_loop(session_factory):
    import threading

    object_id = _make_object_row(session_factory)
    await _seed_samples(session_factory, object_id, 3)
    app = _build_trended_app(session_factory, object_id)
    trend_log = app.get_object_id(ObjectIdentifier(("trend-log", object_id)))
    threads = []

    def recording_factory():
        threads.append(threading.get_ident())
        return session_factory()

    trend_log._trend_session_factory = recording_factory

    resp = await _read_range(app)

    assert len(_decode(resp)) == 3
    assert threads and threading.get_ident() not in threads


# A Trend Log's COV subscription only gets its initial notification (no
# monitor fires on a computed recordCount), so that one must be current.


async def _subscribe_trend_log(app, notifications, *, process_id, source):
    from bacpypes3.apdu import SubscribeCOVRequest
    from bacpypes3.pdu import Address

    subscribe = SubscribeCOVRequest(
        subscriberProcessIdentifier=process_id,
        monitoredObjectIdentifier=("trend-log", 1),
        issueConfirmedNotifications=False,
        lifetime=60,
    )
    subscribe.pduSource = Address(source)
    await app.do_SubscribeCOVRequest(subscribe)
    await asyncio.sleep(0)
    return notifications[-1]


def _reported_record_count(apdu):
    for item in apdu.listOfValues:
        if item.propertyIdentifier.attr == "recordCount":
            return int(item.value.cast_out(Unsigned))
    raise AssertionError("no recordCount in the notification")


@pytest.mark.asyncio
async def test_each_trend_log_subscriber_is_told_the_current_record_count(session_factory):
    object_id = _make_object_row(session_factory)
    app = _build_trended_app(session_factory, object_id)
    app.response = _ignore_response
    notifications = []
    app.cov_notification = lambda cov, apdu: notifications.append(apdu)

    first = await _subscribe_trend_log(app, notifications, process_id=1, source="192.168.1.98")
    assert _reported_record_count(first) == 0

    for v in [1.0, 2.0, 3.0]:
        record_sample(session_factory, object_id, v)
    second = await _subscribe_trend_log(app, notifications, process_id=2, source="192.168.1.99")
    assert _reported_record_count(second) == 3

    record_sample(session_factory, object_id, 4.0)
    renewed = await _subscribe_trend_log(app, notifications, process_id=1, source="192.168.1.98")
    assert _reported_record_count(renewed) == 4


async def _ignore_response(apdu):
    pass


@pytest.mark.asyncio
async def test_trend_log_uses_the_fixed_instance_and_name(session_factory):
    app = _build_trended_app(session_factory, 5, trend_log_instance=77, trend_log_name="Zone Temp Log")
    log = app.get_object_id(ObjectIdentifier(("trend-log", 77)))
    assert log is not None and str(log.objectName) == "Zone Temp Log"
    assert log._trend_object_id == 5
    assert app.get_object_id(ObjectIdentifier(("trend-log", 5))) is None


@pytest.mark.asyncio
async def test_default_instance_and_name_are_unchanged(session_factory):
    app = _build_trended_app(session_factory, 5)
    log = app.get_object_id(ObjectIdentifier(("trend-log", 5)))
    assert str(log.objectName) == "AV-1-Trend"


@pytest.mark.asyncio
async def test_read_range_on_a_fixed_instance_log_returns_the_points_samples(session_factory):
    object_id = _make_object_row(session_factory)
    for v in [10.0, 20.0]:
        record_sample(session_factory, object_id, v)
    app = _build_trended_app(session_factory, object_id, trend_log_instance=77)

    records = _decode(await _read_range(app, None, instance=77))

    assert [float(r.logDatum.realValue) for r in records] == [10.0, 20.0]
    with pytest.raises(ExecutionError):
        await _read_range(app, None, instance=object_id)


# --- ReadRange by time continues from the client's cursor -----------------


def _window(session_factory, object_id, range_choice, *, cursor=None, octet_limit=100000):
    choice = Range(**{range_choice[0]: range_choice[1]}) if range_choice else None
    return read_trend_window(session_factory, object_id, "analog-value", choice, octet_limit, (cursor,) if cursor else ())


def test_window_end_is_the_last_record_sent_in_the_reads_direction(session_factory):
    object_id = _make_object_row(session_factory)
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _add_rows(session_factory, object_id, [(base + dt.timedelta(minutes=i), float(i), 0.0, "value") for i in range(6)])
    forward = _window(session_factory, object_id, ("byPosition", RangeByPosition(referenceIndex=2, count=3)))
    backward = _window(session_factory, object_id, ("byPosition", RangeByPosition(referenceIndex=5, count=-3)))
    with session_factory() as session:
        ids = [r.id for r in session.query(TrendSample).order_by(TrendSample.id)]
    assert forward.end == (_local(base + dt.timedelta(minutes=3)), ids[3])
    assert backward.end == (_local(base + dt.timedelta(minutes=2)), ids[2])
    assert _window(session_factory, object_id, ("byPosition", RangeByPosition(referenceIndex=99, count=3))).end is None


def _repeated_hour_rows(base):
    """120 rows a minute apart. The first 60 run 12:00-12:59 on the wall; the
    clock is then set back an hour, so the next 60 repeat 12:00-12:59. Values
    0..119 are unique, so a record names its row."""
    return [(base + dt.timedelta(minutes=i), float(i), 0.0 if i < 60 else -3600.0, "value") for i in range(120)]


def _hour_log(session_factory):
    object_id = _make_object_row(session_factory)
    _add_rows(session_factory, object_id, _repeated_hour_rows(dt.datetime(2026, 1, 1, 12, 0, 0)))
    return object_id


def _by_time(reference, count):
    return ("byTime", RangeByTime(referenceTime=reference, count=count))


def _row_id_of(session_factory, object_id, value):
    with session_factory() as session:
        return session.query(TrendSample).filter_by(object_id=object_id, value=value).one().id


WALL_14 = _local(dt.datetime(2026, 1, 1, 12, 14))  # rows 14 and 74 both read this


def test_without_a_cursor_a_reference_in_the_repeated_hour_goes_back_to_the_first_pass(session_factory):
    object_id = _hour_log(session_factory)
    assert _values(_window(session_factory, object_id, _by_time(WALL_14, 1)).records) == [15.0]


def test_a_matching_cursor_continues_right_after_its_row(session_factory):
    object_id = _hour_log(session_factory)
    cursor = (WALL_14, _row_id_of(session_factory, object_id, 74.0))
    window = _window(session_factory, object_id, _by_time(WALL_14, 3), cursor=cursor)
    assert _values(window.records) == [75.0, 76.0, 77.0]


def test_a_matching_backward_cursor_continues_before_its_row(session_factory):
    object_id = _hour_log(session_factory)
    cursor = (WALL_14, _row_id_of(session_factory, object_id, 74.0))
    window = _window(session_factory, object_id, _by_time(WALL_14, -3), cursor=cursor)
    assert _values(window.records) == [71.0, 72.0, 73.0]


def test_a_cursor_whose_timestamp_is_not_the_reference_is_ignored(session_factory):
    object_id = _hour_log(session_factory)
    cursor = (_local(dt.datetime(2026, 1, 1, 12, 20)), _row_id_of(session_factory, object_id, 74.0))
    plain = _window(session_factory, object_id, _by_time(WALL_14, 3))
    stale = _window(session_factory, object_id, _by_time(WALL_14, 3), cursor=cursor)
    assert _values(stale.records) == _values(plain.records) == [15.0, 16.0, 17.0]


def test_a_cursor_whose_row_is_gone_falls_back_to_the_time_lookup(session_factory):
    object_id = _hour_log(session_factory)
    window = _window(session_factory, object_id, _by_time(WALL_14, 3), cursor=(WALL_14, 10**9))
    assert _values(window.records) == [15.0, 16.0, 17.0]


def test_a_cursor_on_the_newest_row_matches_nothing(session_factory):
    # The client has every record: no fallback, which after a clock set-back
    # could resend older rows whose wall time is later.
    object_id = _hour_log(session_factory)
    wall = _local(dt.datetime(2026, 1, 1, 12, 59))
    cursor = (wall, _row_id_of(session_factory, object_id, 119.0))
    assert _window(session_factory, object_id, _by_time(wall, 3), cursor=cursor).records == []


def test_a_cursor_on_the_oldest_row_matches_nothing_backward(session_factory):
    object_id = _hour_log(session_factory)
    wall = _local(dt.datetime(2026, 1, 1, 12, 0))
    cursor = (wall, _row_id_of(session_factory, object_id, 0.0))
    assert _window(session_factory, object_id, _by_time(wall, -3), cursor=cursor).records == []


def test_a_cursor_is_not_used_by_position_or_sequence_reads(session_factory):
    object_id = _hour_log(session_factory)
    cursor = (WALL_14, _row_id_of(session_factory, object_id, 74.0))
    window = _window(
        session_factory, object_id, ("byPosition", RangeByPosition(referenceIndex=1, count=2)), cursor=cursor
    )
    assert _values(window.records) == [0.0, 1.0]


CLIENT_A = Address("192.168.1.10")
CLIENT_B = Address("192.168.1.11")
LONG_AGO = dt.datetime(2000, 1, 1)
FAR_AHEAD = dt.datetime(2150, 1, 1)


async def _page_by_time(app, *, count, start, source=None, limit=20):
    """Pages by time the way a client does: each reference is the last
    timestamp received in the direction of travel. Returns the values in the
    order received; fails if the pager hasn't finished within `limit` pages."""
    seen, reference = [], start
    for _ in range(limit):
        records = _decode(await _read_range(app, _by_time(reference, count), source=source))
        if not records:
            return seen
        seen += _values(records)
        reference = python_datetime(records[-1 if count > 0 else 0].timestamp)
    pytest.fail(f"still paging after {limit} pages: {seen[:60]}")


@pytest.mark.asyncio
async def test_paging_forward_by_time_through_a_repeated_hour_gets_every_record_once(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    seen = await _page_by_time(app, count=20, start=LONG_AGO, source=CLIENT_A)
    assert seen == [float(i) for i in range(120)]


@pytest.mark.asyncio
async def test_paging_backward_by_time_through_a_repeated_hour_gets_every_record_once(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    seen = await _page_by_time(app, count=-20, start=FAR_AHEAD, source=CLIENT_A)
    assert sorted(seen) == [float(i) for i in range(120)]
    assert seen[:20] == [float(i) for i in range(100, 120)]


async def _advance_into_the_second_pass(app, source):
    """Four forward pages of 20: the client holds rows 0-79 and its next
    reference is row 79's, which reads 12:19 like row 19."""
    reference = LONG_AGO
    for _ in range(4):
        records = _decode(await _read_range(app, _by_time(reference, 20), source=source))
        reference = python_datetime(records[-1].timestamp)
    return reference


@pytest.mark.asyncio
async def test_paging_with_pages_of_every_size_is_exact(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    for count in (1, 7, 59, 60, 61, 119, 120):
        seen = await _page_by_time(app, count=count, start=LONG_AGO, source=CLIENT_A, limit=200)
        assert seen == [float(i) for i in range(120)], count
        app._read_range_cursors = ReadRangeCursors()


@pytest.mark.asyncio
async def test_a_resent_request_gets_the_same_page_and_paging_carries_on(session_factory):
    # The reply to a request is lost and the client sends it again: the
    # server has already moved on, but the retry must not lose its place.
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    reference = await _advance_into_the_second_pass(app, CLIENT_A)
    first = _decode(await _read_range(app, _by_time(reference, 20), source=CLIENT_A))
    retry = _decode(await _read_range(app, _by_time(reference, 20), source=CLIENT_A))
    assert _values(first) == [float(i) for i in range(80, 100)]
    assert _values(retry) == _values(first)
    assert [r.timestamp for r in retry] == [r.timestamp for r in first]
    rest = await _page_by_time(app, count=20, start=python_datetime(retry[-1].timestamp), source=CLIENT_A)
    assert rest == [float(i) for i in range(100, 120)]


@pytest.mark.asyncio
async def test_a_different_client_address_does_not_inherit_the_cursor(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    reference = await _advance_into_the_second_pass(app, CLIENT_A)
    other = _decode(await _read_range(app, _by_time(reference, 1), source=CLIENT_B))
    mine = _decode(await _read_range(app, _by_time(reference, 1), source=CLIENT_A))
    assert _values(other) == [20.0]
    assert _values(mine) == [80.0]


@pytest.mark.asyncio
async def test_a_reference_that_is_not_the_remembered_timestamp_uses_the_lookup(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    await _advance_into_the_second_pass(app, CLIENT_A)
    records = _decode(await _read_range(app, _by_time(_local(dt.datetime(2026, 1, 1, 12, 30)), 1), source=CLIENT_A))
    assert _values(records) == [31.0]


@pytest.mark.asyncio
async def test_forward_and_backward_reads_keep_separate_cursors(session_factory):
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    reference = await _advance_into_the_second_pass(app, CLIENT_A)
    await _read_range(app, _by_time(FAR_AHEAD, -5), source=CLIENT_A)
    nxt = _decode(await _read_range(app, _by_time(reference, 1), source=CLIENT_A))
    assert _values(nxt) == [80.0]


@pytest.mark.asyncio
async def test_position_and_sequence_reads_neither_use_nor_move_the_cursor(session_factory):
    object_id = _hour_log(session_factory)
    app = _build_trended_app(session_factory, object_id)
    reference = await _advance_into_the_second_pass(app, CLIENT_A)
    await _read_range(app, ("byPosition", RangeByPosition(referenceIndex=3, count=2)), source=CLIENT_A)
    await _read_range(app, ("bySequenceNumber", RangeBySequenceNumber(referenceSequenceNumber=3, count=2)), source=CLIENT_A)
    assert _values(_decode(await _read_range(app, _by_time(reference, 1), source=CLIENT_A))) == [80.0]
    # and a position read alone never creates one
    fresh = _build_trended_app(session_factory, object_id)
    await _read_range(fresh, ("byPosition", RangeByPosition(referenceIndex=1, count=75)), source=CLIENT_A)
    assert _values(_decode(await _read_range(fresh, _by_time(reference, 1), source=CLIENT_A))) == [20.0]


@pytest.mark.asyncio
async def test_an_empty_reply_leaves_the_cursor_alone(session_factory):
    object_id = _hour_log(session_factory)
    app = _build_trended_app(session_factory, object_id)
    await _page_by_time(app, count=20, start=LONG_AGO, source=CLIENT_A)
    key = (CLIENT_A, object_id, True)
    before = app._read_range_cursors.get(key)
    assert before
    assert _decode(await _read_range(app, _by_time(before[0][0], 5), source=CLIENT_A)) == []
    assert app._read_range_cursors.get(key) == before


@pytest.mark.asyncio
async def test_the_handlers_map_is_bounded(session_factory, monkeypatch):
    monkeypatch.setattr(readrange_cursors, "MAX_CURSORS", 20)
    app = _build_trended_app(session_factory, _hour_log(session_factory))
    for i in range(30):
        await _read_range(app, _by_time(LONG_AGO, 1), source=Address(f"10.0.0.{i + 1}"))
    assert len(app._read_range_cursors) == 20
