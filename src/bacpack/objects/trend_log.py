"""Trend Log object: the BACnet-object half of historical trending (storage
and sampling are in trending.py).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from bacpypes3.basetypes import (
    DeviceObjectPropertyReference,
    EventState,
    LoggingType,
    LogRecord,
    LogRecordLogDatum,
    Reliability,
    StatusFlags,
)
from bacpypes3.errors import PropertyError
from bacpypes3.local.cov import COVDetection
from bacpypes3.local.object import Object as LocalObjectMixin
from bacpypes3.object import TrendLogObject as _TrendLogObjectBase
from bacpypes3.primitivedata import Real, Unsigned
from sqlalchemy import func, select

from .. import timeutil, trend_buffer
from ..bacnet import WritePropertyAllowlisted
from ..readrange_cursors import Cursor
from ..models import (
    BacnetObject,
    Device,
    TrendSample,
    TrendState,
    BINARY_TYPES,
    MULTISTATE_TYPES,
    effective_trend_sample_cap,
)
from .reserved_names import effective_trend_log_instance, effective_trend_log_name


class TrendLogCriteria(COVDetection):
    """`_cov_criteria` for LocalTrendLogObject, tracking recordCount (there's
    no presentValue). Without some criteria, SubscribeCOV on the log fails
    with covSubscriptionFailed, and YABE's trend viewer subscribes before
    sending ReadRange, so it showed nothing.

    Only the subscription succeeds: recordCount is computed, never set, so
    property monitors never fire and the only notification a subscriber
    gets is the one sent when it subscribes or renews. That one re-reads the
    tracked values, which otherwise stay as the first subscriber found them.
    """

    properties_tracked = ("recordCount", "statusFlags")
    properties_reported = ("recordCount", "statusFlags")
    monitored_property_reference = "recordCount"

    def send_cov_notifications(self, subscription=None) -> None:
        for property_name in self.properties_tracked:
            setattr(self, property_name, getattr(self.obj, property_name))
        super().send_cov_notifications(subscription)


class LocalTrendLogObject(WritePropertyAllowlisted, LocalObjectMixin, _TrendLogObjectBase):
    """A Trend_Log whose history properties are read live from SQLite.

    recordCount/totalRecordCount/logBuffer are @property, not constructor
    kwargs (no setter; construction would raise); the base read_property()
    calls a property's getter. logBuffer is served only by
    LoggingApplication.do_ReadRangeRequest.

    Refuses every WriteProperty (writeAccessDenied): nothing would save or
    honour the change. ReportingTrendLogObject handles its own reporting
    properties before this is reached.
    """

    _WRITABLE_ATTRS = frozenset()
    _cov_criteria = TrendLogCriteria
    _trend_session_factory: Any
    _trend_object_id: int
    _trend_object_type: str

    @property
    def recordCount(self):
        with self._trend_session_factory() as session:
            count = trend_buffer.count_records(session, self._trend_object_id)
        # A real Unsigned: a raw int crashes when encoded as an Any (a COV
        # notification's listOfValues), and @property values aren't
        # type-checked like constructor kwargs.
        return Unsigned(count)

    @property
    def totalRecordCount(self):
        with self._trend_session_factory() as session:
            state = session.get(TrendState, self._trend_object_id)
            count = (state.next_sequence - 1) if state is not None else 0
        return Unsigned(count)

    @property
    def logBuffer(self):
        # ReadRange only: ReadProperty/RPM can't encode a plain list, so the
        # whole request failed with operational-problem.
        raise PropertyError("readAccessDenied")


def device_local_timestamp(sample: TrendSample) -> dt.datetime:
    """The device-local wall time a BACnet client sees for this row: stored
    UTC plus the row's device clock offset, in the host zone (the device's
    own), naive like every BACnet DateTime."""
    shifted = timeutil.from_db(sample.timestamp) + dt.timedelta(seconds=sample.clock_offset or 0.0)
    return timeutil.host_local(shifted)


def _wire_timestamp(sample: TrendSample) -> dt.datetime:
    """device_local_timestamp as a LogRecord sends it: truncated to
    hundredths, the way bacpypes3 encodes a Time. byTime compares at this
    precision, or a client paging by the timestamps it received gets the
    reference record again whenever the clock offset has a finer fraction."""
    local = device_local_timestamp(sample)
    return local.replace(microsecond=local.microsecond // 10000 * 10000)


def _log_record_from_sample(sample: TrendSample, value_family: str) -> LogRecord:
    """Pick the LogDatum choice matching the source's value family (analog
    realValue, binary enumValue, ...); samples are stored as floats.
    `value_family` is the object_type, or for a schedule/calendar the
    effective family build_trend_log_object computes.
    """
    timestamp = device_local_timestamp(sample)
    if sample.kind == "time-change":
        # value holds the clock jump in seconds (trending.record_time_change).
        return LogRecord(timestamp=timestamp, logDatum=LogRecordLogDatum(timeChange=Real(sample.value)))
    if value_family in BINARY_TYPES or value_family == "binary":
        log_datum = LogRecordLogDatum(enumValue=int(sample.value))
    elif value_family in MULTISTATE_TYPES or value_family == "multi-state":
        log_datum = LogRecordLogDatum(unsignedValue=int(sample.value))
    elif value_family == "boolean":
        log_datum = LogRecordLogDatum(booleanValue=bool(sample.value))
    else:
        log_datum = LogRecordLogDatum(realValue=Real(sample.value))
    return LogRecord(timestamp=timestamp, logDatum=log_datum)


@dataclass
class TrendWindow:
    """One ReadRange reply's part of a Trend Log's buffer. `more_items`:
    the reply was trimmed to fit, so more records matched than it carries."""

    records: List[LogRecord]
    first_sequence: Optional[int]
    first_item: bool
    last_item: bool
    more_items: bool
    # Wire timestamp and row id of the record the reply ends on in the read's
    # direction (newest for a forward read, oldest for a backward one).
    end: Optional[Cursor] = None
    # Index of the cursor passed in that the read continued from, if any.
    cursor_used: Optional[int] = None


_EMPTY_WINDOW = TrendWindow([], None, False, False, False)

_SAMPLE_COLUMNS = (
    TrendSample.id,
    TrendSample.sequence,
    TrendSample.timestamp,
    TrendSample.value,
    TrendSample.clock_offset,
    TrendSample.kind,
)
# Rows per query while walking the buffer: a reply carries a few dozen.
_CHUNK_ROWS = 128
# Slack on the byTime bounds, well past julianday()'s rounding.
_TIME_SLACK = dt.timedelta(hours=1)
_UNIX_EPOCH = dt.datetime(1970, 1, 1)
_UNIX_EPOCH_JULIAN_DAY = 2440587.5


def _julian_day(naive_utc: dt.datetime) -> float:
    return (naive_utc - _UNIX_EPOCH).total_seconds() / 86400 + _UNIX_EPOCH_JULIAN_DAY


class _Buffer:
    """One Trend Log's rows, oldest first by `id`, not `sequence`: sequence
    wraps to 1 at trending._MAX_SEQUENCE."""

    def __init__(self, session, object_id: int):
        self.session = session
        self.object_id = object_id
        ends = trend_buffer.log_ends(session, object_id)
        self.first = ends[0] if ends else None
        self.last = ends[1] if ends else None
        self.first_id = ends[0].id if ends else None
        self.last_id = ends[1].id if ends else None
        self.total = trend_buffer.record_count(ends)

    def id_at(self, offset: int) -> Optional[int]:
        """The row at 0-based `offset` from the oldest, by its sequence number
        (the log is a gap-free run: trend_buffer)."""
        if self.first is None:
            return None
        return self.id_of_sequence(trend_buffer.sequence_at(self.first, offset + 1))

    def id_of_sequence(self, sequence: int) -> Optional[int]:
        # (object_id, sequence) is unique.
        return self.session.execute(
            select(TrendSample.id).where(TrendSample.object_id == self.object_id, TrendSample.sequence == sequence)
        ).scalar()

    def rows_from(self, anchor: Optional[int], forward: bool, *conditions):
        """Rows from `anchor` (included; None = the end) on, oldest first
        or newest first, fetched a chunk at a time."""
        last = None
        while True:
            query = select(*_SAMPLE_COLUMNS).where(TrendSample.object_id == self.object_id, *conditions)
            if last is not None:
                query = query.where(TrendSample.id > last if forward else TrendSample.id < last)
            elif anchor is not None:
                query = query.where(TrendSample.id >= anchor if forward else TrendSample.id <= anchor)
            query = query.order_by(TrendSample.id if forward else TrendSample.id.desc()).limit(_CHUNK_ROWS)
            rows = self.session.execute(query).all()
            yield from rows
            if len(rows) < _CHUNK_ROWS:
                return
            last = rows[-1].id

    def after_cursor(self, cursor: Cursor, reference: dt.datetime, forward: bool) -> Tuple[bool, Optional[int]]:
        """(applies, anchor) for a client's remembered cursor. It applies when
        `reference` is the cursor's timestamp and its row is still in this
        log; the anchor is then the first row after it in the direction, or
        None when nothing follows. A wall time can name two moments when the
        clock falls back or is set back, and the cursor says which one the
        client means."""
        stamp, row_id = cursor
        if stamp != reference:
            return False, None
        present = self.session.execute(
            select(TrendSample.id).where(TrendSample.object_id == self.object_id, TrendSample.id == row_id)
        ).scalar()
        if present is None:
            return False, None
        query = select(TrendSample.id).where(
            TrendSample.object_id == self.object_id,
            TrendSample.id > row_id if forward else TrendSample.id < row_id,
        )
        return True, self.session.execute(query.order_by(TrendSample.id if forward else TrendSample.id.desc()).limit(1)).scalar()

    def _head_end(self, tail) -> int:
        """Rows with an id below this are the unordered head."""
        return tail.ordered_from_id if tail is not None else (self.last_id or 0) + 1

    def first_newer(self, reference: dt.datetime) -> Optional[int]:
        """The oldest row whose device-local time is after `reference`.
        Local times aren't sorted (a clock set back, a DST change), so the
        unordered head is scanned, and the ordered tail from where the
        timestamp index says a match can first appear
        (trend_buffer.newer_start). Either way only rows whose UTC instant
        could show after it are looked at: no zone offset turns an older
        instant into a later wall time."""
        _lowest, highest = timeutil.host_utc_offsets_near(reference)
        bound = reference - highest - _TIME_SLACK
        candidates = _instant_julian_day() > _julian_day(bound)
        tail = trend_buffer.trusted_tail(self.session, self.object_id, self.last) if self.last else None
        for row in self.rows_from(None, True, candidates, TrendSample.id < self._head_end(tail)):
            if _wire_timestamp(row) > reference:
                return row.id
        if tail is None:
            return None
        start = trend_buffer.newer_start(self.session, self.object_id, tail, bound)
        for row in self.rows_from(start, True, candidates):
            if _wire_timestamp(row) > reference:
                return row.id
        return None

    def last_older(self, reference: dt.datetime) -> Optional[int]:
        """The newest row whose device-local time is before `reference`;
        see first_newer: the tail first, then the head."""
        lowest, _highest = timeutil.host_utc_offsets_near(reference)
        bound = reference - lowest + _TIME_SLACK
        candidates = _instant_julian_day() < _julian_day(bound)
        tail = trend_buffer.trusted_tail(self.session, self.object_id, self.last) if self.last else None
        if tail is not None:
            start = trend_buffer.older_start(self.session, self.object_id, tail, bound)
            for row in self.rows_from(start, False, candidates, TrendSample.id >= tail.ordered_from_id):
                if _wire_timestamp(row) < reference:
                    return row.id
        for row in self.rows_from(None, False, candidates, TrendSample.id < self._head_end(tail)):
            if _wire_timestamp(row) < reference:
                return row.id
        return None


def _instant_julian_day():
    """A row's stored UTC time plus its clock offset, as a julian day."""
    return func.julianday(TrendSample.timestamp) + func.coalesce(TrendSample.clock_offset, 0.0) / 86400.0


def _locate(buffer: _Buffer, range_choice, cursors: Sequence[Cursor] = ()):
    """(anchor row id, signed count, index of the cursor used or None) for a
    ReadRange Range choice, or None when nothing matches.

    Positive `count`: the reference is the first record, going forward;
    negative: the last, going backward; included either way. A count of 0,
    or a by-position/by-sequence reference that doesn't exist, matches
    nothing (135 clause 15.8; positions count from 1), so a client resuming
    from a pruned cursor learns of the gap. `byTime` is strict: records
    newer / older than Reference_Time, never one stamped at it, so a client
    paging by its last timestamp doesn't get that record twice. The first of
    `cursors` that applies (_Buffer.after_cursor) replaces the time lookup:
    the read continues after its row, and matches nothing if none follows.
    """
    if range_choice is not None and range_choice.byPosition is not None:
        count = int(range_choice.byPosition.count)
        reference = int(range_choice.byPosition.referenceIndex)
        if count == 0 or not 1 <= reference <= buffer.total:
            return None
        anchor = buffer.id_at(reference - 1)
        return None if anchor is None else (anchor, count, None)

    if range_choice is not None and range_choice.bySequenceNumber is not None:
        count = int(range_choice.bySequenceNumber.count)
        if count == 0:
            return None
        anchor = buffer.id_of_sequence(int(range_choice.bySequenceNumber.referenceSequenceNumber))
        return None if anchor is None else (anchor, count, None)

    if range_choice is not None and range_choice.byTime is not None:
        count = int(range_choice.byTime.count)
        if count == 0:
            return None
        reference = timeutil.python_datetime(range_choice.byTime.referenceTime)
        for index, cursor in enumerate(cursors):
            applies, anchor = buffer.after_cursor(cursor, reference, count > 0)
            if applies:
                return None if anchor is None else (anchor, count, index)
        anchor = buffer.first_newer(reference) if count > 0 else buffer.last_older(reference)
        return None if anchor is None else (anchor, count, None)

    # No Range, or one with no choice set: everything.
    return None if buffer.total == 0 else (buffer.first_id, buffer.total, None)


def read_trend_window(
    session_factory,
    object_id: int,
    value_family: str,
    range_choice,
    octet_limit: int,
    cursors: Sequence[Cursor] = (),
) -> TrendWindow:
    """The records one ReadRange reply carries: those the Range matches,
    as many as fit in `octet_limit`, from the oldest end or, for a negative
    count, the newest. Reads only the rows it needs: a client pulls a big
    log a reply at a time, so loading the buffer per request is quadratic.

    Raises ValueError for a byTime reference that isn't a real instant.
    """
    with session_factory() as session:
        buffer = _Buffer(session, object_id)
        located = _locate(buffer, range_choice, cursors)
        if located is None:
            return _EMPTY_WINDOW
        anchor, count, cursor_used = located
        rows, records, used, trimmed = [], [], 0, False
        for row in buffer.rows_from(anchor, count > 0):
            if len(rows) == abs(count):
                break
            record = _log_record_from_sample(row, value_family)
            size = len(record.encode().encode().pduData)
            if used + size > octet_limit:
                trimmed = True
                break
            used += size
            rows.append(row)
            records.append(record)
    # Read back from the record, so it is what a client will send as its next
    # reference and no second local-time conversion is made.
    end = (timeutil.python_datetime(records[-1].timestamp), rows[-1].id) if rows else None
    if count < 0:
        rows.reverse()
        records.reverse()
    if end is None:
        return TrendWindow([], None, False, False, trimmed)
    return TrendWindow(
        records=records,
        first_sequence=rows[0].sequence,
        first_item=rows[0].id == buffer.first_id,
        last_item=rows[-1].id == buffer.last_id,
        more_items=trimmed,
        end=end,
        cursor_used=cursor_used,
    )


def build_trend_log_object(
    device: Device, obj: BacnetObject, session_factory, *, cls=None, extra_kwargs=None
) -> LocalTrendLogObject:
    """One Trend_Log object per trended BacnetObject.

    `cls`/`extra_kwargs`: trend_log_reporting.py builds its subclass here;
    the reporting properties must be constructor values because its
    `__init__` builds the event algorithm, which binds them.

    The instance is the point's fixed Trend Log instance, else `obj.id`, not
    `obj.instance_number`: every Trend_Log shares one type, so analog-input:2
    and analog-output:2 would collide.
    """
    identity = {
        "id": obj.id,
        "name": obj.name,
        "trend_log_instance": obj.trend_log_instance,
        "trend_log_name": obj.trend_log_name,
    }
    trend_log = (cls or LocalTrendLogObject)(
        objectIdentifier=("trend-log", effective_trend_log_instance(identity)),
        objectName=effective_trend_log_name(identity),
        description=f"History of {obj.name}",
        enable=True,
        stopWhenFull=False,
        # The sample cap keeps Record_Count at or under it. Some software
        # refuses a Buffer_Size over 32767.
        bufferSize=effective_trend_sample_cap(obj.trend_sample_cap),
        loggingType=LoggingType("cov" if obj.trend_mode == "cov" else "polled"),
        # Hundredths of a second (clause 12.25.13); 0 when COV-driven.
        logInterval=0 if obj.trend_mode == "cov" else (obj.trend_interval_seconds or 60) * 100,
        logDeviceObjectProperty=DeviceObjectPropertyReference(
            objectIdentifier=(obj.object_type, obj.instance_number),
            propertyIdentifier="presentValue",
            deviceIdentifier=("device", device.id),
        ),
        statusFlags=StatusFlags([0, 0, 0, 0]),
        # Required on a Trend Log up to protocol revision 4's era, and a
        # reporting log's EventAlgorithm only transitions out of "normal".
        eventState=EventState("normal"),
        reliability=Reliability("noFaultDetected"),
        **(extra_kwargs or {}),
    )
    trend_log._trend_session_factory = session_factory
    trend_log._trend_object_id = obj.id
    # A schedule's/calendar's object_type doesn't say its value family.
    if obj.object_type == "schedule":
        trend_log._trend_object_type = obj.schedule_value_type
    elif obj.object_type == "calendar":
        trend_log._trend_object_type = "boolean"
    else:
        trend_log._trend_object_type = obj.object_type
    return trend_log
