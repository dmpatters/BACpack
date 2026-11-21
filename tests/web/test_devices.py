"""Device CRUD: bind_address and vendor_name round-trips and validation."""
from __future__ import annotations

import pytest

from tests.web._helpers import _create_device, _create_object


# --- Devices API ---------------------------------------------------------


def test_device_crud(web_client):
    assert web_client.get("/api/devices").json() == []

    created = _create_device(web_client)
    assert created["id"] == 599001

    r = web_client.post("/api/devices", json={"id": 599001, "name": "dup"})
    assert r.status_code == 409

    r = web_client.put("/api/devices/599001", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    r = web_client.delete("/api/devices/599001")
    assert r.status_code == 204
    assert web_client.get("/api/devices").json() == []


def test_get_missing_device_404(web_client):
    assert web_client.get("/api/devices/1").status_code == 404


# --- Device bind_address --------------------------------------------------
# The per-device bind_address column used to be invisible to
# DeviceCreate/DeviceUpdate/_device_dict entirely -- every device fell back
# to the single global ServerSettings.bind_address, making concurrent
# multi-device hosting unreachable through BACpack's own API.


def test_device_create_and_update_bind_address_round_trip(web_client):
    r = web_client.post(
        "/api/devices",
        json={"id": 599001, "name": "Demo Device", "bind_address": "192.168.1.50/24"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["bind_address"] == "192.168.1.50/24"

    r = web_client.get("/api/devices/599001")
    assert r.json()["bind_address"] == "192.168.1.50/24"

    r = web_client.put("/api/devices/599001", json={"bind_address": "192.168.1.51/24"})
    assert r.status_code == 200
    assert r.json()["bind_address"] == "192.168.1.51/24"


def test_device_create_rejects_invalid_bind_address(web_client):
    r = web_client.post(
        "/api/devices",
        json={"id": 599001, "name": "Demo Device", "bind_address": "192.168.1.999/24"},
    )
    assert r.status_code == 400
    assert "bind_address" in r.json()["detail"]
    assert web_client.get("/api/devices").json() == []


def test_device_update_rejects_invalid_bind_address(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"bind_address": "192.168.1.999/24"})
    assert r.status_code == 400
    assert "bind_address" in r.json()["detail"]
    # Confirm it didn't half-apply.
    assert web_client.get("/api/devices/599001").json()["bind_address"] is None


# --- Device vendor_name -----------------------------------------------------
# Per-device vendorName so a client under test sees a configured vendor
# instead of bacpypes3's own class default ("BACpypes") -- see
# tests/test_device_object_identity.py for the bacpypes3-object-level coverage.


def test_vendor_name_round_trips_through_create_and_update(web_client):
    created = web_client.post(
        "/api/devices",
        json={"id": 599950, "name": "identity-test", "vendor_name": "Example Controls"},
    )
    assert created.status_code == 201
    assert created.json()["vendor_name"] == "Example Controls"

    updated = web_client.put("/api/devices/599950", json={"vendor_name": "Someone Else"})
    assert updated.status_code == 200
    assert updated.json()["vendor_name"] == "Someone Else"

    assert web_client.get("/api/devices/599950").json()["vendor_name"] == "Someone Else"


def test_vendor_name_is_optional_on_create(web_client):
    created = web_client.post("/api/devices", json={"id": 599951, "name": "no-vendor-name"})
    assert created.status_code == 201
    assert created.json()["vendor_name"] is None


def test_location_round_trips_through_create_and_update(web_client):
    created = web_client.post("/api/devices", json={"id": 599952, "name": "loc-test", "location": "Plant room"})
    assert created.status_code == 201
    assert created.json()["location"] == "Plant room"

    updated = web_client.put("/api/devices/599952", json={"location": "Roof"})
    assert updated.status_code == 200
    assert updated.json()["location"] == "Roof"

    assert web_client.get("/api/devices/599952").json()["location"] == "Roof"


def test_location_is_optional_on_create(web_client):
    created = web_client.post("/api/devices", json={"id": 599953, "name": "no-location"})
    assert created.status_code == 201
    assert created.json()["location"] is None


def test_backup_failure_timeout_round_trips_through_update(web_client):
    created = web_client.post("/api/devices", json={"id": 599954, "name": "bft-test"})
    assert created.json()["backup_failure_timeout"] == 60

    updated = web_client.put("/api/devices/599954", json={"backup_failure_timeout": 15})
    assert updated.status_code == 200
    assert updated.json()["backup_failure_timeout"] == 15
    assert web_client.get("/api/devices/599954").json()["backup_failure_timeout"] == 15


@pytest.mark.parametrize("bad", [0, 65536, None, -1])
def test_update_device_refuses_a_bad_backup_failure_timeout(web_client, bad):
    web_client.post("/api/devices", json={"id": 599955, "name": "bft-bad"})
    r = web_client.put("/api/devices/599955", json={"backup_failure_timeout": bad})
    assert r.status_code == 422
    assert web_client.get("/api/devices/599955").json()["backup_failure_timeout"] == 60


def test_update_device_rejects_explicit_null_name(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"name": None})
    assert r.status_code == 422


# --- vendor_identifier range ------------------------------------------------
# Vendor_Identifier is an Unsigned16; a negative one saved cleanly and then
# stopped the device from starting.


def test_create_device_rejects_out_of_range_vendor_identifier(web_client):
    for vendor_identifier in (-1, 65536):
        r = web_client.post(
            "/api/devices", json={"id": 599001, "name": "D", "vendor_identifier": vendor_identifier}
        )
        assert r.status_code == 422, r.text


def test_update_device_rejects_negative_or_null_vendor_identifier(web_client):
    _create_device(web_client)
    for vendor_identifier in (-1, None):
        r = web_client.put("/api/devices/599001", json={"vendor_identifier": vendor_identifier})
        assert r.status_code == 422, r.text


def test_device_rename_refuses_making_an_object_name_the_reserved_backup_file_name(web_client):
    # Renaming the device to "Plant" makes an existing object named
    # "Plant-config.json" collide with file,1 -- the device then can't start.
    _create_device(web_client)
    _create_object(web_client, name="Plant-config.json")
    r = web_client.put("/api/devices/599001", json={"name": "Plant"})
    assert r.status_code == 409
    assert "reserved" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"


def test_device_rename_refuses_an_object_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AHU-1")
    r = web_client.put("/api/devices/599001", json={"name": "AHU-1"})
    assert r.status_code == 409
    assert "device's own name" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_device_names_are_refused(web_client, name):
    # A blank name was saved, then its export wouldn't import.
    assert web_client.post("/api/devices", json={"id": 599001, "name": name}).status_code == 422
    _create_device(web_client)
    assert web_client.put("/api/devices/599001", json={"name": name}).status_code == 422


def test_device_rename_refuses_a_trend_log_name(web_client):
    # AV-1's trend log is "AV-1-Trend"; renaming the device to
    # that gave 200 and the device then failed to start.
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True)
    r = web_client.put("/api/devices/599001", json={"name": "AV-1-Trend"})
    assert r.status_code == 409
    assert "trend log" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["name"] == "Demo Device"


def test_device_rename_refuses_the_network_port_name(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"name": "NetworkPort-1"})
    assert r.status_code == 409
    assert "network port" in r.json()["detail"]


def test_device_names_are_trimmed_on_create_and_update(web_client):
    # Names never start or end with whitespace, on any save path.
    r = web_client.post("/api/devices", json={"id": 599001, "name": "  Demo Device "})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Demo Device"
    r = web_client.put("/api/devices/599001", json={"name": " AHU Panel\t"})
    assert r.status_code == 200, r.text
    assert web_client.get("/api/devices/599001").json()["name"] == "AHU Panel"


def test_a_slow_device_delete_does_not_hold_up_other_requests(web_client, monkeypatch):
    import threading

    from bacpack import trending

    _create_device(web_client)
    _create_object(web_client)
    entered, release = threading.Event(), threading.Event()
    real = trending.release_trend_samples_for_objects

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(trending, "release_trend_samples_for_objects", blocked)
    deleted = {}
    deleter = threading.Thread(target=lambda: deleted.update(r=web_client.delete("/api/devices/599001")))
    deleter.start()
    try:
        assert entered.wait(5)
        answered = {}
        reader = threading.Thread(target=lambda: answered.update(r=web_client.get("/api/version")))
        reader.start()
        reader.join(3)
        assert "r" in answered and answered["r"].status_code == 200
    finally:
        release.set()
        deleter.join(10)
    assert deleted["r"].status_code == 204
    assert web_client.get("/api/devices/599001").status_code == 404


# --- Network role and BBMD links --------------------------------------------


def _two_devices_on_different_ports(web_client):
    _create_device(web_client, 599001, "D")
    _create_device(web_client, 599002, "E")
    web_client.put("/api/devices/599001", json={"bind_address": "192.168.1.50/24:47810"})
    web_client.put("/api/devices/599002", json={"bind_address": "192.168.1.50/24:47811"})


def test_a_new_device_is_normal(web_client):
    created = _create_device(web_client)
    assert created["network_role"] == "normal"
    assert created["foreign_bbmd"] is None and created["foreign_ttl"] == 60
    assert created["bbmd_peers"] is None and created["bbmd_links"] == []


def test_a_bbmd_with_links_and_typed_peers_round_trips(web_client):
    _two_devices_on_different_ports(web_client)
    r = web_client.put(
        "/api/devices/599001",
        json={"network_role": "bbmd", "bbmd_links": [599002], "bbmd_peers": ["10.0.0.9:47808"]},
    )
    assert r.status_code == 200, r.text
    for body in (r.json(), web_client.get("/api/devices/599001").json(), web_client.get("/api/devices").json()[0]):
        assert body["network_role"] == "bbmd"
        assert body["bbmd_links"] == [599002]
        assert body["bbmd_peers"] == ["10.0.0.9:47808"]
    # One-way: the linked device is untouched.
    assert web_client.get("/api/devices/599002").json()["network_role"] == "normal"


def test_links_can_be_replaced_and_cleared(web_client):
    _two_devices_on_different_ports(web_client)
    _create_device(web_client, 599003, "F")
    web_client.put("/api/devices/599003", json={"bind_address": "192.168.1.50/24:47812"})
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002, 599003]})

    r = web_client.put("/api/devices/599001", json={"bbmd_links": [599003, 599002]})
    assert r.status_code == 200 and r.json()["bbmd_links"] == [599002, 599003]
    r = web_client.put("/api/devices/599001", json={"bbmd_links": [599003]})
    assert r.json()["bbmd_links"] == [599003]
    r = web_client.put("/api/devices/599001", json={"bbmd_links": []})
    assert r.json()["bbmd_links"] == []


def test_a_device_can_be_created_as_a_foreign_device(web_client):
    r = web_client.post(
        "/api/devices",
        json={"id": 599001, "name": "D", "network_role": "foreign", "foreign_bbmd": "10.0.0.5:47808", "foreign_ttl": 120},
    )
    assert r.status_code == 201, r.text
    assert (r.json()["network_role"], r.json()["foreign_bbmd"], r.json()["foreign_ttl"]) == ("foreign", "10.0.0.5:47808", 120)


def test_creating_a_foreign_device_without_its_bbmd_is_refused(web_client):
    r = web_client.post("/api/devices", json={"id": 599001, "name": "D", "network_role": "foreign"})
    assert r.status_code == 400
    assert web_client.get("/api/devices/599001").status_code == 404


def test_the_role_ignores_case(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"network_role": "Foreign", "foreign_bbmd": "10.0.0.5"})
    assert r.status_code == 200, r.text
    assert r.json()["network_role"] == "foreign"


def test_a_link_to_itself_is_refused(web_client):
    _two_devices_on_different_ports(web_client)
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599001]})
    assert r.status_code == 400 and "itself" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["network_role"] == "normal"


def test_a_link_to_a_foreign_device_is_refused_naming_it(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599002", json={"network_role": "foreign", "foreign_bbmd": "10.0.0.5"})
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    assert r.status_code == 400
    assert "E (599002) is a foreign device" in r.json()["detail"]


def test_a_link_to_a_device_on_the_same_subnet_and_port_is_refused(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599002", json={"bind_address": "192.168.1.51/24:47810"})
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    assert r.status_code == 400 and "already on this device's network" in r.json()["detail"]


def test_a_link_to_a_missing_device_is_refused(web_client):
    _two_devices_on_different_ports(web_client)
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [123]})
    assert r.status_code == 400 and "123" in r.json()["detail"]


def test_bad_foreign_bbmd_and_bad_peer_are_refused(web_client):
    _create_device(web_client)
    r = web_client.put("/api/devices/599001", json={"network_role": "foreign", "foreign_bbmd": "not-a-host"})
    assert r.status_code == 400 and "foreign_bbmd" in r.json()["detail"]
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_peers": ["10.0.0.9", "not-a-peer"]})
    assert r.status_code == 400 and "bbmd_peers" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["network_role"] == "normal"


def test_a_bbmd_peer_with_a_prefix_is_refused_on_update_and_create(web_client):
    _create_device(web_client)
    peers = {"network_role": "bbmd", "bbmd_peers": ["10.0.0.5/24:47808"]}
    r = web_client.put("/api/devices/599001", json=peers)
    assert r.status_code == 400 and "/prefix" in r.json()["detail"]
    assert web_client.get("/api/devices/599001").json()["network_role"] == "normal"
    r = web_client.post("/api/devices", json={"id": 599777, "name": "peered", **peers})
    assert r.status_code == 400 and "/prefix" in r.json()["detail"]
    assert web_client.get("/api/devices/599777").status_code == 404
    r = web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_peers": ["10.0.0.5:47808"]})
    assert r.status_code == 200


def test_the_foreign_ttl_is_range_checked_and_the_role_not_nullable(web_client):
    _create_device(web_client)
    for ttl in (0, -5, 65536):
        assert web_client.put("/api/devices/599001", json={"foreign_ttl": ttl}).status_code == 422
    for field in ("network_role", "foreign_ttl", "bbmd_links"):
        assert web_client.put("/api/devices/599001", json={field: None}).status_code == 422, field


def test_a_linked_device_cant_become_a_foreign_device(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    r = web_client.put("/api/devices/599002", json={"network_role": "foreign", "foreign_bbmd": "10.0.0.5"})
    assert r.status_code == 400
    assert "D (599001)" in r.json()["detail"] and "foreign device" in r.json()["detail"]
    assert web_client.get("/api/devices/599002").json()["network_role"] == "normal"


def test_a_linked_device_cant_move_onto_its_bbmds_network(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    r = web_client.put("/api/devices/599002", json={"bind_address": "192.168.1.51/24:47810"})
    assert r.status_code == 400
    assert "D (599001)" in r.json()["detail"] and "already on this device's network" in r.json()["detail"]
    assert web_client.get("/api/devices/599002").json()["bind_address"] == "192.168.1.50/24:47811"


def test_a_linked_device_can_still_move_to_another_free_port(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    r = web_client.put("/api/devices/599002", json={"bind_address": "192.168.1.50/24:47812"})
    assert r.status_code == 200, r.text


def test_deleting_a_linked_device_removes_it_from_the_link_list(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    assert web_client.delete("/api/devices/599002").status_code == 204
    assert web_client.get("/api/devices/599001").json()["bbmd_links"] == []


def test_a_bbmds_links_survive_its_role_changing_away(web_client):
    _two_devices_on_different_ports(web_client)
    web_client.put("/api/devices/599001", json={"network_role": "bbmd", "bbmd_links": [599002]})
    r = web_client.put("/api/devices/599001", json={"network_role": "normal"})
    assert r.status_code == 200 and r.json()["bbmd_links"] == [599002]



def test_device_rename_refuses_a_fixed_trend_log_name(web_client):
    _create_device(web_client)
    _create_object(web_client, name="AV-1", trended=True, trend_log_name="History")
    r = web_client.put("/api/devices/599001", json={"name": "History"})
    assert r.status_code == 409
    assert "trend log" in r.json()["detail"]
    # The default name is no longer the log's, so it is free.
    assert web_client.put("/api/devices/599001", json={"name": "AV-1-Trend"}).status_code == 200
