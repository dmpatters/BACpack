"""FastAPI application factory: wires the SQLite session factory and the
EngineManager into app.state, applies the login middleware, and serves the
built React SPA.
"""
from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import StatementError
from starlette.responses import Response

from .. import bacpack_version, history_writer, lifecycle, security_log
from ..secrets_store import SecretKeyProblem
from ..connectors import ConnectorPoller
from ..db import (
    get_db_path,
    init_db,
    make_engine,
    make_session_factory,
    sqlite_version_problem,
    vacuum_if_mostly_free,
)
from ..engine import EngineManager
from ..log_capture import install_log_capture
from ..trending import TrendPruner, run_retention_task
from .api import router as api_router
from .auth_middleware import install_auth
from .json_guard import RejectUnpairedSurrogates

def _is_api_path(path: str) -> bool:
    return path == "api" or path.startswith("api/")


def _api_not_found() -> JSONResponse:
    return JSONResponse({"detail": "not found"}, status_code=404)


def _json_safe(value):
    """Non-finite floats and unpaired surrogates as text, so a 422 can echo
    the input it refused."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


_CERTIFICATE_UPLOAD_PATH = "/api/settings/web/certificate"


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, except that a refused NaN/Infinity in the
    echoed input is sent as text, since the default can't encode it. The
    certificate upload gets no echo at all: its body holds a private key."""
    errors = exc.errors()
    if request.url.path == _CERTIFICATE_UPLOAD_PATH:
        errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")} for error in errors]
    return JSONResponse({"detail": _json_safe(jsonable_encoder(errors))}, status_code=422)


logger = logging.getLogger(__name__)

_SQLITE_INTEGER = range(-(2**63), 2**63)


def _fits_sqlite_integer(text: str) -> bool:
    try:
        return int(text) in _SQLITE_INTEGER
    except ValueError:
        return True  # not an integer at all: not what overflowed


async def _id_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    """sqlite3 can't bind an int outside a signed 64-bit INTEGER, so such an
    id fails the query rather than matching nothing: no such row (404) when
    it's in the path, a refused value (422) in a body or query string. Any
    other OverflowError is still a 500."""
    if "SQLite INTEGER" not in str(exc):
        raise exc
    if any(not _fits_sqlite_integer(v) for v in request.path_params.values()):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return JSONResponse({"detail": "an id is outside the range the database can hold"}, status_code=422)


async def _too_deeply_nested(request: Request, exc: RecursionError) -> JSONResponse:
    """Any RecursionError a request reaches, most likely json.loads on deeply
    nested input, is the client's 400. Logged with its traceback so a real
    recursion bug still shows on the Logs page."""
    logger.error("request nested too deeply: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": "request nested too deeply"}, status_code=400)


def _key_refusal(exc: SecretKeyProblem) -> JSONResponse:
    security_log.secret_key_unusable(exc.key_file)
    return JSONResponse({"detail": str(exc)}, status_code=409)


async def _secret_key_problem(request: Request, exc: SecretKeyProblem) -> JSONResponse:
    return _key_refusal(exc)


async def _statement_error(request: Request, exc: StatementError) -> JSONResponse:
    """The column type raises SecretKeyProblem while a save runs, and
    SQLAlchemy wraps whatever a bind step raises. Saving a credential with an
    unusable secret.key is a refusal (409), not a server error; any other
    statement error is still a 500."""
    if isinstance(exc.orig, SecretKeyProblem):
        return _key_refusal(exc.orig)
    raise exc


BASE_DIR = Path(__file__).resolve().parent
# Vite's build.outDir (frontend/vite.config.ts), committed to git so an
# install never needs Node.
SPA_DIR = BASE_DIR / "spa"


def _data_dir(db_path: str) -> Path:
    """Absolute, so the diagnostics download can find it in log lines."""
    return Path(db_path).resolve().parent


def create_app(db_path: Optional[str] = None, *, process_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        problem = sqlite_version_problem()
        if problem is not None:
            logger.critical(problem)
            lifecycle.exit_process(lifecycle.SQLITE_TOO_OLD_EXIT_CODE)
        resolved_db_path = str(db_path) if db_path is not None else str(get_db_path())
        engine = make_engine(resolved_db_path)
        init_db(engine)
        vacuum_if_mostly_free(engine)
        session_factory = make_session_factory(engine)

        app.state.session_factory = session_factory
        app.state.data_dir = _data_dir(resolved_db_path)
        install_log_capture(session_factory)
        engine_kwargs: dict[str, Any] = {"db_path": resolved_db_path}
        if process_factory is not None:
            engine_kwargs["process_factory"] = process_factory
        app.state.engine = EngineManager(session_factory, **engine_kwargs)

        app.state.connector_poller = ConnectorPoller(session_factory)
        app.state.connector_poller.start()

        # Retention pruning and the health-check task run for the whole
        # process lifetime, not tied to any one device's start/stop.
        app.state.trend_pruner = TrendPruner(session_factory)
        app.state.trend_pruner.start()
        retention_task = asyncio.ensure_future(
            run_retention_task(session_factory, pruner=app.state.trend_pruner)
        )
        health_check_task = asyncio.ensure_future(app.state.engine.run_health_check_task())

        await app.state.engine.reattach_running_devices()
        resume_task = asyncio.ensure_future(app.state.engine.resume_remembered_devices())
        yield
        # A requested shutdown stops every device and remembers it; a crash
        # never gets here, so devices survive it and are reattached.
        # Background tasks go first, so none can
        # start or reap a device while they stop.
        resume_task.cancel()
        health_check_task.cancel()
        try:
            await asyncio.gather(resume_task, health_check_task, return_exceptions=True)
            try:
                app.state.connector_poller.stop()
            finally:
                await app.state.engine.stop_all_for_shutdown()
        finally:
            # Whatever failed above, the queued log lines (often about that
            # failure) must reach the database before the process exits.
            retention_task.cancel()
            try:
                await app.state.trend_pruner.stop()
            finally:
                history_writer.close_current()
                engine.dispose()

    # /docs is the SPA's own page (Swagger UI bundled into the build, so
    # nothing loads from a CDN); FastAPI still serves /openapi.json.
    app = FastAPI(title="BACpack", version=bacpack_version(), lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(RecursionError, _too_deeply_nested)
    app.add_exception_handler(OverflowError, _id_out_of_range)
    # Starlette types a handler's exception as Exception, not the subclass.
    app.add_exception_handler(SecretKeyProblem, _secret_key_problem)  # pyright: ignore[reportArgumentType]
    app.add_exception_handler(StatementError, _statement_error)  # pyright: ignore[reportArgumentType]

    app.include_router(api_router)

    # Mounted before the catch-all below so it's matched first. Absent in a
    # dev checkout until `npm run build` has run.
    assets_dir = SPA_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Catch-all for client-side routing: index.html for every path not
        registered above (/api, /openapi.json, /assets)."""
        if _is_api_path(full_path):
            # An unmatched /api path is a real 404, not index.html that a
            # JSON client would choke on.
            return _api_not_found()

        index = SPA_DIR / "index.html"
        if not index.is_file():
            return Response(
                "frontend not built: run `npm install && npm run build` in frontend/",
                status_code=503,
            )
        return FileResponse(index)

    async def _method_not_allowed(request: Request, exc):
        # Only the GET catch-all matched a non-GET /api request: no such
        # route, not a wrong method. A real route's 405 keeps its Allow.
        if request.scope.get("endpoint") is serve_spa and _is_api_path(request.url.path.lstrip("/")):
            return _api_not_found()
        return await http_exception_handler(request, exc)

    app.add_exception_handler(405, _method_not_allowed)

    # Before install_auth: the later-added middleware is the outer one, so
    # an unauthenticated request is refused before its body is read.
    app.add_middleware(RejectUnpairedSurrogates)
    install_auth(app)

    return app


app = create_app()
