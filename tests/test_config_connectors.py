from __future__ import annotations

import pytest

from bacpack.config_connectors import (
    SETTINGS_DEFAULTS,
    export_connectors,
    export_settings,
    object_label,
)
from bacpack.models import (
    BacnetObject,
    Connector,
    ConnectorMapping,
    ConnectorWriteMapping,
    Device,
    ServerSettings,
)


def _device(session, device_id=599001, objects=((("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2")))):
    device = Device(id=device_id, name=f"Dev-{device_id}")
    for object_type, instance, name in objects:
        device.objects.append(BacnetObject(object_type=object_type, instance_number=instance, name=name))
    session.add(device)
    session.flush()
    return device


def _obj(device, object_type, instance):
    return next(o for o in device.objects if (o.object_type, o.instance_number) == (object_type, instance))


def _http_connector(session, device, instance=1, **overrides):
    connector = Connector(
        name="Weather",
        type="http-poll",
        url="https://example.invalid/forecast",
        poll_interval_seconds=300,
        auth_type="basic",
        auth_username="me",
        auth_password="hunter2",
        auth_token="tok",
        headers={"X-Api-Key": "secret"},
        **overrides,
    )
    connector.mappings.append(
        ConnectorMapping(
            object=_obj(device, "analog-value", instance),
            json_path="current.temp",
            scale=2.0,
            offset=1.0,
            value_map={"open": 1},
        )
    )
    session.add(connector)
    session.flush()
    return connector


def _bacnet_connector(session, device, name="Room"):
    connector = Connector(
        name=name,
        type="bacnet",
        url="",
        device_id=device.id,
        remote_device_instance=17,
        remote_address="192.168.1.17",
        poll_interval_seconds=45,
        timeout_seconds=7,
        rewrite_interval_seconds=30,
    )
    connector.mappings.append(
        ConnectorMapping(
            object=_obj(device, "analog-value", 1),
            remote_object_type="analog-input",
            remote_object_instance=1,
            remote_property="present-value",
            direction="read",
        )
    )
    connector.write_mappings.append(
        ConnectorWriteMapping(
            object=_obj(device, "analog-value", 2),
            remote_object_type="analog-value",
            remote_object_instance=2,
            remote_property="present-value",
            priority=9,
        )
    )
    session.add(connector)
    session.flush()
    return connector


def test_object_label():
    assert object_label(599001, "analog-value", 1) == "599001 analog-value:1"


def test_export_connectors_http_shape(session):
    device = _device(session)
    _http_connector(session, device)

    (exported,) = export_connectors(session.query(Connector).all())

    assert list(exported) == [
        "name", "type", "enabled", "url", "poll_interval_seconds", "timeout_seconds",
        "response_format", "auth_type", "auth_username", "headers", "verify_tls", "mappings",
    ]
    assert exported["headers"] == {"X-Api-Key": None}
    assert exported["auth_username"] == "me"
    assert exported["mappings"] == [
        {
            "device": 599001,
            "object": "analog-value:1",
            "json_path": "current.temp",
            "scale": 2.0,
            "offset": 1.0,
            "value_map": {"open": 1},
        }
    ]
    assert "secret" not in str(exported) and "hunter2" not in str(exported) and "tok" not in str(exported)


def test_export_connectors_bacnet_shape(session):
    device = _device(session)
    _bacnet_connector(session, device)

    (exported,) = export_connectors(session.query(Connector).all())

    assert list(exported) == [
        "name", "type", "enabled", "device", "remote_device_instance", "remote_address",
        "poll_interval_seconds", "timeout_seconds", "rewrite_interval_seconds", "mappings", "write_mappings",
    ]
    assert exported["device"] == 599001
    assert exported["poll_interval_seconds"] == 45
    assert exported["timeout_seconds"] == 7
    assert exported["remote_device_instance"] == 17
    assert exported["mappings"] == [
        {
            "object": "analog-value:1",
            "remote_object_type": "analog-input",
            "remote_object_instance": 1,
            "remote_property": "present-value",
            "remote_array_index": None,
            "scale": 1.0,
            "offset": 0.0,
            "value_map": None,
            "direction": "read",
        }
    ]
    assert exported["write_mappings"] == [
        {
            "object": "analog-value:2",
            "remote_object_type": "analog-value",
            "remote_object_instance": 2,
            "remote_property": "present-value",
            "remote_array_index": None,
            "priority": 9,
        }
    ]


def test_export_connectors_sorts_by_name_and_mappings_by_point(session):
    device = _device(session, objects=(("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2"), ("analog-value", 3, "AV-3")))
    connector = _bacnet_connector(session, device, name="Zeta")
    for instance, index in ((3, None), (3, 2), (3, 0)):
        connector.write_mappings.append(
            ConnectorWriteMapping(
                object=_obj(device, "analog-value", 3),
                remote_object_type="analog-output",
                remote_object_instance=instance,
                remote_property="priority-array",
                remote_array_index=index,
            )
        )
    _http_connector(session, device, instance=3)

    exported = export_connectors(session.query(Connector).all())

    assert [c["name"] for c in exported] == ["Weather", "Zeta"]
    points = [(w["remote_object_type"], w["remote_array_index"]) for w in exported[1]["write_mappings"]]
    assert points == [("analog-output", None), ("analog-output", 0), ("analog-output", 2), ("analog-value", None)]


def test_export_settings(session):
    settings = session.get(ServerSettings, 1)
    settings.trend_retention_days = 30
    settings.log_retention_days = None
    settings.trend_soft_limit_mb = 64

    assert export_settings(settings) == {
        "trend_retention_days": 30,
        "log_retention_days": None,
        "trend_soft_limit_mb": 64,
    }


def test_settings_defaults_are_the_model_defaults():
    assert SETTINGS_DEFAULTS == {"trend_retention_days": 90, "log_retention_days": 14, "trend_soft_limit_mb": None}
    assert ServerSettings.__table__.c.trend_retention_days.default.arg == 90
    assert ServerSettings.__table__.c.log_retention_days.default.arg == 14


def test_export_config_adds_the_keys_only_when_given(session):
    from bacpack.config_io import export_config

    device = _device(session)
    assert set(export_config([device])) == {"version", "bacpack_version", "devices"}
    document = export_config([device], connectors=[], settings=ServerSettings(id=1))
    assert document["connectors"] == []
    assert set(document["settings"]) == set(SETTINGS_DEFAULTS)


# Columns a config file deliberately leaves out. A new column is exported or joins this list.
_NOT_EXPORTED = {
    Device: {
        "bacnet_password_hash": "a secret",
        "clock_offset_seconds": "runtime state",
        "created_at": "bookkeeping",
        "database_revision": "runtime state a restore must not set",
        "dcc_state": "runtime state",
        "last_restore_time": "runtime state",
    },
    BacnetObject: {"id": "row key", "device_id": "the device it sits under"},
    Connector: {"id": "row key", "auth_password": "a secret", "auth_token": "a secret"},
    ConnectorMapping: {"id": "row key", "connector_id": "the connector it sits under", "mode": "always poll"},
    ConnectorWriteMapping: {"id": "row key", "connector_id": "the connector it sits under"},
}
# The file names the owning device and the fed object differently from the column.
_EXPORTED_AS = {"device_id": "device", "object_id": "object"}


@pytest.mark.parametrize("model", list(_NOT_EXPORTED), ids=lambda m: m.__name__)
def test_every_column_is_exported_or_named_as_left_out(session, model):
    from bacpack.config_io import export_config

    device = _device(session, objects=(("analog-value", n, f"AV-{n}") for n in (1, 2, 3)))
    http = _http_connector(session, device, instance=3)
    bacnet = _bacnet_connector(session, device)
    document = export_config([device], connectors=[http, bacnet])
    exported_device = document["devices"][0]
    by_type = {c["type"]: c for c in document["connectors"]}
    keys = {
        Device: set(exported_device),
        BacnetObject: set(exported_device["objects"][0]),
        Connector: set(by_type["http-poll"]) | set(by_type["bacnet"]),
        ConnectorMapping: set(by_type["http-poll"]["mappings"][0]) | set(by_type["bacnet"]["mappings"][0]),
        ConnectorWriteMapping: set(by_type["bacnet"]["write_mappings"][0]),
    }[model]

    wanted = {_EXPORTED_AS.get(c, c) for c in model.__table__.columns.keys() if c not in _NOT_EXPORTED[model]}
    assert wanted <= keys, f"{model.__name__} columns missing from the export: {sorted(wanted - keys)}"


def test_listen_address_is_not_in_a_config_export_or_import(session):
    import json

    import pytest

    from bacpack.config_connectors import ConfigImportError, parse_settings
    from bacpack.config_io import export_config
    from bacpack.models import get_or_create_settings

    settings = get_or_create_settings(session)
    document = json.dumps(export_config([_device(session)], connectors=[], settings=settings)).lower()
    for word in ("listen", "tls", "certificate"):
        assert word not in document
    assert set(SETTINGS_DEFAULTS) == {"trend_retention_days", "log_retention_days", "trend_soft_limit_mb"}
    with pytest.raises(ConfigImportError, match="web_listen_address"):
        parse_settings({"web_listen_address": "127.0.0.1"})
    assert settings.web_listen_address == "0.0.0.0"
