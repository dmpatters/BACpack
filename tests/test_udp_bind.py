"""The loopback build helper: a busy port is a failure, a missing address a skip."""
from __future__ import annotations

import errno

import pytest
from _pytest.outcomes import Failed, Skipped

from bacpack.models import Device
from tests import udp_bind


def _failing_build(code):
    def build(device, objects, **kwargs):
        raise OSError(code, "bind refused")

    return build


@pytest.fixture
def device_one(session_factory):
    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()


def test_a_busy_port_fails_instead_of_skipping(session_factory, device_one, monkeypatch):
    monkeypatch.setattr(udp_bind, "build_application", _failing_build(errno.EADDRINUSE))
    with pytest.raises(Failed, match="in use"):
        udp_bind.build_on_loopback(session_factory, 1, "127.0.0.2/8")


def test_a_missing_loopback_address_skips(session_factory, device_one, monkeypatch):
    monkeypatch.setattr(udp_bind, "build_application", _failing_build(errno.EADDRNOTAVAIL))
    with pytest.raises(Skipped):
        udp_bind.build_on_loopback(session_factory, 1, "127.0.0.2/8")


def test_any_other_bind_error_is_raised(session_factory, device_one, monkeypatch):
    monkeypatch.setattr(udp_bind, "build_application", _failing_build(errno.EACCES))
    with pytest.raises(OSError):
        udp_bind.build_on_loopback(session_factory, 1, "127.0.0.2/8")
