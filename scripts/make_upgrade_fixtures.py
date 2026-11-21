"""Builds a saved-by-a-release fixture set: the files one BACpack release writes
(both databases, the key, a config export, a device backup and a CSV point
list), for tests/test_upgrade_fixtures.py to open with later code.

Run it with the release's own code, into an empty directory:

    python scripts/make_upgrade_fixtures.py tests/upgrade_fixtures/1.0.0 --release 1.0.0

Configuration goes in through the public JSON API, as a user makes it; the
history rows (trend samples, traffic, logs) go in directly, since making them
for real needs running devices. The content is fixed, so two runs differ only
in what SQLite itself varies.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from sqlalchemy.sql.elements import ClauseElement  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from bacpack import bacpack_version, config_io, migrations, models, trend_buffer  # noqa: E402
from bacpack.db import lock_both_files, make_engine, make_session_factory  # noqa: E402
from bacpack.models import BacnetObject, Device  # noqa: E402
from bacpack.web.app import create_app  # noqa: E402

# A throwaway key: it protects nothing real.
FIXTURE_KEY = base64.urlsafe_b64encode(b"upgrade-fixtures-test-key-00001!")
USERNAME = "fixture-admin"
PASSWORD = "fixture password 2026"
BASE_TIME = dt.datetime(2026, 1, 1, 0, 0, 0)
SAMPLES_PER_LOG = 12
SAMPLE_STEP_SECONDS = 300
AHU, GATEWAY = 599001, 599002
MANIFEST = "manifest.json"
HISTORY_ROWS = 5  # traffic rows, and fixture log rows
BACNET_PASSWORD = "fixture-pw"


def _process_factory(db_path, device_id):
    from tests.fakes import FakeDeviceProcess

    engine = make_engine(db_path)
    return FakeDeviceProcess(make_session_factory(engine), device_id)


def _send(client, method, url, **kwargs):
    response = client.request(method, url, **kwargs)
    if response.status_code >= 300:
        raise SystemExit(f"{method} {url} -> {response.status_code}: {response.text}")
    return response


def _post(client, url, body):
    return _send(client, "POST", url, json=body).json()


def _put(client, url, body):
    return _send(client, "PUT", url, json=body).json()


def _obj(client, device_id, object_type, instance, name, **fields):
    body = {"object_type": object_type, "instance_number": instance, "name": name, **fields}
    return _post(client, f"/api/devices/{device_id}/objects", body)


_WEEK = [[{"time": "08:00", "value": "21.0"}, {"time": "18:00", "value": "16.0"}] for _ in range(7)]
_RECIPIENT = {
    "recipient": {"kind": "address", "address": "192.168.1.200"},
    "valid_days": [True] * 7,
    "from_time": "00:00",
    "to_time": "23:59:59.99",
    "transitions": [True, True, True],
    "confirmed": True,
    "process_identifier": 7,
}


def _configure(client) -> dict:
    """Everything a user can save, through the API. Returns ids for later steps."""
    _post(client, "/api/devices", {
        "id": AHU, "name": "Fixture AHU", "description": "air handler", "location": "roof",
        "vendor_name": "Fixture Vendor", "network_role": "normal",
    })
    _post(client, "/api/devices", {
        "id": GATEWAY, "name": "Fixture Gateway", "bind_address": "192.168.1.50/24:47809",
    })
    _put(client, f"/api/devices/{AHU}", {
        "network_role": "bbmd", "bbmd_links": [GATEWAY], "bbmd_peers": ["10.0.0.9:47808"],
        "backup_failure_timeout": 600,
    })
    _put(client, f"/api/devices/{GATEWAY}", {"bacnet_password": BACNET_PASSWORD})

    ids: dict = {}
    nc = _obj(client, AHU, "notification-class", 1, "Fixture NC",
              notification_priority=[3, 2, 1], notification_ack_required=[True, False, False],
              notification_recipient_list=[_RECIPIENT])
    ids["nc"] = nc["id"]
    ai = _obj(client, AHU, "analog-input", 1, "Supply Temp", units="degreesFahrenheit",
              trended=True, trend_mode="interval", trend_interval_seconds=300, trend_log_instance=101,
              trend_log_name="Supply Temp Log", trend_sample_cap=500,
              alarming_enabled=True, notification_class_instance=1,
              alarm_high_limit=95.0, alarm_low_limit=40.0, alarm_deadband=1.5, alarm_time_delay=5)
    ids["ai"] = ai["id"]
    _obj(client, AHU, "analog-output", 1, "Damper", units="percent", relinquish_default="10")
    av_sim = _obj(client, AHU, "analog-value", 1, "Room Temp", units="degreesFahrenheit",
                  trended=True, trend_mode="cov", trend_log_instance=102, trend_log_name="Room Temp Log",
                  simulation={"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600})
    ids["av_sim"] = av_sim["id"]
    ids["av_read"] = _obj(client, AHU, "analog-value", 2, "Remote Read", units="percent")["id"]
    ids["av_write"] = _obj(client, AHU, "analog-value", 3, "Remote Write", units="percent")["id"]
    _obj(client, AHU, "binary-input", 1, "Filter Status", active_text="Dirty", inactive_text="Clean")
    _obj(client, AHU, "binary-output", 1, "Fan", relinquish_default="inactive", out_of_service_default=True)
    _obj(client, AHU, "binary-value", 1, "Occupied", alarming_enabled=True, notification_class_instance=1,
         alarm_value=True)
    _obj(client, AHU, "multi-state-input", 1, "Mode", state_text=["Off", "Heat", "Cool"])
    _obj(client, AHU, "multi-state-output", 1, "Speed", state_text=["Low", "Medium", "High"], relinquish_default="1")
    _obj(client, AHU, "multi-state-value", 1, "Season", state_text=["Winter", "Summer"], start_value="2")
    _obj(client, AHU, "calendar", 1, "Holidays",
         date_list=[{"kind": "date", "year": "*", "month": 12, "day": 25},
                    {"kind": "date-range", "start": "2026-07-01", "end": "2026-07-04"}])
    _obj(client, AHU, "schedule", 1, "Occupancy", schedule_value_type="analog", schedule_default="16.0",
         weekly_schedule=_WEEK,
         exception_schedule=[{"period": {"kind": "calendar-reference", "instance_number": 1}, "priority": 1,
                              "time_values": [{"time": "00:00", "value": "14.0"}]}],
         schedule_write_targets=[{"object_type": "analog-value", "instance_number": 1}])
    _obj(client, GATEWAY, "schedule", 1, "External Setpoint", schedule_value_type="analog",
         schedule_default="20.0", weekly_schedule=_WEEK,
         schedule_write_targets=[{"device_instance": 599500, "object_type": "analog-value", "instance_number": 3}])
    ids["gw_av"] = _obj(client, GATEWAY, "analog-value", 1, "Outside Temp", units="degreesFahrenheit",
                        trended=True, trend_mode="interval", trend_interval_seconds=600, trend_log_instance=201,
                        trend_log_name="Outside Temp Log")["id"]

    http = _post(client, "/api/connectors", {
        "name": "Weather", "url": "http://example.invalid/weather", "auth_type": "basic",
        "auth_username": "fixture", "auth_password": "http-secret", "poll_interval_seconds": 60,
        "headers": {"X-Api-Token": "header-secret"},
    })
    _post(client, f"/api/connectors/{http['id']}/mappings", {"object_id": ids["gw_av"], "json_path": "temp"})
    bacnet = _post(client, "/api/connectors", {
        "name": "Remote Controller", "type": "bacnet", "device_id": AHU,
        "remote_device_instance": 1234, "remote_address": "10.0.0.9:47809",
    })
    _post(client, f"/api/connectors/{bacnet['id']}/mappings", {
        "object_id": ids["av_read"], "remote_object_type": "analog-input", "remote_object_instance": 3,
    })
    _post(client, f"/api/connectors/{bacnet['id']}/write-mappings", {
        "object_id": ids["av_write"], "remote_object_type": "analog-output", "remote_object_instance": 4,
        "priority": 10,
    })
    bearer = _post(client, "/api/connectors", {
        "name": "Token Feed", "url": "https://example.invalid/feed", "auth_type": "bearer",
        "auth_token": "token-secret", "enabled": False,
    })
    ids["connectors"] = [http["id"], bacnet["id"], bearer["id"]]
    _put(client, "/api/settings/network", {"trend_retention_days": 400, "trend_soft_limit_mb": 500})
    return ids


def schema_fingerprint() -> dict:
    """What the code's models say both files hold: per table, each column's
    name, type, not-null and defaults. A column added only through
    add_missing_columns changes it too, so it asks for a new set."""
    from sqlalchemy.dialects import sqlite

    from bacpack.db import Base

    result = {}
    for table in Base.metadata.sorted_tables:
        columns = []
        for column in sorted(table.columns, key=lambda c: c.name):
            default = column.default
            if default is None:
                default_text = None
            elif default.is_callable:
                default_text = "callable"
            else:
                arg = getattr(default, "arg", None)
                # A SQL expression's repr can carry a memory address.
                default_text = str(arg) if isinstance(arg, ClauseElement) else repr(arg)
            server = None if column.server_default is None else str(getattr(column.server_default, "arg", "?"))
            columns.append(
                [column.name, column.type.compile(dialect=sqlite.dialect()), not column.nullable, default_text, server]
            )
        result[f"{table.schema or 'main'}.{table.name}"] = columns
    return result


def _stored(naive_utc: dt.datetime) -> str:
    """The text SQLAlchemy keeps a naive-UTC DateTime as."""
    return naive_utc.strftime("%Y-%m-%d %H:%M:%S.%f")


def _history(db_path: Path) -> dict:
    """Samples, traffic and log rows for the trended objects, written directly."""
    engine = make_engine(db_path)
    try:
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            lock_both_files(session)
            trended = [o for o in session.query(BacnetObject).order_by(BacnetObject.id) if o.trended]
            plan = [(o.id, o.device_id, o.object_type, o.instance_number) for o in trended]
        stamps = [BASE_TIME + dt.timedelta(seconds=SAMPLE_STEP_SECONDS * k) for k in range(SAMPLES_PER_LOG)]
        sample_sql = (
            "INSERT INTO history.trend_samples (object_id, sequence, timestamp, value, clock_offset, kind) "
            "VALUES (?, ?, ?, ?, 0.0, 'value')"
        )
        with engine.connect() as connection:
            for object_id, _device, _type, _instance in plan:
                rows = [
                    (object_id, k + 1, _stored(stamps[k]), float(60 + (object_id + k * 3) % 17))
                    for k in range(SAMPLES_PER_LOG)
                ]
                connection.exec_driver_sql(sample_sql, rows)
                connection.exec_driver_sql(
                    "INSERT OR REPLACE INTO history.trend_state (object_id, next_sequence, last_notify_record) "
                    "VALUES (?, ?, 0)",
                    (object_id, SAMPLES_PER_LOG + 1),
                )
            trend_buffer.rebuild_tails(connection)
            connection.exec_driver_sql(
                "INSERT OR REPLACE INTO history.history_counters (id, trend_total_sample_count) VALUES (1, ?)",
                (SAMPLES_PER_LOG * len(plan),),
            )
            for k in range(HISTORY_ROWS):
                stamp = _stored(BASE_TIME + dt.timedelta(minutes=k))
                connection.exec_driver_sql(
                    "INSERT INTO history.traffic_log_entries (device_id, timestamp, event_type, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (AHU, stamp, "who-is", f"who-is-request: low=0 high=4194303 from 192.168.1.60:47808 #{k}"),
                )
                connection.exec_driver_sql(
                    "INSERT INTO history.log_entries (device_id, timestamp, level, logger_name, message) "
                    "VALUES (?, ?, 'INFO', 'bacpack.fixture', ?)",
                    (AHU, stamp, f"fixture log line {k}"),
                )
            connection.commit()
    finally:
        engine.dispose()
    return {"plan": plan}


def _finish_files(db_path: Path) -> None:
    """Vacuumed, checkpointed, no side files: the set is just the .db files."""
    engine = make_engine(db_path)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for schema in migrations.SCHEMAS:
                connection.exec_driver_sql(f"VACUUM {schema}")
                connection.exec_driver_sql(f"PRAGMA {schema}.wal_checkpoint(TRUNCATE)")
            connection.exec_driver_sql("PRAGMA main.journal_mode = DELETE")
            connection.exec_driver_sql("PRAGMA history.journal_mode = DELETE")
    finally:
        engine.dispose()
    for suffix in ("-wal", "-shm"):
        for path in (db_path, db_path.with_name("bacpack-history.db")):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def build_set(out_dir: Path, *, provisional: bool = True, release: str = "1.0.0") -> dict:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"{out_dir} is not empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "secret.key").write_bytes(FIXTURE_KEY)
    db_path = out_dir / "bacpack.db"

    app = create_app(db_path=str(db_path), process_factory=_process_factory)
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        _post(client, "/api/auth/setup", {"mode": "login", "username": USERNAME, "password": PASSWORD})
        api_key = _post(client, "/api/settings/api-keys", {"name": "fixture-key"})["key"]
        ids = _configure(client)
        export = _send(client, "GET", "/api/config/export").content
        csv_bytes = _send(client, "GET", f"/api/devices/{AHU}/objects/export.csv").content
    (out_dir / "config-export.json").write_bytes(export)
    (out_dir / "points.csv").write_bytes(csv_bytes)

    _history(db_path)

    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            ahu = session.get(Device, AHU)
            backup_name = f"backup-{_slug(ahu.name)}.json"
            (out_dir / backup_name).write_bytes(config_io.export_device_backup(ahu))
            devices = []
            for device in session.query(Device).order_by(Device.id):
                devices.append({
                    "id": device.id, "name": device.name, "bind_address": device.bind_address,
                    "network_role": device.network_role, "foreign_bbmd": device.foreign_bbmd,
                    "foreign_ttl": device.foreign_ttl, "bbmd_peers": device.bbmd_peers,
                    "bbmd_links": sorted(link.linked_device_id for link in device.bbmd_links),
                    "object_count": len(device.objects),
                })
            object_types: dict = {}
            for obj in session.query(BacnetObject):
                object_types[obj.object_type] = object_types.get(obj.object_type, 0) + 1
            connectors = [
                {"name": c.name, "type": c.type} for c in session.query(models.Connector).order_by(models.Connector.id)
            ]
            trend_logs = [
                {"object_id": o.id, "device_id": o.device_id, "object_type": o.object_type,
                 "instance_number": o.instance_number, "mode": o.trend_mode, "samples": SAMPLES_PER_LOG}
                for o in session.query(BacnetObject).order_by(BacnetObject.id) if o.trended
            ]
    finally:
        engine.dispose()

    _finish_files(db_path)

    engine = make_engine(db_path)
    try:
        with engine.connect() as connection:
            main_version = migrations.get_schema_version(connection, "main")
            history_version = migrations.get_schema_version(connection, "history")
    finally:
        engine.dispose()
    _finish_files(db_path)

    manifest = {
        "release": release,
        "bacpack_version": bacpack_version(),
        "provisional": provisional,
        "main_schema_version": main_version,
        "history_schema_version": history_version,
        "config_version": config_io.CONFIG_VERSION,
        "schema_fingerprint": schema_fingerprint(),
        "login": {"username": USERNAME, "password": PASSWORD},
        "api_key": api_key,
        "devices": devices,
        "object_types": object_types,
        "connectors": connectors,
        "trend_logs": trend_logs,
        "backup_device": {"id": AHU, "name": "Fixture AHU", "file": backup_name},
        "feed_object": {"device_id": AHU, "object_type": "analog-value", "instance_number": 1},
        "ids": ids,
        "history_rows": {"traffic": HISTORY_ROWS, "logs": HISTORY_ROWS},
        "bacnet_passwords": {str(GATEWAY): BACNET_PASSWORD},
        "connector_secrets": {
            "Weather": {"auth_password": "http-secret", "headers": {"X-Api-Token": "header-secret"}},
            "Token Feed": {"auth_token": "token-secret"},
        },
        "sha256": {},
    }
    manifest["sha256"] = {
        path.name: _sha256(path) for path in sorted(out_dir.iterdir()) if path.name != MANIFEST
    }
    # Bytes, so the hash-checked set has LF line ends on every OS.
    (out_dir / MANIFEST).write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a saved-by-a-release fixture set.")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--release", default="1.0.0", help="the release name recorded in the manifest")
    parser.add_argument("--frozen", action="store_true", help='record "provisional": false')
    parser.add_argument("--replace", action="store_true", help="empty out_dir first (a provisional set)")
    args = parser.parse_args(argv)
    if args.replace and args.out_dir.exists():
        # The files, not the directory: a synced folder can hold the directory open.
        for child in args.out_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    manifest = build_set(args.out_dir, provisional=not args.frozen, release=args.release)
    size = sum(path.stat().st_size for path in args.out_dir.iterdir())
    print(f"{args.out_dir}: {len(manifest['sha256'])} files, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
