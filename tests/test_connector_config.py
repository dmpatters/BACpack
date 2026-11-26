import pytest

from bacpack import connector_config
from bacpack.connector_config import (
    ConnectorRuleError,
    check_json_path,
    check_mapping_target,
    check_not_proxied,
    check_not_written,
    check_proxy,
    check_remote_instance,
    check_remote_point_given,
    check_same_device,
    check_write_source,
    check_write_target_free,
    missing_credentials,
)

PROXY_OK = {
    "direction": "proxy",
    "scale": 1.0,
    "offset": 0.0,
    "value_map": None,
    "remote_object_type": "analog-value",
    "remote_object_instance": 2,
    "remote_property": "present-value",
    "remote_array_index": None,
}


def _refused(call, *args):
    with pytest.raises(ConnectorRuleError) as exc:
        call(*args)
    return exc.value


def test_rule_error_is_a_value_error():
    assert issubclass(ConnectorRuleError, ValueError)


def test_check_remote_instance_refuses_the_device_itself():
    exc = _refused(check_remote_instance, 599001, 599001)
    assert exc.status == 422
    assert str(exc) == "device 599001 can't be its own remote device"


def test_check_remote_instance_allows_another_device():
    check_remote_instance(599001, 17)


def test_check_same_device_names_both_devices():
    exc = _refused(check_same_device, 599001, "object 5", 599002)
    assert exc.status == 422
    assert str(exc) == "object 5 is on device 599002; this connector belongs to device 599001"
    check_same_device(599001, "object 5", 599001)


@pytest.mark.parametrize("object_type", ["schedule", "calendar", "notification-class"])
def test_check_mapping_target_refuses_calculated_types(object_type):
    exc = _refused(check_mapping_target, "object 5", object_type, False)
    assert exc.status == 400
    assert str(exc) == f"{object_type} objects can't be a connector-mapping target"


def test_check_mapping_target_refuses_a_simulated_object():
    exc = _refused(check_mapping_target, "object 5", "analog-value", True)
    assert exc.status == 409
    assert str(exc) == "object 5 is simulated; turn its simulator off before mapping a connector onto it"
    check_mapping_target("object 5", "analog-value", False)


def test_check_json_path_needs_a_path_for_json():
    exc = _refused(check_json_path, "json", None)
    assert exc.status == 400
    assert str(exc) == "json_path is required for a json-format connector"
    assert _refused(check_json_path, "json", "").status == 400
    check_json_path("json", "a.b")
    check_json_path("plain-text", None)


def test_check_remote_point_given():
    exc = _refused(check_remote_point_given, None, 1)
    assert exc.status == 422
    assert str(exc) == "remote_object_type and remote_object_instance are required for a bacnet connector"
    assert _refused(check_remote_point_given, "analog-input", None).status == 422
    check_remote_point_given("analog-input", 0)


def test_check_proxy_ignores_a_read_mapping():
    check_proxy("http-poll", "schedule", {**PROXY_OK, "direction": "read"})


def test_check_proxy_is_bacnet_only():
    exc = _refused(check_proxy, "http-poll", "analog-value", PROXY_OK)
    assert exc.status == 422
    assert str(exc) == "proxy mappings are only for bacnet connectors"


def test_check_proxy_needs_a_commandable_object():
    exc = _refused(check_proxy, "bacnet", "analog-input", PROXY_OK)
    assert exc.status == 422
    assert str(exc) == (
        "a proxy needs a commandable local object (analog, binary or multi-state output or value); "
        "analog-input has no priority array to mirror"
    )


@pytest.mark.parametrize("change", [{"scale": 2.0}, {"offset": 1.0}, {"value_map": {"a": 1}}])
def test_check_proxy_passes_values_as_they_are(change):
    exc = _refused(check_proxy, "bacnet", "analog-value", {**PROXY_OK, **change})
    assert exc.status == 422
    assert str(exc) == "a proxy passes values as they are: scale must be 1, offset 0, and no value map"


@pytest.mark.parametrize("change", [{"remote_property": "units"}, {"remote_array_index": 3}])
def test_check_proxy_mirrors_present_value_only(change):
    exc = _refused(check_proxy, "bacnet", "analog-value", {**PROXY_OK, **change})
    assert exc.status == 422
    assert str(exc) == "a proxy mirrors the remote's present-value, with no array index"


def test_check_proxy_accepts_a_good_proxy():
    check_proxy("bacnet", "analog-value", PROXY_OK)
    check_proxy("bacnet", "analog-value", {**PROXY_OK, "remote_property": None})


def test_check_not_written_names_the_write_and_its_connector():
    point = ("analog-value", 2, "present-value", None)
    exc = _refused(check_not_written, point, [("write mapping 7", "Room", point)])
    assert exc.status == 422
    assert "write mapping 7 (connector 'Room') already writes analog-value,2 present-value" in str(exc)
    assert str(exc).endswith("a proxy on the same point would send its own value back")
    check_not_written(point, [("write mapping 7", "Room", ("analog-value", 3, "present-value", None))])


def test_check_not_proxied_names_the_mapping_and_its_connector():
    target = ("analog-value", 2, "present-value", None)
    exc = _refused(check_not_proxied, target, [("read mapping 4", "Room", target)])
    assert exc.status == 422
    assert str(exc) == (
        "read mapping 4 (connector 'Room') proxies analog-value,2 present-value; "
        "a write mapping on the same point would send the mirrored value back"
    )
    check_not_proxied(target, [("read mapping 4", "Room", ("analog-value", 9, "present-value", None))])


def test_check_write_target_free_names_the_other_write():
    target = ("analog-value", 2, "present-value", None)
    exc = _refused(check_write_target_free, target, [("write mapping 7", target)])
    assert exc.status == 409
    assert str(exc) == "write mapping 7 already writes analog-value,2 present-value"
    check_write_target_free(target, [("write mapping 7", ("analog-value", 2, "present-value", 1))])


def test_check_write_source_refuses_a_notification_class():
    exc = _refused(check_write_source, "notification-class")
    assert exc.status == 400
    assert str(exc) == "notification-class objects have no Present_Value to send"
    check_write_source("analog-value")


@pytest.mark.parametrize(
    "fields, missing",
    [
        ({"auth_type": "none", "headers": None}, []),
        ({"auth_type": "basic", "auth_password": None}, ["password"]),
        ({"auth_type": "basic", "auth_password": ""}, ["password"]),
        ({"auth_type": "basic", "auth_password": "p"}, []),
        ({"auth_type": "bearer", "auth_token": None}, ["token"]),
        ({"auth_type": "none", "auth_password": None, "auth_token": None}, []),
        ({"auth_type": "basic", "headers": {"X-Have": "v", "X-Empty": "", "X-None": None}},
         ["password", "header X-Empty", "header X-None"]),
    ],
)
def test_missing_credentials(fields, missing):
    full = {"auth_type": "none", "auth_password": None, "auth_token": None, "headers": None, **fields}
    assert missing_credentials(**full) == missing


def test_unreadable_credentials_lists_only_unreadable_ones():
    from bacpack.connector_config import unreadable_credentials
    from bacpack.secrets_store import Unreadable

    assert unreadable_credentials(
        auth_password=Unreadable(), auth_token="", headers={"A": Unreadable(), "B": "", "C": "x"}
    ) == ["password", "header A"]
    assert unreadable_credentials(auth_password=None, auth_token=None, headers=None) == []


def test_missing_credentials_message_names_unreadable_values():
    from bacpack.connector_config import missing_credentials_message

    assert missing_credentials_message(["password"]) == "Missing credentials: password"
    assert missing_credentials_message(["password", "token"], ["password"]) == (
        "Missing credentials: password, token (the saved password can't be read; enter it again)"
    )


@pytest.mark.parametrize(
    "old, new, follows",
    [
        ("http://a.invalid/x", "http://a.invalid/y?q=1", True),
        ("http://A.invalid/x", "http://a.INVALID/x", True),
        ("http://a.invalid", "http://a.invalid:80/x", True),
        ("http://a.invalid", "https://a.invalid", True),
        ("http://a.invalid:80/x", "https://a.invalid:443/x", True),
        ("https://a.invalid", "http://a.invalid", False),
        ("http://a.invalid", "https://a.invalid:8443", False),
        ("http://a.invalid:8080", "https://a.invalid", False),
        ("http://a.invalid", "http://b.invalid", False),
        ("http://a.invalid", "http://a.invalid:81", False),
        ("https://a.invalid", "https://a.invalid:444", False),
        ("", "http://a.invalid", False),
        ("not a url", "http://a.invalid", False),
        ("http://a.invalid", "", False),
        ("http://", "http://", True),
        (" http://a.invalid/x", " http://a.invalid/x", True),
        ("not a url", "not a url", True),
        ("http://", "http://a.invalid", False),
    ],
)
def test_credentials_follow(old, new, follows):
    assert connector_config.credentials_follow(old, new) is follows
