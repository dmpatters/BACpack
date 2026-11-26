"""Building a device application on a loopback address for a real-wire test."""
from __future__ import annotations

import errno

import pytest

from bacpack.models import Device
from bacpack.object_builder import build_application


def build_on_loopback(session_factory, device_id, bind_address, **kwargs):
    """The device's application bound to `bind_address`. A busy port fails the
    test (something else, an installed service for one, holds it, and a skip
    would hide that the real-wire test never ran); only a missing loopback
    address (127.0.0.2 on a host without 127/8) skips."""
    with session_factory() as session:
        device = session.get(Device, device_id)
        try:
            return build_application(device, list(device.objects), bind_address=bind_address, **kwargs)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                pytest.fail(f"{bind_address} is in use by another process: {exc}")
            if exc.errno == errno.EADDRNOTAVAIL:
                pytest.skip(f"{bind_address} is not available here: {exc}")
            raise
