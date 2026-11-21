"""The diagnostics zip for bug reports.

Read only: builds from SQLite through a session and writes nothing to either
file. With stand-ins (the default) the site's names, addresses, URLs and free
text are replaced by one table shared across every file in the zip.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import platform
import sqlite3
import zipfile
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bacpack_version, config_io, timeutil
from .diagnostics_standins import StandIns, mask_config_urls, standin_config
from .lifecycle import service_mode
from .models import Connector, Device, LogEntry, ServerSettings, TrafficLogEntry

LOG_LIMIT = 2000
TRAFFIC_LIMIT = 1000

# Names in log lines are replaced by the `id "name"` shape (log_names.py); a
# line written before that shape existed may name something the file can't replace.
LEGACY_LOG_NOTE = (
    "Note: log lines written by older BACpack versions may name renamed or deleted "
    "objects, devices or connectors that this file could not replace."
)


def diagnostics_filename(now: dt.datetime) -> str:
    return f"bacpack-diagnostics-{bacpack_version()}-{now:%Y%m%d-%H%M%S}.zip"


def system_facts() -> dict:
    return {
        "bacpack_version": bacpack_version(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "install_kind": "service" if service_mode() else "hand-run",
    }


def _unchanged(text: str) -> str:
    return text


def _dump(value) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")


def build_diagnostics(
    session: Session,
    *,
    data_dir: Path,
    real_values: bool,
    capabilities: dict,
    now: dt.datetime | None = None,
) -> bytes:
    generated = now if now is not None else timeutil.utc_now()
    devices = list(session.scalars(select(Device).order_by(Device.id)))
    connectors = list(session.scalars(select(Connector)))
    # session.get, not get_or_create_settings: that one can write.
    settings = session.get(ServerSettings, 1)

    document = config_io.export_config(devices, connectors=connectors, settings=settings)
    scan: Callable[[str], str] = _unchanged
    if real_values:
        config = mask_config_urls(document)
        table = None
    else:
        table = StandIns(data_dir=str(data_dir), home=str(Path.home()))
        config = standin_config(document, table)
        if settings is not None:
            table.address(settings.bind_address)
            if settings.login_username:
                table.text(settings.login_username)
        scan = table.scan

    def device_name(device: Device) -> str:
        return device.name if table is None else table.name("device", device.name)

    logs = [
        {
            "id": e.id,
            "timestamp": timeutil.iso_utc(e.timestamp),
            "level": e.level,
            "logger_name": e.logger_name,
            "message": scan(e.message),
            "device_id": e.device_id,
        }
        for e in session.scalars(select(LogEntry).order_by(LogEntry.id.desc()).limit(LOG_LIMIT))
    ]
    traffic = {
        str(device.id): [
            {
                "timestamp": timeutil.iso_utc(e.timestamp),
                "event_type": e.event_type,
                "detail": scan(e.detail),
            }
            for e in session.scalars(
                select(TrafficLogEntry)
                .where(TrafficLogEntry.device_id == device.id)
                .order_by(TrafficLogEntry.id.desc())
                .limit(TRAFFIC_LIMIT)
            )
        ]
        for device in devices
    }

    facts = system_facts()
    stamp = timeutil.iso_utc(generated)
    mode = "REAL values" if real_values else "stand-in values"
    lines = [
        f"BACpack diagnostics ({mode})",
        f"BACpack {facts['bacpack_version']}, {facts['install_kind']}",
        f"OS: {facts['os']}",
        f"Python {facts['python']}, SQLite {facts['sqlite']}",
        f"Made: {stamp}",
        "",
    ]
    for device in devices:
        runtime = device.runtime
        line = f"{device.id} {device_name(device)} {runtime.process_status}"
        if runtime.process_error:
            line += f"; error: {scan(runtime.process_error)}"
        if runtime.apply_error:
            line += f"; apply error: {scan(runtime.apply_error)}"
        if runtime.restart_pending:
            line += "; restart pending"
        lines.append(line)
    traffic_count = sum(len(rows) for rows in traffic.values())
    lines += ["", f"Included: {len(logs)} log lines, {traffic_count} traffic entries"]
    if not real_values:
        lines.append(LEGACY_LOG_NOTE)

    system = {
        **facts,
        "stand_ins": not real_values,
        "generated_at": stamp,
        "login_mode": None if settings is None else settings.login_mode,
        "capabilities": capabilities,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.txt", "\n".join(lines) + "\n")
        archive.writestr("system.json", _dump(system))
        archive.writestr("config.json", _dump(config))
        archive.writestr("logs.json", _dump(logs))
        archive.writestr("traffic.json", _dump(traffic))
    return buffer.getvalue()
