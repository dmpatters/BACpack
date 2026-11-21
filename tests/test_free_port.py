"""The free-port suggestion for a device whose port is in use."""
from __future__ import annotations

import errno
import socket

import pytest

from bacpack import free_port
from bacpack.device_process import _bind_error_message, is_port_in_use_error
from bacpack.free_port import suggest_port

# Not the BACnet range: a test holding 47809 would collide with a device other
# xdist workers serve there.
_FIRST = 52610


@pytest.fixture(autouse=True)
def _a_range_of_ten_nobody_else_uses(monkeypatch):
    monkeypatch.setattr(free_port, "FIRST_PORT", _FIRST)
    monkeypatch.setattr(free_port, "LAST_PORT", _FIRST + 9)


def _hold(port, host="127.0.0.1"):
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind((host, port))
    return held


def test_the_default_range_is_47809_to_47908(monkeypatch):
    monkeypatch.undo()
    assert (free_port.FIRST_PORT, free_port.LAST_PORT) == (47809, 47908)


def test_the_first_free_port_is_suggested():
    assert suggest_port("127.0.0.1", set()) == _FIRST


def test_a_port_held_by_another_program_is_skipped():
    held = _hold(_FIRST)
    try:
        assert suggest_port("127.0.0.1", set()) == _FIRST + 1
    finally:
        held.close()


def test_a_port_another_device_is_configured_with_is_skipped():
    assert suggest_port("127.0.0.1", {_FIRST, _FIRST + 1}) == _FIRST + 2


def test_a_held_port_and_a_taken_one_are_both_skipped():
    held = _hold(_FIRST + 1)
    try:
        assert suggest_port("127.0.0.1", {_FIRST}) == _FIRST + 2
    finally:
        held.close()


def test_every_port_taken_gives_none():
    assert suggest_port("127.0.0.1", set(range(_FIRST, _FIRST + 10))) is None


def test_every_port_held_gives_none():
    held = [_hold(port) for port in range(_FIRST, _FIRST + 10)]
    try:
        assert suggest_port("127.0.0.1", set()) is None
    finally:
        for sock in held:
            sock.close()


def test_a_host_this_machine_does_not_have_gives_none():
    assert suggest_port("203.0.113.5", set()) is None


def test_the_probe_is_released_at_once():
    assert suggest_port("127.0.0.1", set()) == _FIRST
    again = _hold(_FIRST)  # would fail if the probe socket were still open
    again.close()


# --- The "in use" message -----------------------------------------------------

def test_the_in_use_message_is_recognised():
    message = _bind_error_message(OSError(errno.EADDRINUSE, "Address in use"), "192.168.1.200", 47808)
    assert is_port_in_use_error(message)


def test_other_bind_errors_and_no_error_are_not_in_use():
    assert not is_port_in_use_error(_bind_error_message(OSError(errno.EADDRNOTAVAIL, "x"), "192.168.1.200", 47808))
    assert not is_port_in_use_error(_bind_error_message(OSError(errno.EACCES, "denied"), "192.168.1.200", 47808))
    assert not is_port_in_use_error(None)
    assert not is_port_in_use_error("bind address '10.0.0.5' is already in use by currently-running device 7")


# --- port_is_free ---------------------------------------------------------------

def test_port_is_free_for_a_port_nobody_holds():
    assert free_port.port_is_free("127.0.0.1", _FIRST) is True


def test_port_is_free_is_false_for_a_held_port():
    held = _hold(_FIRST)
    try:
        assert free_port.port_is_free("127.0.0.1", _FIRST) is False
    finally:
        held.close()


def test_port_is_free_is_false_for_a_host_this_machine_does_not_have():
    assert free_port.port_is_free("203.0.113.5", _FIRST) is False
