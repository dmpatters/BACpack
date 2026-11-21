"""Tests for object_builder's construction/dispatch logic itself -- calling
build_application as test setup doesn't put a test here; asserting on
construction/dispatch behavior does. Handler behavior tests (do_* methods,
indication, request, _set_dcc_state, _dcc_timer_fired) belong in
tests/test_application.py instead. Direct build_bacnet_object tests per
object type live in tests/objects/test_*.py, not here."""
from __future__ import annotations

import pytest
from bacpypes3.apdu import GetEventInformationACK
from bacpypes3.basetypes import GetEventInformationEventSummary
from bacpypes3.constructeddata import SequenceOf
from bacpypes3.basetypes import EventState
from bacpypes3.primitivedata import ObjectIdentifier, ObjectType, PropertyIdentifier

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import OBJECT_CLASSES_IR, build_application

from tests.fakes import _device_and_objects


@pytest.mark.asyncio
async def test_build_application_registers_device_and_objects():
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    assert app.device_object.objectIdentifier == ObjectIdentifier(("device", 599001))

    ai = app.get_object_name("AI-1")
    assert ai is not None
    assert await ai.read_property("presentValue") == 0.0

    msv = app.get_object_name("MSV-1")
    assert msv is not None
    assert await msv.read_property("stateText") == ["Off", "Low", "High"]


@pytest.mark.asyncio
async def test_binary_object_reads_on_off_active_inactive_text():
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    bv = app.get_object_name("BV-1")
    assert bv is not None
    assert await bv.read_property("activeText") == "On"
    assert await bv.read_property("inactiveText") == "Off"


@pytest.mark.asyncio
async def test_multistate_object_requires_state_text():
    device, _ = _device_and_objects()
    bad_object = BacnetObject(object_type="multi-state-input", instance_number=2, name="MSI-2")

    with pytest.raises(ValueError):
        build_application(device, [bad_object])


@pytest.mark.asyncio
async def test_unknown_object_type_rejected():
    device, _ = _device_and_objects()
    bad_object = BacnetObject(object_type="not-a-real-type", instance_number=1, name="X-1")

    with pytest.raises(ValueError):
        build_application(device, [bad_object])


@pytest.mark.asyncio
async def test_build_application_error_identifies_the_offending_object():
    device, _ = _device_and_objects()
    # Bypasses the API-layer validate_units() call on purpose, to exercise
    # build_application's own defense-in-depth wrapping directly -- this is
    # exactly the live incident: bad data already in the DB (e.g. from
    # before validation existed) must still fail cleanly, attributed to the
    # object that caused it, not as a bare library exception.
    bad_object = BacnetObject(
        object_type="analog-output", instance_number=2, name="DPTestAO", units="F"
    )

    with pytest.raises(ValueError, match=r"analog-output:2 \"DPTestAO\".*not a valid BACnet"):
        build_application(device, [bad_object])


@pytest.mark.asyncio
async def test_build_bacnet_object_excludes_out_of_service_for_calendar():
    # CalendarObject has no outOfService element at all (unlike every other
    # type, including Schedule) -- build_bacnet_object must exclude it from
    # the shared kwargs for this type or construction fails outright.
    # out_of_service_default is set here specifically to prove it's ignored,
    # not just left at its default.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    cal = BacnetObject(
        object_type="calendar",
        instance_number=1,
        name="Cal-1",
        out_of_service_default=True,
        date_list=[{"kind": "date", "year": "*", "month": "*", "day": "*"}],
    )
    app = build_application(device, [cal])
    obj = app.get_object_name("Cal-1")
    assert obj is not None
    assert not hasattr(obj, "outOfService")


@pytest.mark.asyncio
async def test_build_application_builds_ir_variant_when_alarming_enabled():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(
        object_type="notification-class", instance_number=1, name="NC-1"
    )
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
    )
    app = build_application(device, [nc, ai])
    obj = app.get_object_name("AI-1")
    assert hasattr(obj, "_event_algorithm")
    assert obj._event_algorithm is not None


@pytest.mark.asyncio
async def test_build_application_plain_variant_when_alarming_disabled():
    device, objects = _device_and_objects()  # AI-1 has no alarming fields set
    app = build_application(device, objects)
    obj = app.get_object_name("AI-1")
    assert obj._event_algorithm is None


@pytest.mark.asyncio
async def test_build_application_rejects_missing_notification_class_before_construction():
    """Closes the design doc's startup-crash gotcha: bacpypes3's own
    Object._post_init links to a Notification Class object in a
    fire-and-forget asyncio task and raises RuntimeError uncaught if it's
    missing -- BACpack must never let construction get that far."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=99,  # no such notification-class object
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
    )
    with pytest.raises(ValueError, match="notification-class:99 as its notification class, which does not exist"):
        build_application(device, [ai])


@pytest.mark.asyncio
async def test_transition_to_offnormal_resets_acked_transitions_to_false():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
    )
    app = build_application(device, [nc, ai])
    obj = app.get_object_name("AI-1")
    assert list(obj.ackedTransitions) == [True, True, True]

    obj.presentValue = 150.0  # above highLimit -- triggers a transition
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert list(obj.ackedTransitions)[0] is False  # offnormal-family index


@pytest.mark.asyncio
async def test_alarming_object_initial_event_time_stamps_are_wire_encodable():
    """Regression test for a bug a live-wire check caught: a fresh,
    never-transitioned alarming object's eventTimeStamps entries must be
    genuinely encodable TimeStamp values, not bare None. bacpypes3's TimeStamp
    is a Choice with exactly three variants (time/sequenceNumber/dateTime) and
    no "unused"/null variant, so a None -- or a no-args TimeStamp() with no
    Choice variant selected, which is what an ArrayOf(TimeStamp) casts a bare
    None element to -- cannot be encoded onto the wire.

    Deliberately does NOT trigger a transition: the bug is specifically
    about the untransitioned/initial-state array slots (a real transition
    already overwrites its slot with a real TimeStamp via bacpypes3's own
    EventAlgorithm.state_transition).

    This builds the exact same GetEventInformationEventSummary the real
    do_GetEventInformationRequest handler builds (see application.py) and
    then genuinely encodes it, mirroring the encode step a real
    GetEventInformationRequest/ACK round-trip performs on the wire --
    not just a local Python-state inspection like `list(obj.eventTimeStamps)`,
    which is exactly the class of test that already existed and missed this
    bug (every prior transition-focused test only inspects local state, or
    only exercises the offnormal branch where index 0 gets overwritten by a
    real transition, leaving the other two never-transitioned slots
    unencoded in any prior test)."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai_row = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
        # alarm_notify_type has a real "alarm" default at the DB column
        # level, but that default is only applied by SQLAlchemy on flush,
        # not on direct construction like this -- set it explicitly here
        # so this test's object matches what a real DB-loaded row would
        # give build_bacnet_object, rather than the notifyType=None a bare
        # in-memory BacnetObject() would otherwise have (an artifact of
        # this test's construction, not of the bug under test).
        alarm_notify_type="alarm",
    )
    app = build_application(device, [nc, ai_row])
    ai = app.get_object_name("AI-1")
    assert ai.eventState.attr == "normal"  # untransitioned -- the state the bug affects

    summary = GetEventInformationEventSummary(
        objectIdentifier=ai.objectIdentifier,
        eventState=ai.eventState,
        acknowledgedTransitions=ai.ackedTransitions,
        eventTimeStamps=ai.eventTimeStamps,
        notifyType=ai.notifyType,
        eventEnable=ai.eventEnable,
        eventPriorities=[0, 0, 0],
    )
    resp = GetEventInformationACK(
        listOfEventSummaries=SequenceOf(GetEventInformationEventSummary)([summary]),
        moreEvents=False,
    )
    # Real wire-encoding pass: APCISequence.encode() -> Sequence.encode(),
    # which recurses into encoding every field including each
    # eventTimeStamps[i]'s own Choice.encode(). Before the fix this raises
    # AttributeError("no choice") -- confirmed directly against the
    # pre-fix code -- because the None-cast TimeStamp has no Choice variant
    # selected.
    resp.encode()


@pytest.mark.asyncio
async def test_alarming_object_initial_event_time_stamps_are_wildcard_date_times():
    """A never-transitioned slot must use the dateTime choice (all
    wildcards), not sequenceNumber. YABE's GetEventInformation decoder
    (Services.DecodeAlarmSummaryOrEvent) skips a time-choice stamp and
    otherwise assumes dateTime; fed a sequenceNumber stamp its cursor
    desyncs and it throws, leaving the Active Alarms window stuck on
    "Loading alarms" once any point is in alarm. A real transition only
    overwrites its own slot, so the other two keep this initial value."""
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai_row = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_low_limit=0.0,
        alarm_notify_type="alarm",
    )
    app = build_application(device, [nc, ai_row])
    ai = app.get_object_name("AI-1")

    for stamp in ai.eventTimeStamps:
        assert stamp.sequenceNumber is None
        assert stamp.time is None
        assert str(stamp.dateTime) == "*-*-* * *:*:*.*"


# --- Every OBJECT_CLASSES_IR entry
# actually gets built and driven at least once -- closes the coverage gap
# ("4 of 7 IR classes are never instantiated in any test") that let
# ValidatedBinaryValueObjectCmdIR's construction crash ship undetected. See
# tests/objects/test_binary.py for the focused regression test on that
# specific class; this is the broader net that would have caught it (or
# any sibling regression) regardless of which type it hit. ------------


def _alarming_row_for_type(object_type: str) -> BacnetObject:
    """One representative alarming-enabled BacnetObject row per
    ALARMABLE_TYPES member, each configured so a single presentValue
    write (see _trigger_transition below) drives a real transition out of
    "normal"."""
    common = dict(
        object_type=object_type,
        instance_number=1,
        name="OBJ-1",
        alarming_enabled=True,
        notification_class_instance=1,
    )
    if object_type in ("analog-input", "analog-output", "analog-value"):
        return BacnetObject(
            **common,
            units="degreesFahrenheit",
            alarm_high_limit=100.0,
            alarm_low_limit=0.0,
        )
    if object_type in ("binary-input", "binary-value"):
        return BacnetObject(**common, alarm_value=True)
    if object_type in ("multi-state-input", "multi-state-value"):
        return BacnetObject(**common, state_text=["Off", "Low", "High"], alarm_values=[2])
    raise AssertionError(f"no alarming row builder for {object_type!r}")  # pragma: no cover


def _trigger_transition(obj, object_type: str) -> None:
    if object_type in ("analog-input", "analog-output", "analog-value"):
        obj.presentValue = 150.0  # above highLimit
    elif object_type in ("binary-input", "binary-value"):
        obj.presentValue = "active"  # matches alarmValue
    elif object_type in ("multi-state-input", "multi-state-value"):
        obj.presentValue = 2  # matches alarmValues
    else:
        raise AssertionError(f"no transition trigger for {object_type!r}")  # pragma: no cover


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type", sorted(OBJECT_CLASSES_IR))
async def test_every_alarmable_ir_class_builds_and_transitions(object_type):
    import asyncio

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    row = _alarming_row_for_type(object_type)

    app = build_application(device, [nc, row])
    obj = app.get_object_name("OBJ-1")
    assert obj.eventState.attr == "normal"

    _trigger_transition(obj, object_type)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert obj.eventState.attr != "normal"


# --- Binary alarming with
# alarm_value=None must fail with a clear message, not a bare/empty
# TypeError -------------------------------------------------------------


@pytest.mark.asyncio
async def test_binary_alarming_with_no_alarm_value_raises_clear_error():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    bv = BacnetObject(
        object_type="binary-value",
        instance_number=1,
        name="BV-1",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_value=None,
    )
    with pytest.raises(ValueError, match="Alarming tab"):
        build_application(device, [nc, bv])


# --- Analog alarming with both
# limits unset must fail with a clear message at build time, and a
# one-sided limit must actually work (not silently never fire / crash the
# event loop) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_analog_alarming_with_no_limits_set_raises_clear_error():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=None,
        alarm_low_limit=None,
    )
    with pytest.raises(ValueError, match="Alarming tab"):
        build_application(device, [nc, ai])


@pytest.mark.asyncio
async def test_analog_alarming_with_only_low_limit_transitions_on_low_limit():
    """Regression test for Finding 3's core fix: limitEnable must be
    derived from which limits are actually set, not unconditionally
    [True, True] -- otherwise OutOfRangeEventAlgorithm.execute() compares
    against a None highLimit and raises TypeError from inside the asyncio
    event loop the moment presentValue changes at all."""
    import asyncio

    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=None,
        alarm_low_limit=10.0,
    )
    app = build_application(device, [nc, ai])
    obj = app.get_object_name("AI-1")
    assert list(obj.limitEnable) == [True, False]

    # A high value would have crashed the old unconditional-[True, True]
    # code (comparison against a None highLimit) -- must not raise here.
    obj.presentValue = 1000.0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert obj.eventState.attr == "normal"  # highLimit disabled -- no alarm

    obj.presentValue = 1.0  # below the real 10.0 lowLimit
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert obj.eventState.attr == "lowLimit"


@pytest.mark.asyncio
async def test_build_application_exposes_session_factory_for_dcc_and_reinitialize():
    device, objects = _device_and_objects()

    def fake_session_factory():
        raise AssertionError("not called by this test")

    app = build_application(device, objects, trend_session_factory=fake_session_factory)
    assert app.session_factory is fake_session_factory


@pytest.mark.asyncio
async def test_build_application_session_factory_defaults_to_none():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    assert app.session_factory is None


@pytest.mark.asyncio
async def test_event_detection_enable_comes_from_the_row():
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai_row = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_notify_type="alarm",
        alarm_event_detection_enable=False,
    )
    app = build_application(device, [nc, ai_row])
    assert app.get_object_name("AI-1").eventDetectionEnable == False  # noqa: E712 (bacpypes3 Boolean)


@pytest.mark.asyncio
async def test_event_detection_enable_defaults_to_true_on_an_unsaved_row():
    # An unsaved BacnetObject reads None for every column default (applied
    # only at flush), so the builder must treat None as the default True.
    device = Device(id=599001, name="Test Device", vendor_identifier=999)
    nc = BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")
    ai_row = BacnetObject(
        object_type="analog-input",
        instance_number=1,
        name="AI-1",
        units="degreesFahrenheit",
        alarming_enabled=True,
        notification_class_instance=1,
        alarm_high_limit=100.0,
        alarm_notify_type="alarm",
    )
    app = build_application(device, [nc, ai_row])
    assert app.get_object_name("AI-1").eventDetectionEnable == True  # noqa: E712


def test_build_application_names_both_objects_on_a_trend_log_name_clash(session_factory):
    device = Device(id=599001, name="Test Device")
    objects = [
        BacnetObject(id=1, object_type="analog-value", instance_number=1, name="AV-1", trended=True),
        BacnetObject(id=2, object_type="analog-value", instance_number=2, name="AV-1-Trend"),
    ]
    with pytest.raises(ValueError, match="AV-1-Trend"):
        build_application(device, objects, trend_session_factory=session_factory)


# --- Backup/Restore (DM-BR-B) ----------------------------------------------

import datetime as dt

from bacpypes3.basetypes import BackupState, FileAccessMethod
from bacpypes3.errors import ExecutionError
from bacpypes3.primitivedata import ObjectIdentifier, Unsigned

from tests.fakes import _device_and_objects


@pytest.mark.asyncio
async def test_config_file_object_is_always_in_the_object_list():
    device, objects = _device_and_objects()
    app = build_application(device, objects)

    object_list = await app.device_object.read_property("objectList")
    assert ("file", 1) in [(str(t), i) for t, i in object_list]
    config_files = await app.device_object.read_property("configurationFiles")
    assert [(str(t), i) for t, i in config_files] == [("file", 1)]

    file_obj = app.get_object_id(ObjectIdentifier(("file", 1)))
    assert file_obj.objectName == "Test Device-config.json"
    assert file_obj.fileType == "application/json"
    assert file_obj.fileAccessMethod == FileAccessMethod.streamAccess
    assert await file_obj.read_property("fileSize") == 0
    assert bool(await file_obj.read_property("readOnly")) is True


@pytest.mark.asyncio
async def test_file_object_follows_the_session():
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    file_obj = app.get_object_id(ObjectIdentifier(("file", 1)))

    app.backup_restore.start_restore()
    app.backup_restore.write(-1, b"abc")
    assert await file_obj.read_property("fileSize") == 3
    assert bool(await file_obj.read_property("readOnly")) is False

    await file_obj.write_property("fileSize", Unsigned(0))
    assert app.backup_restore.size == 0
    with pytest.raises(ExecutionError):
        await file_obj.write_property("fileSize", Unsigned(4))
    app.backup_restore.reset()
    with pytest.raises(ExecutionError):
        await file_obj.write_property("fileSize", Unsigned(0))


@pytest.mark.asyncio
async def test_config_file_object_has_a_property_list():
    # Built on bacpypes3's protocol-only FileObject, file,1
    # had no Property_List.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    file_obj = app.get_object_id(ObjectIdentifier(("file", 1)))

    property_list = [str(p) for p in await file_obj.read_property("propertyList")]

    assert {"file-size", "read-only", "modification-date", "file-access-method"} <= set(property_list)
    await file_obj._post_init()  # a File object has no Notification_Class to link


@pytest.mark.asyncio
async def test_device_reports_the_backup_properties():
    device, objects = _device_and_objects()
    device.database_revision = 7
    # Stored naive UTC; the wire shows device-local time, the same date in any zone UTC-10:30..+13:29.
    device.last_restore_time = dt.datetime(2026, 9, 26, 10, 30, 0)
    app = build_application(device, objects)
    dev = app.device_object

    assert await dev.read_property("databaseRevision") == 7
    assert await dev.read_property("backupFailureTimeout") == 60
    assert await dev.read_property("backupPreparationTime") == 0
    assert await dev.read_property("restorePreparationTime") == 0
    assert await dev.read_property("restoreCompletionTime") == 10
    assert await dev.read_property("backupAndRestoreState") == BackupState.idle
    stamp = await dev.read_property("lastRestoreTime")
    # bacpypes3 Date is a (year - 1900, month, day, day_of_week) tuple.
    assert tuple(stamp.dateTime.date)[:3] == (2026 - 1900, 9, 26)

    app.backup_restore.start_backup(b"x")
    assert await dev.read_property("backupAndRestoreState") == BackupState.performingABackup


@pytest.mark.asyncio
async def test_never_restored_device_reports_an_unspecified_last_restore_time():
    device, objects = _device_and_objects()
    app = build_application(device, objects)  # bare Device: both new columns read None
    assert await app.device_object.read_property("databaseRevision") == 1
    stamp = await app.device_object.read_property("lastRestoreTime")
    assert tuple(stamp.dateTime.date)[0] == 255


def test_object_named_like_the_config_file_fails_startup():
    device, objects = _device_and_objects()
    objects.append(BacnetObject(object_type="binary-value", instance_number=9, name="Test Device-config.json"))
    with pytest.raises(ValueError, match="reserved for the device's backup/restore file"):
        build_application(device, objects)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr",
    [
        "databaseRevision",
        "lastRestoreTime",
        "configurationFiles",
        "backupPreparationTime",
        "restorePreparationTime",
        "restoreCompletionTime",
        "backupAndRestoreState",
    ],
)
@pytest.mark.parametrize("as_identifier", [False, True])
async def test_device_rejects_writes_to_the_backup_properties(attr, as_identifier):
    # Fixed by the process (a restore respawns it) or by the controller
    # (Backup_Failure_Timeout is writable, test_device_backup_status.py):
    # a write can't take effect, so it must be writeAccessDenied, not a
    # Simple-ACK that reads back a value the device ignores. A request off
    # the wire names the property as a PropertyIdentifier, not a string --
    # a live check caught a write slipping through that way.
    from bacpypes3.basetypes import PropertyIdentifier
    from bacpypes3.errors import PropertyError

    device, objects = _device_and_objects()
    app = build_application(device, objects)
    current = await app.device_object.read_property(attr)
    target = PropertyIdentifier(attr) if as_identifier else attr
    with pytest.raises(PropertyError) as excinfo:
        await app.device_object.write_property(target, current)
    assert excinfo.value.errorCode == "writeAccessDenied"


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["objectName", "description", "fileType", "archive", "fileAccessMethod"])
async def test_config_file_object_rejects_writes_outside_file_size(attr):
    from bacpypes3.errors import PropertyError

    device, objects = _device_and_objects()
    app = build_application(device, objects)
    file_obj = app.get_object_id(ObjectIdentifier(("file", 1)))
    current = await file_obj.read_property(attr)
    with pytest.raises((PropertyError, ExecutionError)) as excinfo:
        await file_obj.write_property(attr, current)
    assert str(excinfo.value.errorCode) == "writeAccessDenied"
    assert file_obj.objectName == "Test Device-config.json"


def test_reporting_trend_log_with_a_missing_notification_class_refuses_to_start(session_factory):
    import pytest as _pytest

    from bacpack.models import BacnetObject, Device
    from bacpack.object_builder import build_application

    device = Device(id=599001, name="Test Device")
    av = BacnetObject(
        id=7, object_type="analog-value", instance_number=1, name="AV-1", units="percent",
        trended=True, trend_reporting_enabled=True, trend_notification_class_instance=4,
        trend_notification_threshold=10, trend_notify_type="event", trend_event_detection_enable=True,
    )
    with _pytest.raises(ValueError, match="notification-class:4 as its trend log.s notification class, which does not exist"):
        build_application(device, [av], trend_session_factory=session_factory)


@pytest.mark.parametrize(
    "device_name, extra, message",
    [
        ("Test Device", {"name": "NetworkPort-1"}, "network port"),
        ("X-Trend", {"name": "X", "trended": True}, "trend log"),
    ],
)
def test_a_name_the_device_needs_fails_startup_with_a_clear_message(device_name, extra, message):
    # Without this, bacpypes3 fails with "already an object with name".
    device, objects = _device_and_objects()
    device.name = device_name
    objects.append(BacnetObject(object_type="analog-value", instance_number=99, **extra))
    with pytest.raises(ValueError, match=message):
        build_application(device, objects, trend_session_factory=object())


@pytest.mark.asyncio
async def test_a_schedule_listed_before_its_calendar_still_builds():
    # bacpypes3 looks a calendar reference up as the schedule is added, and
    # an import saves objects in file order.
    schedule = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="SCH-1",
        schedule_value_type="analog",
        schedule_default="1.0",
        schedule_priority=14,
        exception_schedule=[
            {
                "priority": 16,
                "period": {"kind": "calendar-reference", "instance_number": 2},
                "time_values": [{"time": "00:00", "value": "5"}],
            }
        ],
    )
    calendar = BacnetObject(
        object_type="calendar",
        instance_number=2,
        name="CAL-2",
        date_list=[{"kind": "date", "year": "*", "month": 1, "day": 1}],
    )
    app = build_application(Device(id=599001, name="Dev", vendor_identifier=999), [schedule, calendar])
    assert app.get_object_name("SCH-1") is not None


# --- Alarm state at start ---------------------------------------------------
# bacpypes3 evaluates an intrinsic alarm only when a bound property changes;
# nothing changes at start, so a value already past a limit sat in `normal`.

import asyncio

from bacpack import bacpypes3_compat


def _start_nc():
    return BacnetObject(object_type="notification-class", instance_number=1, name="NC-1")


def _start_ai(**fields):
    # An analog input starts at Present_Value 0.0.
    defaults = dict(
        object_type="analog-input", instance_number=1, name="AI-1", units="degreesFahrenheit",
        alarming_enabled=True, notification_class_instance=1, alarm_high_limit=100.0, alarm_low_limit=0.0,
        alarm_deadband=1.0, alarm_time_delay=0,
    )
    return BacnetObject(**{**defaults, **fields})


async def _ticks(n=3):
    for _ in range(n):
        await asyncio.sleep(0)


def _start_app(*rows):
    return build_application(Device(id=599001, name="D", vendor_identifier=999), list(rows))


@pytest.mark.asyncio
async def test_an_object_already_past_its_limit_at_start_is_in_alarm():
    app = _start_app(_start_nc(), _start_ai(alarm_low_limit=10.0))
    await _ticks()
    assert app.get_object_name("AI-1").eventState.attr == "lowLimit"


@pytest.mark.asyncio
async def test_an_object_within_its_limits_at_start_stays_normal():
    app = _start_app(_start_nc(), _start_ai())
    ai = app.get_object_name("AI-1")
    stamps = list(ai.eventTimeStamps)
    await _ticks()
    assert ai.eventState.attr == "normal"
    assert list(ai.eventTimeStamps) == stamps


@pytest.mark.asyncio
async def test_an_object_past_its_limit_at_start_waits_out_its_time_delay():
    app = _start_app(_start_nc(), _start_ai(alarm_low_limit=10.0, alarm_time_delay=60))
    ai = app.get_object_name("AI-1")
    await _ticks()
    assert ai.eventState.attr == "normal"
    assert bacpypes3_compat.has_pending_transition(bacpypes3_compat.event_algorithm(ai))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["AI-1", "AO-1", "BV-1", "MSV-1"])
async def test_non_alarming_object_has_a_normal_event_state(name):
    # Event_State is required on analog, binary and multi-state objects
    # (ASHRAE 135 clause 12); a revision-4 client's import stops at the
    # first unknown-property it gets for it.
    device, objects = _device_and_objects()
    app = build_application(device, objects)
    obj = app.get_object_name(name)
    assert obj._event_algorithm is None
    assert await obj.read_property("eventState") == EventState.normal
    assert PropertyIdentifier.eventState in await obj.read_property("propertyList")


@pytest.mark.asyncio
async def test_non_reporting_trend_log_has_a_normal_event_state(session_factory):
    device, objects = _device_and_objects()
    objects[0].id = 1
    objects[0].trended = True
    app = build_application(device, objects, trend_session_factory=session_factory)
    trend_log = next(o for o in app.objectIdentifier.values() if o.objectIdentifier[0] == ObjectType.trendLog)
    assert await trend_log.read_property("eventState") == EventState.normal
    assert PropertyIdentifier.eventState in await trend_log.read_property("propertyList")


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "expected"), [(None, 32767), (500, 500)])
async def test_trend_log_buffer_size_is_the_sample_cap(session_factory, cap, expected):
    # Some software refuses a trend log whose Buffer_Size is above 32767
    # ("Requested Field Panel buffers exceeds actual number at Field Panel").
    device, objects = _device_and_objects()
    objects[0].id = 1
    objects[0].trended = True
    objects[0].trend_sample_cap = cap
    app = build_application(device, objects, trend_session_factory=session_factory)
    trend_log = next(o for o in app.objectIdentifier.values() if o.objectIdentifier[0] == ObjectType.trendLog)
    assert await trend_log.read_property("bufferSize") == expected


@pytest.mark.asyncio
async def test_binary_object_uses_saved_active_and_inactive_text():
    device = Device(id=599001, name="Test Device")
    bv = BacnetObject(
        object_type="binary-value", instance_number=1, name="BV-1", active_text="Running", inactive_text="Stopped"
    )
    app = build_application(device, [bv])

    built = app.get_object_name("BV-1")
    assert await built.read_property("activeText") == "Running"
    assert await built.read_property("inactiveText") == "Stopped"


@pytest.mark.asyncio
async def test_binary_object_defaults_to_on_off():
    device = Device(id=599001, name="Test Device")
    app = build_application(device, [BacnetObject(object_type="binary-input", instance_number=1, name="BI-1")])

    built = app.get_object_name("BI-1")
    assert await built.read_property("activeText") == "On"
    assert await built.read_property("inactiveText") == "Off"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, start, extra, expected",
    [
        ("analog-input", "21.5", {}, 21.5),
        ("binary-input", "active", {}, "active"),
        ("multi-state-input", "3", {"state_text": ["a", "b", "c", "d"]}, 3),
    ],
)
async def test_input_starts_at_its_start_value(object_type, start, extra, expected):
    row = BacnetObject(object_type=object_type, instance_number=1, name="IN-1", start_value=start, **extra)
    built = build_application(Device(id=599001, name="D"), [row]).get_object_name("IN-1")
    assert str(await built.read_property("presentValue")) == str(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, extra, expected",
    [
        ("analog-input", {}, 0.0),
        ("binary-input", {}, "inactive"),
        ("multi-state-input", {"state_text": ["a", "b"]}, 1),
    ],
)
async def test_input_without_start_value_starts_at_zero_inactive_state_one(object_type, extra, expected):
    row = BacnetObject(object_type=object_type, instance_number=1, name="IN-1", **extra)
    built = build_application(Device(id=599001, name="D"), [row]).get_object_name("IN-1")
    assert str(await built.read_property("presentValue")) == str(expected)


@pytest.mark.asyncio
async def test_start_value_is_ignored_on_an_output():
    row = BacnetObject(object_type="analog-output", instance_number=1, name="AO-1", start_value="21.5")
    built = build_application(Device(id=599001, name="D"), [row]).get_object_name("AO-1")
    assert await built.read_property("presentValue") == 0.0


def test_a_fixed_trend_log_name_clashing_with_an_object_name_names_both(session_factory):
    device = Device(id=599001, name="Test Device")
    objects = [
        BacnetObject(
            id=1, object_type="analog-value", instance_number=1, name="AV-1", trended=True, trend_log_name="AV-2"
        ),
        BacnetObject(id=2, object_type="analog-value", instance_number=2, name="AV-2"),
    ]
    with pytest.raises(ValueError, match="AV-2.*AV-1"):
        build_application(device, objects, trend_session_factory=session_factory)


def _trend_logs(app):
    return sorted(int(oid[1]) for oid in app.objectIdentifier if str(oid[0]) == "trend-log")


@pytest.mark.asyncio
async def test_application_serves_a_fixed_trend_log_by_its_instance_and_name(session_factory):
    device = Device(id=599001, name="Test Device")
    row = BacnetObject(
        id=5, object_type="analog-value", instance_number=1, name="AV-1", trended=True,
        trend_log_instance=77, trend_log_name="Zone Temp Log",
    )
    app = build_application(device, [row], trend_session_factory=session_factory)
    assert _trend_logs(app) == [77]
    assert str(app.get_object_id(ObjectIdentifier(("trend-log", 77))).objectName) == "Zone Temp Log"
    assert app.get_object_name("Zone Temp Log") is not None
    assert app.get_object_id(ObjectIdentifier(("trend-log", 5))) is None


def test_a_fixed_trend_log_instance_equal_to_another_rows_id_is_refused_at_build(session_factory):
    device = Device(id=599001, name="Test Device")
    objects = [
        BacnetObject(
            id=1, object_type="analog-value", instance_number=1, name="AV-1", trended=True, trend_log_instance=2
        ),
        BacnetObject(id=2, object_type="analog-value", instance_number=2, name="AV-2", trended=True),
    ]
    with pytest.raises(ValueError, match="AV-1.*AV-2"):
        build_application(device, objects, trend_session_factory=session_factory)
