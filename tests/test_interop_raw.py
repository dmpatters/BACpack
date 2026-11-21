from __future__ import annotations

import pytest

from tests.interop import raw


def test_reinitialize_device_bytes():
    packet = raw.reinitialize_device(9, raw.WARMSTART, "secret")
    # BVLC, NPDU, confirmed request header, [0] state, [1] password.
    assert packet == bytes.fromhex("810a0015" "0104" "0005" "09" "14" "0901" "1d07" "00" + b"secret".hex())
    assert int.from_bytes(packet[2:4], "big") == len(packet)


def test_device_communication_control_bytes():
    packet = raw.device_communication_control(3, raw.ENABLE, "pw")
    assert packet == bytes.fromhex("810a0010" "0104" "0005" "03" "11" "1900" "2b" "00" + b"pw".hex())


def test_short_and_long_context_tags():
    # 0-4 content octets (the charset octet counts) sit in the tag octet; 5 and up use a length octet.
    assert raw.character_string(2, "abc") == bytes.fromhex("2c00") + b"abc"
    assert raw.character_string(2, "abcd") == bytes.fromhex("2d0500") + b"abcd"
    assert raw.character_string(1, "") == bytes.fromhex("1900")
    with pytest.raises(ValueError):
        raw.character_string(1, "x" * 300)


def test_decode_only_accepts_the_matching_reply():
    simple = bytes.fromhex("810a0008" "0100" "20" "09" "14")
    assert raw.decode_reply(simple, invoke_id=9).kind == "simple-ack"
    assert raw.decode_reply(simple, invoke_id=10).kind == "other"
    assert raw.decode_reply(bytes.fromhex("810b0008" "0100" "20" "09" "14")).kind == "other"


def test_decode_replies():
    simple = bytes.fromhex("810a0008" "0100" "20" "09" "14")
    assert raw.decode_reply(simple) == raw.Reply("simple-ack", 9, 20)
    error = bytes.fromhex("810a000b" "0100" "50" "09" "14" "9104" "911a")
    assert raw.decode_reply(error) == raw.Reply("error", 9, 20, error_class=4, error_code=26)
    assert raw.decode_reply(b"\x81\x0b\x00\x04").kind == "other"
