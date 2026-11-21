"""Server-wide settings (the single ServerSettings row): the default bind
address, trend retention and soft limit. Login settings are in
security_settings.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...models import get_or_create_settings
from ...objects.network_port import validate_network_settings
from ...db import lock_both_files
from ...trending import TrendPruner
from ..deps import get_session, get_trend_pruner
from ...config_schemas import NetworkSettingsUpdate

router = APIRouter()


# --- Network settings ----------------------------------------------------


def _settings_dict(settings) -> dict:
    return {
        "bind_address": settings.bind_address,
        "active_device_id": settings.active_device_id,
        "trend_retention_days": settings.trend_retention_days,
        "trend_soft_limit_mb": settings.trend_soft_limit_mb,
    }


@router.get("/settings/network")
def get_network_settings(session: Session = Depends(get_session)):
    return _settings_dict(get_or_create_settings(session))


@router.put("/settings/network")
def update_network_settings(
    body: NetworkSettingsUpdate,
    session: Session = Depends(get_session),
    pruner: TrendPruner = Depends(get_trend_pruner),
):
    # The lock is taken before the row is read: get_or_create_settings writes
    # when the row is missing.
    lock_both_files(session)
    settings = get_or_create_settings(session)
    updates = body.model_dump(exclude_unset=True)

    # Checked before anything is written; the bind address would otherwise only
    # fail when a device starts.
    try:
        validate_network_settings(updates.get("bind_address", settings.bind_address))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    soft_limit_changed = (
        "trend_soft_limit_mb" in updates and updates["trend_soft_limit_mb"] != settings.trend_soft_limit_mb
    )
    for field, value in updates.items():
        setattr(settings, field, value)
    session.commit()
    if soft_limit_changed:
        # The excess is trimmed in the background, never under this save's lock.
        pruner.kick()
    return _settings_dict(settings)
