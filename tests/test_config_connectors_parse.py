from __future__ import annotations

import pytest

from bacpack.config_connectors import SETTINGS_DEFAULTS, export_connectors, parse_connectors, parse_settings
from bacpack.config_io import parse_config, parse_config_document
from bacpack.config_schemas import ConfigImportError
from bacpack.models import Connector
from tests.test_config_connectors import _bacnet_connector, _device, _http_connector


def _http(**overrides):
    return {
        "name": "Weather",
        "type": "http-poll",
        "url": "https://example.invalid/forecast",
        "mappings": [{"device": 599001, "object": "analog-value:1", "json_path": "a.b"}],
        **overrides,
    }


def _bacnet(**overrides):
    return {
        "name": "Room",
        "type": "bacnet",
        "device": 599001,
        "remote_device_instance": 17,
        "mappings": [
            {"object": "analog-value:1", "remote_object_type": "analog-input", "remote_object_instance": 1}
        ],
        "write_mappings": [
            {"object": "analog-value:2", "remote_object_type": "analog-value", "remote_object_instance": 2}
        ],
        **overrides,
    }


def _refused(raw, fragment):
    with pytest.raises(ConfigImportError) as exc:
        parse_connectors(raw)
    assert fragment in str(exc.value), str(exc.value)
    return str(exc.value)


def test_parse_round_trips_the_export(session):
    device = _device(
        session, objects=(("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2"), ("analog-value", 3, "AV-3"))
    )
    _http_connector(session, device, instance=3)
    _bacnet_connector(session, device)

    room, weather = parse_connectors(export_connectors(session.query(Connector).all()))

    assert weather == {
        "name": "Weather",
        "type": "http-poll",
        "enabled": True,
        "url": "https://example.invalid/forecast",
        "poll_interval_seconds": 300,
        "timeout_seconds": 10,
        "response_format": "json",
        "auth_type": "basic",
        "auth_username": "me",
        "headers": {"X-Api-Key": None},
        "verify_tls": True,
        "device_id": None,
        "remote_device_instance": None,
        "remote_address": None,
        "rewrite_interval_seconds": 60,
        "mappings": [
            {
                "object_key": (599001, "analog-value", 3),
                "json_path": "current.temp",
                "scale": 2.0,
                "offset": 1.0,
                "value_map": {"open": 1},
                "remote_object_type": None,
                "remote_object_instance": None,
                "remote_property": None,
                "remote_array_index": None,
                "direction": "read",
            }
        ],
        "write_mappings": [],
    }
    assert room["type"] == "bacnet"
    assert room["device_id"] == 599001
    assert room["url"] == ""
    assert room["remote_address"] == "192.168.1.17"
    assert room["poll_interval_seconds"] == 45 and room["timeout_seconds"] == 7
    assert room["headers"] == {}
    assert room["mappings"][0]["object_key"] == (599001, "analog-value", 1)
    assert room["mappings"][0]["remote_property"] == "present-value"
    assert room["mappings"][0]["json_path"] is None
    assert room["write_mappings"] == [
        {
            "object_key": (599001, "analog-value", 2),
            "remote_object_type": "analog-value",
            "remote_object_instance": 2,
            "remote_property": "present-value",
            "remote_array_index": None,
            "priority": 9,
        }
    ]


def test_parse_connectors_fills_defaults():
    (parsed,) = parse_connectors([_http(mappings=[])])
    assert parsed["enabled"] is True
    assert parsed["poll_interval_seconds"] == 30
    assert parsed["headers"] == {}
    assert parse_connectors([]) == []


def test_parse_accepts_a_whole_number_for_scale():
    mapping = {"device": 599001, "object": "analog-value:1", "json_path": "a", "scale": 2, "offset": 0}
    (parsed,) = parse_connectors([_http(mappings=[mapping])])
    assert parsed["mappings"][0]["scale"] == 2


def test_parse_refuses_a_non_list():
    _refused({"name": "x"}, "connectors: must be a list")


def test_parse_refuses_unknown_fields_including_secrets():
    message = _refused([_http(auth_password="hunter2")], "connectors[0] ('Weather'): unknown field 'auth_password'")
    assert "hunter2" not in message
    _refused([_http(auth_token="x")], "unknown field 'auth_token'")
    _refused([_bacnet(url="http://x")], "unknown field 'url'")
    _refused([_http(device=1)], "unknown field 'device'")


def test_parse_refuses_unknown_mapping_fields():
    _refused(
        [_http(mappings=[{"device": 1, "object": "analog-value:1", "json_path": "a", "bogus": 1}])],
        "connectors[0] ('Weather').mappings[0]: unknown field 'bogus'",
    )
    _refused(
        [
            _bacnet(
                mappings=[
                    {"device": 1, "object": "analog-value:1", "remote_object_type": "analog-input", "remote_object_instance": 1}
                ]
            )
        ],
        ".mappings[0]: unknown field 'device'",
    )
    _refused(
        [
            _bacnet(
                write_mappings=[
                    {"object": "analog-value:1", "remote_object_type": "analog-value", "remote_object_instance": 1, "scale": 2}
                ]
            )
        ],
        ".write_mappings[0]: unknown field 'scale'",
    )


def test_parse_refuses_a_header_value():
    _refused(
        [_http(headers={"X-Api-Key": "secret"})],
        "header values aren't carried in the file; set them on the Connectors page",
    )


def test_parse_accepts_header_names_with_null_values():
    (parsed,) = parse_connectors([_http(headers={"X-Api-Key": None})])
    assert parsed["headers"] == {"X-Api-Key": None}


def test_parse_refuses_a_duplicate_connector_name():
    _refused([_http(mappings=[]), _http(mappings=[])], "duplicate connector name 'Weather'")


def test_parse_refuses_a_blank_name():
    _refused([_http(name="  ")], "name")


def test_parse_refuses_http_without_a_url():
    raw = _http(mappings=[])
    del raw["url"]
    _refused([raw], "url is required for an http-poll connector")


def test_parse_a_bacnet_connector_without_the_intervals_takes_the_defaults():
    (room,) = parse_connectors([_bacnet()])

    assert (room["poll_interval_seconds"], room["timeout_seconds"]) == (30, 10)


@pytest.mark.parametrize("field", ["poll_interval_seconds", "timeout_seconds"])
def test_parse_refuses_a_bacnet_interval_out_of_range(field):
    _refused([_bacnet(**{field: 0})], field)


def test_parse_refuses_bacnet_without_a_device():
    raw = _bacnet()
    del raw["device"]
    _refused([raw], "device is required for a bacnet connector")


def test_parse_refuses_a_bacnet_connector_that_is_its_own_remote_device():
    _refused([_bacnet(remote_device_instance=599001)], "device 599001 can't be its own remote device")


@pytest.mark.parametrize("ref", ["analog-value", "analog-value:x", "nonsense:1", "analog-value:-1", 5, "analog-value:1:2"])
def test_parse_refuses_a_malformed_object_ref(ref):
    _refused(
        [_http(mappings=[{"device": 599001, "object": ref, "json_path": "a"}])],
        "object must be '<object-type>:<instance>'",
    )


def test_parse_accepts_an_object_type_in_another_case():
    (parsed,) = parse_connectors(
        [_http(mappings=[{"device": 599001, "object": "Analog_Value:1", "json_path": "a"}], response_format="JSON")]
    )
    assert parsed["mappings"][0]["object_key"] == (599001, "analog-value", 1)
    assert parsed["response_format"] == "json"


def test_parse_refuses_a_json_connector_mapping_without_a_path():
    _refused(
        [_http(mappings=[{"device": 599001, "object": "analog-value:1"}])],
        "json_path is required for a json-format connector",
    )


def test_parse_refuses_the_same_object_mapped_twice_in_a_connector():
    mapping = {"device": 599001, "object": "analog-value:1", "json_path": "a"}
    _refused([_http(mappings=[mapping, mapping])], "analog-value:1 is mapped twice")


def test_parse_applies_the_proxy_rules():
    proxy = {
        "object": "analog-input:1",
        "remote_object_type": "analog-value",
        "remote_object_instance": 1,
        "direction": "proxy",
    }
    _refused([_bacnet(mappings=[proxy])], "a proxy needs a commandable local object")
    proxy["object"] = "analog-value:1"
    proxy["scale"] = 2.0
    _refused([_bacnet(mappings=[proxy])], "a proxy passes values as they are")


def test_parse_refuses_a_bacnet_mapping_without_a_remote_point():
    _refused(
        [_bacnet(mappings=[{"object": "analog-value:1"}])],
        "remote_object_type and remote_object_instance are required",
    )


def test_parse_refuses_a_write_mapping_from_a_notification_class():
    _refused(
        [
            _bacnet(
                write_mappings=[
                    {"object": "notification-class:1", "remote_object_type": "analog-value", "remote_object_instance": 1}
                ]
            )
        ],
        "notification-class objects have no Present_Value to send",
    )


def test_parse_refuses_two_write_mappings_to_one_point():
    write = {"object": "analog-value:2", "remote_object_type": "analog-value", "remote_object_instance": 2}
    _refused(
        [_bacnet(write_mappings=[write, {**write, "object": "analog-value:3"}])],
        "already writes analog-value,2 present-value",
    )


def test_parse_refuses_wrongly_typed_fields():
    _refused([_http(poll_interval_seconds="fast")], "poll_interval_seconds")
    _refused([_http(poll_interval_seconds=0)], "poll_interval_seconds")
    _refused([_http(enabled="yes")], "enabled")


def test_parse_settings_defaults_and_values():
    assert parse_settings({}) == SETTINGS_DEFAULTS
    assert parse_settings({"log_retention_days": 3, "trend_soft_limit_mb": 10}) == {
        "trend_retention_days": 90,
        "log_retention_days": 3,
        "trend_soft_limit_mb": 10,
    }


def test_parse_settings_null_keeps_forever():
    # The export writes a keep-forever retention as null, so re-importing it
    # must not turn it back into the default.
    assert parse_settings({"trend_retention_days": None, "log_retention_days": None, "trend_soft_limit_mb": None}) == {
        "trend_retention_days": None,
        "log_retention_days": None,
        "trend_soft_limit_mb": None,
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"trend_retention_days": 0},
        {"trend_retention_days": 36501},
        {"log_retention_days": 0},
        {"log_retention_days": 36501},
        {"trend_soft_limit_mb": -1},
        {"trend_soft_limit_mb": 1025},
        {"trend_retention_days": "7"},
        {"log_retention_days": True},
    ],
)
def test_parse_settings_refuses_out_of_range(settings):
    with pytest.raises(ConfigImportError) as exc:
        parse_settings(settings)
    assert str(exc.value).startswith("settings: ")


def test_parse_settings_refuses_a_non_object_and_unknown_keys():
    for raw in ([], "x", 3, None):
        with pytest.raises(ConfigImportError) as exc:
            parse_settings(raw)
        assert "settings: must be an object" in str(exc.value)
    with pytest.raises(ConfigImportError) as exc:
        parse_settings({"auth_enabled": True})
    assert "settings: unknown field 'auth_enabled'" in str(exc.value)


def test_parse_config_document_defaults():
    parsed = parse_config_document({"devices": []})
    assert parsed.connectors == []
    assert parsed.settings is None
    assert parsed.devices == []


def test_parse_config_document_parses_all_three():
    parsed = parse_config_document(
        {"devices": [{"id": 599001, "name": "D"}], "settings": {"log_retention_days": 3}, "connectors": [_http()]}
    )
    assert parsed.settings["log_retention_days"] == 3
    assert parsed.connectors[0]["name"] == "Weather"
    assert [d["id"] for d in parsed.devices] == [599001]
    assert parse_config({"devices": [{"id": 599001, "name": "D"}], "connectors": [_http()]}) == parsed.devices
