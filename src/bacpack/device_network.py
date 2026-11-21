"""A device's network role, the BBMD table it builds from its links, and the
rules for changing them.

Sockets are compared as parsed (host, port), never as strings: the same socket
is written "192.168.1.200/24", "192.168.1.200:47808" and
"192.168.1.200/24:47808" depending on where it came from."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from bacpypes3.pdu import IPv4Address
from sqlalchemy import select
from sqlalchemy.orm import Session

from .choices import NETWORK_ROLES
from .models import Device, DeviceLink, get_or_create_settings
from .objects.network_port import validate_network_settings

# Role fields are validated apart from any bind address.
_ANY_BIND = "127.0.0.1"
# The Network Port's default, for a device that doesn't register as foreign.
_UNUSED_TTL = 60


def split_socket(bind_address: str) -> Optional[Tuple[str, int]]:
    """(host, port) of a bind address; None if it doesn't parse."""
    try:
        host, port = IPv4Address(bind_address).addrTuple  # pyright: ignore[reportAttributeAccessIssue] - bacpypes3 types IPv4Address() as Address
    except Exception:
        return None
    return host, port


def socket_of(bind_address: str) -> str:
    """"host:port" of a bind address; the string itself if it doesn't parse."""
    parts = split_socket(bind_address)
    return bind_address if parts is None else f"{parts[0]}:{parts[1]}"


# A bind address: host, optional /prefix, optional :port.
BIND_FORM = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?::\d{1,5})?")


def bind_with_port(bind: str, port: int) -> Optional[str]:
    """`bind` with its port replaced; None if it isn't a bind address."""
    match = BIND_FORM.fullmatch(bind)
    if match is None:
        return None
    host, prefix = match.group(1), match.group(2)
    return f"{host}/{prefix}:{port}" if prefix else f"{host}:{port}"


def ports_in_use(session: Session, host: str, ignore_device_id: Optional[int] = None) -> set:
    """The UDP ports BACpack devices are configured to use on `host`."""
    taken = set()
    for other in session.scalars(select(Device)).all():
        other_bind = effective_bind(session, other)
        parts = split_socket(other_bind) if other_bind and other.id != ignore_device_id else None
        if parts is not None and parts[0] == host:
            taken.add(parts[1])
    return taken


def same_network(a: str, b: str) -> bool:
    """One BACnet network: the same UDP port and overlapping IPv4 subnets
    (each address with its own prefix, none meaning one host). Devices like
    that hear each other's broadcasts whatever BACpack does."""
    try:
        first, second = IPv4Address(a), IPv4Address(b)
        return (
            first.addrTuple[1] == second.addrTuple[1]  # pyright: ignore[reportAttributeAccessIssue] - bacpypes3 types IPv4Address() as Address
            and first.network.overlaps(second.network)  # pyright: ignore[reportAttributeAccessIssue] - same
        )
    except Exception:
        return False


@dataclass(frozen=True)
class NetworkConfig:
    """What a device's Network Port is built from, and what a running device
    compares to know a restart is due."""

    foreign_bbmd: Optional[str]
    foreign_ttl: int
    # Only for a BBMD: its own socket, its linked devices', then typed peers.
    bdt: Optional[Tuple[str, ...]]


def effective_bind(session: Session, device: Device) -> Optional[str]:
    return device.bind_address or get_or_create_settings(session).bind_address or None


def resolve_network(session: Session, device: Device) -> NetworkConfig:
    role = device.network_role
    if role == "foreign" and device.foreign_bbmd:
        return NetworkConfig(foreign_bbmd=device.foreign_bbmd, foreign_ttl=device.foreign_ttl, bdt=None)
    # Any other role ignores the TTL, so a change to it must not ask for a restart.
    if role == "bbmd":
        sockets: List[str] = []
        own = effective_bind(session, device)
        if own:
            sockets.append(socket_of(own))
        for linked_id in sorted(link.linked_device_id for link in device.bbmd_links):
            linked = session.get(Device, linked_id)
            bind = effective_bind(session, linked) if linked is not None else None
            # The saves refuse these, but a server default change can still make
            # one: a foreign device drops forwarded broadcasts, and a device on
            # this network would hear each one twice.
            if not bind or linked is None or linked.network_role == "foreign" or (own and same_network(own, bind)):
                continue
            sockets.append(socket_of(bind))
        sockets.extend(socket_of(peer) for peer in device.bbmd_peers or [])
        bdt = tuple(dict.fromkeys(sockets))
        return NetworkConfig(foreign_bbmd=None, foreign_ttl=_UNUSED_TTL, bdt=bdt or None)
    return NetworkConfig(foreign_bbmd=None, foreign_ttl=_UNUSED_TTL, bdt=None)


@dataclass(frozen=True)
class LinkTarget:
    """What the link rules need to know about a device a BBMD would link."""

    id: int
    name: str
    role: str
    bind: Optional[str]  # its effective bind address


def _named(device: "LinkTarget | Device") -> str:
    return f"{device.name} ({device.id})"


def check_role_fields(
    role: str, foreign_bbmd: Optional[str], foreign_ttl: int, bbmd_peers: Optional[List[str]]
) -> Optional[str]:
    """The part of a network setting that needs no other device."""
    if role not in NETWORK_ROLES:
        return f"network role must be one of {', '.join(NETWORK_ROLES)}"
    if role == "foreign" and not foreign_bbmd:
        return "a foreign device needs the address of the BBMD it registers with"
    for peer in bbmd_peers or []:
        # A /prefix would read as a broadcast mask, but the BDT is built from
        # the peer's socket only (resolve_network), so the mask would do nothing.
        if isinstance(peer, str) and "/" in peer:
            return f"invalid bbmd_peers entry {peer!r}: a BBMD peer is an IPv4 address or address:port, with no /prefix"
    try:
        validate_network_settings(
            _ANY_BIND,
            network_role=role,
            foreign_bbmd=foreign_bbmd,
            foreign_ttl=foreign_ttl,
            bbmd_peers=bbmd_peers,
        )
        # Peers are kept while the role is something else; they must still parse.
        if role != "bbmd" and bbmd_peers:
            validate_network_settings(_ANY_BIND, network_role="bbmd", bbmd_peers=bbmd_peers)
    except ValueError as exc:
        return str(exc)
    return None


def check_links(
    device: "LinkTarget | Device",
    own_bind: Optional[str],
    links: List[int],
    find: Callable[[int], Optional[LinkTarget]],
) -> Optional[str]:
    """None when `device` may link each of `links`; `find` looks a device up
    by id, from the database or from a file's planned devices."""
    for linked_id in links:
        if linked_id == device.id:
            return f"{_named(device)} can't be linked to itself"
        linked = find(linked_id)
        if linked is None:
            return f"device {linked_id} doesn't exist"
        if linked.role == "foreign":
            return f"{_named(linked)} is a foreign device; it can't be linked"
        if not linked.bind:
            return (
                f"{_named(linked)} has no bind address, its own or a server default, "
                "so there is no socket to link"
            )
        if own_bind and same_network(own_bind, linked.bind):
            return (
                f"{_named(linked)} is already on this device's network "
                "(same subnet and UDP port); it can't be linked"
            )
    return None


def check_network(
    session: Session,
    device: Device,
    *,
    role: str,
    foreign_bbmd: Optional[str],
    foreign_ttl: int,
    bbmd_peers: Optional[List[str]],
    links: List[int],
) -> Optional[str]:
    """None when this role, foreign BBMD, typed peers and link list may be
    saved for `device`, else the reason."""
    problem = check_role_fields(role, foreign_bbmd, foreign_ttl, bbmd_peers)
    if problem is not None:
        return problem

    def find(device_id: int) -> Optional[LinkTarget]:
        linked = session.get(Device, device_id)
        if linked is None:
            return None
        return LinkTarget(linked.id, linked.name, linked.network_role, effective_bind(session, linked))

    return check_links(device, effective_bind(session, device), links, find)


def check_linking_bbmds(session: Session, device: Device) -> Optional[str]:
    """None when every BBMD linking `device` may still link it as it is now
    (its unsaved role and bind address): the rules hold from both sides."""
    target = LinkTarget(device.id, device.name, device.network_role, effective_bind(session, device))
    # No autoflush: the unsaved change must not reach the database before it passes.
    with session.no_autoflush:
        bbmds = session.scalars(
            select(Device).join(DeviceLink, DeviceLink.device_id == Device.id).where(DeviceLink.linked_device_id == device.id)
        ).all()
    for bbmd in bbmds:
        problem = check_links(bbmd, effective_bind(session, bbmd), [device.id], lambda _id: target)
        if problem is not None:
            return f"{_named(bbmd)} links this device: {problem}"
    return None
