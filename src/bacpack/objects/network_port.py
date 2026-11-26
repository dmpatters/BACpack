"""NetworkPort object construction + BACnet/IP network settings validation."""

from __future__ import annotations

import re
from typing import List, Optional

from bacpypes3.basetypes import BDTEntry, HostNPort
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.pdu import Address

from ..bacnet import WritePropertyAllowlisted
from .reserved_names import NETWORK_PORT_NAME

class ReadOnlyNetworkPortObject(WritePropertyAllowlisted, NetworkPortObject):
    """Network settings are configured in BACpack, not over BACnet: upstream
    would ack a write and apply none of it."""

    _WRITABLE_ATTRS = frozenset()


# IPv4 with an optional /prefix and :port. Checked before bacpypes3's
# parsers, which would look anything else up as a hostname.
_IPV4_FORM = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?::(\d{1,5}))?")


def _require_ipv4_form(field: str, text: str) -> None:
    match = _IPV4_FORM.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"invalid {field} {text!r}: use an IPv4 address, e.g. 192.168.1.50/24:47808")
    # bacpypes3's Address() doesn't range-check: :0 binds a random port no
    # client can find, and :99999 fails only when the device starts.
    port = match.group(1)
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError(f"invalid {field} {text!r}: the port must be 1-65535")


def build_network_port_object(
    bind_address: str,
    *,
    foreign_bbmd: Optional[str] = None,
    foreign_ttl: int = 60,
    bbmd_peers: Optional[List[str]] = None,
) -> ReadOnlyNetworkPortObject:
    """`bind_address` is CIDR, e.g. "192.168.1.50/24".

    Network role, mutually exclusive: normal (local broadcast only);
    foreign_bbmd set (host[:port]): register as a foreign device there;
    bbmd_peers set: act as a BBMD with these peers as the BDT.
    """
    if foreign_bbmd and bbmd_peers:
        raise ValueError("a device can register as foreign or act as a BBMD, not both")

    network_port_object = ReadOnlyNetworkPortObject(
        bind_address,
        objectIdentifier=("network-port", 1),
        objectName=NETWORK_PORT_NAME,
    )

    if foreign_bbmd:
        network_port_object.bacnetIPMode = "foreign"
        network_port_object.fdBBMDAddress = HostNPort(foreign_bbmd)
        network_port_object.fdSubscriptionLifetime = foreign_ttl
    elif bbmd_peers:
        network_port_object.bacnetIPMode = "bbmd"
        network_port_object.bbmdAcceptFDRegistrations = True
        network_port_object.bbmdForeignDeviceTable = []
        network_port_object.bbmdBroadcastDistributionTable = [
            BDTEntry(addr) for addr in bbmd_peers
        ]

    return network_port_object


def validate_network_settings(
    bind_address: Optional[str],
    *,
    network_role: str = "normal",
    foreign_bbmd: Optional[str] = None,
    foreign_ttl: int = 60,
    bbmd_peers: Optional[List[str]] = None,
) -> None:
    """Raises ValueError if this combination would fail to build a real
    NetworkPortObject, so the API rejects a typo at save time instead of
    at device start.

    Doesn't call build_network_port_object: building any bacpypes3 local
    Object needs a running event loop (_post_init), and the sync route runs
    in a worker thread without one. Address/HostNPort/BDTEntry need no loop,
    but resolve a hostname, so only IPv4 forms are passed to them.

    A blank bind_address ("not configured yet") is allowed. `network_role`
    decides which of foreign_bbmd/bbmd_peers applies.
    """
    if not bind_address:
        return
    _require_ipv4_form("bind_address", bind_address)
    try:
        Address(bind_address)
    except Exception as exc:
        raise ValueError(f"invalid bind_address {bind_address!r}: {exc}") from exc

    if network_role == "foreign" and foreign_bbmd:
        _require_ipv4_form("foreign_bbmd", foreign_bbmd)
        try:
            HostNPort(foreign_bbmd)
        except Exception as exc:
            raise ValueError(f"invalid foreign_bbmd {foreign_bbmd!r}: {exc}") from exc

    if network_role == "bbmd" and bbmd_peers:
        for peer in bbmd_peers:
            _require_ipv4_form("bbmd_peers entry", peer)
            try:
                BDTEntry(peer)
            except Exception as exc:
                raise ValueError(f"invalid bbmd_peers entry {peer!r}: {exc}") from exc
