"""Contract tests for the bacpypes3 compatibility boundary.

An upgrade attempt starts here: a fingerprint failure lists each upstream
function that changed, why BACpack depends on it, and where BACpack's side
lives. Re-review each one, then paste the new hash."""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import inspect
import pathlib
import pkgutil

import pytest
from bacpypes3.apdu import SubscribeCOVRequest
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import CharacterString, Real

import bacpack
from bacpack import bacpypes3_compat
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application


def upstream_source(qualified_name: str) -> str:
    """The source of one upstream definition, read from bacpypes3's files
    (not the live object: a swapped module global would hand back BACpack's
    replacement). Several same-named definitions in one class (a property's
    getter and setter) are joined."""
    parts = qualified_name.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            spec = importlib.util.find_spec(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        if spec is not None and spec.origin:
            break
    else:
        raise LookupError(qualified_name)
    text = pathlib.Path(spec.origin).read_text(encoding="utf-8")
    body = ast.parse(text).body
    path = parts[split:]
    for depth, name in enumerate(path):
        last = depth == len(path) - 1
        found = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                found.append(node)
            elif last and isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                found.append(node)
            elif (
                last
                and isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value is not None
            ):
                found.append(node)
        if not found:
            raise LookupError(qualified_name)
        if not last:
            body = found[0].body
    return "\n".join(ast.get_source_segment(text, node) for node in found)


def fingerprint(qualified_name: str) -> str:
    """Whitespace-normalised: re-indenting or blank lines alone don't count
    as a change."""
    lines = (line.strip() for line in upstream_source(qualified_name).splitlines())
    return hashlib.sha256("\n".join(line for line in lines if line).encode("utf-8")).hexdigest()[:16]


# Recorded against bacpypes3 0.0.106. A mismatch means upstream changed
# code BACpack overrides or relies on: re-review it (see OVERRIDES /
# DEPENDS_ON in bacpypes3_compat.py for why), then paste the new hash.
FINGERPRINTS = {
    "bacpypes3.app.Application.add_object": "7ae5febd9678275b",
    "bacpypes3.app.Application.get_object_id": "96005fec25366cf5",
    "bacpypes3.app.Application.get_services_supported": "10ef3d48345be2b7",
    "bacpypes3.app.Application.indication": "6cb11c49266fcccb",
    "bacpypes3.app.Application.request": "2de262745d3e427f",
    "bacpypes3.basetypes.DateTime.now": "57f4acc93726fa9f",
    "bacpypes3.basetypes.TimeStamp.as_dateTime": "2d338924348d5d67",
    "bacpypes3.basetypes.TimeStamp.as_time": "4ab9472bde2659f1",
    "bacpypes3.local.analog.AnalogInputObjectIR.__init__": "e042212990d8d594",
    "bacpypes3.local.analog.AnalogOutputObjectIR.__init__": "05c646488f0d6f9e",
    "bacpypes3.local.analog.AnalogValueObjectIR.__init__": "f6cfc4dd4de0e865",
    "bacpypes3.local.binary.BinaryInputObjectIR.__init__": "a4aa976048c25f65",
    "bacpypes3.local.binary.BinaryValueObject._required": "07c52a1dd87776cb",
    "bacpypes3.local.cmd.Commandable.__init__": "4c3a859bc0b997de",
    "bacpypes3.local.cmd.Commandable.__setattr__": "2e450e0ba127f4f6",
    "bacpypes3.local.cmd.Commandable.write_property": "dfaa0e12cbdd30ff",
    "bacpypes3.local.cmd.PriorityArray.__setitem__": "02ee70be1220e984",
    "bacpypes3.local.cov.COVDetection.monitored_property_reference": "c61001c55ad55b75",
    "bacpypes3.local.cov.COVDetection.properties_reported": "610dc84a4b04376a",
    "bacpypes3.local.cov.COVDetection.properties_tracked": "f2eb154620504d70",
    "bacpypes3.local.cov.COVDetection.send_cov_notifications": "9d4c2889a220d80f",
    "bacpypes3.local.device.DeviceObject.activeCovSubscriptions": "c37dd182670e8382",
    "bacpypes3.local.device.DeviceObject.deviceAddressBinding": "eaa0e088b172857a",
    "bacpypes3.local.device.DeviceObject.localDate": "b947c29d74cc61af",
    "bacpypes3.local.device.DeviceObject.localTime": "11cb0c9ae5e4ddc0",
    "bacpypes3.local.device.DeviceObject.objectList": "50717327a3aa20a9",
    "bacpypes3.local.device.DeviceObject.protocolObjectTypesSupported": "2b95b58068f83407",
    "bacpypes3.local.device.DeviceObject.protocolServicesSupported": "640d2d8027356fd0",
    "bacpypes3.local.device.DeviceObject.systemStatus": "f01ee807b4bb2a02",
    "bacpypes3.local.event.EventAlgorithm.__init__": "019831b5fa4115e8",
    "bacpypes3.local.event.EventAlgorithm._execute": "7e9b7d3059a06964",
    "bacpypes3.local.event.EventAlgorithm.event_notification_parameters": "a2b4627f4cd14e9f",
    "bacpypes3.local.event.EventAlgorithm.event_type": "f83f57a2841f292c",
    "bacpypes3.local.event.EventAlgorithm.execute": "afceaf3c64d075c9",
    "bacpypes3.local.event.EventAlgorithm.send_notifications": "78f72c03f8f6d451",
    "bacpypes3.local.event.EventAlgorithm.state_transition": "17f947ba29ddcfba",
    "bacpypes3.local.event.EventAlgorithm.state_transition_cancel": "a42870131481448d",
    "bacpypes3.local.multistate.MultiStateInputObject._cov_criteria": "f92b7ce2901696e4",
    "bacpypes3.local.multistate.MultiStateInputObject._required": "e33feb24046d82cc",
    "bacpypes3.local.multistate.MultiStateOutputObject._cov_criteria": "f92b7ce2901696e4",
    "bacpypes3.local.multistate.MultiStateValueObject._cov_criteria": "f92b7ce2901696e4",
    "bacpypes3.local.multistate.MultiStateValueObject._required": "65ad5f915773dd58",
    "bacpypes3.local.object.Algorithm.__init__": "2aec4bf705d0bddb",
    "bacpypes3.local.object.Object.__init__": "482dc11950aea694",
    "bacpypes3.local.object.Object.__getattribute__": "109f82753d02cd66",
    "bacpypes3.local.object.Object.__setattr__": "ed7fc2ac8ba739de",
    "bacpypes3.local.object.Object._post_init": "ab34e3314e200435",
    "bacpypes3.local.object.Object.propertyList": "c8bba3575a241291",
    "bacpypes3.local.object.Parameter.property_change": "5801982ad41b68e4",
    "bacpypes3.local.cov.DetectionMonitor.property_change": "9d5470491cc040ba",
    "bacpypes3.local.oos.OutOfService.write_property": "7a559fdb1063033e",
    "bacpypes3.local.schedule.ScheduleObject.__init__": "dd74e5c857fce0f7",
    "bacpypes3.local.schedule.ScheduleObject.check_reliability": "43cd099f8da84f8c",
    "bacpypes3.local.schedule.ScheduleObject.eval": "f6a92cca3131691a",
    "bacpypes3.local.schedule.ScheduleObject.interpret_schedule": "c657fbde97a216ab",
    "bacpypes3.local.schedule.ScheduleObject.present_value_changed": "3d2174c6cc7e7550",
    "bacpypes3.local.schedule.date_in_calendar_entry": "d11413212437eeb4",
    "bacpypes3.local.schedule.match_date_range": "071e16342e504baa",
    "bacpypes3.object.Object.get_property_type": "f38b9826c5868e44",
    "bacpypes3.object.Object.write_property": "23987163a529ca32",
    "bacpypes3.primitivedata.Date.now": "6afdd516b5786bdc",
    "bacpypes3.primitivedata.ElementInterface._optional": "a531266fc3fb8122",
    "bacpypes3.primitivedata.Time.now": "f2ba4cfe9f96ef2f",
    "bacpypes3.service.cov.ChangeOfValueServices.__init__": "911e9c38130759cf",
    "bacpypes3.service.cov.ChangeOfValueServices.add_subscription": "3105bde56f8e9650",
    "bacpypes3.service.cov.ChangeOfValueServices.cov_notification": "413003edb5eaaf42",
    "bacpypes3.service.cov.ChangeOfValueServices.do_SubscribeCOVRequest": "8a451bee353f56c4",
    "bacpypes3.service.cov.Subscription.__init__": "dc4b63b9ad476462",
    "bacpypes3.service.cov.Subscription.renew_subscription": "9b8dc97ed2cbff02",
    "bacpypes3.app.DeviceInfoCache.set_device_info": "d550cc447b903f21",
    "bacpypes3.service.device.WhoIsFuture.match": "2750cee65d2b6789",
    "bacpypes3.service.device.WhoIsIAmServices.do_IAmRequest": "0cfa66b94532c36a",
    "bacpypes3.service.device.WhoIsIAmServices.do_WhoIsRequest": "f6295b34b2b539f3",
    "bacpypes3.service.device.WhoIsIAmServices.i_am": "3ca58012afa5a53e",
    "bacpypes3.service.object.ReadRangeServices.do_ReadRangeRequest": "438b8122f2e3be04",
    "bacpypes3.service.object.ReadWritePropertyMultipleServices.do_ReadPropertyMultipleRequest": "7151211653c66355",
    "bacpypes3.service.object.ReadWritePropertyMultipleServices.do_WritePropertyMultipleRequest": "9480dde9748d04d9",
    "bacpypes3.service.object.ReadWritePropertyServices.do_ReadPropertyRequest": "f0151cf38eeaafa2",
    "bacpypes3.service.object.ReadWritePropertyServices.do_WritePropertyRequest": "65c82bda9870f9a3",
    "bacpypes3.service.object.read_property_to_result_element": "29de7b3d80cca010",
}


def test_every_inventoried_name_has_a_fingerprint_and_back():
    inventoried = set(bacpypes3_compat.OVERRIDES) | set(bacpypes3_compat.DEPENDS_ON)
    assert inventoried == set(FINGERPRINTS), (
        f"in an inventory without a fingerprint: {sorted(inventoried - set(FINGERPRINTS))}; "
        f"fingerprinted but in no inventory: {sorted(set(FINGERPRINTS) - inventoried)}"
    )


def test_upstream_source_is_unchanged():
    changed = []
    for name, recorded in sorted(FINGERPRINTS.items()):
        current = fingerprint(name)
        if current != recorded:
            reasons = []
            if name in bacpypes3_compat.OVERRIDES:
                where, why = bacpypes3_compat.OVERRIDES[name]
                reasons.append(f"overridden by {where}: {why}")
            if name in bacpypes3_compat.DEPENDS_ON:
                reasons.append(f"relied on: {bacpypes3_compat.DEPENDS_ON[name]}")
            changed.append(f"  {name}\n    {'; '.join(reasons)}\n    new hash: {current}")
    assert not changed, "bacpypes3 changed code BACpack depends on. Re-review, then update FINGERPRINTS:\n" + "\n".join(changed)


# Built by bacpypes3's metaclasses for every Object/Sequence subclass, not
# written by BACpack.
_GENERATED = {
    "_object_identifier_class",
    "_property_identifier_class",
    "_debug_contents",
    "_elements",
    "_inits",
    "_order",
    "_vendor_id",
}
_NOT_DEFINITIONS = {
    "__module__", "__doc__", "__qualname__", "__dict__", "__weakref__", "__annotations__",
    "__firstlineno__", "__static_attributes__", "__orig_bases__", "__parameters__", "__slots__",
    # Python 3.14's lazy annotations put these in every annotated class's
    # dict (3.13 doesn't have them).
    "__annotate__", "__annotate_func__", "__annotations_cache__",
}


def _bacpack_classes_with_bacpypes3_bases():
    classes = set()
    for module_info in pkgutil.walk_packages(bacpack.__path__, "bacpack."):
        if module_info.name.startswith("bacpack.web"):
            continue
        module = importlib.import_module(module_info.name)
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and value.__module__.startswith("bacpack")
                and any(base.__module__.startswith("bacpypes3") for base in value.__mro__)
            ):
                classes.add(value)
    return classes


def discovered_overrides() -> dict:
    """Upstream qualified name -> the BACpack classes (mixins included)
    that replace it somewhere in a class BACpack builds."""
    found: dict = {}
    for cls in _bacpack_classes_with_bacpypes3_bases():
        upstream = [base for base in cls.__mro__ if base.__module__.startswith("bacpypes3")]
        for own in cls.__mro__:
            if not own.__module__.startswith("bacpack"):
                continue
            for attr in vars(own):
                if attr in _GENERATED or attr in _NOT_DEFINITIONS:
                    continue
                for base in upstream:
                    if attr in vars(base):
                        found.setdefault(f"{base.__module__}.{base.__qualname__}.{attr}", set()).add(
                            f"{own.__module__}.{own.__qualname__}"
                        )
                        break
    return found


def test_every_override_is_inventoried():
    discovered = discovered_overrides()
    missing = sorted(set(discovered) - set(bacpypes3_compat.OVERRIDES))
    stale = sorted(set(bacpypes3_compat.OVERRIDES) - set(discovered))
    assert not missing, "BACpack overrides these without an OVERRIDES entry (add one, with a fingerprint):\n" + "\n".join(
        f"  {name} <- {', '.join(sorted(discovered[name]))}" for name in missing
    )
    assert not stale, f"OVERRIDES lists names nothing overrides any more: {stale}"


def test_every_overriding_class_is_named_in_its_where_text():
    discovered = discovered_overrides()
    unnamed = sorted(
        f"  {name} <- {qualified}"
        for name, classes in discovered.items()
        if name in bacpypes3_compat.OVERRIDES
        for qualified in classes
        if qualified.rsplit(".", 1)[1] not in bacpypes3_compat.OVERRIDES[name][0]
    )
    assert not unnamed, "OVERRIDES 'where' text doesn't name these overriding classes:\n" + "\n".join(unnamed)


import subprocess
import sys

import bacpypes3.local.event as upstream_event
import bacpypes3.local.object as upstream_object
import bacpypes3.local.schedule as upstream_schedule
from bacpypes3.basetypes import DateRange, TimeStamp
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import Date, Null, Real


def _loaded_names(qualified_name: str) -> set:
    return {
        node.id
        for node in ast.walk(ast.parse(upstream_source(qualified_name)))
        if isinstance(node, ast.Name)
    }


def test_swapped_names_are_still_module_globals_upstream():
    # The swaps work only because upstream looks these up at call time.
    assert {"match_date_range", "Null"} <= _loaded_names("bacpypes3.local.schedule.ScheduleObject.eval")
    assert "match_date_range" in _loaded_names("bacpypes3.local.schedule.date_in_calendar_entry")
    assert "TimeStamp" in _loaded_names("bacpypes3.local.event.EventAlgorithm.state_transition")
    for name in ("__getattribute__", "__setattr__", "propertyList"):
        assert "inspect" in _loaded_names(f"bacpypes3.local.object.Object.{name}"), name


def test_import_time_swaps_are_installed():
    assert upstream_schedule.match_date_range is bacpypes3_compat._match_date_range_with_wildcards
    assert upstream_schedule.Null is bacpypes3_compat._ScheduleNull
    assert upstream_event.TimeStamp is bacpypes3_compat._DateTimeStamp
    assert upstream_object.Object.propertyList.fget is bacpypes3_compat._property_list_without_required
    assert upstream_object.inspect.getattr_static is bacpypes3_compat.getattr_static
    assert upstream_object.inspect.iscoroutinefunction is inspect.iscoroutinefunction


def test_swaps_are_in_place_in_a_fresh_process_that_imports_object_builder():
    # A device child is spawned: it re-imports, and object_builder is what
    # it imports before building anything.
    code = (
        "import bacpack.object_builder\n"
        "import bacpypes3.local.schedule as s, bacpypes3.local.event as e\n"
        "assert s.match_date_range.__module__ == 'bacpack.bacpypes3_compat', s.match_date_range\n"
        "assert s.Null.__module__ == 'bacpack.bacpypes3_compat', s.Null\n"
        "assert e.TimeStamp.__module__ == 'bacpack.bacpypes3_compat', e.TimeStamp\n"
        "assert e.EventAlgorithm.send_notifications.__module__ == 'bacpack.bacpypes3_compat', e.EventAlgorithm.send_notifications\n"
        "import bacpypes3.local.object as o\n"
        "assert o.Object.propertyList.fget.__module__ == 'bacpack.bacpypes3_compat', o.Object.propertyList\n"
        "assert o.inspect.getattr_static.__module__ == 'bacpack.bacpypes3_compat', o.inspect\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 0, result.stderr


def test_wildcarded_effective_period_matches_any_date():
    always = DateRange(startDate=Date((255, 255, 255, 255)), endDate=Date((255, 255, 255, 255)))
    assert bacpypes3_compat._match_date_range_with_wildcards(Date((126, 8, 30, 0)), always) is True


def test_concrete_range_still_uses_upstream_comparison():
    bounded = DateRange(startDate=Date((126, 1, 1, 0)), endDate=Date((126, 6, 30, 0)))
    assert bacpypes3_compat._match_date_range_with_wildcards(Date((126, 3, 15, 0)), bounded) is True
    assert bacpypes3_compat._match_date_range_with_wildcards(Date((126, 8, 30, 0)), bounded) is False


def test_schedule_null_recognises_wrapped_and_bare_null_only():
    assert isinstance(AnyAtomic(Null(())), bacpypes3_compat._ScheduleNull)
    assert isinstance(Null(()), bacpypes3_compat._ScheduleNull)
    assert not isinstance(AnyAtomic(Real(1.0)), bacpypes3_compat._ScheduleNull)


def test_alarm_time_stamp_carries_a_date():
    stamp = bacpypes3_compat._DateTimeStamp.as_time()
    assert stamp.dateTime is not None and stamp.time is None
    assert issubclass(bacpypes3_compat._DateTimeStamp, TimeStamp)


import time as real_time

import bacpypes3.basetypes as upstream_basetypes
import bacpypes3.primitivedata as upstream_primitivedata
from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import Time

from bacpack import device_clock


class _FixedTime:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def time(self) -> float:
        return self.seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


def test_now_follows_a_swapped_clock_and_restores():
    fixed = 1_900_000_000.0  # 2030
    bacpypes3_compat.swap_clock(_FixedTime(fixed))
    try:
        assert bacpypes3_compat.clock_swapped()
        assert tuple(Date.now()) == tuple(Date.now(fixed))
        assert tuple(Time.now()) == tuple(Time.now(fixed))
        assert tuple(DateTime.now().date) == tuple(DateTime.now(fixed).date)
    finally:
        bacpypes3_compat.restore_clock()
    assert not bacpypes3_compat.clock_swapped()
    assert upstream_primitivedata.time is real_time
    assert upstream_basetypes.time is real_time


def test_restore_without_swap_is_harmless():
    bacpypes3_compat.restore_clock()
    assert upstream_primitivedata.time is real_time


def test_second_swap_keeps_the_real_originals():
    bacpypes3_compat.swap_clock(_FixedTime(1.0))
    bacpypes3_compat.swap_clock(_FixedTime(2.0))
    bacpypes3_compat.restore_clock()
    assert upstream_primitivedata.time is real_time
    assert upstream_basetypes.time is real_time


def test_device_clock_install_goes_through_the_boundary():
    device_clock.install()
    device_clock.install()
    try:
        assert device_clock.is_installed() and bacpypes3_compat.clock_swapped()
    finally:
        device_clock.uninstall()
    assert not device_clock.is_installed()
    assert upstream_primitivedata.time is real_time


from bacpypes3.local.analog import AnalogValueObject


def _analog_value():
    return AnalogValueObject(objectIdentifier=("analog-value", 1), objectName="AV", presentValue=0.0)


async def test_property_monitors_fire_on_set():
    obj = _analog_value()
    seen = []
    bacpypes3_compat.property_monitors(obj, "presentValue").append(lambda old, new: seen.append((old, new)))
    obj.presentValue = Real(2.0)
    assert seen == [(0.0, 2.0)]


async def test_remove_property_monitor_twice_is_harmless():
    obj = _analog_value()

    def callback(old, new):
        pass

    bacpypes3_compat.property_monitors(obj, "presentValue").append(callback)
    bacpypes3_compat.remove_property_monitor(obj, "presentValue", callback)
    bacpypes3_compat.remove_property_monitor(obj, "presentValue", callback)
    assert callback not in bacpypes3_compat.property_monitors(obj, "presentValue")


async def test_element_class_known_and_unknown():
    obj = _analog_value()
    assert bacpypes3_compat.element_class(obj, "presentValue") is Real
    assert bacpypes3_compat.element_class(AnalogValueObject, "presentValue") is Real
    assert bacpypes3_compat.element_class(obj, "noSuchProperty") is None


async def test_required_element_class_raises_key_error_for_an_unknown_property():
    obj = _analog_value()
    assert bacpypes3_compat.required_element_class(obj, "presentValue") is Real
    with pytest.raises(KeyError, match="noSuchProperty"):
        bacpypes3_compat.required_element_class(obj, "noSuchProperty")


async def test_property_name_from_identifier():
    obj = _analog_value()
    assert bacpypes3_compat.property_name(obj, 85) == "presentValue"


import asyncio

from bacpypes3.basetypes import EventState
from bacpypes3.local.analog import AnalogValueObjectIR


def _alarming_analog_value():
    return AnalogValueObjectIR(
        objectIdentifier=("analog-value", 3), objectName="AVIR", presentValue=0.0,
        highLimit=100.0, lowLimit=0.0, deadband=1.0, limitEnable=[1, 1], eventEnable=[1, 1, 1],
        timeDelay=0, notifyType="alarm", eventState=EventState.normal, statusFlags=[0, 0, 0, 0],
    )


async def test_event_algorithm_present_and_absent():
    assert bacpypes3_compat.event_algorithm(_alarming_analog_value()) is not None
    assert bacpypes3_compat.event_algorithm(_analog_value()) is None


async def test_attach_event_algorithm():
    obj = _analog_value()
    marker = object()
    bacpypes3_compat.attach_event_algorithm(obj, marker)
    assert bacpypes3_compat.event_algorithm(obj) is marker


async def test_schedule_algorithm_run_runs_once_on_the_next_tick():
    algorithm = bacpypes3_compat.event_algorithm(_alarming_analog_value())
    bacpypes3_compat.schedule_algorithm_run(algorithm)
    first = algorithm._execute_handle
    bacpypes3_compat.schedule_algorithm_run(algorithm)  # already pending: not scheduled again
    assert first is not None and algorithm._execute_handle is first
    await asyncio.sleep(0)
    assert algorithm._execute_handle is None  # upstream _execute ran and cleared it


async def test_cancel_algorithm_run_cancels_a_queued_run_and_tolerates_none():
    algorithm = bacpypes3_compat.event_algorithm(_alarming_analog_value())
    bacpypes3_compat.cancel_algorithm_run(algorithm)  # nothing queued: no error
    bacpypes3_compat.schedule_algorithm_run(algorithm)
    handle = algorithm._execute_handle
    bacpypes3_compat.cancel_algorithm_run(algorithm)
    assert handle.cancelled() and algorithm._execute_handle is None


async def test_has_pending_transition_follows_the_timer():
    algorithm = bacpypes3_compat.event_algorithm(_alarming_analog_value())
    assert not bacpypes3_compat.has_pending_transition(algorithm)
    algorithm._transition_timeout_handle = asyncio.get_running_loop().call_later(100, lambda: None)
    assert bacpypes3_compat.has_pending_transition(algorithm)
    algorithm.state_transition_cancel()
    assert not bacpypes3_compat.has_pending_transition(algorithm)


async def test_notification_class_link():
    obj = _alarming_analog_value()
    assert bacpypes3_compat.notification_class_object(obj) is None
    marker = object()
    bacpypes3_compat.link_notification_class(obj, marker)
    assert bacpypes3_compat.notification_class_object(obj) is marker


async def test_object_app_is_none_until_added():
    # local Object.__init__ leaves _app None; Application.add_object sets it
    # (that half is pinned by add_object's fingerprint, and exercised by
    # every test that builds an application).
    obj = _analog_value()
    assert bacpypes3_compat.object_app(obj) is None
    sentinel = object()
    obj._app = sentinel
    assert bacpypes3_compat.object_app(obj) is sentinel


async def test_cancel_pending_interpretation_cancels_and_tolerates_none():
    class _Schedule:
        _interpret_schedule_handle = None

    schedule = _Schedule()
    bacpypes3_compat.cancel_pending_interpretation(schedule)  # nothing pending: no error
    handle = asyncio.get_running_loop().call_later(100, lambda: None)
    schedule._interpret_schedule_handle = handle
    bacpypes3_compat.cancel_pending_interpretation(schedule)
    assert handle.cancelled()


from bacpypes3.apdu import WritePropertyRequest
from bacpypes3.basetypes import OptionalUnsigned, PriorityValue
from bacpypes3.constructeddata import Any, ArrayOf
from bacpypes3.local.analog import AnalogValueObjectCmd
from bacpypes3.primitivedata import ObjectType


def test_enum_name_maps():
    every_spelling, camel, kebab = bacpypes3_compat.enum_name_maps(ObjectType)
    assert every_spelling["analog-value"] == 2 and every_spelling["analogValue"] == 2
    assert camel[2] == "analogValue"
    assert kebab[2] == "analog-value"


def test_sequence_fields_in_order():
    assert bacpypes3_compat.sequence_fields(DateTime(date=Date((126, 1, 1, 4)), time=Time((0, 0, 0, 0)))) == ["date", "time"]


def test_chosen_field():
    assert bacpypes3_compat.chosen_field(PriorityValue(real=Real(1.0))) == "real"
    assert bacpypes3_compat.chosen_field(PriorityValue(null=())) == "null"
    assert bacpypes3_compat.chosen_field(Real(1.0)) is None  # not a Choice at all


def test_array_element_type():
    assert bacpypes3_compat.array_element_type(ArrayOf(Real)) is Real


def test_is_context_encoded():
    request = WritePropertyRequest(
        objectIdentifier=("analog-value", 1), propertyIdentifier="presentValue", propertyValue=Any(Null(()))
    )
    assert bacpypes3_compat.is_context_encoded(request.propertyValue)
    assert not bacpypes3_compat.is_context_encoded(Any(Null(())))


async def test_store_priority_array_raw_sets_exactly_what_it_is_given():
    obj = AnalogValueObjectCmd(objectIdentifier=("analog-value", 2), objectName="AVC", presentValue=0.0, relinquishDefault=0.0)
    seen = []
    bacpypes3_compat.property_monitors(obj, "presentValue").append(lambda old, new: seen.append(new))
    slots = [PriorityValue(null=()) for _ in range(16)]
    slots[7] = PriorityValue(real=Real(5.0))
    bacpypes3_compat.store_priority_array_raw(obj, slots, OptionalUnsigned(unsigned=8), Real(5.0))
    assert obj.presentValue == 5.0
    assert obj.currentCommandPriority.unsigned == 8
    assert obj.priorityArray[7].real == 5.0
    assert seen == [5.0]  # Present_Value monitors fired exactly once


import re

import bacpypes3

_SRC = pathlib.Path(bacpack.__file__).parent
_BOUNDARY = _SRC / "bacpypes3_compat.py"

# BACpack's own attributes whose names also happen to appear somewhere in
# bacpypes3's source. Each is on a BACpack class with no such upstream
# attribute, so it isn't a reach-in. Add to this only after checking that.
BACPACK_OWN_NAMES = {
    "_dcc_enable_handle",  # LoggingApplication's DCC timer; Application doesn't include upstream's DCC service
    "_key",  # bacnet_client's binding key helper
    "_log",  # bacnet_client's traffic logger helper
    "_requests",  # client_requests' in-flight task set
    "_run",  # client_requests' task body
}


def _bacpypes3_private_names() -> set:
    names = set()
    for path in pathlib.Path(bacpypes3.__file__).parent.rglob("*.py"):
        names |= set(re.findall(r"\b(_[A-Za-z][A-Za-z0-9_]*)\b", path.read_text(encoding="utf-8")))
    return {name for name in names if not name.startswith("__")}


def reach_ins(source: str, private: set) -> list:
    """(line, what) for each bacpypes3 reach-in in one module's source.
    Not caught: vars(obj)[...] (can't be told from ordinary code without
    type information; an accepted gap)."""
    tree = ast.parse(source)
    bacpypes3_modules = set()
    bacpypes3_classes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bacpypes3"):
                    bacpypes3_modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("bacpypes3"):
            for alias in node.names:
                bound = alias.asname or alias.name
                (bacpypes3_classes if alias.name[:1].isupper() else bacpypes3_modules).add(bound)
    found = []

    def root_name(expr):
        while isinstance(expr, ast.Attribute):
            expr = expr.value
        return expr.id if isinstance(expr, ast.Name) else None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in private and node.attr not in BACPACK_OWN_NAMES:
                found.append((node.lineno, f"attribute {node.attr}"))
            if isinstance(node.ctx, ast.Store) and root_name(node.value) in bacpypes3_modules:
                found.append((node.lineno, f"assignment to {ast.unparse(node)}"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if (
                node.func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in private
                and node.args[1].value not in BACPACK_OWN_NAMES
            ):
                found.append((node.lineno, f"{node.func.id} {node.args[1].value}"))
            if node.func.id == "setattr" and node.args and root_name(node.args[0]) in bacpypes3_modules:
                found.append((node.lineno, f"setattr on {ast.unparse(node.args[0])}"))
            if (
                node.func.id == "super"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in bacpypes3_classes
            ):
                found.append((node.lineno, f"super({node.args[0].id}, ...)"))
    return found


def test_scan_catches_each_kind_of_reach_in():
    source = (
        "import bacpypes3.local.schedule as sched\n"  # 1
        "from bacpypes3.local.cmd import Commandable\n"  # 2
        "x = obj._property_monitors\n"  # 3
        "y = getattr(obj, '_app', None)\n"  # 4
        "sched.match_date_range = f\n"  # 5
        "super(Commandable, obj).__setattr__('a', 1)\n"  # 6
        "z = self._dcc_enable_handle\n"  # 7: BACpack's own name, allowed
        "import bacpypes3.local.event\n"  # 8
        "bacpypes3.local.event.TimeStamp = f\n"  # 9: store through a dotted module path
        "setattr(sched, 'Public', f)\n"  # 10: setattr on a bacpypes3 module
        "from bacpypes3 import primitivedata\n"  # 11
        "setattr(primitivedata, 'Date', f)\n"  # 12
        "other.thing.attr = 1\n"  # 13: not bacpypes3, allowed
        "setattr(obj, 'public', 1)\n"  # 14: not a module, allowed
    )
    found = reach_ins(source, {"_property_monitors", "_app", "_dcc_enable_handle"})
    assert sorted(line for line, _ in found) == [3, 4, 5, 6, 9, 10, 12]


def test_no_bacpypes3_reach_ins_outside_the_boundary():
    private = _bacpypes3_private_names()
    problems = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _BOUNDARY:
            continue
        for line, what in reach_ins(path.read_text(encoding="utf-8"), private):
            problems.append(f"  {path.relative_to(_SRC).as_posix()}:{line}: {what}")
    problems.sort()
    assert not problems, (
        "bacpypes3 internals reached outside bacpypes3_compat.py -- add a helper there "
        "(or, for BACpack's own attribute that shares a name, add it to BACPACK_OWN_NAMES):\n" + "\n".join(problems)
    )


# --- End-to-end swap contracts ---------------------------------------------
# The swaps above, driven through the upstream code that looks them up: an
# upgrade that stops using the swapped name fails here, beside the
# fingerprints, not only in the feature tests.

from bacpypes3.primitivedata import Date as _Date, Time as _Time

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application


def _contract_device():
    return Device(id=599001, name="Test Device", vendor_identifier=999)


async def test_contract_a_null_exception_entry_falls_through_to_the_weekly_schedule():
    # The exception path is the one upstream gets wrong unswapped (its value
    # is AnyAtomic-wrapped, so isinstance(..., Null) never matches); a null
    # weekly entry happens to come out right either way.
    schedule = BacnetObject(
        object_type="schedule",
        instance_number=1,
        name="Sched-1",
        schedule_value_type="analog",
        schedule_default="10.0",
        weekly_schedule=[[{"time": "08:00", "value": "50.0"}, {"time": "17:00", "value": None}] for _ in range(7)],
        exception_schedule=[
            {
                "period": {"kind": "date", "year": "*", "month": "*", "day": "*"},
                "priority": 1,
                "time_values": [{"time": "09:00", "value": "99.0"}, {"time": "10:00", "value": None}],
            }
        ],
        schedule_priority=8,
    )
    app = build_application(_contract_device(), [schedule])
    current, _next = app.get_object_name("Sched-1").eval(_Date.now(), _Time((11, 0, 0, 0)))
    assert current.get_value() == 50.0


async def test_contract_an_alarm_transition_stamps_a_date():
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
    app = build_application(_contract_device(), [nc, ai])
    live = app.get_object_name("AI-1")
    live.presentValue = 150.0  # above highLimit
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    stamp = live.eventTimeStamps[0]  # to-offnormal
    assert stamp.dateTime is not None and stamp.time is None
    assert tuple(stamp.dateTime.date)[0] != 255


async def _subscribed_app(lifetime):
    app = build_application(
        Device(id=599001, name="D", vendor_identifier=999),
        [BacnetObject(object_type="analog-value", instance_number=1, name="AV-1")],
    )
    sent = []
    app.cov_notification = lambda cov, apdu: sent.append(apdu)
    request = SubscribeCOVRequest(
        subscriberProcessIdentifier=7,
        monitoredObjectIdentifier=("analog-value", 1),
        issueConfirmedNotifications=False,
        lifetime=lifetime,
    )
    request.pduSource = Address("192.168.1.99")
    app.response = lambda apdu: asyncio.sleep(0)
    await app.do_SubscribeCOVRequest(request)
    await asyncio.sleep(0)
    sent.clear()
    return app, app.get_object_name("AV-1"), sent


async def test_take_cov_subscriptions_detaches_and_reports_the_remaining_lifetime():
    app, av, _ = await _subscribed_app(60)
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.proc_id == 7 and state.confirmed is False
    assert 59 <= state.lifetime <= 60
    assert str(state.client_addr) == "192.168.1.99"
    assert bacpypes3_compat.take_cov_subscriptions(app, av) == []  # nothing left


async def test_a_permanent_subscription_stays_permanent():
    app, av, _ = await _subscribed_app(0)
    [state] = bacpypes3_compat.take_cov_subscriptions(app, av)
    assert state.lifetime == 0


async def test_restore_cov_subscriptions_resubscribes_and_notifies_once():
    app, av, sent = await _subscribed_app(60)
    taken = bacpypes3_compat.take_cov_subscriptions(app, av)
    bacpypes3_compat.restore_cov_subscriptions(app, av, taken)
    await asyncio.sleep(0)
    assert len(sent) == 1
    await av.write_property("presentValue", Real(50.0), priority=8)
    await asyncio.sleep(0)
    assert len(sent) == 2  # the restored subscription still notifies on change


# --- Cached getattr_static ---------------------------------------------------
# bacpypes3_compat.getattr_static must return exactly what inspect's does.

_NO_DEFAULT = object()


def _outcome(function, obj, attr, default=_NO_DEFAULT):
    try:
        if default is _NO_DEFAULT:
            return "found", function(obj, attr)
        return "found", function(obj, attr, default)
    except AttributeError as exc:
        return "missing", str(exc)


def _same(obj, attr):
    """Twice, so the second (cached) answer is checked as well as the first."""
    marker = object()
    for _ in range(2):
        for default in (_NO_DEFAULT, None, marker):
            want = _outcome(inspect.getattr_static, obj, attr, default)
            got = _outcome(bacpypes3_compat.getattr_static, obj, attr, default)
            assert got[0] == want[0], (attr, default, got, want)
            if want[0] == "found":
                assert got[1] is want[1], (attr, default, got, want)
            else:
                assert got[1] == want[1]


def _one_of_each_object_class():
    kinds = [
        "analog-input", "analog-output", "analog-value", "binary-input", "binary-output", "binary-value",
        "multi-state-input", "multi-state-output", "multi-state-value", "schedule", "calendar", "notification-class",
    ]
    alarmable = ["analog-input", "analog-output", "analog-value", "binary-input", "binary-value", "multi-state-input", "multi-state-value"]
    rows = []
    for number, kind in enumerate(kinds, 1):
        fields = {}
        if kind.startswith("multi-state"):
            fields["state_text"] = ["a", "b", "c"]
        if kind == "calendar":
            fields["date_list"] = [{"kind": "date", "month": 12, "day": 25}]
        if kind == "schedule":
            fields.update(
                schedule_value_type="analog",
                schedule_default="10.0",
                weekly_schedule=[[{"time": "08:00", "value": "50.0"}] for _ in range(7)],
                schedule_priority=8,
            )
        rows.append(BacnetObject(object_type=kind, instance_number=number, name=f"O-{kind}", **fields))
    for number, kind in enumerate(alarmable, 50):
        fields = {"alarming_enabled": True, "notification_class_instance": 12}
        if kind.startswith("analog"):
            fields.update(alarm_high_limit=100.0, alarm_low_limit=0.0)
        if kind.startswith("binary"):
            fields["alarm_value"] = True
        if kind.startswith("multi-state"):
            fields.update(state_text=["a", "b", "c"], alarm_values=[2])
        rows.append(BacnetObject(object_type=kind, instance_number=number, name=f"IR-{kind}", **fields))
    app = build_application(Device(id=599001, name="D", vendor_identifier=999), rows)
    return list(app.objectIdentifier.values())


async def test_cached_lookup_matches_the_stdlib_on_every_object_class():
    objects = _one_of_each_object_class()
    assert len({type(obj) for obj in objects}) >= 20  # every class, the device and the IR variants
    for obj in objects:
        names = [*obj._elements, "_app", "no_such_name", "write_property"]
        for name in names:
            _same(obj, name)
        if "description" in obj._elements:
            obj.description = "now set"
            for name in names:
                _same(obj, name)
        # A class property also held in the instance dict: the property wins.
        obj.__dict__["propertyList"] = "shadowed"
        try:
            _same(obj, "propertyList")
        finally:
            del obj.__dict__["propertyList"]


def test_data_descriptor_beats_the_instance_dict():
    class Holder:
        @property
        def x(self):
            return 1

    holder = Holder()
    holder.__dict__["x"] = 5
    assert bacpypes3_compat.getattr_static(holder, "x") is Holder.__dict__["x"]
    _same(holder, "x")


def test_instance_dict_beats_a_non_data_descriptor():
    class Holder:
        def x(self):
            return 1

        y = 7

    holder = Holder()
    holder.__dict__["x"] = 5
    holder.__dict__["y"] = 8
    assert bacpypes3_compat.getattr_static(holder, "x") == 5
    assert bacpypes3_compat.getattr_static(holder, "y") == 8
    _same(holder, "x")
    _same(holder, "y")
    del holder.__dict__["x"]  # the class entry again, from the same cached facts
    _same(holder, "x")


def test_a_class_that_shadows_dict_skips_the_instance():
    class Shadowing:
        __dict__ = {}  # pyright: ignore[reportAssignmentType]
        x = "class"

    shadowing = Shadowing()
    shadowing.x = "instance"  # lands in the real instance dict, which the class's __dict__ hides
    shadowing.z = "instance only"
    assert bacpypes3_compat.getattr_static(shadowing, "x") == "class"
    assert bacpypes3_compat.getattr_static(shadowing, "z", None) is None
    _same(shadowing, "x")
    _same(shadowing, "z")


def test_a_slots_class_without_dict():
    class Slotted:
        __slots__ = ("x",)

    slotted = Slotted()
    _same(slotted, "x")  # a slot that is not set: the member descriptor
    slotted.x = 3
    _same(slotted, "x")
    _same(slotted, "y")


def test_a_class_object_goes_to_the_stdlib():
    class Plain:
        x = 1

    _same(Plain, "x")
    _same(Plain, "mro")
    _same(Plain, "no_such_name")


def test_a_descriptor_with_only_get_and_delete_is_a_data_descriptor():
    class GetDelete:
        def __get__(self, obj, objtype=None):
            return 1

        def __delete__(self, obj):
            pass

    class Holder:
        x = GetDelete()

    holder = Holder()
    holder.__dict__["x"] = 5
    assert bacpypes3_compat.getattr_static(holder, "x") is Holder.__dict__["x"]
    _same(holder, "x")


def test_a_class_entry_whose_metaclass_shadows_dict_is_skipped():
    class ShadowingMeta(type):
        __dict__ = property(lambda cls: {})  # pyright: ignore[reportAssignmentType]

    class Plain:
        x = "plain"

    class Hidden(metaclass=ShadowingMeta):
        x = "hidden"

    class Child(Hidden, Plain):
        pass

    child = Child()
    assert bacpypes3_compat.getattr_static(child, "x") == "plain"
    _same(child, "x")


def test_a_member_descriptor_dict_shadow_still_consults_the_instance():
    class Slotted:
        __slots__ = ("a",)

    class Shadowed(Slotted):
        __dict__ = Slotted.__dict__["a"]  # pyright: ignore[reportAssignmentType]
        x = "class"

    shadowed = Shadowed()
    shadowed.a = {"x": "from the slot", "y": "slot only"}
    assert bacpypes3_compat.getattr_static(shadowed, "x") == "from the slot"
    _same(shadowed, "x")
    _same(shadowed, "y")


class _CoroutineDescription(AnalogValueObject):
    saved: list = []

    @property
    async def description(self) -> CharacterString:  # pyright: ignore[reportIncompatibleVariableOverride]
        return CharacterString("from coroutine")

    @description.setter
    async def description(self, value) -> None:
        type(self).saved.append(str(value))


async def test_a_coroutine_property_still_runs_in_its_thread():
    obj = _CoroutineDescription(objectIdentifier=("analog-value", 1), objectName="AV", presentValue=0.0)
    _CoroutineDescription.saved.clear()
    assert obj.description == "from coroutine"
    assert obj.description == "from coroutine"  # the second read uses the cached lookup
    obj.description = "x"
    obj.description = "y"
    assert _CoroutineDescription.saved == ["x", "y"]


async def test_property_list_matches_the_stdlib_lookup():
    skipped = {"objectIdentifier", "objectName", "objectType", "propertyList"}
    objects = [obj for obj in _one_of_each_object_class() if type(obj).__name__ in {
        "WriteLockedAnalogValueObjectCmd", "ValidatedBinaryValueObjectCmd", "ValidatedMultiStateValueObject", "GatedScheduleObject",
    }]
    assert len(objects) == 4
    for obj in objects:
        for set_description in (False, True):
            if set_description:
                obj.description = "now set"
            expected = [name for name in obj._elements if inspect.getattr_static(obj, name, None) is not None and name not in skipped]
            assert [p.attr for p in obj.propertyList] == expected, type(obj).__name__
            # An element the object holds nothing for stays out of the list.
            assert any(name not in expected and name not in skipped for name in obj._elements), type(obj).__name__


def test_send_notifications_replacement_is_installed():
    assert upstream_event.EventAlgorithm.send_notifications is bacpypes3_compat._send_notifications_by_weekday


async def test_an_unspecified_day_of_week_does_not_filter_recipients(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(bacpypes3_compat.Date, "now", classmethod(lambda cls: (2026, 11, 9, 255)))
    destination = SimpleNamespace(
        validDays=[False] * 7,
        fromTime=bacpypes3_compat.Time("00:00:00.00"),
        toTime=bacpypes3_compat.Time("23:59:59.99"),
        transitions=[True, True, True],
        recipient=SimpleNamespace(device=None, address=None),
    )
    monitored = SimpleNamespace(_notification_class_object=SimpleNamespace(recipientList=[destination]))
    algorithm = SimpleNamespace(monitoring_object=None, monitored_object=monitored)
    # Past the day filter, the unusable recipient is what raises.
    with pytest.raises(RuntimeError, match="invalid recipient"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, {"toState": "highLimit"})


# --- Device-instance recipients: Who-Is on a cache miss ----------------------

import asyncio
import logging
from types import SimpleNamespace

from bacpypes3.apdu import IAmRequest
from bacpypes3.app import DeviceInfoCache
from bacpypes3.basetypes import DeviceAddress, Recipient


def _i_am(instance=2, source="127.0.0.2"):
    i_am = IAmRequest(
        iAmDeviceIdentifier=("device", instance),
        maxAPDULengthAccepted=1024,
        segmentationSupported="noSegmentation",
        vendorID=999,
    )
    i_am.pduSource = Address(source)
    return i_am


def _destination(recipient, confirmed=False):
    return SimpleNamespace(
        validDays=[True] * 7,
        fromTime=bacpypes3_compat.Time("00:00:00.00"),
        toTime=bacpypes3_compat.Time("23:59:59.99"),
        transitions=[True, True, True],
        recipient=recipient,
        issueConfirmedNotifications=confirmed,
        processIdentifier=5,
    )


def _device_recipient(instance=2):
    return SimpleNamespace(device=("device", instance), address=None)


def _notifying(recipients, who_is_result, suppressed=False, delay=0.0, who_is_error=None, reply=None):
    """An algorithm whose monitored object's app is a stand-in: a real
    device-info cache, a who_is that records its calls (answering after
    `delay`, or raising `who_is_error`), a request that records what would
    be sent."""
    calls = []
    sent = []

    def who_is(low, high, timeout=None):
        calls.append((low, high, timeout))
        if who_is_error is not None:
            raise who_is_error
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if delay:
            loop.call_later(delay, future.set_result, list(who_is_result))
        else:
            future.set_result(list(who_is_result))
        return future

    def request(apdu):
        sent.append(apdu)
        future = asyncio.get_running_loop().create_future()
        if isinstance(reply, BaseException):
            future.set_exception(reply)
        else:
            future.set_result(None)
        return future

    app = SimpleNamespace(
        device_info_cache=DeviceInfoCache(), who_is=who_is, request=request, initiation_suppressed=suppressed
    )
    monitored = SimpleNamespace(
        _notification_class_object=SimpleNamespace(recipientList=recipients),
        _app=app,
        objectIdentifier=("analog-input", 1),
        objectName="AI-1",
    )
    return SimpleNamespace(monitoring_object=None, monitored_object=monitored), app, calls, sent


_PARAMETERS = {
    "toState": "highLimit",
    "initiatingDeviceIdentifier": ("device", 1),
    "eventObjectIdentifier": ("analog-input", 1),
    "timeStamp": TimeStamp(sequenceNumber=1),
    "notificationClass": 1,
    "priority": 100,
    "eventType": "outOfRange",
    "notifyType": "alarm",
    "ackRequired": False,
    "fromState": "normal",
}


@pytest.mark.asyncio
async def test_a_device_recipient_unknown_to_the_cache_gets_a_who_is_for_that_instance():
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [_i_am()])
    await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert calls == [(2, 2, bacpypes3_compat._WHO_IS_WAIT_SECONDS)]
    assert len(sent) == 1 and sent[0].pduDestination == Address("127.0.0.2")
    assert (await app.device_info_cache.get_device_info(2)).address == Address("127.0.0.2")


@pytest.mark.asyncio
async def test_a_device_the_cache_already_knows_is_not_asked():
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [])
    await app.device_info_cache.set_device_info(_i_am())
    await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert calls == [] and len(sent) == 1


@pytest.mark.asyncio
async def test_an_unanswered_who_is_skips_the_recipient_warns_and_is_not_repeated_within_the_window(
    monkeypatch, caplog
):
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [])
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert sent == [] and len(calls) == 1
    warnings = [r for r in caplog.records if r.name == "bacpack.bacpypes3_compat"]
    assert len(warnings) == 2 and all("device 2" in r.getMessage() for r in warnings)

    now = real_time.monotonic()
    monkeypatch.setattr(bacpypes3_compat.time, "monotonic", lambda: now + 61)
    await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_other_recipients_still_receive_when_a_device_recipient_never_answers():
    address = SimpleNamespace(device=None, address=DeviceAddress("192.168.1.77"))
    algorithm, app, calls, sent = _notifying(
        [_destination(_device_recipient()), _destination(address)], []
    )
    await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert len(sent) == 1
    assert bytes(sent[0].pduDestination.addrAddr) == bytes(DeviceAddress("192.168.1.77").macAddress)


@pytest.mark.asyncio
async def test_alarms_sent_together_share_one_who_is():
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [_i_am()], delay=0.05)
    await asyncio.gather(
        *(bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS)) for _ in range(5))
    )
    assert len(calls) == 1
    assert len(sent) == 5 and all(apdu.pduDestination == Address("127.0.0.2") for apdu in sent)


@pytest.mark.asyncio
async def test_alarms_sent_together_to_a_silent_device_share_one_who_is(caplog):
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [], delay=0.05)
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await asyncio.gather(
            *(bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS)) for _ in range(3))
        )
    assert len(calls) == 1 and sent == []
    warnings = [r for r in caplog.records if r.name == "bacpack.bacpypes3_compat"]
    assert len(warnings) == 3


@pytest.mark.asyncio
async def test_a_who_is_that_raises_skips_only_that_recipient(caplog):
    address = SimpleNamespace(device=None, address=DeviceAddress("192.168.1.77"))
    algorithm, app, calls, sent = _notifying(
        [_destination(_device_recipient()), _destination(address)], [], who_is_error=RuntimeError("closing")
    )
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert len(sent) == 1
    assert bytes(sent[0].pduDestination.addrAddr) == bytes(DeviceAddress("192.168.1.77").macAddress)
    assert any("device 2" in r.getMessage() for r in caplog.records if r.name == "bacpack.bacpypes3_compat")


@pytest.mark.asyncio
async def test_no_who_is_is_sent_while_dcc_suppresses_initiation(caplog):
    algorithm, app, calls, sent = _notifying([_destination(_device_recipient())], [_i_am()], suppressed=True)
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert calls == [] and sent == []
    assert not [r for r in caplog.records if r.name == "bacpack.bacpypes3_compat"]


# --- A refused or unanswered confirmed notification is logged ----------------

from bacpypes3.apdu import AbortPDU, Error, RejectPDU


def _compat_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "bacpack.bacpypes3_compat"]


def _address_recipient():
    return SimpleNamespace(device=None, address=DeviceAddress("192.168.1.77"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply, outcome",
    [
        (RejectPDU(reason="unrecognizedService"), "refused, reject: unrecognized-service"),
        (AbortPDU(reason="outOfResources"), "refused, abort: out-of-resources"),
        (
            Error(errorClass="services", errorCode="serviceRequestDenied", service_choice=2),
            "refused, error: services: service-request-denied",
        ),
        (AbortPDU(reason="noResponse"), "no reply"),
        (RuntimeError("socket closed"), "not sent: socket closed"),
    ],
)
async def test_a_refused_confirmed_notification_is_logged_with_the_reply(caplog, reply, outcome):
    algorithm, app, calls, sent = _notifying([_destination(_address_recipient(), confirmed=True)], [], reply=reply)
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert len(sent) == 1
    [line] = _compat_warnings(caplog)
    assert line.startswith('event notification from analog-input:1 "AI-1" to 192.168.1.77, ')
    assert "process ID 5" in line and "to-offnormal" in line
    assert line.endswith(outcome)


@pytest.mark.asyncio
async def test_a_delivered_notification_logs_nothing(caplog):
    algorithm, app, calls, sent = _notifying([_destination(_address_recipient(), confirmed=True)], [])
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    assert len(sent) == 1 and _compat_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_refused_notification_to_a_device_recipient_names_the_device_and_its_address(caplog):
    algorithm, app, calls, sent = _notifying(
        [_destination(_device_recipient(), confirmed=True)], [_i_am()], reply=RejectPDU(reason="unrecognizedService")
    )
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    [line] = _compat_warnings(caplog)
    assert " to device 2 at 127.0.0.2, " in line


@pytest.mark.asyncio
async def test_refusals_from_one_recipient_are_logged_once_a_minute_with_a_count(monkeypatch, caplog):
    now = real_time.monotonic()
    monkeypatch.setattr(bacpypes3_compat.time, "monotonic", lambda: now)
    algorithm, app, calls, sent = _notifying(
        [_destination(_address_recipient(), confirmed=True)], [], reply=RejectPDU(reason="unrecognizedService")
    )
    with caplog.at_level(logging.WARNING, logger="bacpack.bacpypes3_compat"):
        for _ in range(3):
            await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
        assert len(_compat_warnings(caplog)) == 1
        monkeypatch.setattr(bacpypes3_compat.time, "monotonic", lambda: now + 61)
        await bacpypes3_compat._send_notifications_by_weekday(algorithm, dict(_PARAMETERS))
    lines = _compat_warnings(caplog)
    assert len(lines) == 2 and lines[1].endswith("(2 more since the last)")
    assert len(sent) == 4
