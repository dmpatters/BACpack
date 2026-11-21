"""Login enforcement for the web app: session cookie or API key, the
first-use setup gate, and the cross-site (CSRF) check.

Reads the login state once per request. The settings read and the session
lookup are small synchronous reads on the loop, as the old Basic-auth
middleware's settings read was.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import held_tasks, lifecycle, login_store, timeutil
from ..login_lockout import LoginLockout

SESSION_COOKIE = "bacpack_session"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Reachable without a session while a login is required. Logout is open so a
# dead session can still clear its cookie and land on the login page.
_OPEN_API_PATHS = frozenset({"/api/auth/state", "/api/auth/login", "/api/auth/logout", "/api/version"})
# Reachable while no mode is chosen yet.
_OPEN_UNSET_PATHS = frozenset({"/api/auth/state", "/api/auth/setup", "/api/version"})


@dataclass(frozen=True)
class Authenticated:
    kind: str  # "session", "key" or "none"
    session_id: Optional[int] = None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def cookie_kwargs(request: Request) -> dict:
    """Secure on an HTTPS request, and always under the launcher, which
    serves HTTPS only. The dev server speaks plain HTTP, where a Secure
    cookie would never come back."""
    return {
        "httponly": True,
        "samesite": "strict",
        "path": "/",
        "secure": request.url.scheme == "https" or lifecycle.https_only(),
    }


def is_cross_site(request: Request, *, cookie_authenticated: bool) -> bool:
    """True for a state-changing request a browser sent on another site's
    behalf. A multipart upload needs no CORS preflight, so without this any
    web page could POST a config import and delete every device. Compared
    against Host alone (a proxy's X-Forwarded-Host isn't honoured: proxies are
    untested). A request with no Origin, Referer or Sec-Fetch-Site isn't a
    browser, so it's allowed unless it carries the session cookie: a script
    uses an API key. DNS rebinding isn't covered: closing it
    needs an allowed-hostnames setting."""
    if request.method not in _UNSAFE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        origin = request.headers.get("referer")
    if origin is None:
        return cookie_authenticated
    origin_host = urlsplit(origin).netloc.lower()
    host = request.headers.get("host", "").lower()
    return not host or origin_host != host


def _is_api_like(path: str) -> bool:
    return path == "/api" or path.startswith("/api/") or path == "/openapi.json"


def _bearer(request: Request) -> Optional[str]:
    scheme, _, token = request.headers.get("authorization", "").strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


def _schedule_touch(app: FastAPI, key: tuple, write) -> None:
    now = timeutil.db_utc_now()
    if app.state.write_throttle.due(key, now):
        held_tasks.start(asyncio.to_thread(write, app.state.session_factory, key[1], now))


def _resolve(request: Request) -> Optional[Authenticated]:
    app = request.app
    bearer = _bearer(request)
    with app.state.session_factory() as session:
        if bearer is not None:
            key_id = login_store.resolve_api_key(session, bearer)
            if key_id is None:
                return None
            _schedule_touch(app, ("key", key_id), login_store.touch_api_key)
            return Authenticated("key")
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            session_id = login_store.resolve_session(session, token, timeutil.db_utc_now())
            if session_id is not None:
                _schedule_touch(app, ("session", session_id), login_store.touch_session)
                return Authenticated("session", session_id)
    return None


def install_auth(app: FastAPI) -> None:
    app.state.login_lockout = LoginLockout()
    app.state.login_epoch_seen = None
    app.state.write_throttle = login_store.WriteThrottle()

    @app.middleware("http")
    async def enforce_login(request: Request, call_next):
        path = request.url.path
        with request.app.state.session_factory() as session:
            state = login_store.get_login_state(session)
        if request.app.state.login_epoch_seen != state.epoch:
            # A credential change or a local reset: whoever was locked out may
            # now have a password that works.
            request.app.state.login_lockout.clear()
            request.app.state.login_epoch_seen = state.epoch

        auth = _resolve(request) if state.mode == "login" else None
        cookie_authenticated = auth is not None and auth.kind == "session"
        if is_cross_site(request, cookie_authenticated=cookie_authenticated):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)

        if state.mode is None:
            if _is_api_like(path) and path not in _OPEN_UNSET_PATHS:
                return JSONResponse({"detail": "setup_required"}, status_code=403)
            request.state.auth = Authenticated("none")
        elif state.mode == "login":
            if auth is None and _is_api_like(path) and path not in _OPEN_API_PATHS:
                return JSONResponse({"detail": "login required"}, status_code=401)
            request.state.auth = auth or Authenticated("none")
        else:
            request.state.auth = Authenticated("none")
        return await call_next(request)
