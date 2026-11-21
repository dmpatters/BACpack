"""JSON API, served under /api (Swagger docs at /docs).

One router module per feature area: a new feature gets its own module plus
one entry in _MODULES below. Shared helpers live in the _-prefixed modules;
router modules never import each other.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import (
    auth,
    client_tool,
    clone,
    connectors,
    devices,
    diagnostics,
    ede,
    live,
    meta,
    object_io,
    objects,
    objects_batch,
    sample,
    security_settings,
    settings,
    trends,
    web_server,
    writes,
)

router = APIRouter(prefix="/api")

# Include order matters where two routes share an HTTP method and a literal
# path segment could otherwise be captured by a path parameter: object_io's
# GET .../objects/export.csv and objects_batch's DELETE .../objects/batch
# must be registered before objects' .../objects/{object_id} routes.
# connectors.py keeps /connectors/status ahead of /connectors/{connector_id}
# internally. Tests in tests/web/ guard both.
_MODULES = (
    auth,
    devices,
    object_io,
    objects_batch,
    objects,
    trends,
    connectors,
    client_tool,
    clone,
    ede,
    sample,
    live,
    writes,
    security_settings,
    web_server,
    settings,
    meta,
    diagnostics,
)

for _module in _MODULES:
    router.include_router(_module.router)
