"""The BACnet client role.

A thin layer over one device's own LoggingApplication, so every request
comes from that device's address and inherits its BBMD/foreign-device
settings. Runs inside the device's child process.

bacpypes3 raises error/reject/abort replies as ErrorRejectAbortNack, a
BaseException subclass -- `except Exception` does NOT catch them. Every call
into bacpypes3's client methods goes through BacnetClient._call, which turns
them (and timeouts) into ClientError.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from bacpypes3.apdu import AbortPDU, ErrorRejectAbortNack, RejectPDU, WritePropertyRequest
from bacpypes3.basetypes import BinaryPV, ErrorType, PropertyReference
from bacpypes3.constructeddata import AnyAtomic, Array, Choice
from bacpypes3.constructeddata import Sequence as BACnetSequence
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import (
    Atomic,
    BitString,
    Boolean,
    CharacterString,
    Date,
    Double,
    Enumerated,
    Null,
    ObjectIdentifier,
    ObjectType,
    PropertyIdentifier,
    Real,
    Time,
    Unsigned,
)
from bacpypes3.vendor import get_vendor_info

from . import bacpypes3_compat
from .choices import enum_name
from .models import WILDCARD_DEVICE_INSTANCE

logger = logging.getLogger(__name__)

# Properties per ReadPropertyMultiple request. Small enough for a
# non-segmenting 480-octet remote; halved per binding if a reply is still
# too big (see BacnetClient.read_many).
RPM_BATCH_SIZE = 20

_TOO_BIG = {"segmentation-not-supported", "apdu-too-long", "buffer-overflow"}


class ClientError(Exception):
    """A client request failed. `kind`:

    - "transport": nothing usable came back (no binding, timeout, abort).
    - "too-big": the reply didn't fit (segmentation not supported, APDU too long).
    - "unsupported": the remote rejected the service as unrecognized.
    - "error": the remote answered with an error, or a value couldn't be
      converted or decoded.
    - "dcc": this device is under DeviceCommunicationControl; nothing was sent.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        error_class: Optional[str] = None,
        error_code: Optional[str] = None,
        reply: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        # The remote's own Error, kebab-case ("property", "unknown-property"):
        # a proxy passes it back to the client that wrote.
        self.error_class = error_class
        self.error_code = error_code
        # "error" | "reject" | "abort" when the remote answered with one;
        # None otherwise (a timeout, a local failure).
        self.reply = reply

    @property
    def transport(self) -> bool:
        return self.kind == "transport"


def error_text(exc: BaseException) -> str:
    """An exception as one readable line. An error carrying
    errorClass/errorCode reads "property: value-out-of-range" instead of
    str()'s tuple; a str() that raises (a bare ErrorRejectAbortNack) or is
    empty falls back to the type name."""
    error_class = getattr(exc, "errorClass", None)
    error_code = getattr(exc, "errorCode", None)
    if error_class is not None and error_code is not None:
        return f"{error_class}: {error_code}"
    try:
        text = str(exc)
    except Exception:
        text = ""
    return text or type(exc).__name__


def error_from_reply(exc: ErrorRejectAbortNack) -> ClientError:
    """The ClientError for an error/reject/abort reply bacpypes3 raised.
    Enumerated reasons compare by str() (kebab-case)."""
    if isinstance(exc, AbortPDU):
        reason = str(exc.apduAbortRejectReason)
        # bacpypes3 reports a timeout as a no-response abort: not an answer.
        reply = None if reason == "no-response" else "abort"
        return ClientError(
            f"abort: {reason}", kind="too-big" if reason in _TOO_BIG else "transport", reply=reply
        )
    if isinstance(exc, RejectPDU):
        reason = str(exc.apduAbortRejectReason)
        kind = "unsupported" if reason == "unrecognized-service" else "error"
        return ClientError(f"reject: {reason}", kind=kind, reply="reject")
    error_class = getattr(exc, "errorClass", None)
    if error_class is not None:
        error_code = getattr(exc, "errorCode", None)
        return ClientError(
            f"{error_class}: {error_code}",
            error_class=str(error_class),
            error_code=None if error_code is None else str(error_code),
            reply="error",
        )
    return ClientError(error_text(exc))


def _enum_arg(text: str):
    """A name, or a number for a proprietary value (e.g. "200")."""
    return int(text) if text.isdigit() else text


def _normalize(enum_class, what: str, value: str) -> str:
    text = str(value).strip()
    if not text.isdigit():
        # Any case or separator variant of a standard name.
        name = enum_name(enum_class, text)
        if name is not None:
            return name
    try:
        return str(enum_class(_enum_arg(text)))
    except (ValueError, TypeError):
        raise ValueError(f"unknown {what} {value!r}") from None


def normalize_object_type(value: str) -> str:
    """Kebab-case object type ("analogInput" -> "analog-input"); a number
    names a proprietary type. ValueError for anything else."""
    return _normalize(ObjectType, "object type", value)


def normalize_property(value: str) -> str:
    """Kebab-case property ("presentValue" -> "present-value"); a number
    names a proprietary property. ValueError for anything else."""
    return _normalize(PropertyIdentifier, "property", value)


@dataclass(frozen=True)
class Point:
    """One remote property. Names are kebab-case -- the API normalizes them
    before they're stored (normalize_object_type / normalize_property)."""

    object_type: str
    instance: int
    # Not `property`: that would shadow the builtin for the decorators below.
    prop: str = "present-value"
    array_index: Optional[int] = None

    @property
    def object_identifier(self) -> ObjectIdentifier:
        return ObjectIdentifier((ObjectType(_enum_arg(self.object_type)), self.instance))

    @property
    def property_identifier(self) -> PropertyIdentifier:
        return PropertyIdentifier(_enum_arg(self.prop))

    @property
    def key(self) -> tuple:
        return (self.object_type, self.instance, self.prop, self.array_index)

    def __str__(self) -> str:
        index = "" if self.array_index is None else f"[{self.array_index}]"
        return f"{self.object_type}:{self.instance} {self.prop}{index}"


_ADDRESS_RE = re.compile(
    r"^(?:(?P<net>\d{1,5}):)?"
    r"(?P<station>0x[0-9A-Fa-f]{2,}|\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?|\d{1,3})$"
)


def parse_address(text: str) -> Address:
    """A static remote address: "IP[:port]", or "network:IP[:port]" /
    "network:0xMAC" / "network:MAC" (decimal 1-octet MAC, e.g. "5:18", as
    bacpypes3 prints a routed MS/TP station) behind a BACnet router.
    Hostnames (bacpypes3 would try DNS) and broadcasts raise ValueError."""
    stripped = (text or "").strip()
    match = _ADDRESS_RE.match(stripped)
    if match is None:
        raise ValueError(
            f"not a BACnet address: {text!r} (use IP[:port], network:IP[:port], network:0xMAC or network:MAC)"
        )
    net, station = match["net"], match["station"]
    if net is not None and not 1 <= int(net) <= 65534:
        raise ValueError(f"network number {net} out of range 1-65534")
    if station.startswith("0x") or station.isdigit():
        if net is None:
            raise ValueError("a MAC address needs a network number, e.g. 5:0x12")
        if station.isdigit() and int(station) > 255:
            raise ValueError(f"a decimal MAC is one octet, 0-255 (got {station})")
    else:
        host, _, port = station.partition(":")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(f"not an IPv4 address: {host!r}") from None
        if port and not 1 <= int(port) <= 65535:
            raise ValueError(f"port {port} out of range 1-65535")
    return Address(stripped)


def to_jsonable(value: Any) -> Any:
    """A decoded BACnet value as plain JSON data, for API results, status
    and write-mapping change detection. Order matters: ObjectIdentifier is a
    tuple, Boolean/Enumerated are ints, Date/Time are neither, a Choice is a
    Sequence with no _order."""
    if isinstance(value, AnyAtomic):
        # A Schedule's Present_Value (bacpypes3 types it AnyAtomic).
        value = value.get_value()
    if value is None or isinstance(value, Null):
        return None
    if isinstance(value, ObjectIdentifier):
        return f"{value[0]}:{value[1]}"
    if isinstance(value, Boolean):
        return bool(value)
    if isinstance(value, Enumerated):
        return str(value)
    if isinstance(value, BitString):
        return [int(bit) for bit in value]
    if isinstance(value, (Date, Time)):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        # A client can write NaN/Inf over the wire; JSON has no such number.
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Choice):
        # E.g. each priority-array slot (PriorityValue): its chosen value,
        # None for an empty slot (the "null" choice).
        name = bacpypes3_compat.chosen_field(value)
        return None if name is None else to_jsonable(getattr(value, name, None))
    if isinstance(value, BACnetSequence):
        return {name: to_jsonable(getattr(value, name, None)) for name in bacpypes3_compat.sequence_fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return str(value)


async def read_object_list(
    client,
    binding: Binding,
    instance: int,
    timeout: float,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> list:
    """A remote device's Object_List as ObjectIdentifiers: one read, or, when
    that is too big for an unsegmented reply, the count and then each entry.
    The entry-by-entry read (thousands of requests on a big controller) calls
    `progress(done, count)` after each entry and raises
    ClientError("cancelled") once `cancelled()` is true."""
    point = Point("device", instance, "object-list")
    try:
        return list(await client.read(binding, point, timeout=timeout))
    except ClientError as exc:
        if exc.kind != "too-big":
            raise
    count = int(await client.read(binding, Point("device", instance, "object-list", 0), timeout=timeout))
    identifiers = []
    for index in range(1, count + 1):
        if cancelled is not None and cancelled():
            raise ClientError("cancelled")
        identifiers.append(
            await client.read(binding, Point("device", instance, "object-list", index), timeout=timeout)
        )
        if progress is not None:
            progress(index, count)
    return identifiers


def resolve_datatype(point: Point) -> type:
    """The standard (ASHRAE) datatype of a remote property -- what a write
    must be cast to. ClientError for a proprietary object type or property,
    which has no standard datatype to cast to (the design never guesses)."""
    object_class = get_vendor_info(0).get_object_class(point.object_identifier[0])
    datatype = None if object_class is None else object_class.get_property_type(point.property_identifier)
    if datatype is None:
        raise ClientError(f"no standard datatype for {point}")
    if issubclass(datatype, Array) and point.array_index is not None:
        datatype = Unsigned if point.array_index == 0 else bacpypes3_compat.array_element_type(datatype)
    return datatype


def has_standard_datatype(point: Point) -> bool:
    """Whether the ASHRAE definitions give `point` a datatype -- False for a
    proprietary object type or property."""
    try:
        resolve_datatype(point)
    except ClientError:
        return False
    return True


def _is_wildcard_device(point: Point) -> bool:
    return point.object_type == "device" and point.instance == WILDCARD_DEVICE_INSTANCE


def _wildcard_reply_instance(batch: List[Point], found: dict) -> Optional[int]:
    """The real instance a reply used for the batch's wildcard device points: the
    one device instance in the reply that no other point of the batch names.
    None when there is no wildcard point or the reply doesn't single one out
    (the points then read "no value in reply" rather than guess)."""
    if not any(_is_wildcard_device(p) for p in batch):
        return None
    named = {p.instance for p in batch if p.object_type == "device" and not _is_wildcard_device(p)}
    replied = {key[1] for key in found if key[0] == "device"} - named
    return next(iter(replied)) if len(replied) == 1 else None


def _not_finite(value: Any, point: Point) -> str:
    return f"can't write {value!r} to {point}: not a finite number"


_BINARY_NAMES = {"inactive": 0, "active": 1, "false": 0, "true": 1}


def _binary_value(point: Point, value: Any) -> int:
    """0 or 1 for a BinaryPV write. BinaryPV is an Enumerated, which would
    take any number ("0x10", "1_0", 2) and send a value no binary object
    defines."""
    if isinstance(value, str):
        number = _BINARY_NAMES.get(value.strip().lower())
    elif isinstance(value, (int, float)) and value in (0, 1):
        number = int(value)
    else:
        number = None
    if number is None:
        raise ClientError(f"can't write {value!r} to {point}: use 0, 1, inactive, active, true or false")
    return number


def cast_for_write(point: Point, value: Any) -> Any:
    """`value` cast to the remote property's datatype; None is Null (a
    relinquish). Values convert across families (binary <-> 1.0/0.0), and
    numeric strings from the web form are parsed unless the property is a
    string. A Boolean takes only 0, 1, true or false, a binary Present_Value
    only 0, 1, inactive, active, true or false, and a non-finite number is
    refused. A write to an AnyAtomic property: see _cast_for_any_atomic."""
    if value is None:
        return Null(())
    if isinstance(value, AnyAtomic):
        # get_value() demotes BinaryPV to a plain Enumerated -- cast by its
        # number below, which every Enumerated subclass accepts.
        value = value.get_value()
    datatype = resolve_datatype(point)
    if datatype is AnyAtomic:
        return _cast_for_any_atomic(point, value)
    if isinstance(value, Enumerated):
        value = int(value)
    elif isinstance(value, (bool, Boolean)):
        value = bool(value) if issubclass(datatype, Boolean) else int(value)
    elif isinstance(value, float):
        value = float(value)
    elif isinstance(value, int):
        value = int(value)
    if isinstance(value, str) and not issubclass(datatype, CharacterString):
        try:
            value = float(value)
        except ValueError:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        raise ClientError(_not_finite(value, point))
    if isinstance(value, float) and value.is_integer() and not issubclass(datatype, (Real, Double)):
        value = int(value)
    if (
        issubclass(datatype, Boolean)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value not in (0, 1)
    ):
        raise ClientError(f"can't write {value!r} to {point} as Boolean: use 0, 1, true or false")
    if issubclass(datatype, Boolean) and isinstance(value, str):
        # bacpypes3's own text parse also takes "set" and "reset".
        flag = {"true": True, "false": False}.get(value.strip().lower())
        if flag is None:
            raise ClientError(f"can't write {value!r} to {point} as Boolean: use 0, 1, true or false")
        value = flag
    if issubclass(datatype, BinaryPV):
        value = _binary_value(point, value)
    try:
        return datatype(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ClientError(f"can't write {value!r} to {point} as {datatype.__name__}: {exc}") from None


def _cast_for_any_atomic(point: Point, value: Any) -> AnyAtomic:
    """A write to an AnyAtomic property (a remote Schedule's Present_Value):
    an atomic value keeps its type; bool goes as Boolean, int as Unsigned,
    float or a numeric string as Real."""
    atomic = value
    if not isinstance(value, Atomic):
        if isinstance(value, bool):
            atomic = Boolean(value)
        elif isinstance(value, int):
            atomic = Unsigned(value) if value >= 0 else None
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ClientError(_not_finite(value, point))
            atomic = Real(value)
        elif isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                atomic = None
            else:
                if not math.isfinite(number):
                    raise ClientError(_not_finite(value, point))
                atomic = Real(number)
        else:
            atomic = None
    if isinstance(atomic, float) and not math.isfinite(atomic):
        raise ClientError(_not_finite(atomic, point))
    if atomic is None:
        raise ClientError(f"can't write {value!r} to {point} as AnyAtomic: not a number or boolean")
    return AnyAtomic(atomic)


@dataclass
class Binding:
    """Where a remote device answers, plus what's been learned about it for
    as long as this binding lives (dropped on a transport failure)."""

    address: Address
    no_rpm: bool = False
    batch_size: int = RPM_BATCH_SIZE


class BacnetClient:
    """The client role of one device's application. One instance per device
    process, shared by the connector runner and the client-request poller,
    so they share bindings."""

    def __init__(self, app) -> None:
        self.app = app
        self._bindings: Dict[Tuple[int, Optional[str]], Binding] = {}

    @staticmethod
    def _key(instance: int, static_address: Optional[str]) -> Tuple[int, Optional[str]]:
        return (instance, (static_address or "").strip() or None)

    def binding(self, instance: int, static_address: Optional[str] = None) -> Optional[Binding]:
        return self._bindings.get(self._key(instance, static_address))

    def unbind(self, instance: int, static_address: Optional[str] = None) -> None:
        self._bindings.pop(self._key(instance, static_address), None)

    def _check_dcc(self) -> None:
        # ASHRAE 135: a device under DCC disable/disableInitiation must not
        # initiate requests. LoggingApplication.request() swallows them all;
        # the client checks first to raise a reason instead of sending nothing.
        state = getattr(self.app, "_dcc_state", "enable")
        if state != "enable":
            raise ClientError(f"suppressed by DCC ({state})", kind="dcc")

    def _log(self, event_type: str, detail: str) -> None:
        # The traffic log's own entries are written by the do_* handlers,
        # i.e. incoming requests only -- outgoing client requests log here.
        traffic_log = getattr(self.app, "traffic_log", None)
        if traffic_log is None:
            return
        try:
            traffic_log.add(event_type, detail)
        except Exception:
            logger.exception("client traffic log entry failed")

    async def _call(self, awaitable, timeout: float):
        """Every call into bacpypes3's client methods goes through here: its
        error/reject/abort replies are BaseExceptions, not Exceptions."""
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError:
            raise ClientError(f"no response within {timeout:g} s", kind="transport") from None
        except ErrorRejectAbortNack as exc:
            raise error_from_reply(exc) from None

    async def bind(self, instance: int, static_address: Optional[str] = None, *, timeout: float) -> Binding:
        key = self._key(instance, static_address)
        existing = self._bindings.get(key)
        if existing is not None:
            return existing
        if key[1] is not None:
            try:
                address = parse_address(key[1])
            except ValueError as exc:
                raise ClientError(str(exc), kind="transport") from None
        else:
            self._check_dcc()
            self._log("client-who-is", f"device {instance}")
            # low == high: bacpypes3 resolves this on the first I-Am.
            i_ams = await self._call(self.app.who_is(instance, instance, timeout=timeout), timeout + 1)
            if not i_ams:
                raise ClientError(f"device {instance} did not answer Who-Is", kind="transport")
            address = i_ams[0].pduSource
        binding = Binding(address)
        self._bindings[key] = binding
        return binding

    async def read(self, binding: Binding, point: Point, *, timeout: float) -> Any:
        self._check_dcc()
        self._log("client-read-property", f"{binding.address} {point}")
        try:
            value = await self._call(
                self.app.read_property(
                    binding.address, point.object_identifier, point.property_identifier, point.array_index
                ),
                timeout,
            )
        except ClientError:
            raise
        except Exception as exc:
            # bacpypes3's cast_out can raise a plain Exception decoding a
            # value (e.g. a proprietary type).
            raise ClientError(f"{point}: can't decode ({exc})") from None
        # bacpypes3 returns (not raises) "-no object class-" / "-no property
        # type-" when it can't decode a proprietary value; a real string
        # value is a CharacterString, never a bare str.
        if type(value) is str:
            raise ClientError(f"{point}: can't decode ({value.strip('-')})")
        return value

    async def read_many(self, binding: Binding, points: List[Point], *, timeout: float) -> Dict[Point, Any]:
        """Every point's value, keyed by point. A point the remote answered
        with an error, or that couldn't be decoded, maps to a ClientError
        instead of a value. Raises ClientError for a failure of the whole
        request (transport, or DCC)."""
        self._check_dcc()
        unique = list(dict.fromkeys(points))
        results: Dict[Point, Any] = {}
        if binding.no_rpm:
            return await self._read_each(binding, unique, timeout)

        start = 0
        while start < len(unique):
            batch = unique[start:start + binding.batch_size]
            try:
                results.update(await self._read_batch(binding, batch, timeout))
            except ClientError as exc:
                if exc.kind == "unsupported":
                    binding.no_rpm = True
                    results.update(await self.read_many(binding, unique[start:], timeout=timeout))
                    return results
                if exc.kind == "too-big":
                    if binding.batch_size > 1:
                        binding.batch_size = max(1, binding.batch_size // 2)
                        continue
                    # One property alone too big: that point's own error,
                    # not the whole read's.
                    point = batch[0]
                    results[point] = ClientError(
                        f"{point}: too big for the remote to send unsegmented", kind="too-big"
                    )
                    start += 1
                    continue
                if exc.kind == "error":
                    # The batch failed to decode (e.g. a proprietary object
                    # type): a problem with these points, not RPM, so read
                    # them one at a time and stay on RPM.
                    results.update(await self._read_each(binding, batch, timeout))
                    start += len(batch)
                    continue
                raise
            start += len(batch)
        return results

    async def _read_each(self, binding: Binding, points: List[Point], timeout: float) -> Dict[Point, Any]:
        """Every point read individually via read_property. A point's own
        error becomes a ClientError value for it; a transport failure (the
        link itself, not this point) still ends the whole read."""
        results: Dict[Point, Any] = {}
        for point in points:
            try:
                results[point] = await self.read(binding, point, timeout=timeout)
            except ClientError as exc:
                if exc.kind in ("transport", "dcc"):
                    raise
                results[point] = exc
        return results

    async def _read_batch(self, binding: Binding, batch: List[Point], timeout: float) -> Dict[Point, Any]:
        by_object: Dict[ObjectIdentifier, list] = {}
        for point in batch:
            reference = PropertyReference(propertyIdentifier=point.property_identifier)
            if point.array_index is not None:
                reference.propertyArrayIndex = point.array_index
            by_object.setdefault(point.object_identifier, []).append(reference)
        # bacpypes3 unpacks this flat: objid, [references], objid, [references], ...
        parameters: list = []
        for object_identifier, references in by_object.items():
            parameters.extend([object_identifier, references])

        self._log("client-read-property-multiple", f"{binding.address} {len(batch)} properties")
        try:
            rows = await self._call(self.app.read_property_multiple(binding.address, parameters), timeout)
        except ClientError:
            raise
        except Exception as exc:
            # bacpypes3 can't decode a reply naming a proprietary object type
            # (it has no class to look the property up on) and raises
            # AttributeError -- a problem with these points, not the link.
            raise ClientError(f"can't decode reply: {exc}") from None

        found = {}
        for object_identifier, property_identifier, array_index, value in rows or []:
            key = (str(object_identifier[0]), int(object_identifier[1]), str(property_identifier), array_index)
            found[key] = value
        wildcard_instance = _wildcard_reply_instance(batch, found)
        out: Dict[Point, Any] = {}
        for point in batch:
            key = point.key
            if wildcard_instance is not None and _is_wildcard_device(point):
                key = (point.object_type, wildcard_instance, point.prop, point.array_index)
            if key not in found:
                out[point] = ClientError(f"{point}: no value in reply")
                continue
            value = found[key]
            if isinstance(value, ErrorType):
                out[point] = ClientError(
                    f"{point}: {value.errorClass}: {value.errorCode}",
                    error_class=str(value.errorClass),
                    error_code=str(value.errorCode),
                    reply="error",
                )
            elif value is None:
                out[point] = ClientError(f"{point}: can't decode (no standard datatype)")
            else:
                out[point] = value
        return out

    async def write(
        self,
        binding: Binding,
        point: Point,
        value: Any,
        *,
        priority: Optional[int],
        timeout: float,
        native: Optional[Atomic] = None,
    ) -> None:
        """WriteProperty, `value` cast to the remote property's datatype.
        `native` is sent as-is instead when the property has no standard
        datatype. Only a Schedule passes it: the standard expects its targets
        to take the schedule's own type, so nothing is guessed. Without `native`, such a
        point is a ClientError."""
        self._check_dcc()
        if value is None and priority is None:
            raise ClientError("a relinquish (null) needs a priority")
        if native is not None and not has_standard_datatype(point):
            await self._write_raw(binding, point, native, priority=priority, timeout=timeout)
            return
        typed = cast_for_write(point, value)
        at = "" if priority is None else f" @ {priority}"
        self._log("client-write-property", f"{binding.address} {point} = {to_jsonable(typed)!r}{at}")
        result = await self._call(
            self.app.write_property(
                binding.address,
                point.object_identifier,
                point.property_identifier,
                typed,
                point.array_index,
                priority,
            ),
            timeout,
        )
        if result is not None:
            # bacpypes3 returns (doesn't raise) "-no object class-" / "-no
            # property type-" and sends nothing; a relinquish skips
            # resolve_datatype, so only this catches it.
            raise ClientError(f"{point}: can't write ({str(result).strip('-')})")

    async def _write_raw(
        self, binding: Binding, point: Point, typed: Atomic, *, priority: Optional[int], timeout: float
    ) -> None:
        """A WritePropertyRequest built here: bacpypes3's write_property looks
        the object class up itself and returns "-no object class-" for a type
        it doesn't know, without sending anything."""
        at = "" if priority is None else f" @ {priority}"
        self._log("client-write-property", f"{binding.address} {point} = {to_jsonable(typed)!r}{at}")
        request = WritePropertyRequest(
            objectIdentifier=point.object_identifier,
            propertyIdentifier=point.property_identifier,
            propertyValue=typed,
            destination=binding.address,
        )
        if point.array_index is not None:
            request.propertyArrayIndex = point.array_index
        if priority is not None:
            request.priority = priority
        response = await self._call(self.app.request(request), timeout)
        if isinstance(response, ErrorRejectAbortNack):
            raise error_from_reply(response)

    async def who_is(
        self, low: Optional[int] = None, high: Optional[int] = None, *, window: float
    ) -> List[dict]:
        self._check_dcc()
        self._log("client-who-is", "all devices" if low is None else f"devices {low}-{high}")
        i_ams = await self._call(self.app.who_is(low, high, timeout=window), window + 1)
        found = [
            {
                "instance": int(i_am.iAmDeviceIdentifier[1]),
                "address": str(i_am.pduSource),
                "vendor_id": int(i_am.vendorID),
                "max_apdu": int(i_am.maxAPDULengthAccepted),
                "segmentation": str(i_am.segmentationSupported),
            }
            for i_am in i_ams or []
        ]
        return sorted(found, key=lambda device: device["instance"])
