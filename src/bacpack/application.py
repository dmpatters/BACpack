"""LoggingApplication: the bacpypes3 Application subclass every device is
served through, with BACpack's service-handler overrides. Construction
lives in object_builder.py.
"""
from __future__ import annotations

import asyncio
import logging
import time

from typing import Optional

from bacpypes3.apdu import (
    IAmRequest,
    AcknowledgeAlarmRequest,
    AddListElementRequest,
    AtomicReadFileACK,
    AtomicReadFileRequest,
    AtomicWriteFileACK,
    AtomicWriteFileRequest,
    ChangeListError,
    DeviceCommunicationControlRequest,
    GetEventInformationACK,
    GetEventInformationRequest,
    ReadPropertyMultipleACK,
    ReadRangeACK,
    ReadRangeRequest,
    ReinitializeDeviceRequest,
    RemoveListElementRequest,
    SimpleAckPDU,
    TimeSynchronizationRequest,
    UTCTimeSynchronizationRequest,
    WritePropertyMultipleError,
    WritePropertyMultipleRequest,
    WritePropertyRequest,
    decode_max_apdu_length_accepted,
    decode_max_segments_accepted,
)
from bacpypes3.app import Application
from bacpypes3.basetypes import (
    AtomicReadFileACKAccessMethodChoice,
    AtomicReadFileACKAccessMethodStreamAccess,
    Destination,
    ErrorType,
    EventType,
    GetEventInformationEventSummary,
    LogRecord,
    NotifyType,
    ObjectPropertyReference,
    PropertyIdentifier,
    ReadAccessResult,
    ResultFlags,
)
from bacpypes3.constructeddata import Array, List, ListOf, SequenceOf
from bacpypes3.errors import DecodingError, ExecutionError, InvalidTag, ObjectError
from bacpypes3.primitivedata import TagClass, TagNumber, Unsigned
from bacpypes3.service.object import read_property_to_result_element

from . import bacpypes3_compat, config_save, device_clock, held_tasks, security_log
from .auth import verify_password
from .backup_restore import CONFIG_FILE_INSTANCE, BackupRestore, refusal
from .bacnet import normalized_attr
from .capabilities import services_supported
from .clock_sync import apply_clock_offset, bacnet_datetime_fields
from .config_io import apply_device_restore, export_device_backup
from .db import lock_both_files
from .models import WILDCARD_DEVICE_INSTANCE, Device
from .objects.alarm_config import state_group_index, time_stamp_matches
from .objects.notification_class import ListElementError, RecipientListEditable
from .objects.trend_log import read_trend_window
from .readrange_cursors import ReadRangeCursors
from .traffic import TrafficLog, object_id_text
from .trending import trim_devices_to_caps

logger = logging.getLogger(__name__)

# Reply size caps for AtomicReadFile and ReadRange:
# bacpypes3's ServerSSM aborts a reply bigger than the client accepts instead
# of shortening it. A shorter file chunk is spec-legal (endOfFile=false).
# READ_ACK_OVERHEAD is the ACK's non-data service octets.
SEGMENTED_READ_CHUNK = 4096
READ_ACK_OVERHEAD = 20
_UNSPECIFIED_MAX_SEGMENTS = 64

# ReadRange-ACK service octets that aren't log records (about 20; bacpypes3
# sizes a reply by service data, not the APDU header), plus a margin.
READ_RANGE_ACK_OVERHEAD = 24

# GetEventInformation-ACK octets that aren't event summaries: the list's
# open/close tags and moreEvents, plus a margin.
GET_EVENT_INFORMATION_ACK_OVERHEAD = 12


def _reply_octet_limit(apdu, overhead: int) -> int:
    """Data octets one reply may carry without bacpypes3 aborting it, from
    the client's max APDU/segmentation, at most SEGMENTED_READ_CHUNK. A
    locally built request reads as a 480-octet, unspecified-segments client."""
    max_apdu = decode_max_apdu_length_accepted(getattr(apdu, "apduMaxResp", 3))
    if not getattr(apdu, "apduSA", False):
        return max_apdu - overhead
    segments = decode_max_segments_accepted(getattr(apdu, "apduMaxSegs", 0)) or _UNSPECIFIED_MAX_SEGMENTS
    return min(SEGMENTED_READ_CHUNK, max_apdu * segments - overhead)


def _read_chunk_cap(apdu) -> int:
    """How many file octets one AtomicReadFile-ACK to this request may carry."""
    return _reply_octet_limit(apdu, READ_ACK_OVERHEAD)


def _reports_first_sequence_number(range_choice) -> bool:
    """firstSequenceNumber goes with a by-sequence-number or by-time read
    only (135 clause 15.8)."""
    return range_choice is not None and (
        range_choice.bySequenceNumber is not None or range_choice.byTime is not None
    )


def _object_order(object_identifier) -> tuple:
    return (int(object_identifier[0]), int(object_identifier[1]))


def _fit_summaries(summaries: list, limit: int) -> list:
    """The leading event summaries that fit in `limit` octets, at least one:
    an empty page with moreEvents set would have the client ask forever."""
    used, kept = 0, []
    for summary in summaries:
        used += len(summary.encode().encode().pduData)
        if kept and used > limit:
            break
        kept.append(summary)
    return kept


# WriteProperty's Priority parameter range (135 clause 15.9). An omitted
# priority means the lowest, 16 (clause 19.2).
_MIN_PRIORITY = 1
_MAX_PRIORITY = 16


def _is_null_value(any_value) -> bool:
    """True if a WriteProperty value is a single application-tagged Null."""
    tags = list(any_value.tagList)
    if bacpypes3_compat.is_context_encoded(any_value):
        # A context-encoded value (WriteProperty's [3], WPM's [2]) keeps its
        # opening and closing tags in tagList; cast_out skips them too.
        tags = tags[1:-1]
    return (
        len(tags) == 1
        and tags[0].tag_class == TagClass.application
        and tags[0].tag_number == TagNumber.null
    )


# How long an RPM reply is built before the device's loop gets a turn.
RPM_TURN_SECONDS = 0.02


def _rpm_special(identifier) -> Optional[str]:
    """"all", "required" or "optional" for those identifiers, else None."""
    for name in ("all", "required", "optional"):
        if identifier == getattr(PropertyIdentifier, name):
            return name
    return None


def _rpm_property_names(obj, special: str) -> list:
    """The properties an RPM all/required/optional reads, in declaration
    order. Property_List is left out of all and required, as upstream does."""
    names = bacpypes3_compat.element_names(obj)
    if special == "all":
        return [name for name in names if name != "propertyList"]
    required = bacpypes3_compat.required_properties(obj)
    if special == "required":
        return [name for name in names if name in required and name != "propertyList"]
    return [name for name in names if name not in required]


async def _write_one(obj, property_identifier, array_index, priority, any_value) -> None:
    """One property of a WriteProperty or WPM, checked as 135 says before
    the value is decoded, so each malformed write gets its proper
    ExecutionError (bacpypes3's handlers got several wrong)."""
    property_type = obj.get_property_type(property_identifier)
    if property_type is None:
        raise ExecutionError(errorClass="property", errorCode="unknownProperty")
    if array_index is not None:
        if not issubclass(property_type, Array):
            raise ExecutionError(errorClass="property", errorCode="propertyIsNotAnArray")
        property_type = Unsigned if array_index == 0 else bacpypes3_compat.array_element_type(property_type)
    if priority is not None and not _MIN_PRIORITY <= priority <= _MAX_PRIORITY:
        raise ExecutionError(errorClass="services", errorCode="parameterOutOfRange")
    # Only a commandable Present_Value takes a Null (a relinquish); anywhere
    # else a Null is the wrong datatype.
    commandable = (
        getattr(obj, "priorityArray", None) is not None
        and normalized_attr(obj, property_identifier) == "presentValue"
    )
    if commandable and priority is None and _is_null_value(any_value):
        priority = _MAX_PRIORITY
    try:
        value = any_value.cast_out(property_type, null=(commandable and priority is not None))
    except (InvalidTag, DecodingError, AttributeError, TypeError, ValueError) as exc:
        # AttributeError: a constructed value missing a required element
        # (a Real written to Effective_Period).
        raise ExecutionError(errorClass="property", errorCode="invalidDataType") from exc
    await obj.write_property(property_identifier, value, array_index, priority)


class LoggingApplication(Application):
    """An Application that records service activity to a TrafficLog.
    traffic_log is attached after construction (build_application), since
    Application.from_object_list() takes no extra constructor kwargs.
    """

    traffic_log: TrafficLog
    # Set by build_application, one map per application.
    _read_range_cursors: ReadRangeCursors
    # The database for BACnet writes that save config and for backup/restore.
    # None in tests that don't need it.
    session_factory = None
    # "enable"/"disable"/"disableInitiation", in memory; mirrored to
    # Device.dcc_state only for the UI. bacpypes3's own DCC service is dead
    # code (writes a nonexistent self.smap), so it isn't used.
    _dcc_state: str = "enable"
    _dcc_enable_handle: Optional[asyncio.TimerHandle] = None
    # DM-BR-B session, set by build_application -- see backup_restore.py.
    backup_restore: Optional[BackupRestore] = None
    # The backup export and the restore run in a worker thread, so requests
    # keep being served meanwhile: the lock keeps ReinitializeDevice requests
    # one at a time, and DeviceWatch holds live reloads while a restore applies.
    _reinitialize_lock: Optional[asyncio.Lock] = None
    _password_lock: Optional[asyncio.Lock] = None
    applying_restore: bool = False

    def get_services_supported(self):
        """Replaces bacpypes3's own (0.0.106), which sets the wrong bit for
        every unconfirmed service.
        """
        return services_supported(type(self))

    def _resolve_device_wildcard(self, object_identifier):
        """device,4194303 means this device. bacpypes3's RP/RPM check for it
        compares an ObjectIdentifier with a tuple, which is never equal."""
        if (
            object_identifier is not None
            and self.device_object is not None
            and str(object_identifier[0]) == "device"
            and int(object_identifier[1]) == WILDCARD_DEVICE_INSTANCE
        ):
            return self.device_object.objectIdentifier
        return object_identifier

    def get_object_id(self, objid):
        # Every service looks objects up here, so the wildcard reaches the
        # device for WriteProperty, WPM, ReadRange, COV and the alarm services.
        return super().get_object_id(self._resolve_device_wildcard(objid))

    async def do_ReadPropertyRequest(self, apdu) -> None:
        # The ACK echoes the request's identifier: rewrite it first so the
        # reply names the real device, not device,4194303.
        apdu.objectIdentifier = self._resolve_device_wildcard(apdu.objectIdentifier)
        await super().do_ReadPropertyRequest(apdu)

    async def do_ReadPropertyMultipleRequest(self, apdu) -> None:
        """bacpypes3's handler, except that it gives the loop a turn every
        RPM_TURN_SECONDS while it builds the reply (a whole-device read
        stalled COV and the schedules), and an object that is missing beside
        ones that exist gets its own unknown-object result (upstream failed
        the whole request on all/required/optional)."""
        specs: list = apdu.listOfReadAccessSpecs  # pyright: ignore[reportAssignmentType] -- bacpypes3 declares the field as its ListOf class
        for spec in specs:
            spec.objectIdentifier = self._resolve_device_wildcard(spec.objectIdentifier)
        if all(self.get_object_id(spec.objectIdentifier) is None for spec in specs):
            raise ObjectError("unknown-object")

        turn_due = time.perf_counter() + RPM_TURN_SECONDS
        results = []
        for spec in specs:
            obj = self.get_object_id(spec.objectIdentifier)
            elements = []
            for reference in spec.listOfPropertyReferences:
                if time.perf_counter() >= turn_due:
                    await asyncio.sleep(0)
                    turn_due = time.perf_counter() + RPM_TURN_SECONDS
                identifier = reference.propertyIdentifier
                special = _rpm_special(identifier)
                if obj is None or special is None:
                    index = None if special else reference.propertyArrayIndex
                    elements.append(await read_property_to_result_element(obj, identifier, index))
                    continue
                for name in _rpm_property_names(obj, special):
                    # A property declared but without a value isn't listed.
                    if bacpypes3_compat.getattr_static(obj, name, None) is None:
                        continue
                    element = await read_property_to_result_element(obj, name, None)
                    if not element.readResult.propertyAccessError:
                        elements.append(element)
            results.append(ReadAccessResult(objectIdentifier=spec.objectIdentifier, listOfResults=elements))
        ack = ReadPropertyMultipleACK(listOfReadAccessResults=results, context=apdu)
        await self.response(ack)  # pyright: ignore[reportArgumentType] -- an ACK is an APDU; bacpypes3 types it as its Sequence base

    async def do_WhoIsRequest(self, apdu) -> None:
        self.traffic_log.add("who-is", str(apdu.pduSource))
        await super().do_WhoIsRequest(apdu)

    def i_am(self, address=None) -> None:
        self.traffic_log.add("i-am", str(address) if address else "local broadcast")
        super().i_am(address)

    async def do_IAmRequest(self, apdu) -> None:
        self.traffic_log.add(
            "i-am-received", f"{apdu.pduSource} is device {apdu.iAmDeviceIdentifier[1]}"
        )
        await super().do_IAmRequest(apdu)
        # Upstream records an I-Am only for a Who-Is of its own that is
        # waiting; a workstation that announced itself is known at once.
        if apdu.pduSource is not None:
            await self.device_info_cache.set_device_info(apdu)

    async def do_SubscribeCOVRequest(self, apdu) -> None:
        self.traffic_log.add(
            "subscribe-cov", f"{apdu.pduSource} -> {object_id_text(apdu.monitoredObjectIdentifier)}"
        )
        # Both fields absent is a cancellation, which upstream detects itself.
        # Lifetime alone absent is an indefinite subscription (135 13.14.1);
        # upstream compares it to 0 and fails, and 0 is its "permanent".
        if apdu.lifetime is None and apdu.issueConfirmedNotifications is not None:
            apdu.lifetime = 0
        await super().do_SubscribeCOVRequest(apdu)

    def cov_notification(self, cov, apdu) -> None:
        # Swallowed by request(): the traffic log mustn't say it was sent.
        if self._suppresses(apdu):
            return
        self.traffic_log.add(
            "cov-notification", f"-> {apdu.pduDestination} obj {object_id_text(apdu.monitoredObjectIdentifier)}"
        )
        super().cov_notification(cov, apdu)

    async def do_WritePropertyRequest(self, apdu: WritePropertyRequest) -> None:
        """Replaces bacpypes3's handler so each malformed write gets the
        error 135 names -- see _write_one."""
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            raise ExecutionError(errorClass="object", errorCode="unknownObject")
        await _write_one(
            obj, apdu.propertyIdentifier, apdu.propertyArrayIndex, apdu.priority, apdu.propertyValue
        )
        await self.response(SimpleAckPDU(context=apdu))

    async def do_WritePropertyMultipleRequest(self, apdu: WritePropertyMultipleRequest) -> None:
        """DS-WPM-B, replacing bacpypes3's handler: each property goes
        through _write_one; the first failure ends the request as
        firstFailedWriteAttempt and earlier writes stand. The error is sent
        directly with context=apdu (bacpypes3's never correlated to the
        request). Every failure, an unknown object included, is a
        WritePropertyMultiple-Error naming a property: the reference doesn't
        encode without one."""
        for spec in apdu.listOfWriteAccessSpecs:
            obj = self.get_object_id(spec.objectIdentifier)
            if obj is None:
                first = spec.listOfProperties[0] if spec.listOfProperties else None
                await self._wpm_failed(apdu, "object", "unknownObject", spec.objectIdentifier, first)
                return
            for prop_value in spec.listOfProperties:
                try:
                    await _write_one(
                        obj,
                        prop_value.propertyIdentifier,
                        prop_value.propertyArrayIndex,
                        prop_value.priority,
                        prop_value.value,
                    )
                except ExecutionError as err:
                    await self._wpm_failed(apdu, err.errorClass, err.errorCode, obj.objectIdentifier, prop_value)
                    return
                except Exception:
                    logger.exception("WritePropertyMultiple to %s failed", obj.objectIdentifier)
                    await self._wpm_failed(apdu, "device", "operationalProblem", obj.objectIdentifier, prop_value)
                    return
        await self.response(SimpleAckPDU(context=apdu))

    async def _wpm_failed(self, apdu, error_class, error_code, object_identifier, prop_value) -> None:
        reference = ObjectPropertyReference(
            objectIdentifier=object_identifier,
            propertyIdentifier=prop_value.propertyIdentifier if prop_value is not None else "all",
        )
        if prop_value is not None and prop_value.propertyArrayIndex is not None:
            reference.propertyArrayIndex = prop_value.propertyArrayIndex
        await self.response(
            WritePropertyMultipleError(
                errorType=ErrorType(errorClass=error_class, errorCode=error_code),
                firstFailedWriteAttempt=reference,
                context=apdu,
            )
        )

    def track_trend_log(self, log) -> None:
        """Index `log` by the trended row id it records, so finding it never
        walks the objects. Called wherever a log joins the application."""
        row_id = getattr(log, "_trend_object_id", None)
        if row_id is not None:
            self.__dict__.setdefault("_trend_logs_by_row", {})[row_id] = log

    def untrack_trend_log(self, log) -> None:
        by_row = self.__dict__.get("_trend_logs_by_row", {})
        row_id = getattr(log, "_trend_object_id", None)
        if row_id is not None and by_row.get(row_id) is log:
            del by_row[row_id]

    def _trend_logs_of(self, object_ids) -> list:
        """The live Trend Logs of the trended rows `object_ids`."""
        by_row = self.__dict__.get("_trend_logs_by_row", {})
        return [by_row[row_id] for row_id in object_ids if row_id in by_row]

    def trend_records_added(self, object_ids=None) -> None:
        """After new Trend Log records are committed, lets each BUFFER_READY
        log check its threshold. `object_ids` are trended row ids; None means
        every trend log. A method here so trending.py needn't import
        trend_log_reporting.py (an import cycle)."""
        if object_ids is None:
            candidates = list(self.objectIdentifier.values())
        else:
            candidates = self._trend_logs_of(object_ids)
        for obj in candidates:
            records_added = getattr(obj, "records_added", None)
            if records_added is not None:
                records_added()

    async def do_ReadRangeRequest(self, apdu: ReadRangeRequest) -> None:
        """Server-side ReadRange for a Trend Log's logBuffer only; bacpypes3's
        own handler raises NotImplementedError.
        """
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            raise ExecutionError(errorClass="object", errorCode="unknownObject")

        trend_object_id = getattr(obj, "_trend_object_id", None)
        try:
            prop_attr = normalized_attr(obj, apdu.propertyIdentifier)
        except Exception:
            prop_attr = None
        if not prop_attr or bacpypes3_compat.element_class(obj, prop_attr) is None:
            raise ExecutionError(errorClass="property", errorCode="unknownProperty")
        if trend_object_id is None or prop_attr != "logBuffer":
            raise ExecutionError(errorClass="services", errorCode="propertyIsNotAList")
        if apdu.propertyArrayIndex is not None:
            raise ExecutionError(errorClass="property", errorCode="propertyIsNotAnArray")

        # A by-time reference is a wall time that repeats when the clock falls
        # back; the remembered end of this client's last reply says which of
        # the moments it means.
        by_time = apdu.range.byTime if apdu.range is not None else None
        cursor_key = None
        cursors = ()
        if by_time is not None and int(by_time.count) != 0:
            cursor_key = (apdu.pduSource, trend_object_id, int(by_time.count) > 0)
            cursors = self._read_range_cursors.get(cursor_key)

        try:
            # Off the event loop: the device answers nothing else meanwhile.
            # bacpypes3 aborts a reply bigger than the client accepts, so
            # the window is trimmed to fit.
            window = await asyncio.to_thread(
                read_trend_window,
                obj._trend_session_factory,
                trend_object_id,
                obj._trend_object_type,
                apdu.range,
                _reply_octet_limit(apdu, READ_RANGE_ACK_OVERHEAD),
                cursors,
            )
        except ValueError:
            # A byTime referenceTime that isn't a real instant.
            raise ExecutionError(errorClass="services", errorCode="parameterOutOfRange") from None
        if cursor_key is not None and window.end is not None:
            self._read_range_cursors.remember(cursor_key, window.end, window.cursor_used)

        # Direct assignment, not SequenceOf(Any)().cast_in() as bacpypes3's
        # unfinished stub does: this is what round-trips through a real
        # client's Any.cast_out().
        resp = ReadRangeACK(
            objectIdentifier=apdu.objectIdentifier,
            propertyIdentifier=apdu.propertyIdentifier,
            resultFlags=ResultFlags([int(window.first_item), int(window.last_item), int(window.more_items)]),
            itemCount=len(window.records),
            itemData=ListOf(LogRecord)(window.records),
            context=apdu,
        )
        if window.records and _reports_first_sequence_number(apdu.range):
            resp.firstSequenceNumber = window.first_sequence
        await self.response(resp)

    async def do_AcknowledgeAlarmRequest(self, apdu: AcknowledgeAlarmRequest) -> None:
        """AE-ACK-B; bacpypes3 has the APDU but no handler. The ack must carry
        the time stamp of the object's last transition of that kind
        (invalidTimeStamp), and, while the object is in that kind of state,
        its current Event_State (invalidEventState); bits are unchanged on a
        refusal. A newly set Acked_Transitions bit sends an ACK_NOTIFICATION;
        a repeated ack sends nothing."""
        self.traffic_log.add(
            "acknowledge-alarm", f"{apdu.pduSource} -> {object_id_text(apdu.eventObjectIdentifier)}"
        )
        obj = self.get_object_id(apdu.eventObjectIdentifier)
        if obj is None or bacpypes3_compat.event_algorithm(obj) is None:
            raise ExecutionError(errorClass="object", errorCode="unknownObject")

        # Same grouping as bacpypes3's EventAlgorithm.state_transition:
        # normal=2, fault=1, every other state is the offnormal family=0.
        new_state_index = state_group_index(apdu.eventStateAcknowledged)
        try:
            if not time_stamp_matches(obj.eventTimeStamps[new_state_index], apdu.timeStamp):  # pyright: ignore[reportArgumentType] -- the APDU field is a TimeStamp; bacpypes3 types it as its Sequence base
                raise refusal(
                    "services", "invalidTimeStamp",
                    "the time stamp isn't that of the object's last transition of that kind",
                )
            if state_group_index(obj.eventState) == new_state_index and apdu.eventStateAcknowledged != obj.eventState:
                raise refusal(
                    "services", "invalidEventState",
                    f"{apdu.eventStateAcknowledged} isn't the object's current event state",
                )
        except ExecutionError as exc:
            self._log_refusal("acknowledge-alarm-refused", apdu, "AcknowledgeAlarm", exc)
            raise

        acked = list(obj.ackedTransitions)
        newly_acked = not acked[new_state_index]
        acked[new_state_index] = True
        obj.ackedTransitions = acked

        await self.response(SimpleAckPDU(context=apdu))
        if newly_acked:
            # In the background, so a slow recipient can't hold up the reply.
            held_tasks.start(self._send_ack_notification(obj, apdu, new_state_index))

    async def _send_ack_notification(self, obj, apdu, state_index: int) -> None:
        """An ACK_NOTIFICATION through the object's own send_notifications,
        which applies Event_Enable and each recipient's filters. From_State,
        Ack_Required and Event_Values are omitted, as for every ack
        notification."""
        notification_class = bacpypes3_compat.notification_class_object(obj)
        if notification_class is None:
            return
        algorithm = bacpypes3_compat.event_algorithm(obj)
        params = {
            "initiatingDeviceIdentifier": self.device_object.objectIdentifier,
            "eventObjectIdentifier": obj.objectIdentifier,
            "timeStamp": apdu.timeOfAcknowledgment,
            "notificationClass": obj.notificationClass,
            "priority": notification_class.priority[state_index],
            "eventType": EventType.changeOfReliability if state_index == 1 else algorithm.event_type,
            "notifyType": NotifyType.ackNotification,
            "toState": apdu.eventStateAcknowledged,
        }
        try:
            await algorithm.send_notifications(params)
        except Exception:
            logger.exception("acknowledgement notification for %s failed", obj.objectIdentifier)

    async def do_GetEventInformationRequest(self, apdu: GetEventInformationRequest) -> None:
        """AE-INFO-B (bacpypes3 has no handler). Events go in object
        identifier order, starting after lastReceivedObjectIdentifier, and
        as many as fit the client's reply size (bacpypes3 aborts a bigger
        reply); moreEvents says the client should ask again. The order
        doesn't depend on whether the last object is still in alarm."""
        self.traffic_log.add("get-event-information", str(apdu.pduSource))

        last = apdu.lastReceivedObjectIdentifier
        last_key = _object_order(last) if last is not None else None
        summaries = []
        for obj in sorted(self.objectIdentifier.values(), key=lambda o: _object_order(o.objectIdentifier)):
            if last_key is not None and _object_order(obj.objectIdentifier) <= last_key:
                continue
            algorithm = bacpypes3_compat.event_algorithm(obj)
            if algorithm is None:
                continue
            # Not Normal, or any unacknowledged transition; deliberately not
            # filtered by eventEnable.
            if obj.eventState.attr == "normal" and all(obj.ackedTransitions):
                continue

            notification_class = bacpypes3_compat.notification_class_object(obj)
            priorities = notification_class.priority if notification_class else [0, 0, 0]
            summaries.append(
                GetEventInformationEventSummary(
                    objectIdentifier=obj.objectIdentifier,
                    eventState=obj.eventState,
                    acknowledgedTransitions=obj.ackedTransitions,
                    eventTimeStamps=obj.eventTimeStamps,
                    notifyType=obj.notifyType,
                    eventEnable=obj.eventEnable,
                    eventPriorities=list(priorities),
                )
            )

        kept = _fit_summaries(summaries, _reply_octet_limit(apdu, GET_EVENT_INFORMATION_ACK_OVERHEAD))
        resp = GetEventInformationACK(
            listOfEventSummaries=SequenceOf(GetEventInformationEventSummary)(kept),
            moreEvents=len(kept) < len(summaries),
            context=apdu,
        )
        await self.response(resp)

    async def do_AddListElementRequest(self, apdu: AddListElementRequest) -> None:
        """AE-CRL-B: add an element to a list property (Recipient_List)."""
        await self._change_list(apdu, "add-list-element")

    async def do_RemoveListElementRequest(self, apdu: RemoveListElementRequest) -> None:
        """AE-CRL-B: remove an element from a list property (Recipient_List)."""
        await self._change_list(apdu, "remove-list-element")

    async def _change_list(self, apdu, service: str) -> None:
        """bacpypes3 has no handler. A failure must go back as a
        ChangeListError, which Application.indication's ExecutionError path
        can't build, so this answers itself instead of raising."""
        self.traffic_log.add(service, f"{apdu.pduSource} -> {object_id_text(apdu.objectIdentifier)} {apdu.propertyIdentifier}")
        try:
            obj = self._list_target(apdu)
            try:
                destinations = apdu.listOfElements.cast_out(ListOf(Destination))
            except Exception as exc:
                raise ListElementError("property", "invalidDataType") from exc
            if service == "add-list-element":
                await obj.add_recipients(destinations)
            else:
                await obj.remove_recipients(destinations)
        except Exception as exc:
            # Anything else is a bug here, but it still has to go back as a
            # ChangeListError, and be logged where the Logs page shows it.
            if isinstance(exc, ListElementError):
                err = exc
            else:
                logger.exception("%s on %s failed", service, apdu.objectIdentifier)
                err = ListElementError("device", "operationalProblem")
            await self.response(
                ChangeListError(
                    service_choice=type(apdu).service_choice,
                    errorType=ErrorType(errorClass=err.error_class, errorCode=err.error_code),
                    firstFailedElementNumber=err.position,
                    context=apdu,
                )
            )
            return
        await self.response(SimpleAckPDU(context=apdu))

    def _list_target(self, apdu) -> RecipientListEditable:
        """The object whose list the request edits, or a request-level
        ListElementError (position 0). Only a Notification Class's
        Recipient_List is editable."""
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            raise ListElementError("object", "unknownObject")
        try:
            attr = normalized_attr(obj, apdu.propertyIdentifier)
        except Exception:
            attr = None
        element = bacpypes3_compat.element_class(obj, attr) if attr else None
        if element is None:
            raise ListElementError("property", "unknownProperty")
        if not (isinstance(element, type) and issubclass(element, List)):
            raise ListElementError("services", "propertyIsNotAList")
        if apdu.propertyArrayIndex is not None:
            raise ListElementError("property", "propertyIsNotAnArray")
        if attr != "recipientList" or not isinstance(obj, RecipientListEditable):
            raise ListElementError("property", "writeAccessDenied")
        return obj

    async def do_ReinitializeDeviceRequest(self, apdu: ReinitializeDeviceRequest) -> None:
        """DM-RD-B and DM-BR-B (bacpypes3 has no handler).

        coldstart/warmstart (and a successful endRestore) don't kill the
        process: they set process_status="reinitializing", _serve_device's
        stop check exits cleanly, and the parent's health check respawns it.
        """
        self.traffic_log.add(
            "reinitialize-device", f"{apdu.pduSource} -> {apdu.reinitializedStateOfDevice}"
        )
        async with self._password_order_lock():
            await self._require_bacnet_password(apdu, "ReinitializeDevice")
            state = apdu.reinitializedStateOfDevice.attr if apdu.reinitializedStateOfDevice is not None else None
            if self._reinitialize_lock is None:
                self._reinitialize_lock = asyncio.Lock()
            async with self._reinitialize_lock:
                await self._reinitialize(apdu, state)

    def _password_order_lock(self) -> asyncio.Lock:
        """Held by DCC and ReinitializeDevice around the password check and the
        apply. The check awaits worker threads, so without it two requests
        could be applied out of order, and a flood of wrong passwords would
        run scrypt on every executor thread at once. Taken before
        _reinitialize_lock, never inside it, so a bad password cannot hold
        that lock. Created on first use, inside the loop."""
        if self._password_lock is None:
            self._password_lock = asyncio.Lock()
        return self._password_lock

    async def _require_bacnet_password(self, apdu, service: str) -> None:
        """The device's optional DCC/ReinitializeDevice password. Read from
        SQLite on each request, so a change in the web UI applies at once; the
        read and the scrypt check run in worker threads, and before the
        reinitialize lock, so a bad password neither stalls the loop nor holds
        the lock."""
        if self.session_factory is None:
            return
        expected = await asyncio.to_thread(self._stored_bacnet_password_hash)
        if expected is None:
            return
        supplied = str(apdu.password) if apdu.password is not None else ""
        if await asyncio.to_thread(verify_password, supplied, expected):
            return
        security_log.bacnet_password_failed(str(apdu.pduSource), service)
        self.traffic_log.add("bacnet-password-failed", f"{apdu.pduSource}: {service}")
        raise ExecutionError(errorClass="security", errorCode="passwordFailure")

    def _stored_bacnet_password_hash(self) -> str | None:
        """Runs in a worker thread."""
        if self.session_factory is None:
            return None
        with self.session_factory() as session:  # pyright: ignore[reportGeneralTypeIssues] -- class attribute declared None, assigned per instance
            device = session.get(Device, self.device_object.objectIdentifier[1])  # pyright: ignore[reportOptionalMemberAccess] -- set before any request
            return device.bacnet_password_hash if device is not None else None

    async def _reinitialize(self, apdu: ReinitializeDeviceRequest, state) -> None:
        if state in ("coldstart", "warmstart"):
            if self.backup_restore is not None:
                self.backup_restore.reset()  # a reboot ends any backup/restore session
            await self.response(SimpleAckPDU(context=apdu))
            await self._mark_reinitializing(str(state))
            return

        try:
            await self._backup_restore_step(apdu, state)
        except ExecutionError as exc:
            self._log_refusal("reinitialize-refused", apdu, str(state), exc)
            raise

    async def _backup_restore_step(self, apdu: ReinitializeDeviceRequest, state) -> None:
        """The DM-BR-B states; a refusal raises out to the caller's logging."""
        backup_restore = self.backup_restore
        if backup_restore is None or state not in (
            "startBackup", "endBackup", "startRestore", "endRestore", "abortRestore"
        ):
            raise refusal(
                "services",
                "optionalFunctionalityNotSupported",
                "this device doesn't support that reinitialize state",
            )

        if state == "startBackup":
            # Fail a wrong-state request before the read; start_backup
            # re-checks, on the loop, where it starts the timeout timer.
            backup_restore.require("idle")
            backup_restore.start_backup(await asyncio.to_thread(self._backup_snapshot))
        elif state == "endBackup":
            backup_restore.end_backup()
        elif state == "startRestore":
            backup_restore.start_restore()
        elif state == "abortRestore":
            backup_restore.abort_restore()
        else:  # endRestore
            # Held until the row says "reinitializing", which stops DeviceWatch.
            self.applying_restore = True
            try:
                await self._apply_restore(backup_restore.take_restore_data())
                await self.response(SimpleAckPDU(context=apdu))
                await self._mark_reinitializing("restore (the restored config is saved)")
            finally:
                self.applying_restore = False
            return
        await self.response(SimpleAckPDU(context=apdu))

    def _log_refusal(self, event_type: str, apdu, request: str, exc: ExecutionError) -> None:
        """The wire Error says only class/code, so a refusal carrying a
        reason (backup_restore.refusal) also goes to the Traffic and Logs
        pages."""
        reason = getattr(exc, "reason", None)
        if reason is None:
            return
        detail = f"{request} refused: {reason}"
        logger.warning("%s (from %s)", detail, apdu.pduSource)
        self.traffic_log.add(event_type, f"{apdu.pduSource}: {detail}")

    async def _mark_reinitializing(self, what: str) -> None:
        """Runs after the SimpleAck went out, so a failure mustn't escape:
        bacpypes3 would send a second, Error, response to the request."""
        if self.session_factory is None:
            return
        device_id = self.device_object.objectIdentifier[1]

        def mark(session) -> None:
            device = session.get(Device, device_id)
            if device is not None:
                device.runtime.process_status = "reinitializing"

        try:
            await config_save.save_or_raise(self.session_factory, mark)
        except Exception:
            logger.exception(
                "%s acknowledged but the device could not be marked for reinitialization; "
                "it is still running its old config; restart it from the web UI",
                what,
            )
            self.traffic_log.add("reinitialize-failed", what)

    def _backup_snapshot(self) -> bytes:
        """startBackup: this device's config, read fresh from SQLite (the web
        UI may have edited it since this process started). Runs in a worker
        thread."""
        if self.session_factory is None:
            raise ExecutionError(errorClass="device", errorCode="operationalProblem")
        with self.session_factory() as session:
            device = session.get(Device, self.device_object.objectIdentifier[1])
            if device is None:
                raise ExecutionError(errorClass="device", errorCode="operationalProblem")
            return export_device_backup(device)

    async def _apply_restore(self, data: bytes) -> None:
        """endRestore: validate and apply in one transaction, or change
        nothing. The wire error is generic, so the reason goes to the Logs
        page and the traffic log."""
        if self.session_factory is None:
            raise ExecutionError(errorClass="device", errorCode="operationalProblem")
        device_id = self.device_object.objectIdentifier[1]  # pyright: ignore[reportOptionalMemberAccess] -- set before any request
        try:
            summary = await asyncio.to_thread(self._write_restore, self.session_factory, device_id, data)
        except Exception as exc:
            logger.warning("restore rejected: %s", exc)
            self.traffic_log.add("restore-rejected", str(exc))
            raise ExecutionError(errorClass="device", errorCode="invalidConfigurationData") from None
        logger.info(
            "restore applied: %d created, %d updated, %d deleted",
            len(summary["created"]),
            len(summary["updated"]),
            len(summary["deleted"]),
        )

    @staticmethod
    def _write_restore(session_factory, device_id: int, data: bytes) -> dict:
        """Runs in a worker thread."""
        with session_factory() as session:
            lock_both_files(session)
            device = session.get(Device, device_id)
            if device is None:
                raise ValueError("this device's row no longer exists")
            summary = apply_device_restore(session, device, data)
            session.commit()
            trim_devices_to_caps(session, [device_id])
        return summary

    def _config_file_session(self, file_identifier) -> BackupRestore:
        object_type, instance = file_identifier
        if self.backup_restore is None or str(object_type) != "file" or instance != CONFIG_FILE_INSTANCE:
            raise refusal(
                "object",
                "unknownObject",
                f"{object_type},{instance} is not the config file (only file,{CONFIG_FILE_INSTANCE} is)",
            )
        return self.backup_restore

    async def do_AtomicReadFileRequest(self, apdu: AtomicReadFileRequest) -> None:
        """DM-BR-B, stream access to file,1 only -- bacpypes3 has the APDU
        classes but no handler."""
        self.traffic_log.add("atomic-read-file", f"{apdu.pduSource} -> {object_id_text(apdu.fileIdentifier)}")
        try:
            await self._atomic_read(apdu)
        except ExecutionError as exc:
            self._log_refusal("atomic-file-refused", apdu, "AtomicReadFile", exc)
            raise

    async def _atomic_read(self, apdu: AtomicReadFileRequest) -> None:
        backup_restore = self._config_file_session(apdu.fileIdentifier)
        stream = apdu.accessMethod.streamAccess
        if stream is None:
            raise refusal("services", "invalidFileAccessMethod", "only stream access is supported")
        cap = _read_chunk_cap(apdu)
        chunk, end_of_file = backup_restore.read(
            int(stream.fileStartPosition), min(int(stream.requestedOctetCount), cap)
        )
        await self.response(
            AtomicReadFileACK(
                endOfFile=end_of_file,
                accessMethod=AtomicReadFileACKAccessMethodChoice(
                    streamAccess=AtomicReadFileACKAccessMethodStreamAccess(
                        fileStartPosition=int(stream.fileStartPosition), fileData=chunk
                    )
                ),
                context=apdu,
            )
        )

    async def do_AtomicWriteFileRequest(self, apdu: AtomicWriteFileRequest) -> None:
        """DM-BR-B, stream access to file,1 during performingARestore only."""
        self.traffic_log.add("atomic-write-file", f"{apdu.pduSource} -> {object_id_text(apdu.fileIdentifier)}")
        try:
            await self._atomic_write(apdu)
        except ExecutionError as exc:
            self._log_refusal("atomic-file-refused", apdu, "AtomicWriteFile", exc)
            raise

    async def _atomic_write(self, apdu: AtomicWriteFileRequest) -> None:
        backup_restore = self._config_file_session(apdu.fileIdentifier)
        stream = apdu.accessMethod.streamAccess
        if stream is None:
            raise refusal("services", "invalidFileAccessMethod", "only stream access is supported")
        start = backup_restore.write(int(stream.fileStartPosition), bytes(stream.fileData))
        await self.response(AtomicWriteFileACK(fileStartPosition=start, context=apdu))

    async def do_DeviceCommunicationControlRequest(
        self, apdu: DeviceCommunicationControlRequest
    ) -> None:
        """DM-DCC-B. bacpypes3's own DeviceCommunicationControlServices would
        crash on Disable (it writes a nonexistent self.smap).
        """
        self.traffic_log.add(
            "device-communication-control", f"{apdu.pduSource} -> {apdu.enableDisable}"
        )
        async with self._password_order_lock():
            await self._require_bacnet_password(apdu, "DeviceCommunicationControl")
            if apdu.enableDisable is None:
                raise ExecutionError(errorClass="services", errorCode="missingRequiredParameter")

            state = apdu.enableDisable.attr
            self._set_dcc_state(state)

            if self._dcc_enable_handle is not None:
                self._dcc_enable_handle.cancel()
                self._dcc_enable_handle = None
            if state != "enable" and apdu.timeDuration:
                self._dcc_enable_handle = asyncio.get_running_loop().call_later(
                    apdu.timeDuration * 60, self._dcc_timer_fired, "enable"
                )

            await self.response(SimpleAckPDU(context=apdu))

    def _set_dcc_state(self, state: str) -> None:
        """Sets the in-memory state at once and mirrors it to the DB in a
        held task (a call_later callback uses this, so it can't await). The
        in-memory state is what enforces DCC; a failed save is logged. Never
        touches _dcc_enable_handle (see _dcc_timer_fired)."""
        self._dcc_state = state
        if self.session_factory is None:
            return
        device_id = self.device_object.objectIdentifier[1]
        held_tasks.start(
            config_save.save_columns(self.session_factory, Device, device_id, {"dcc_state": state}, f"dcc_state={state}")
        )

    def _dcc_timer_fired(self, state: str) -> None:
        """The timeDuration auto-re-enable callback; clears the handle it
        fired from. Not in _set_dcc_state: the DCC handler calls that before
        its cancel check, which would then miss a still-pending timer."""
        self._set_dcc_state(state)
        self._dcc_enable_handle = None

    async def do_TimeSynchronizationRequest(self, apdu: TimeSynchronizationRequest) -> None:
        """DM-TS-B (bacpypes3 has no handler). `time` is local time.
        Unconfirmed: nothing is sent back, even when ignored."""
        self.traffic_log.add("time-synchronization", f"{apdu.pduSource} -> {apdu.time}")
        await self._apply_time_sync(apdu, device_clock.offset_for_local, "TimeSynchronization")

    async def do_UTCTimeSynchronizationRequest(self, apdu: UTCTimeSynchronizationRequest) -> None:
        """DM-UTC-B -- as above, but `time` is UTC."""
        self.traffic_log.add("utc-time-synchronization", f"{apdu.pduSource} -> {apdu.time}")
        await self._apply_time_sync(apdu, device_clock.offset_for_utc, "UTCTimeSynchronization")

    async def _apply_time_sync(self, apdu, offset_for, source: str) -> None:
        fields = bacnet_datetime_fields(apdu.time)
        if fields is None:
            logger.warning("ignoring %s with an unspecified or special date/time: %s", source, apdu.time)
            return
        try:
            new_offset = offset_for(*fields)
        except ValueError:
            new_offset = None
        if new_offset is None or not device_clock.is_representable(new_offset):
            logger.warning("ignoring %s to a time this device can't hold: %s", source, apdu.time)
            return
        await apply_clock_offset(
            self.session_factory, self, self.device_object.objectIdentifier[1], new_offset, source
        )

    async def indication(self, apdu) -> None:
        """Enforces DCC "disable" at the single dispatch point for incoming
        requests: drop everything except DeviceCommunicationControl and
        ReinitializeDevice (135's recovery path; no Who-Is exemption).
        "disableInitiation" blocks nothing incoming; see request().
        """
        if self._dcc_state == "disable" and not isinstance(
            apdu, (DeviceCommunicationControlRequest, ReinitializeDeviceRequest)
        ):
            return
        await super().indication(apdu)

    @property
    def initiation_suppressed(self) -> bool:
        """DCC disable or disableInitiation: the device starts no request
        (an I-Am excepted under disableInitiation; see _suppresses)."""
        return self._dcc_state != "enable"

    def _suppresses(self, apdu) -> bool:
        if self._dcc_state == "disable":
            return True
        return self._dcc_state == "disableInitiation" and not isinstance(apdu, IAmRequest)

    def request(self, apdu):
        """DCC suppresses every request this device initiates (135 16.1):
        under disable all of them, under disableInitiation all but an I-Am.
        Responses don't pass through here."""
        if self._suppresses(apdu):
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future
        return super().request(apdu)

