from __future__ import annotations

import json

import pytest

from bacpack import trending
from bacpack.config_schemas import ConfigImportError
from bacpack.config_io import (
    export_config,
    parse_config_document,
    plan_digest,
    sync_config,
)
from bacpack.db import lock_both_files
from bacpack.models import (
    Connector,
    ConnectorMapping,
    ConnectorWriteMapping,
    Device,
    ServerSettings,
)
from tests.test_config_connectors import _bacnet_connector, _device, _http_connector, _obj

THREE = (("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2"), ("analog-value", 3, "AV-3"))


def _export(session) -> dict:
    document = export_config(
        session.query(Device).order_by(Device.id).all(),
        connectors=session.query(Connector).all(),
        settings=session.get(ServerSettings, 1),
    )
    return json.loads(json.dumps(document))


def _import(session, document: dict, *, dry_run: bool = False) -> dict:
    parsed = parse_config_document(document)
    if not dry_run:
        lock_both_files(session)
    result = sync_config(
        session, parsed.devices, dry_run=dry_run, connectors=parsed.connectors, settings=parsed.settings
    )
    if not dry_run:
        session.commit()
    return result


def _seed(session):
    """599001 with a polled HTTP connector (credentials set) and a BACnet connector."""
    device = _device(session, objects=THREE)
    http = _http_connector(session, device, instance=3)
    bacnet = _bacnet_connector(session, device)
    session.commit()
    return device, http, bacnet


def _by_name(session, name) -> Connector:
    session.expire_all()
    return session.query(Connector).filter_by(name=name).one()


def test_reimporting_the_export_keeps_rows_and_credentials(session, monkeypatch):
    _seed(session)
    released = []
    monkeypatch.setattr("bacpack.config_connectors.release_http_mappings", lambda mappings: released.extend(mappings))
    ids = {
        "connectors": {c.name: c.id for c in session.query(Connector)},
        "mappings": sorted(m.id for m in session.query(ConnectorMapping)),
        "writes": sorted(m.id for m in session.query(ConnectorWriteMapping)),
    }

    result = _import(session, _export(session))

    assert result["connectors_updated"] == ["Room", "Weather"]
    assert result["connectors_created"] == []
    session.expire_all()
    assert {c.name: c.id for c in session.query(Connector)} == ids["connectors"]
    assert sorted(m.id for m in session.query(ConnectorMapping)) == ids["mappings"]
    assert sorted(m.id for m in session.query(ConnectorWriteMapping)) == ids["writes"]
    weather = _by_name(session, "Weather")
    assert (weather.auth_password, weather.auth_token) == ("hunter2", "tok")
    assert weather.headers == {"X-Api-Key": "secret"}
    assert released == []
    assert result["needs_credentials"] == {}


def test_dry_run_writes_no_connectors_or_settings(session):
    _seed(session)
    document = _export(session)
    document["settings"]["trend_retention_days"] = 7
    document["connectors"].append(
        {"name": "New", "type": "http-poll", "url": "https://example.invalid/x", "mappings": []}
    )
    for connector in document["connectors"]:
        if connector["name"] == "Weather":
            connector["poll_interval_seconds"] = 5
            connector["mappings"] = []

    result = _import(session, document, dry_run=True)

    assert result["connectors_created"] == ["New"]
    assert result["settings_changed"] is True
    assert not session.new and not session.dirty and not session.deleted
    session.rollback()
    assert [c.name for c in session.query(Connector).order_by(Connector.name)] == ["Room", "Weather"]
    assert session.query(ConnectorMapping).count() == 2
    assert session.get(ServerSettings, 1).trend_retention_days == 90
    assert _by_name(session, "Weather").poll_interval_seconds == 300


def test_import_creates_connectors_with_empty_credentials(session):
    _device(session, objects=THREE)
    session.commit()
    document = _export(session)
    document["connectors"] = [
        {
            "name": "Weather",
            "type": "http-poll",
            "url": "https://example.invalid/forecast",
            "auth_type": "basic",
            "auth_username": "me",
            "headers": {"X-Api-Key": None},
            "mappings": [{"device": 599001, "object": "analog-value:1", "json_path": "a"}],
        },
        {
            "name": "Room",
            "type": "bacnet",
            "device": 599001,
            "remote_device_instance": 17,
            "poll_interval_seconds": 90,
            "timeout_seconds": 12,
            "mappings": [{"object": "analog-value:2", "remote_object_type": "analog-input", "remote_object_instance": 1}],
            "write_mappings": [{"object": "analog-value:3", "remote_object_type": "analog-value", "remote_object_instance": 4, "priority": 5}],
        },
    ]

    result = _import(session, document)

    assert result["connectors_created"] == ["Weather", "Room"]
    assert result["needs_credentials"] == {"Weather": ["password", "header X-Api-Key"]}
    weather = _by_name(session, "Weather")
    assert weather.auth_password is None and weather.auth_token is None
    assert weather.headers == {"X-Api-Key": ""}
    assert [(m.object.name, m.json_path) for m in weather.mappings] == [("AV-1", "a")]
    room = _by_name(session, "Room")
    assert room.device_id == 599001 and room.remote_device_instance == 17
    assert (room.poll_interval_seconds, room.timeout_seconds) == (90, 12)
    assert room.headers is None
    assert [(m.object.name, m.remote_object_instance, m.direction, m.remote_property) for m in room.mappings] == [
        ("AV-2", 1, "read", "present-value")
    ]
    assert [(m.object.name, m.priority) for m in room.write_mappings] == [("AV-3", 5)]
    assert room.runtime is not None


def test_import_removes_a_mapping_missing_from_the_file_and_releases_an_http_one(session, monkeypatch):
    _seed(session)
    released = []
    monkeypatch.setattr("bacpack.config_connectors.release_http_mappings", lambda mappings: released.extend(mappings))
    document = _export(session)
    for connector in document["connectors"]:
        connector["mappings"] = []
        connector.get("write_mappings") and connector.update(write_mappings=[])

    result = _import(session, document)

    assert result["connector_mappings"]["Weather"]["deleted"] == ["599001 analog-value:3"]
    assert result["connector_mappings"]["Room"]["deleted"] == [
        "599001 analog-value:1",
        "write analog-value,2 present-value",
    ]
    assert session.query(ConnectorMapping).count() == 0
    assert session.query(ConnectorWriteMapping).count() == 0
    assert len(released) == 1 and released[0].json_path == "current.temp"


def test_import_updates_a_write_mapping_in_place(session):
    _seed(session)
    write_id = session.query(ConnectorWriteMapping).one().id
    document = _export(session)
    room = next(c for c in document["connectors"] if c["name"] == "Room")
    room["write_mappings"][0]["priority"] = 3

    result = _import(session, document)

    assert result["connector_mappings"]["Room"]["updated"] == ["599001 analog-value:1", "write analog-value,2 present-value"]
    session.expire_all()
    write = session.query(ConnectorWriteMapping).one()
    assert (write.id, write.priority) == (write_id, 3)


def test_import_moves_a_mapping_between_connectors(session):
    _seed(session)
    document = _export(session)
    room = next(c for c in document["connectors"] if c["name"] == "Room")
    weather = next(c for c in document["connectors"] if c["name"] == "Weather")
    room["mappings"] = [{**weather["mappings"][0], "object": "analog-value:3", **{"remote_object_type": "analog-input", "remote_object_instance": 9}}]
    for key in ("device", "json_path"):
        room["mappings"][0].pop(key)
    room["mappings"][0].update(remote_property="present-value", remote_array_index=None, direction="read")
    weather["mappings"] = []

    _import(session, document)

    session.expire_all()
    assert [m.object.name for m in _by_name(session, "Room").mappings] == ["AV-3"]
    assert _by_name(session, "Weather").mappings == []


def test_import_drops_a_header_missing_from_the_file(session):
    _seed(session)
    weather = _by_name(session, "Weather")
    weather.headers = {"X-Api-Key": "secret", "X-Other": "other"}
    session.commit()
    document = _export(session)
    next(c for c in document["connectors"] if c["name"] == "Weather")["headers"] = {"X-Other": None}

    _import(session, document)

    assert _by_name(session, "Weather").headers == {"X-Other": "other"}


def test_import_disabling_an_http_connector_releases_its_mappings(session, monkeypatch):
    _seed(session)
    released = []
    monkeypatch.setattr("bacpack.config_connectors.release_http_mappings", lambda mappings: released.extend(mappings))
    document = _export(session)
    next(c for c in document["connectors"] if c["name"] == "Weather")["enabled"] = False

    _import(session, document)

    assert len(released) == 1


def test_import_sets_settings_without_pruning(session, monkeypatch):
    _device(session)
    session.commit()
    pruned = []
    monkeypatch.setattr(trending, "prune_global_to_limit", lambda s, limit, **kw: pruned.append(limit) or 0)
    document = _export(session)
    document["settings"] = {"trend_retention_days": 30, "log_retention_days": None, "trend_soft_limit_mb": 8}

    result = _import(session, document)

    assert result["settings_changed"] is True
    settings = session.get(ServerSettings, 1)
    assert (settings.trend_retention_days, settings.log_retention_days, settings.trend_soft_limit_mb) == (30, None, 8)
    assert pruned == []


def test_import_without_settings_leaves_them_alone(session):
    _device(session)
    settings = session.get(ServerSettings, 1)
    settings.trend_retention_days = 11
    session.commit()
    document = _export(session)
    del document["settings"]

    result = _import(session, document)

    assert result["settings_changed"] is False
    assert session.get(ServerSettings, 1).trend_retention_days == 11


def test_reimporting_keep_forever_retention_keeps_it(session):
    _device(session)
    settings = session.get(ServerSettings, 1)
    settings.trend_retention_days = None
    settings.log_retention_days = None
    session.commit()

    result = _import(session, _export(session))

    assert result["settings_changed"] is False
    settings = session.get(ServerSettings, 1)
    assert (settings.trend_retention_days, settings.log_retention_days) == (None, None)


def test_import_with_identical_settings_reports_no_change(session):
    _device(session)
    session.commit()
    assert _import(session, _export(session))["settings_changed"] is False


def test_import_without_connectors_key_leaves_connectors_alone(session):
    _seed(session)
    document = _export(session)
    del document["connectors"]

    result = _import(session, document)

    assert result["connectors_created"] == result["connectors_updated"] == []
    assert session.query(Connector).count() == 2
    assert session.query(ConnectorMapping).count() == 2


def test_import_refuses_a_simulator_on_a_fed_object_whatever_the_connector_list(session):
    _seed(session)
    document = _export(session)
    del document["connectors"]
    for row in document["devices"][0]["objects"]:
        if row["instance_number"] == 3:
            row["simulation"] = {"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600}

    with pytest.raises(ConfigImportError) as exc:
        _import(session, document, dry_run=True)
    assert "599001 analog-value:3 is fed by 'Weather' and also has a simulator" in str(exc.value)


def test_a_connector_goes_with_a_device_the_file_deletes(session):
    _seed(session)
    document = _export(session)
    document["devices"] = []
    document["connectors"] = []

    _import(session, document)

    session.expire_all()
    # The BACnet connector went with its device; the HTTP one is left alone,
    # without the mapping that went with the object.
    assert [(c.name, len(c.mappings)) for c in session.query(Connector)] == [("Weather", 0)]


def test_round_trip_into_an_empty_install(session):
    _seed(session)
    session.get(ServerSettings, 1).trend_retention_days = 30
    session.commit()
    original = _export(session)
    _import(session, {"version": 1, "devices": [], "connectors": []})
    for connector in session.query(Connector).all():
        session.delete(connector)
    session.commit()
    assert session.query(Connector).count() == 0

    _import(session, original)

    assert _export(session) == original


def _room_entry(document, **overrides):
    (room,) = [c for c in document["connectors"] if c["name"] == "Room"]
    room.pop("poll_interval_seconds", None)
    room.pop("timeout_seconds", None)
    room.update(overrides)
    return room


def test_import_sets_a_bacnet_connectors_poll_interval_and_timeout(session):
    _seed(session)
    document = _export(session)
    _room_entry(document, poll_interval_seconds=90, timeout_seconds=12)

    _import(session, document)

    room = _by_name(session, "Room")
    assert (room.poll_interval_seconds, room.timeout_seconds) == (90, 12)


def test_import_without_the_intervals_sets_the_defaults(session):
    _seed(session)
    document = _export(session)
    assert _by_name(session, "Room").poll_interval_seconds == 45
    _room_entry(document)

    _import(session, document)

    room = _by_name(session, "Room")
    assert (room.poll_interval_seconds, room.timeout_seconds) == (30, 10)


def test_plan_digest_changes_when_a_connector_is_added(session):
    _seed(session)
    document = _export(session)
    before = plan_digest(_import(session, document, dry_run=True))
    assert before == plan_digest(_import(session, document, dry_run=True))
    document["connectors"].append(
        {"name": "New", "type": "http-poll", "url": "https://example.invalid/x", "mappings": []}
    )

    assert plan_digest(_import(session, document, dry_run=True)) != before


def test_plan_digest_ignores_credentials_and_settings(session):
    _seed(session)
    document = _export(session)
    before = plan_digest(_import(session, document, dry_run=True))
    document["settings"]["trend_retention_days"] = 5

    assert plan_digest(_import(session, document, dry_run=True)) == before


def test_sync_config_without_connectors_keeps_the_old_result_shape(session):
    _device(session)
    session.commit()
    parsed = parse_config_document(_export(session))
    result = sync_config(session, parsed.devices, dry_run=True)
    assert result["connectors_created"] == [] and result["connectors_updated"] == []
    assert result["connector_mappings"] == {} and result["needs_credentials"] == {}
    assert result["settings_changed"] is False
