import datetime as dt
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from bacpack import bacpack_version, diagnostics
from bacpack.db import history_db_path
from bacpack.diagnostics import LOG_LIMIT, TRAFFIC_LIMIT, build_diagnostics, diagnostics_filename
from bacpack.models import (
    BacnetObject,
    Connector,
    ConnectorMapping,
    Device,
    LogEntry,
    ServerSettings,
    TrafficLogEntry,
)

NOW = dt.datetime(2026, 11, 5, 1, 2, 3)
DEV1 = 'PLANT-Dev "q" é'
SECRETS = [
    "SECRET-PW", "SECRET-TOKEN", "SECRET-HDR", "PLANT-HASH-xyz", "PLANT-admin", "SECRET-UPW", "SECRET-KEY",
]
FILES = {"summary.txt", "system.json", "config.json", "logs.json", "traffic.json"}


def _unzip(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return {name: z.read(name) for name in z.namelist()}


def _forms(marker: str) -> list[bytes]:
    return [
        form.encode("utf-8")
        for form in {marker, json.dumps(marker)[1:-1], json.dumps(marker, ensure_ascii=False)[1:-1]}
    ]


def _anywhere(marker: str, files: dict[str, bytes]) -> bool:
    return any(form in blob for blob in files.values() for form in _forms(marker))


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path


def _markers(data_dir: Path) -> list[str]:
    return [
        DEV1, "PLANT-desc-1", "PLANT-loc-1", "PLANT-vendor-1", "192.168.77.5",
        "PLANT-Dev-2", "192.168.88.9", "plant-bbmd.corp.example",
        "PLANT-AV-1", "PLANT-avdesc", "PLANT-BV-1", "PLANT-active", "PLANT-trend-name",
        "PLANT-MSV-1", "PLANT-st-1", "PLANT-st-2", "PLANT-NC-1", "DEADBEEF01", "192.168.99.9",
        "PLANT-conn-http", "plant-marker.corp.example", "site-marker", "k-marker", "PLANT-user",
        "PLANTVMKEY", "X-PLANTSITE-Key", "PLANT-OLD-NAME",
        "PLANT-conn-bn", "192.168.66.6", "192.168.55.5", "192.168.123.4",
        str(data_dir), str(Path.home()),
    ]


@pytest.fixture()
def planted(session_factory, data_dir):
    with session_factory() as session:
        d1 = Device(
            id=599001, name=DEV1, description="PLANT-desc-1", location="PLANT-loc-1",
            vendor_name="PLANT-vendor-1", bind_address="192.168.77.5/24",
        )
        d2 = Device(
            id=599002, name="PLANT-Dev-2", network_role="bbmd",
            bbmd_peers=["192.168.88.9:47808"], foreign_bbmd="plant-bbmd.corp.example:47808",
        )
        session.add_all([d1, d2])
        session.flush()
        session.add_all(
            [
                BacnetObject(
                    device_id=599001, object_type="analog-value", instance_number=1,
                    name="PLANT-AV-1", description="PLANT-avdesc", units="percent",
                ),
                BacnetObject(
                    device_id=599001, object_type="binary-value", instance_number=2,
                    name="PLANT-BV-1", active_text="PLANT-active", trended=True,
                    trend_log_name="PLANT-trend-name",
                ),
                BacnetObject(
                    device_id=599001, object_type="multi-state-value", instance_number=3,
                    name="PLANT-MSV-1", state_text=["PLANT-st-1", "PLANT-st-2"],
                ),
                BacnetObject(
                    device_id=599001, object_type="notification-class", instance_number=4,
                    name="PLANT-NC-1",
                    notification_recipient_list=[
                        {"recipient": {"kind": "address", "address": "0xDEADBEEF01"},
                         "valid_days": [True] * 7, "from_time": "00:00:00", "to_time": "23:59:59",
                         "transitions": [True] * 3, "confirmed": False, "process_identifier": 1},
                        {"recipient": {"kind": "address", "address": "192.168.99.9:47808"},
                         "valid_days": [True] * 7, "from_time": "00:00:00", "to_time": "23:59:59",
                         "transitions": [True] * 3, "confirmed": False, "process_identifier": 2},
                    ],
                ),
            ]
        )
        session.add_all(
            [
                Connector(
                    name="PLANT-conn-http", type="http-poll",
                    url=(
                        "https://alice:SECRET-UPW@plant-marker.corp.example:8443/site-marker/api"
                        "?region=k-marker&apikey=SECRET-KEY"
                    ),
                    auth_type="basic", auth_username="PLANT-user", auth_password="SECRET-PW",
                    auth_token="SECRET-TOKEN", headers={"X-PLANTSITE-Key": "SECRET-HDR"},
                ),
                Connector(
                    name="PLANT-conn-bn", type="bacnet", url="", device_id=599001,
                    remote_device_instance=1234, remote_address="7:192.168.66.6",
                ),
            ]
        )
        session.flush()
        http = session.scalars(select(Connector).where(Connector.name == "PLANT-conn-http")).one()
        avobj = session.scalars(select(BacnetObject).where(BacnetObject.name == "PLANT-AV-1")).one()
        session.add(ConnectorMapping(connector_id=http.id, object_id=avobj.id, json_path="$.v", value_map={"PLANTVMKEY": 1}))
        settings = session.get(ServerSettings, 1)
        settings.bind_address = "192.168.55.5/24"
        settings.login_mode = "login"
        settings.login_username = "PLANT-admin"
        settings.login_password_hash = "PLANT-HASH-xyz"
        d1.runtime.process_status = "crashed"
        d1.runtime.process_error = "PLANT-Dev-2 could not bind 192.168.88.9"
        session.add_all(
            [
                LogEntry(
                    device_id=599001, level="WARNING", logger_name="bacpack.engine",
                    timestamp=dt.datetime(2026, 11, 4, 12, 0, 0),
                    message=(
                        f'Device {DEV1} started on 192.168.77.5; PLANT-AV-1 read by client '
                        f"192.168.123.4 (server default bind 192.168.55.5/24); file {data_dir}{'/' if '/' in str(data_dir) else chr(92)}x.db "
                        f"home {Path.home()}"
                    ),
                ),
                LogEntry(
                    device_id=599001, level="INFO", logger_name="bacpack.live_config",
                    timestamp=dt.datetime(2026, 11, 4, 12, 0, 0),
                    message='device 599001: removed 1 (analog-value:7 "PLANT-OLD-NAME"); plant-av-1 and PLANT-MARKER.CORP.EXAMPLE seen',
                ),
                LogEntry(
                    device_id=None, level="INFO", logger_name="bacpack.web",
                    timestamp=dt.datetime(2026, 11, 4, 12, 0, 1), message="server ready",
                ),
                TrafficLogEntry(
                    device_id=599001, event_type="who-is",
                    timestamp=dt.datetime(2026, 11, 4, 12, 0, 2),
                    detail="Who-Is from 192.168.77.5 for PLANT-BV-1",
                ),
            ]
        )
        session.commit()
    return session_factory


def _build(session_factory, data_dir, *, real_values):
    with session_factory() as session:
        return build_diagnostics(
            session, data_dir=data_dir, real_values=real_values, capabilities={"bibbs": []}, now=NOW
        )


def _json(files, name):
    return json.loads(files[name].decode("utf-8"))


def test_the_zip_holds_exactly_the_five_files(planted, data_dir):
    for real in (False, True):
        assert set(_unzip(_build(planted, data_dir, real_values=real))) == FILES


def test_standin_file_holds_none_of_the_planted_values(planted, data_dir):
    files = _unzip(_build(planted, data_dir, real_values=False))
    leaked = [m for m in _markers(data_dir) + SECRETS if _anywhere(m, files)]
    assert leaked == []


def test_real_values_file_holds_every_planted_value(planted, data_dir):
    files = _unzip(_build(planted, data_dir, real_values=True))
    missing = [m for m in _markers(data_dir) if not _anywhere(m, files)]
    assert missing == []


@pytest.mark.parametrize("real", [False, True])
def test_no_credential_or_header_value_in_either_mode(planted, data_dir, real):
    files = _unzip(_build(planted, data_dir, real_values=real))
    assert [m for m in SECRETS if _anywhere(m, files)] == []
    connectors = _json(files, "config.json")["connectors"]
    headers = [c.get("headers") for c in connectors if c.get("headers")]
    assert [list(h.values()) for h in headers] == [[None]]
    urls = [c["url"] for c in connectors if c.get("url")]
    assert all("alice:***@" in u or "host-" in u for u in urls)


def test_names_and_addresses_line_up_across_files(planted, data_dir):
    files = _unzip(_build(planted, data_dir, real_values=False))
    config = _json(files, "config.json")
    name = config["devices"][0]["name"]
    assert name == "device-1"
    assert name in files["summary.txt"].decode("utf-8")
    logs = _json(files, "logs.json")
    assert any(name in row["message"] for row in logs)
    bind_ip = config["devices"][0]["bind_address"].split("/")[0]
    traffic = _json(files, "traffic.json")["599001"]
    assert bind_ip in traffic[0]["detail"]
    assert bind_ip in logs[-1]["message"] or any(bind_ip in row["message"] for row in logs)


def test_kept_real_fields_are_unchanged(planted, data_dir):
    files = _unzip(_build(planted, data_dir, real_values=False))
    config = _json(files, "config.json")
    dev1, dev2 = config["devices"]
    assert (dev1["id"], dev2["id"]) == (599001, 599002)
    assert dev1["vendor_identifier"] == 999
    assert dev1["bind_address"].endswith("/24")
    assert dev2["bbmd_peers"][0].endswith(":47808")
    av = dev1["objects"][0]
    assert (av["object_type"], av["instance_number"], av["units"]) == ("analog-value", 1, "percent")
    assert config["settings"]["trend_retention_days"] == 90
    logs = _json(files, "logs.json")
    assert [r["level"] for r in logs] == ["INFO", "INFO", "WARNING"]
    assert logs[2]["timestamp"] == "2026-11-04T12:00:00+00:00"
    assert _json(files, "traffic.json")["599001"][0]["event_type"] == "who-is"


def test_summary_first_line_names_the_mode_and_lists_one_line_per_device(planted, data_dir):
    standins = _unzip(_build(planted, data_dir, real_values=False))["summary.txt"].decode("utf-8")
    real = _unzip(_build(planted, data_dir, real_values=True))["summary.txt"].decode("utf-8")
    assert standins.splitlines()[0] == "BACpack diagnostics (stand-in values)"
    assert real.splitlines()[0] == "BACpack diagnostics (REAL values)"
    lines = [line for line in standins.splitlines() if line.startswith("599001") or line.startswith("599002")]
    assert len(lines) == 2
    assert "crashed" in lines[0] and "device-1" in lines[0]
    assert "could not bind" in lines[0]
    assert "3 log lines" in standins and "traffic" in standins


def test_system_json_has_versions_install_kind_capabilities_and_login_mode_but_no_username(
    planted, data_dir
):
    system = _json(_unzip(_build(planted, data_dir, real_values=False)), "system.json")
    assert system["bacpack_version"] == bacpack_version()
    assert system["install_kind"] in ("service", "hand-run")
    assert {"os", "python", "sqlite"} <= set(system)
    assert system["stand_ins"] is True
    assert system["login_mode"] == "login"
    assert "auth_enabled" not in system
    assert system["capabilities"] == {"bibbs": []}
    assert system["generated_at"] == "2026-11-05T01:02:03+00:00"
    assert "PLANT-admin" not in json.dumps(system)


def test_logs_are_newest_first_and_capped(session_factory, data_dir):
    with session_factory() as session:
        session.execute(
            insert(LogEntry),
            [
                {"level": "INFO", "logger_name": "x", "message": f"m{i}", "timestamp": NOW}
                for i in range(LOG_LIMIT + 5)
            ],
        )
        session.commit()
        newest = session.scalar(select(LogEntry.id).order_by(LogEntry.id.desc()).limit(1))
    logs = _json(_unzip(_build(session_factory, data_dir, real_values=True)), "logs.json")
    assert len(logs) == LOG_LIMIT
    assert logs[0]["id"] == newest
    assert [r["id"] for r in logs] == sorted((r["id"] for r in logs), reverse=True)


def test_traffic_is_per_device_and_capped(session_factory, data_dir):
    with session_factory() as session:
        session.add_all([Device(id=599001, name="A"), Device(id=599002, name="B")])
        session.flush()
        session.execute(
            insert(TrafficLogEntry),
            [
                {"device_id": 599001, "event_type": "e", "detail": f"d{i}", "timestamp": NOW}
                for i in range(TRAFFIC_LIMIT + 5)
            ],
        )
        session.commit()
    traffic = _json(_unzip(_build(session_factory, data_dir, real_values=True)), "traffic.json")
    assert len(traffic["599001"]) == TRAFFIC_LIMIT
    assert traffic["599001"][0]["detail"] == f"d{TRAFFIC_LIMIT + 4}"
    assert traffic["599002"] == []


@pytest.mark.parametrize("real", [False, True])
def test_an_empty_database_still_builds(session_factory, data_dir, real):
    files = _unzip(_build(session_factory, data_dir, real_values=real))
    assert set(files) == FILES
    assert _json(files, "config.json")["devices"] == []
    assert _json(files, "logs.json") == []
    assert _json(files, "traffic.json") == {}


def test_building_writes_nothing(planted, data_dir, session_factory):
    with session_factory() as session:
        path = session.get_bind().url.database
    watcher = sqlite3.connect(path)
    watcher.execute("ATTACH ? AS history", (str(history_db_path(path)),))

    def state():
        return (
            watcher.execute("PRAGMA main.data_version").fetchone(),
            watcher.execute("PRAGMA history.data_version").fetchone(),
            watcher.execute("SELECT count(*) FROM main.device_runtime").fetchone(),
            watcher.execute("SELECT count(*) FROM history.log_entries").fetchone(),
        )

    before = state()
    _build(planted, data_dir, real_values=False)
    _build(planted, data_dir, real_values=True)
    after = state()
    watcher.close()
    assert after == before


def test_a_large_config_stays_small(session_factory, data_dir):
    with session_factory() as session:
        session.add(Device(id=599001, name="Big"))
        session.flush()
        session.execute(
            insert(BacnetObject),
            [
                {
                    "device_id": 599001, "object_type": "analog-value", "instance_number": i,
                    "name": f"Zone {i} temperature setpoint", "units": "percent",
                    "description": f"Setpoint for zone {i}",
                }
                for i in range(5000)
            ],
        )
        session.commit()
    for real in (False, True):
        assert len(_build(session_factory, data_dir, real_values=real)) < 5 * 1024 * 1024


def test_filename_uses_version_and_utc_stamp():
    assert diagnostics_filename(dt.datetime(2026, 11, 5, 1, 2, 3)) == (
        f"bacpack-diagnostics-{bacpack_version()}-20261105-010203.zip"
    )


def test_system_facts_names_the_running_system():
    facts = diagnostics.system_facts()
    assert facts["sqlite"] == sqlite3.sqlite_version
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", facts["python"])


def test_a_renamed_objects_old_name_and_case_variants_are_replaced_in_logs(planted, data_dir):
    files = _unzip(_build(planted, data_dir, real_values=False))
    message = next(r["message"] for r in _json(files, "logs.json") if "removed 1" in r["message"])
    assert message.startswith('device 599001: removed 1 (analog-value:7 "<name>"); ')
    assert "analog-value-1 and host-" in message


def test_connector_urls_are_masked_in_real_values_mode(planted, data_dir):
    config = _json(_unzip(_build(planted, data_dir, real_values=True)), "config.json")
    url = next(c["url"] for c in config["connectors"] if c.get("url"))
    assert url == (
        "https://alice:***@plant-marker.corp.example:8443/site-marker/api?region=k-marker&apikey=***"
    )


def test_header_names_and_value_map_keys_are_stand_ins(planted, data_dir):
    config = _json(_unzip(_build(planted, data_dir, real_values=False)), "config.json")
    http = next(c for c in config["connectors"] if c.get("headers"))
    assert list(http["headers"]) == ["header-1"]
    (key,) = http["mappings"][0]["value_map"]
    assert re.fullmatch(r"text-\d+", key)


def test_the_stand_in_summary_says_old_log_lines_may_name_unreplaced_things(planted, data_dir):
    standins = _unzip(_build(planted, data_dir, real_values=False))["summary.txt"].decode("utf-8")
    real = _unzip(_build(planted, data_dir, real_values=True))["summary.txt"].decode("utf-8")
    assert diagnostics.LEGACY_LOG_NOTE in standins
    assert diagnostics.LEGACY_LOG_NOTE not in real


def test_a_large_table_scans_in_seconds(session_factory, data_dir):
    """10,000 names, 2,000 log lines, 5,000 traffic entries (measured 0.9 s)."""
    import time

    with session_factory() as session:
        session.add_all([Device(id=599001 + i, name=f"Dev {i}") for i in range(5)])
        session.flush()
        session.execute(
            insert(BacnetObject),
            [
                {"device_id": 599001, "object_type": "analog-value", "instance_number": i,
                 "name": f"Zone {i} temperature setpoint", "description": f"Setpoint for zone {i}"}
                for i in range(5000)
            ],
        )
        session.execute(
            insert(LogEntry),
            [
                {"level": "INFO", "logger_name": "x", "timestamp": NOW, "device_id": 599001,
                 "message": f'device 599001: read analog-value:{i} "Zone {i} temperature setpoint" by 192.168.1.{i % 250}'}
                for i in range(2000)
            ],
        )
        session.execute(
            insert(TrafficLogEntry),
            [
                {"device_id": 599001 + (i % 5), "event_type": "read-property", "timestamp": NOW,
                 "detail": f"192.168.1.{i % 250}:47808 -> analog-value:{i} present-value"}
                for i in range(5000)
            ],
        )
        session.commit()
    started = time.perf_counter()
    files = _unzip(_build(session_factory, data_dir, real_values=False))
    elapsed = time.perf_counter() - started
    assert elapsed < 30, elapsed
    assert b"temperature" not in files["logs.json"]


@pytest.mark.parametrize("real", [False, True])
def test_diagnostics_zip_holds_no_tls_files(session_factory, data_dir, real):
    tls = data_dir / "tls"
    tls.mkdir()
    (tls / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\nPLANTED-KEY-BODY\n", encoding="utf-8")
    (tls / "cert.pem").write_text("PLANTED-CERT-BODY", encoding="utf-8")
    files = _unzip(_build(session_factory, data_dir, real_values=real))
    for blob in files.values():
        assert b"PRIVATE KEY" not in blob
        assert b"PLANTED-KEY-BODY" not in blob and b"PLANTED-CERT-BODY" not in blob
