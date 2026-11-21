"""BACpack's own JSON view of trended objects' history, driving the Trends
page; separate from the Trend Log objects'
ReadRange that BACnet clients use."""
from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BeforeValidator
from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import timeutil
from ...engine import EngineManager
from ...models import BacnetObject, TrendSample, effective_trend_sample_cap, get_or_create_settings
from ...trend_range import fetch_range, is_step
from ...trending import (
    BYTES_PER_TREND_SAMPLE,
    DEFAULT_TREND_INTERVAL_SECONDS,
    TREND_HARD_CAP_BYTES,
    effective_trend_limit_bytes,
    export_trend_csv,
    fetch_samples,
    trend_over_limit,
)
from ..deps import get_engine, get_session
from ..schemas import TrendRange
from ._lookups import get_object_or_404

router = APIRouter()


@router.get("/devices/{device_id}/objects/{object_id}/trend")
def get_object_trend(
    device_id: int,
    object_id: int,
    # ge=1: SQLite treats a negative LIMIT as "unlimited".
    limit: int = Query(1000, ge=1, le=100_000),
    session: Session = Depends(get_session),
    engine: EngineManager = Depends(get_engine),
):
    get_object_or_404(session, device_id, object_id)
    return fetch_samples(engine.session_factory, object_id, limit=limit)


# A query string reads an unencoded "+" as a space, so "+02:00" arrives as " 02:00".
_SPACE_FOR_PLUS = re.compile(r"T\d{2}:\d{2}(:\d{2}(\.\d+)?)? \d{2}:?\d{2}$")


def _no_space_for_plus(value):
    if isinstance(value, str) and _SPACE_FOR_PLUS.search(value):
        raise ValueError("the offset's '+' arrived as a space: URL-encode it as %2B, or send the time in UTC with Z")
    return value


_OFFSET_HELP = "ISO 8601 with an offset, e.g. 2026-10-16T09:00:00Z; URL-encode a '+' in the offset as %2B"
_checked = BeforeValidator(_no_space_for_plus)


@router.get("/devices/{device_id}/objects/{object_id}/trend/range", response_model=TrendRange)
def get_object_trend_range(
    device_id: int,
    object_id: int,
    start: Annotated[Optional[dt.datetime], Query(description=_OFFSET_HELP), _checked] = None,
    end: Annotated[Optional[dt.datetime], Query(description=f"{_OFFSET_HELP}. Defaults to now."), _checked] = None,
    last: Optional[int] = Query(
        None, ge=1, le=366 * 86400, description="Seconds up to the server's now; instead of start and end."
    ),
    max_points: int = Query(1000, ge=10, le=5000),
    session: Session = Depends(get_session),
    engine: EngineManager = Depends(get_engine),
):
    """Samples between start and end, thinned to about max_points, with the
    sample before start and, for interval trends, the gaps. `last` takes
    "now" from the server, so a browser with a wrong clock still gets the
    newest samples."""
    obj = get_object_or_404(session, device_id, object_id)
    if last is not None:
        if start is not None or end is not None:
            raise HTTPException(status_code=422, detail="send last, or start (and optionally end), not both")
        end_db = timeutil.db_utc_now()
        start_db = end_db - dt.timedelta(seconds=last)
    elif start is None:
        raise HTTPException(status_code=422, detail="send last or start")
    else:
        start_db = _stored(start, "start")
        end_db = timeutil.db_utc_now() if end is None else _stored(end, "end")
        if start_db >= end_db:
            raise HTTPException(status_code=422, detail="start must be before end")
    window = fetch_range(
        engine.session_factory,
        object_id=object_id,
        mode=obj.trend_mode if obj.trended else None,
        interval_seconds=obj.trend_interval_seconds or DEFAULT_TREND_INTERVAL_SECONDS,
        step=is_step(obj.object_type, obj.schedule_value_type),
        start=start_db,
        end=end_db,
        max_points=max_points,
    )
    return {**window, "start": timeutil.iso_utc(start_db), "end": timeutil.iso_utc(end_db)}


def _stored(value: dt.datetime, name: str) -> dt.datetime:
    try:
        return timeutil.to_db(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} needs a UTC offset, like 2026-10-16T09:00:00Z")
    except OverflowError:
        # An offset can push a year-1 or year-9999 time past what UTC can hold.
        raise HTTPException(status_code=422, detail=f"{name} is outside the supported range (years 1 to 9999 in UTC)")


@router.get("/devices/{device_id}/objects/{object_id}/trend/export.csv")
def export_object_trend(
    device_id: int,
    object_id: int,
    session: Session = Depends(get_session),
    engine: EngineManager = Depends(get_engine),
):
    obj = get_object_or_404(session, device_id, object_id)
    csv_text = export_trend_csv(engine.session_factory, object_id)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": _attachment(f"{obj.name}-trend.csv")},
    )


def _attachment(filename: str) -> str:
    """Content-Disposition for a user-named file. Headers must be Latin-1,
    and a double quote would break the header, so: an ASCII fallback plus
    the RFC 6266 UTF-8 form browsers prefer."""
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/trends/storage")
def get_trends_storage(session: Session = Depends(get_session)):
    """Trend storage use: sample count and size estimate per trended
    object, the totals, and the storage limits."""
    rows = (
        session.query(
            BacnetObject.id,
            BacnetObject.device_id,
            BacnetObject.name,
            BacnetObject.trend_sample_cap,
            func.count(TrendSample.id),
        )
        .join(TrendSample, TrendSample.object_id == BacnetObject.id)
        .group_by(BacnetObject.id)
        .all()
    )
    settings = get_or_create_settings(session)
    trends = [
        {
            "device_id": device_id,
            "object_id": object_id,
            "object_name": name,
            "sample_count": count,
            "bytes_estimate": count * BYTES_PER_TREND_SAMPLE,
            "sample_cap": effective_trend_sample_cap(cap),
        }
        for object_id, device_id, name, cap, count in rows
    ]
    total_samples = sum(t["sample_count"] for t in trends)
    return {
        "total_samples": total_samples,
        "total_bytes_estimate": total_samples * BYTES_PER_TREND_SAMPLE,
        "effective_limit_bytes": effective_trend_limit_bytes(settings),
        "hard_cap_bytes": TREND_HARD_CAP_BYTES,
        "pruning": trend_over_limit(session),
        "trends": trends,
    }
