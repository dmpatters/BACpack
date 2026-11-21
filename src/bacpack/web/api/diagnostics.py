"""Recorded-history views: the BACnet traffic log and the application log,
and the diagnostics download for bug reports."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ... import bacpack_version, timeutil
from ...application import LoggingApplication
from ...capabilities import capabilities_report
from ...diagnostics import build_diagnostics, diagnostics_filename
from ...models import LogEntry, TrafficLogEntry
from ..deps import get_data_dir, get_session

router = APIRouter()


# --- Diagnostics download ----------------------------------------------------


@router.get("/diagnostics/download")
def download_diagnostics(
    real_values: bool = False,
    session: Session = Depends(get_session),
    data_dir: Path = Depends(get_data_dir),
) -> Response:
    # A plain def: runs in a worker thread, so reading the history and zipping
    # don't stall the web loop.
    now = timeutil.utc_now()
    blob = build_diagnostics(
        session,
        data_dir=data_dir,
        real_values=real_values,
        capabilities=capabilities_report(LoggingApplication, bacpack_version()),
        now=now,
    )
    name = diagnostics_filename(now)
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- Traffic view ------------------------------------------------------------


@router.get("/traffic")
def get_traffic(
    device_id: int,
    limit: int = Query(200, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    entries = (
        session.query(TrafficLogEntry)
        .filter_by(device_id=device_id)
        .order_by(TrafficLogEntry.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "timestamp": timeutil.iso_utc(e.timestamp),
            "event_type": e.event_type,
            "detail": e.detail,
        }
        for e in entries
    ]


# --- Application logging -----------------------------------------------------

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


_LOG_LEVEL_VALUES = dict(zip(_LOG_LEVELS, (10, 20, 30, 40, 50), strict=True))


@router.get("/logs")
def get_logs(
    device_id: Optional[int] = None,
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    limit: int = Query(200, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    min_level = _LOG_LEVEL_VALUES[level]
    allowed_levels = [name for name in _LOG_LEVELS if _LOG_LEVEL_VALUES[name] >= min_level]

    def newest(*criteria):
        return (
            session.query(LogEntry)
            .filter(*criteria, LogEntry.level.in_(allowed_levels))
            .order_by(LogEntry.id.desc())
            .limit(limit)
            .all()
        )

    if device_id is None:
        entries = newest()
    else:
        # Two index walks, merged: one `device_id = N OR device_id IS NULL`
        # query reads and sorts every row of the device to return a page.
        both = newest(LogEntry.device_id == device_id) + newest(LogEntry.device_id.is_(None))
        entries = sorted(both, key=lambda e: e.id, reverse=True)[:limit]
    return [
        {
            "id": e.id,
            "timestamp": timeutil.iso_utc(e.timestamp),
            "level": e.level,
            "logger_name": e.logger_name,
            "message": e.message,
            "device_id": e.device_id,
        }
        for e in entries
    ]
