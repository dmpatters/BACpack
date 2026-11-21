"""Hand-built BACnet/IP requests over a plain UDP socket (no bacpypes3, no
bacnet-stack). bacnet-stack 1.7.0 binds to a device with a Who-Is, which a
DCC-disabled device ignores, and bacrd has no --mac; so a request to a
disabled device has to be built here. Clause numbers are ASHRAE 135."""
from __future__ import annotations

import socket
from dataclasses import dataclass

SERVICE_DEVICE_COMMUNICATION_CONTROL = 17  # clause 16.1
SERVICE_REINITIALIZE_DEVICE = 20  # clause 16.4
ENABLE = 0  # BACnetEnableDisable, clause 16.1.1.1.2
DISABLE_INITIATION = 2  # the same enumeration
WARMSTART = 1  # BACnetReinitializedState, clause 16.4.1.1.1
ERROR_CLASS_SECURITY = 4  # BACnetErrorClass, clause 18
ERROR_CODE_PASSWORD_FAILURE = 26  # BACnetErrorCode, clause 18


def _context_tag(context_tag: int, length: int) -> bytes:
    # Context tag [n] (20.2.1.3.1): a content length of 0-4 octets sits in the
    # tag octet; 5 in that field means the length follows in one more octet.
    # The 2-octet length form (254 and more) is not needed here.
    if length <= 4:
        return bytes([(context_tag << 4) | 0x08 | length])
    if length <= 253:
        return bytes([(context_tag << 4) | 0x08 | 5, length])
    raise ValueError("content too long for the one-octet length form")


def character_string(context_tag: int, text: str) -> bytes:
    # The content is the character-set octet 0 (UTF-8, 20.2.9) and the characters.
    body = b"\x00" + text.encode("utf-8")
    return _context_tag(context_tag, len(body)) + body


def _enumerated(context_tag: int, value: int) -> bytes:
    # One content octet (20.2.11).
    assert 0 <= value < 256
    return _context_tag(context_tag, 1) + bytes([value])


def _confirmed_request(invoke_id: int, service: int, parameters: bytes) -> bytes:
    apdu = (
        b"\x00"  # PDU type 0 (BACnet-Confirmed-Request), not segmented (20.1.2)
        + b"\x05"  # max segments unspecified, max APDU 1476 octets
        + bytes([invoke_id, service])
        + parameters
    )
    npdu = b"\x01\x04"  # version 1; control: a reply is expected (6.2.2)
    packet = npdu + apdu
    # BVLC Original-Unicast-NPDU (Annex J.2.11): type 0x81, function 0x0A, length incl. header.
    return b"\x81\x0a" + (len(packet) + 4).to_bytes(2, "big") + packet


def reinitialize_device(invoke_id: int, state: int, password: str) -> bytes:
    return _confirmed_request(
        invoke_id, SERVICE_REINITIALIZE_DEVICE, _enumerated(0, state) + character_string(1, password)
    )


def device_communication_control(invoke_id: int, enable_disable: int, password: str) -> bytes:
    # [0] timeDuration is left out; [1] enable-disable, [2] password.
    return _confirmed_request(
        invoke_id, SERVICE_DEVICE_COMMUNICATION_CONTROL, _enumerated(1, enable_disable) + character_string(2, password)
    )


@dataclass
class Reply:
    kind: str  # "simple-ack", "error" or "other"
    invoke_id: int | None = None
    service: int | None = None
    error_class: int | None = None
    error_code: int | None = None


def decode_reply(packet: bytes, invoke_id: int | None = None) -> Reply:
    """`invoke_id`, when given, must match the reply's; anything else (a stray
    datagram, another request's answer) decodes as "other"."""
    if len(packet) < 8 or packet[0] != 0x81 or packet[1] != 0x0A:
        return Reply("other")
    # BVLC header (4 octets), then an NPDU of version and control; a reply to
    # a local source carries no network addresses, which is what is expected.
    if packet[4] != 0x01 or packet[5] & 0xE8:
        return Reply("other")
    apdu = packet[6:]
    pdu_type = apdu[0] >> 4
    if invoke_id is not None and apdu[1] != invoke_id:
        return Reply("other")
    if pdu_type == 2 and len(apdu) >= 3:  # Simple-ACK (20.1.4)
        return Reply("simple-ack", invoke_id=apdu[1], service=apdu[2])
    if pdu_type == 5 and len(apdu) >= 7:  # Error (20.1.7): two application-tagged enumerations
        if apdu[3] == 0x91 and apdu[5] == 0x91:
            return Reply("error", apdu[1], apdu[2], error_class=apdu[4], error_code=apdu[6])
    return Reply("other")


def send_and_receive(request: bytes, destination: tuple[str, int], *, source: str = "127.0.0.1", timeout: float = 5) -> Reply:
    """One request from an ephemeral port (not the bacnet-stack client's port
    and not a device's), one reply. A missing reply raises TimeoutError."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((source, 0))
        sock.settimeout(timeout)
        sock.sendto(request, destination)
        packet, _ = sock.recvfrom(2048)
    return decode_reply(packet, invoke_id=request[8])  # the request's APDU invoke id
