"""The one-device backup file carries that device's BACnet connectors."""
from __future__ import annotations

import json

import pytest

from bacpack.config_io import apply_device_restore, export_device_backup
from bacpack.config_schemas import ConfigImportError
from bacpack.db import lock_both_files
from bacpack.models import Connector, Device
from tests.test_config_connectors import _bacnet_connector, _device, _http_connector

THREE = (("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2"), ("analog-value", 3, "AV-3"))


def _two_devices(session):
    one = _device(session, 599001, objects=THREE)
    two = _device(session, 599002, objects=THREE)
    _bacnet_connector(session, one, name="Room")
    _bacnet_connector(session, two, name="Other")
    _http_connector(session, one, instance=3)
    session.commit()
    return one, two


def _backup(session, device) -> dict:
    return json.loads(export_device_backup(device))


def _restore(session, device, document) -> dict:
    lock_both_files(session)
    summary = apply_device_restore(session, device, json.dumps(document).encode("utf-8"))
    session.commit()
    return summary


def _names(session) -> dict[str, int | None]:
    session.expire_all()
    return {c.name: c.device_id for c in session.query(Connector)}


def test_backup_carries_only_this_devices_bacnet_connectors(session):
    one, _ = _two_devices(session)

    document = _backup(session, one)

    assert [c["name"] for c in document["connectors"]] == ["Room"]
    assert document["connectors"][0]["device"] == 599001
    assert "settings" not in document
    assert "Weather" not in json.dumps(document) and "Other" not in json.dumps(document)


def test_a_device_without_connectors_backs_up_an_empty_list(session):
    device = _device(session)
    assert _backup(session, device)["connectors"] == []


def test_restore_creates_and_updates_bacnet_connectors(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["connectors"][0]["remote_device_instance"] = 18
    document["connectors"].append(
        {
            "name": "Room 2",
            "type": "bacnet",
            "device": 599001,
            "remote_device_instance": 19,
            "mappings": [{"object": "analog-value:2", "remote_object_type": "analog-input", "remote_object_instance": 4}],
            "write_mappings": [],
        }
    )

    summary = _restore(session, one, document)

    assert summary["connectors_created"] == ["Room 2"]
    assert summary["connectors_updated"] == ["Room"]
    assert "created" in summary and "updated" in summary and "deleted" in summary
    session.expire_all()
    rows = {c.name: c for c in session.query(Connector)}
    assert rows["Room"].remote_device_instance == 18
    assert rows["Room 2"].device_id == 599001
    assert [m.remote_object_instance for m in rows["Room 2"].mappings] == [4]


def test_restore_points_connectors_at_the_restoring_device(session):
    one, two = _two_devices(session)
    document = _backup(session, one)
    document["connectors"][0]["device"] = 599002
    document["connectors"].append(
        {"name": "Moved", "type": "bacnet", "device": 599002, "remote_device_instance": 30, "mappings": []}
    )

    _restore(session, one, document)

    assert _names(session) == {"Room": 599001, "Other": 599002, "Weather": None, "Moved": 599001}


def test_restore_refuses_an_http_connector(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["connectors"].append(
        {"name": "New", "type": "http-poll", "url": "https://example.invalid/x", "mappings": []}
    )

    with pytest.raises(ConfigImportError, match="a restore file carries only this device's BACnet connectors"):
        _restore(session, one, document)


def test_restore_refuses_settings(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["settings"] = {"trend_retention_days": 5}

    with pytest.raises(ConfigImportError, match="a restore file doesn't carry server settings"):
        _restore(session, one, document)


def test_restore_refuses_a_connector_name_used_by_another_device(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["connectors"][0]["name"] = "Other"

    with pytest.raises(ConfigImportError) as exc:
        _restore(session, one, document)
    assert "connector 'Other' is a bacnet connector on device 599002 here" in str(exc.value)


def test_restore_leaves_a_connector_missing_from_the_file_alone(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["connectors"] = []

    summary = _restore(session, one, document)

    assert summary["connectors_created"] == [] and summary["connectors_updated"] == []
    assert _names(session) == {"Room": 599001, "Other": 599002, "Weather": None}


def test_restore_without_a_connectors_key_leaves_connectors_alone(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    del document["connectors"]

    _restore(session, one, document)

    assert _names(session) == {"Room": 599001, "Other": 599002, "Weather": None}


def test_restore_refuses_a_mapping_onto_an_object_the_file_drops(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["devices"][0]["objects"] = [o for o in document["devices"][0]["objects"] if o["instance_number"] != 1]

    with pytest.raises(ConfigImportError, match="connector 'Room': no object 599001 analog-value:1 after this import"):
        _restore(session, one, document)


def test_restore_checks_other_devices_connectors_against_the_new_objects(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)
    document["devices"][0]["objects"][2]["simulation"] = {"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600}

    with pytest.raises(ConfigImportError, match="599001 analog-value:3 is fed by 'Weather' and also has a simulator"):
        _restore(session, one, document)


def test_a_backup_restores_onto_the_device_it_came_from_unchanged(session):
    one, _ = _two_devices(session)
    document = _backup(session, one)

    _restore(session, one, document)

    session.expire_all()
    assert _backup(session, session.get(Device, 599001))["connectors"] == document["connectors"]
