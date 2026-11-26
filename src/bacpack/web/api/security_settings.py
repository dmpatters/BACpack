"""Login and API key settings. First-use setup,
login and logout are in auth.py. Password work runs in a worker thread."""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import auth, login_store, security_log, timeutil
from ...db import lock_both_files
from ..auth_middleware import SESSION_COOKIE, client_ip, cookie_kwargs
from ..deps import get_session
from ._lockout import locked

router = APIRouter()

MAX_KEY_NAME_LENGTH = 64


class LoginUpdate(BaseModel):
    username: Optional[str] = None
    new_password: Optional[str] = None
    current_password: Optional[str] = None
    revoke_api_keys: bool = False


class EnableBody(BaseModel):
    username: str
    password: str
    revoke_api_keys: bool = False


class DisableBody(BaseModel):
    current_password: str


class KeyBody(BaseModel):
    name: str
    current_password: Optional[str] = None


def _bad_request(problem: str) -> HTTPException:
    return HTTPException(status_code=400, detail=problem)


async def _require_current_password(request: Request, password: Optional[str], action: str) -> None:
    """Checks the password under the same per-address lockout as the login
    page, so a session or key holder can't guess it without limit. A missing
    password is a client mistake, not a guess: a plain 401, not counted."""
    session_factory = request.app.state.session_factory
    lockout = request.app.state.login_lockout
    ip = client_ip(request)

    def read():
        with session_factory() as session:
            return login_store.password_hash_of(session)

    stored = await asyncio.to_thread(read)
    if not password:
        raise HTTPException(status_code=401, detail="the current password is wrong")
    left = lockout.begin(ip)
    if left is not None:
        raise locked(left)
    try:
        ok = (
            len(password) <= auth.MAX_PASSWORD_LENGTH
            and auth.is_storable(password)
            and bool(stored)
            and await asyncio.to_thread(auth.verify_password, password, stored)
        )
        if not ok:
            security_log.current_password_wrong(ip, action)
            started = lockout.record_failure(ip)
            if started is not None:
                security_log.locked_out(ip, started)
                raise locked(started)
            raise HTTPException(status_code=401, detail="the current password is wrong")
        left = lockout.check(ip)
        if left is not None:
            raise locked(left)
        lockout.record_success(ip)
    finally:
        lockout.release(ip)


def _issue_session(request: Request, response: Response, session: Session) -> None:
    token = login_store.create_session(session, client_ip(request), request.headers.get("user-agent"))
    response.set_cookie(SESSION_COOKIE, token, **cookie_kwargs(request))


@router.get("/settings/login")
def get_login_settings(session: Session = Depends(get_session)):
    state = login_store.get_login_state(session)
    return {"mode": state.mode, "username": state.username}


@router.put("/settings/login")
async def update_login(body: LoginUpdate, request: Request, response: Response):
    session_factory = request.app.state.session_factory

    def read():
        with session_factory() as session:
            return login_store.get_login_state(session)

    state = await asyncio.to_thread(read)
    if state.mode != "login":
        raise HTTPException(status_code=409, detail="login is not turned on")
    if body.revoke_api_keys and body.new_password is None:
        raise _bad_request("revoke_api_keys goes with a new password")
    username = state.username or ""
    if body.username is not None:
        username = body.username.strip()
        problem = auth.check_username_rules(username)
        if problem:
            raise _bad_request(problem)
    new_hash = None
    if body.new_password is not None:
        problem = auth.check_password_rules(body.new_password, username)
        if problem:
            raise _bad_request(problem)
        await _require_current_password(request, body.current_password, "password change")
        new_hash = await asyncio.to_thread(auth.hash_password, body.new_password)

    ip = client_ip(request)

    def save() -> list[tuple[int, str]]:
        revoked = []
        with session_factory() as session:
            lock_both_files(session)
            if new_hash is not None:
                login_store.set_login(session, username, new_hash)
                _issue_session(request, response, session)
                if body.revoke_api_keys:
                    revoked = [(row.id, row.prefix) for row in login_store.revoke_all_api_keys(session)]
            elif username != state.username:
                login_store.set_username(session, username)
            session.commit()
        return revoked

    revoked = await asyncio.to_thread(save)
    if new_hash is not None:
        security_log.password_changed(ip)
    for key_id, prefix in revoked:
        security_log.key_revoked(key_id, prefix)
    if username != state.username:
        security_log.username_changed(ip, username)
    return {"mode": "login", "username": username}


@router.post("/settings/login/enable")
async def enable_login(body: EnableBody, request: Request, response: Response):
    session_factory = request.app.state.session_factory
    username = body.username.strip()
    problem = auth.check_username_rules(username) or auth.check_password_rules(body.password, username)
    if problem:
        raise _bad_request(problem)
    password_hash = await asyncio.to_thread(auth.hash_password, body.password)

    def save() -> Optional[list[tuple[int, str]]]:
        with session_factory() as session:
            lock_both_files(session)
            if login_store.get_login_state(session).mode == "login":
                return None
            login_store.set_login(session, username, password_hash)
            _issue_session(request, response, session)
            revoked = []
            if body.revoke_api_keys:
                revoked = [(row.id, row.prefix) for row in login_store.revoke_all_api_keys(session)]
            session.commit()
            return revoked

    revoked = await asyncio.to_thread(save)
    if revoked is None:
        raise HTTPException(status_code=409, detail="login is already turned on")
    security_log.mode_switched(client_ip(request), "login")
    for key_id, prefix in revoked:
        security_log.key_revoked(key_id, prefix)
    return {"mode": "login", "username": username}


@router.post("/settings/login/disable")
async def disable_login(body: DisableBody, request: Request, response: Response):
    session_factory = request.app.state.session_factory

    def mode():
        with session_factory() as session:
            return login_store.get_login_state(session).mode

    if await asyncio.to_thread(mode) != "login":
        raise HTTPException(status_code=409, detail="login is not turned on")
    if not request.app.state.own_names.allows(request.headers.get("host")):
        raise HTTPException(
            status_code=409,
            detail="this page was opened by a name BACpack won't answer to without a login; "
            "open it by its IP address first, then turn the login off",
        )
    await _require_current_password(request, body.current_password, "turning the login off")

    def save():
        with session_factory() as session:
            lock_both_files(session)
            login_store.set_no_login(session)
            session.commit()

    await asyncio.to_thread(save)
    response.delete_cookie(SESSION_COOKIE, path="/")
    security_log.mode_switched(client_ip(request), "none")
    return {"mode": "none", "username": None}


# --- API keys ------------------------------------------------------------------


def _key_dict(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created": timeutil.iso_utc(row.created),
        "last_used": timeutil.iso_utc(row.last_used) if row.last_used else None,
    }


@router.get("/settings/api-keys")
def list_keys(session: Session = Depends(get_session)):
    return [_key_dict(row) for row in login_store.list_api_keys(session)]


@router.post("/settings/api-keys", status_code=201)
async def create_key(body: KeyBody, request: Request):
    """With login on, a session or a key alone can't mint a key: it takes the
    password, so a stolen credential can't leave a hidden one behind."""
    session_factory = request.app.state.session_factory
    name = body.name.strip()
    if not name or len(name) > MAX_KEY_NAME_LENGTH:
        raise _bad_request(f"a key name is 1 to {MAX_KEY_NAME_LENGTH} characters")

    def read():
        with session_factory() as session:
            return login_store.get_login_state(session)

    checked = await asyncio.to_thread(read)
    if checked.mode == "login":
        await _require_current_password(request, body.current_password, "creating an API key")

    def save():
        with session_factory() as session:
            lock_both_files(session)
            if login_store.get_login_state(session).epoch != checked.epoch:
                raise HTTPException(status_code=409, detail="the login changed while the key was made; try again")
            try:
                row, raw = login_store.create_api_key(session, name)
            except ValueError as exc:
                raise _bad_request(str(exc)) from exc
            session.commit()
            return {**_key_dict(row), "key": raw}, row.id, row.prefix

    created, key_id, prefix = await asyncio.to_thread(save)
    security_log.key_created(key_id, prefix)
    return created


@router.delete("/settings/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: int, session: Session = Depends(get_session)):
    lock_both_files(session)
    row = login_store.revoke_api_key(session, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such key")
    session.commit()
    security_log.key_revoked(row.id, row.prefix)
