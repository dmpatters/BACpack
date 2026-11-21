"""GET /api/devices/{id}/port-suggestion. Probes
bind real UDP ports, so this file is in UDP_TEST_FILES."""
from __future__ import annotations

import pytest

from tests.web._helpers import _create_device

@pytest.fixture
def _free_range(monkeypatch):
    # Away from the BACnet ports other xdist workers serve on.
    from bacpack import free_port

    monkeypatch.setattr(free_port, "FIRST_PORT", 52610)
    monkeypatch.setattr(free_port, "LAST_PORT", 52619)


def test_the_suggestion_keeps_the_devices_prefix_and_changes_the_port(web_client, _free_range):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bind_address": "127.0.0.1/8"})
    r = web_client.get("/api/devices/599001/port-suggestion")
    assert r.status_code == 200
    assert r.json() == {"bind_address": "127.0.0.1/8:52610", "port": 52610}


def test_the_suggestion_without_a_prefix_has_none(web_client, _free_range):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bind_address": "127.0.0.1:47808"})
    assert web_client.get("/api/devices/599001/port-suggestion").json() == {
        "bind_address": "127.0.0.1:52610",
        "port": 52610,
    }


def test_the_suggestion_skips_ports_other_devices_use_on_that_host(web_client, _free_range):
    _create_device(web_client, 599001, "A")
    _create_device(web_client, 599002, "B")
    _create_device(web_client, 599003, "C")
    web_client.put("/api/devices/599001", json={"bind_address": "127.0.0.1/8"})
    web_client.put("/api/devices/599002", json={"bind_address": "127.0.0.1/8:52610"})
    # Another host's port is no concern.
    web_client.put("/api/devices/599003", json={"bind_address": "127.0.0.2/8:52611"})
    assert web_client.get("/api/devices/599001/port-suggestion").json()["port"] == 52611


def test_a_device_on_the_server_default_gets_the_defaults_host_and_prefix(web_client, _free_range):
    _create_device(web_client)
    _create_device(web_client, 599002, "B")
    web_client.put("/api/settings/network", json={"bind_address": "127.0.0.1/8"})
    # B uses the default too: its port counts as taken on that host.
    web_client.put("/api/devices/599002", json={"bind_address": "127.0.0.1/8:52610"})
    assert web_client.get("/api/devices/599001/port-suggestion").json() == {
        "bind_address": "127.0.0.1/8:52611",
        "port": 52611,
    }


def test_a_device_with_no_bind_address_anywhere_has_no_suggestion(web_client, _free_range):
    _create_device(web_client)
    assert web_client.get("/api/devices/599001/port-suggestion").json() == {"bind_address": None, "port": None}


def test_a_host_this_machine_does_not_have_has_no_suggestion(web_client, _free_range):
    _create_device(web_client)
    web_client.put("/api/devices/599001", json={"bind_address": "203.0.113.5/24"})
    assert web_client.get("/api/devices/599001/port-suggestion").json() == {"bind_address": None, "port": None}


def test_every_port_taken_has_no_suggestion(web_client, monkeypatch):
    from bacpack import free_port

    monkeypatch.setattr(free_port, "FIRST_PORT", 52610)
    monkeypatch.setattr(free_port, "LAST_PORT", 52611)
    _create_device(web_client, 599001, "A")
    _create_device(web_client, 599002, "B")
    _create_device(web_client, 599003, "C")
    web_client.put("/api/devices/599001", json={"bind_address": "127.0.0.1/8"})
    web_client.put("/api/devices/599002", json={"bind_address": "127.0.0.1/8:52610"})
    web_client.put("/api/devices/599003", json={"bind_address": "127.0.0.1/8:52611"})
    assert web_client.get("/api/devices/599001/port-suggestion").json() == {"bind_address": None, "port": None}


def test_the_suggestion_for_a_missing_device_is_404(web_client):
    assert web_client.get("/api/devices/1/port-suggestion").status_code == 404
