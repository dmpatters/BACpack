import copy
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bacpack.diagnostics_standins import StandIns, standin_config


def test_names_are_numbered_per_kind_and_stable():
    t = StandIns()
    assert t.name("device", "Boiler") == "device-1"
    assert t.name("device", "Chiller") == "device-2"
    assert t.name("device", "Boiler") == "device-1"
    assert t.name("analog-value", "AHU-1") == "analog-value-1"
    assert t.name("analog-input", "AHU-2") == "analog-input-1"


def test_text_is_one_stand_in_per_distinct_string():
    t = StandIns()
    assert t.text("North wing") == "text-1"
    assert t.text("South wing") == "text-2"
    assert t.text("North wing") == "text-1"


def test_ipv4_numbering_rolls_into_the_next_block():
    t = StandIns()
    assert t.ipv4("192.168.1.1") == "10.0.0.1"
    for i in range(2, 255):
        t.ipv4(f"172.16.0.{i}")
    assert t.ipv4("172.16.9.9") == "10.0.1.1"
    assert t.ipv4("192.168.1.1") == "10.0.0.1"


@pytest.mark.parametrize(
    "real,expected",
    [
        ("192.168.1.51/24", "10.0.0.1/24"),
        ("192.168.1.51:47809", "10.0.0.1:47809"),
        ("5:192.168.1.51:47808", "5:10.0.0.1:47808"),
        ("*", "*"),
        ("bbmd.corp.example:47808", "host-1.example:47808"),
        ("bbmd.corp.example", "host-1.example"),
        ("7:12", "7:12"),
        (None, None),
        ("", ""),
    ],
)
def test_address_forms(real, expected):
    assert StandIns().address(real) == expected


def test_hex_address_keeps_network_and_length():
    out = StandIns().address("5:0x0A0B0C")
    assert re.fullmatch(r"5:0x[0-9a-f]{6}", out)
    assert out != "5:0x0A0B0C"


def test_the_same_hex_address_gets_the_same_stand_in():
    t = StandIns()
    first = t.address("0xDEADBEEF01")
    assert t.address("0xDEADBEEF01") == first
    assert t.address("0xDEADBEEF02") != first


def test_url_keeps_scheme_and_port_and_path_depth():
    t = StandIns()
    out = t.url("https://plant.corp.example:8443/api/v2/site7/temps?key=abc")
    assert out.startswith("https://host-1.example:8443/")
    path = out.split("://", 1)[1].split("/", 1)[1].split("?")[0]
    segments = path.split("/")
    assert len(segments) == 4
    assert all(re.fullmatch(r"path-\d+", s) for s in segments)
    for gone in ("plant", "site7", "abc", "temps"):
        assert gone not in out
    assert t.url("https://plant.corp.example:8443/api/v2/site7/temps?key=abc") == out
    assert t.url(None) is None
    assert t.url("") == ""


def test_scan_longest_name_first():
    t = StandIns()
    t.name("analog-value", "AHU-1")
    t.name("analog-value", "AHU-1 Supply")
    assert "AHU" not in t.scan("write AHU-1 Supply then AHU-1")


def test_scan_does_not_mangle_a_longer_name_sharing_a_prefix():
    t = StandIns()
    t.name("analog-value", "AHU-1")
    assert t.scan("read AHU-10 and AHU-1") == "read AHU-10 and analog-value-1"


def test_short_and_keyword_names_match_whole_tokens_only():
    t = StandIns()
    t.name("analog-value", "AV")
    assert t.scan("AV started; AVailable") == "analog-value-1 started; AVailable"
    t.name("device", "analog-value")
    assert t.scan("object analog-value:1 failed") == "object analog-value:1 failed"


def test_scan_replaces_unknown_ipv4_keeping_prefix_and_port_and_is_single_pass():
    t = StandIns()
    t.ipv4("10.0.0.1")
    out = t.scan("from 192.168.5.7:47808 to 10.0.0.1/24 via 10.0.0.1")
    assert out == "from 10.0.0.2:47808 to 10.0.0.1/24 via 10.0.0.1"


def test_scan_leaves_non_addresses_alone():
    t = StandIns()
    text = "999.1.1.1 and 1.2.3 and 1.2.3.4.5 and version 3.14.10"
    assert t.scan(text) == text


def test_scan_replaces_data_dir_and_home():
    t = StandIns(data_dir=r"C:\ProgramData\BACpack", home="/home/bacpack")
    out = t.scan(r"opened C:\ProgramData\BACpack\bacpack.db for /home/bacpack/x")
    assert out == "opened <data-dir>\\bacpack.db for <home>/x"


def test_scan_finds_the_other_slash_form_of_the_data_dir():
    t = StandIns(data_dir=r"C:\ProgramData\BACpack")
    assert t.scan("opened C:/ProgramData/BACpack/x.db") == "opened <data-dir>/x.db"


def test_scan_replaces_a_name_with_quotes_and_non_ascii():
    t = StandIns()
    t.name("device", 'Plant "q" é')
    assert t.scan('device Plant "q" é stopped') == "device device-1 stopped"


def _document():
    return {
        "version": 1,
        "bacpack_version": "0.0.0",
        "settings": {"trend_retention_days": 90},
        "devices": [
            {
                "id": 599001,
                "name": "Boiler Room",
                "description": "North plant",
                "location": "Basement",
                "vendor_identifier": 999,
                "vendor_name": "Acme",
                "bind_address": "192.168.1.51/24",
                "backup_failure_timeout": 60,
                "network_role": "bbmd",
                "foreign_bbmd": "bbmd.corp.example:47808",
                "foreign_ttl": 60,
                "bbmd_peers": ["192.168.2.9:47808"],
                "bbmd_links": [599002],
                "objects": [
                    {
                        "object_type": "analog-value", "instance_number": 1, "name": "AHU-1",
                        "description": "Supply temp", "units": "degrees-celsius",
                        "trend_log_name": None, "state_text": None,
                    },
                    {
                        "object_type": "binary-value", "instance_number": 2, "name": "Fan",
                        "active_text": "Running", "inactive_text": "Idle", "trended": True,
                        "trend_log_name": "Fan history",
                    },
                    {
                        "object_type": "multi-state-value", "instance_number": 3, "name": "Mode",
                        "state_text": ["Heat", "Cool"],
                    },
                    {
                        "object_type": "notification-class", "instance_number": 4, "name": "Alerts",
                        "notification_recipient_list": [
                            {
                                "recipient": {"kind": "address", "address": "192.168.3.3:47808"},
                                "valid_days": [True] * 7, "process_identifier": 1,
                            },
                            {"recipient": {"kind": "device", "instance_number": 77}},
                        ],
                    },
                    {
                        "object_type": "schedule", "instance_number": 5, "name": "Occupancy",
                        "schedule_write_targets": [
                            {"object_type": "analog-value", "instance_number": 1,
                             "device_instance": 599001}
                        ],
                    },
                ],
            }
        ],
        "connectors": [
            {
                "name": "Weather", "type": "http", "enabled": True,
                "url": "https://plant.corp.example:8443/api/temps?key=abc",
                "auth_username": "svc-user", "headers": {"X-Key": None},
                "poll_interval_seconds": 30,
                "mappings": [{"device": 599001, "object": "analog-value:1", "json_path": "$.main.temp"}],
            },
            {
                "name": "Peer", "type": "bacnet", "enabled": True, "device": 599001,
                "remote_device_instance": 1234, "remote_address": "7:192.168.66.6",
                "mappings": [], "write_mappings": [],
            },
        ],
    }


def test_standin_config_replaces_by_field_and_keeps_the_rest():
    original = _document()
    snapshot = copy.deepcopy(original)
    table = StandIns()
    out = standin_config(original, table)
    assert original == snapshot

    dev = out["devices"][0]
    assert dev["name"] == "device-1" == table.name("device", "Boiler Room")
    for field, real in (("description", "North plant"), ("location", "Basement"), ("vendor_name", "Acme")):
        assert dev[field] == table.text(real) and real not in dev[field]
    assert dev["bind_address"] == "10.0.0.1/24" == table.address("192.168.1.51/24")
    assert dev["foreign_bbmd"] == "host-1.example:47808"
    assert dev["bbmd_peers"] == ["10.0.0.2:47808"]

    objs = dev["objects"]
    assert objs[0]["name"] == "analog-value-1" == table.name("analog-value", "AHU-1")
    assert objs[0]["description"] == table.text("Supply temp")
    assert objs[1]["name"] == "binary-value-1"
    assert objs[1]["active_text"] == table.text("Running")
    assert objs[1]["inactive_text"] == table.text("Idle")
    assert objs[1]["trend_log_name"] == table.name("trend-log", "Fan history")
    assert objs[2]["state_text"] == [table.text("Heat"), table.text("Cool")]
    recipients = objs[3]["notification_recipient_list"]
    assert recipients[0]["recipient"]["address"] == "10.0.0.3:47808"
    assert recipients[1]["recipient"] == {"kind": "device", "instance_number": 77}

    http, bacnet = out["connectors"]
    assert http["name"] == "connector-1"
    assert http["url"].startswith("https://host-2.example:8443/")
    assert "plant" not in http["url"] and "abc" not in http["url"]
    assert http["auth_username"] == table.text("svc-user")
    assert http["mappings"][0]["json_path"] == table.text("$.main.temp")
    assert bacnet["name"] == "connector-2"
    assert bacnet["remote_address"] == "7:10.0.0.4"

    # kept real
    assert dev["id"] == 599001 and dev["vendor_identifier"] == 999
    assert dev["foreign_ttl"] == 60 and dev["network_role"] == "bbmd" and dev["bbmd_links"] == [599002]
    assert out["settings"] == {"trend_retention_days": 90}
    assert objs[0]["units"] == "degrees-celsius" and objs[0]["instance_number"] == 1
    assert objs[1]["object_type"] == "binary-value" and objs[1]["trended"] is True
    assert objs[4]["schedule_write_targets"][0]["device_instance"] == 599001
    assert http["headers"] == {"header-1": None} and http["poll_interval_seconds"] == 30
    assert http["mappings"][0]["device"] == 599001 and http["mappings"][0]["object"] == "analog-value:1"
    assert bacnet["device"] == 599001 and bacnet["remote_device_instance"] == 1234

    # a log line naming the same values scans to the same stand-ins
    assert table.scan("Boiler Room at 192.168.1.51") == "device-1 at 10.0.0.1"


# --- masking secrets in URLs ---

from bacpack.diagnostics_standins import mask_config_urls, mask_url_secrets  # noqa: E402


def test_value_map_keys_become_text_stand_ins_for_both_connector_kinds():
    document = _document()
    document["connectors"][0]["mappings"][0]["value_map"] = {"PLANTVMKEY": 1}
    document["connectors"][1]["mappings"] = [{"object": "analog-value:1", "value_map": {"OtherKey": 2}}]
    document["connectors"][1]["write_mappings"] = [{"object": "analog-value:1", "value_map": {"WriteKey": 3}}]
    table = StandIns()
    out = standin_config(document, table)
    text = str(out)
    for real in ("PLANTVMKEY", "OtherKey", "WriteKey"):
        assert real not in text
    assert list(out["connectors"][0]["mappings"][0]["value_map"].values()) == [1]
    assert list(out["connectors"][0]["mappings"][0]["value_map"]) == [table.text("PLANTVMKEY")]


def test_http_header_names_become_numbered_stand_ins():
    document = _document()
    document["connectors"][0]["headers"] = {"X-PLANTSITE-Key": None, "X-Other": None}
    table = StandIns()
    out = standin_config(document, table)
    assert out["connectors"][0]["headers"] == {"header-1": None, "header-2": None}
    assert table.scan("sent X-plantsite-key") == "sent header-1"


def test_names_and_hosts_match_in_any_case():
    t = StandIns()
    t.name("analog-value", "SUPPLY TEMP")
    t.name("device", "AHU-1")
    t.url("https://plant.example.com:8443/x")
    out = t.scan("read supply temp from ahu-1 via PLANT.EXAMPLE.COM")
    assert "supply" not in out.lower() and "ahu" not in out.lower() and "plant" not in out.lower()


def test_a_url_path_is_not_scanned_for_so_identifiers_survive():
    t = StandIns()
    t.url("https://h.example/api/device/1/values")
    text = "analog-value:1 removed 1 599001 device 5"
    assert t.scan(text) == text


def test_free_text_equal_to_a_common_word_leaves_identifiers_and_compound_words():
    t = StandIns()
    t.text("value")
    t.text("Auto")
    line = "192.168.1.5 -> analog-value:1 present-value; Auto-discovered; value refused"
    out = t.scan(line)
    assert "analog-value:1 present-value" in out
    assert "Auto-discovered" in out
    assert "refused" in out and "value refused" not in out


def test_ip_with_prefix_and_port_is_keyed_on_the_bare_ip():
    t = StandIns()
    assert t.address("192.168.40.50/24:47809") == "10.0.0.1/24:47809"
    assert t.scan("bound 192.168.40.50") == "bound 10.0.0.1"


def test_a_bad_url_port_falls_back_instead_of_raising():
    t = StandIns()
    out = t.url("http://host.example:99999/x")
    assert out.startswith("http://host-1.example:99999/")
    assert "host.example" not in t.url("http://host.example:abc/x")


def test_an_empty_or_root_home_is_not_a_pattern():
    assert StandIns(home="/").scan("read ok") == "read ok"
    assert StandIns(home="C:\\").scan(r"read C:\x ok") == r"read C:\x ok"
    assert StandIns(data_dir="").scan("read ok") == "read ok"


def test_ipv4_next_to_an_underscore_is_replaced():
    t = StandIns()
    assert t.scan("peer_192.168.40.77_x") == "peer_10.0.0.1_x"


def test_a_windows_short_home_path_is_replaced_too(monkeypatch):
    from bacpack import diagnostics_standins

    monkeypatch.setattr(
        diagnostics_standins, "_short_path", lambda p: r"C:\Users\USERNA~1" if "username" in p else None
    )
    t = StandIns(home=r"C:\Users\username")
    assert t.scan(r"in C:\Users\USERNA~1\AppData and C:\Users\username\x") == r"in <home>\AppData and <home>\x"


def test_very_short_texts_are_not_scanned_for():
    t = StandIns()
    t.text("On")
    t.text("Off")
    assert t.scan("On startup, Off") == "On startup, Off"


def test_quoted_name_after_an_id_becomes_the_stand_in_or_a_placeholder():
    t = StandIns()
    t.name("analog-value", 'Chiller "A"')
    line = r'object analog-value:9 "Chiller \"A\"" ok; analog-value:3 "Old Chiller" gone; device 599001 "Gone Dev"'
    out = t.scan(line)
    assert out == (
        'object analog-value:9 "analog-value-1" ok; analog-value:3 "<name>" gone; device 599001 "<name>"'
    )


def test_a_quoted_current_name_matches_in_any_case():
    t = StandIns()
    t.name("device", "AHU-1")
    assert t.scan('device 5 "ahu-1"') == 'device 5 "device-1"'


def test_url_userinfo_password_and_secret_query_values_are_masked():
    url = "https://alice:SECRETPW@plant.example:8443/x?apikey=SECRETKEY&region=east&Token=T1&a_b=1"
    out = mask_url_secrets(url)
    assert out == "https://alice:***@plant.example:8443/x?apikey=***&region=east&Token=***&a_b=1"
    assert mask_url_secrets("https://plant.example/x") == "https://plant.example/x"
    assert mask_url_secrets("https://bob@plant.example/") == "https://bob@plant.example/"
    document = {"connectors": [{"url": url}, {"url": ""}, {"type": "bacnet"}]}
    masked = mask_config_urls(document)
    assert "SECRET" not in str(masked) and "SECRET" in str(document)


def test_scan_time_is_linear_in_the_number_of_names():
    import time

    t = StandIns()
    for i in range(10_000):
        t.name("analog-value", f"Zone {i} temperature setpoint")
        t.text(f"Setpoint for zone {i}")
    lines = [
        f"device 599001: read of analog-value:{i} by 192.168.1.{i % 250} took 3 ms zone {i} temperature setpoint"
        for i in range(7000)
    ]
    started = time.perf_counter()
    out = [t.scan(line) for line in lines]
    elapsed = time.perf_counter() - started
    assert "temperature" not in out[5]
    assert elapsed < 10, elapsed


def test_a_repr_escaped_windows_path_is_replaced():
    bs = chr(92)
    t = StandIns(home=bs.join(["C:", "Users", "alice"]))
    doubled = (bs * 2).join(["C:", "Users", "alice", "x"])
    out = t.scan(f"[Errno 2] No such file: '{doubled}'")
    assert out == f"[Errno 2] No such file: '<home>{bs * 2}x'"


def test_the_longer_of_two_overlapping_names_wins_and_is_replaced_whole():
    t = StandIns()
    t.name("analog-value", "AHU-1")
    t.name("analog-value", "AHU-1 Supply")
    assert t.scan("write AHU-1 Supply then AHU-1") == "write analog-value-2 then analog-value-1"


def test_short_and_keyword_names_match_only_in_their_own_case():
    t = StandIns()
    t.name("analog-value", "AV")
    t.name("binary-value", "Device")
    assert t.scan("AV av available Device device") == "analog-value-1 av available binary-value-1 device"


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows file system feature")
def test_the_real_short_path_lookup_returns_the_8_3_form(tmp_path):
    from bacpack.diagnostics_standins import _short_path

    long_dir = tmp_path / "a_directory_with_a_long_name"
    long_dir.mkdir()
    listing = subprocess.run(["cmd", "/c", "dir", "/x", str(tmp_path)], capture_output=True, text=True).stdout
    if "~" not in listing:
        pytest.skip("8.3 name creation is disabled on this volume")
    short = _short_path(str(long_dir))
    assert short is not None
    assert "~" in short and Path(short).exists() and short.lower() != str(long_dir).lower()
    assert _short_path(str(tmp_path / "missing")) is None


def test_the_scan_does_not_slow_down_when_many_values_share_a_first_word():
    import random
    import time

    rng = random.Random(1)
    words = "the supply air temperature for zone floor north south east west pump fan valve damper".split()
    t = StandIns()
    for i in range(10_000):
        t.text("The " + " ".join(rng.choice(words) for _ in range(1 + i % 60)) + f" {i}")
    line = " ".join(["the"] * 400)
    started = time.perf_counter()
    for _ in range(2000):
        t.scan(line)
    elapsed = time.perf_counter() - started
    assert elapsed < 15, elapsed  # 61 s with a lookup per registered length; about 1 s with the trie


# --- every exported field is classed ---
#
# standin_config replaces a hand-written list of fields and keeps the rest
# real, so a new free-text column would go out real in a stand-in zip. Each
# exported key must be in one of the two sets: a new key fails here until
# someone decides which.

_DEVICE_REPLACED = {"name", "description", "location", "vendor_name", "bind_address", "foreign_bbmd", "bbmd_peers"}
_DEVICE_KEPT = {
    "id", "vendor_identifier", "backup_failure_timeout", "objects",
    "network_role", "foreign_ttl", "bbmd_links",
}
_OBJECT_REPLACED = {
    "name", "trend_log_name", "description", "active_text", "inactive_text", "state_text",
    "notification_recipient_list",
}
_OBJECT_KEPT = {
    "object_type", "instance_number", "units", "bacnet_writable", "out_of_service_default",
    "relinquish_default", "start_value", "trended", "trend_mode", "trend_interval_seconds",
    "trend_sample_cap", "trend_log_instance", "trend_reporting_enabled", "trend_notification_threshold",
    "trend_notification_class_instance", "trend_event_enable", "trend_notify_type",
    "trend_event_detection_enable", "schedule_value_type", "schedule_default", "weekly_schedule",
    "exception_schedule", "schedule_effective_start", "schedule_effective_end", "schedule_priority",
    "schedule_write_targets", "date_list", "alarming_enabled", "notification_class_instance",
    "alarm_time_delay", "alarm_time_delay_normal", "alarm_notify_type", "alarm_event_enable",
    "alarm_high_limit", "alarm_low_limit", "alarm_deadband", "alarm_limit_enable", "alarm_value",
    "alarm_values", "alarm_event_detection_enable", "notification_priority", "notification_ack_required",
    "simulation",
}
_CONNECTOR_REPLACED = {"name", "url", "auth_username", "remote_address", "headers", "mappings", "write_mappings"}
_CONNECTOR_KEPT = {
    "type", "enabled", "poll_interval_seconds", "timeout_seconds", "response_format", "auth_type",
    "verify_tls", "device", "remote_device_instance", "rewrite_interval_seconds",
}
_MAPPING_REPLACED = {"json_path", "value_map"}
_MAPPING_KEPT = {
    "device", "object", "scale", "offset", "remote_object_type", "remote_object_instance",
    "remote_property", "remote_array_index", "direction", "priority",
}


def test_every_exported_field_is_classed_for_stand_in_mode():
    from bacpack import config_connectors, config_io

    mapping_keys = {
        *config_connectors._HTTP_MAPPING_KEYS, *config_connectors._BACNET_MAPPING_KEYS,
        *config_connectors._WRITE_MAPPING_KEYS,
    }
    connector_keys = {*config_connectors._HTTP_KEYS, *config_connectors._BACNET_KEYS}
    cases = (
        ("device", config_io._DEVICE_KEYS, _DEVICE_REPLACED, _DEVICE_KEPT),
        ("object", config_io.OBJECT_FIELDS, _OBJECT_REPLACED, _OBJECT_KEPT),
        ("connector", connector_keys, _CONNECTOR_REPLACED, _CONNECTOR_KEPT),
        ("connector mapping", mapping_keys, _MAPPING_REPLACED, _MAPPING_KEPT),
    )
    for what, exported, replaced, kept in cases:
        assert not replaced & kept, (what, replaced & kept)
        unclassed = set(exported) - replaced - kept
        assert not unclassed, (
            f"new exported {what} field(s) {sorted(unclassed)}: replace them in "
            "diagnostics_standins.standin_config, or list them here as safe to keep real"
        )
        # A listed field that no longer exists is a stale entry.
        assert (replaced | kept) <= set(exported), (what, sorted((replaced | kept) - set(exported)))


def test_every_field_classed_as_replaced_is_replaced_by_standin_config():
    marker = "Zq-Marker"
    values = {
        "name": marker, "description": marker, "location": marker, "vendor_name": marker,
        "bind_address": "192.168.7.7/24", "foreign_bbmd": "192.168.7.8:47808", "bbmd_peers": ["192.168.7.9:47808"],
        "trend_log_name": marker, "active_text": marker, "inactive_text": marker, "state_text": [marker],
        "url": f"https://{marker}.example/{marker}", "auth_username": marker, "remote_address": "192.168.7.10",
        "json_path": marker, "value_map": {marker: 1},
    }
    device = {field: values[field] for field in _DEVICE_REPLACED}
    obj = {"object_type": "analog-value", **{f: values[f] for f in _OBJECT_REPLACED - {"notification_recipient_list"}}}
    obj["notification_recipient_list"] = [{"recipient": {"kind": "address", "address": "192.168.7.11:47808"}}]
    device["objects"] = [obj]
    connector = {f: values[f] for f in _CONNECTOR_REPLACED - {"mappings", "write_mappings", "headers"}}
    connector["headers"] = {marker: "v"}
    connector["mappings"] = [{f: values[f] for f in _MAPPING_REPLACED}]
    out = standin_config({"devices": [device], "connectors": [connector]}, StandIns())
    assert marker not in repr(out) and "192.168.7." not in repr(out), out
