"""Server self-description: installed version, BACnet capabilities, and the
engineering-units list the object forms offer."""
from __future__ import annotations

import functools

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ... import bacpack_version
from ...application import LoggingApplication
from ...capabilities import capabilities_report
from ...lifecycle import running_web, service_mode
from ...pics import render_pics
from ...point_config import list_engineering_units

router = APIRouter()


@router.get("/version")
def get_version():
    """The installed BACpack version, whether a service manager runs it (the UI
    shows "Restart UI only" only then), and the address the launcher bound
    (None for a hand-run dev server)."""
    running = running_web()
    return {
        "version": bacpack_version(),
        "service": service_mode(),
        "listen_address": running.address if running else None,
    }


@router.get("/capabilities")
def get_capabilities():
    """What every BACpack device supports and advertises over BACnet
    (services, object types, BIBBs), from the same functions the wire uses.
    """
    return capabilities_report(LoggingApplication, bacpack_version())


@functools.cache
def _pics_text() -> str:
    # Built once per process: it builds sample objects of every type. A plain
    # `def` route runs in a worker thread, which render_pics needs (it starts
    # its own event loops).
    return render_pics()


@router.get("/pics", response_class=PlainTextResponse)
def get_pics():
    """The Protocol Implementation Conformance Statement (ASHRAE 135 Annex A) of
    the running version, as the same text the repository's PICS.md holds.
    Plain text rather than text/markdown, which browsers download instead of show."""
    return PlainTextResponse(
        _pics_text(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="PICS.md"'},
    )


@router.get("/units")
def list_units():
    """Valid BACnet engineering unit names, from the installed bacpypes3."""
    return {"units": list_engineering_units()}
