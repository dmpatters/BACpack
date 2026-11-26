"""Who is signed in, first-use setup, login and logout.
The password work (scrypt) runs in a worker thread
so it can't stall the shared event loop."""
from __future__ import annotations

import asyncio
import hmac
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import auth, login_store, security_log
from ...db import lock_both_files
from ..auth_middleware import SESSION_COOKIE, client_ip, cookie_kwargs
from ..deps import get_session
from ._lockout import locked as _locked

router = APIRouter()


class SetupBody(BaseModel):
    mode: Literal["login", "none"]
    username: Optional[str] = None
    password: Optional[str] = None
    confirm: bool = False


class LoginBody(BaseModel):
    username: str
    password: str


@router.get("/auth/state")
def get_auth_state(request: Request, session: Session = Depends(get_session)):
    state = login_store.get_login_state(session)
    authenticated = getattr(request.state, "auth", None)
    signed_in = state.mode == "login" and authenticated is not None and authenticated.kind in ("session", "key")
    return {"mode": state.mode, "signed_in": signed_in, "username": state.username if signed_in else None}


def _bad_request(problem: str) -> HTTPException:
    return HTTPException(status_code=400, detail=problem)


def _start_session(request: Request, response: Response, session: Session) -> None:
    token = login_store.create_session(session, client_ip(request), request.headers.get("user-agent"))
    session.commit()
    response.set_cookie(SESSION_COOKIE, token, **cookie_kwargs(request))


@router.post("/auth/setup")
async def setup(body: SetupBody, request: Request, response: Response):
    """Claims a fresh install: the first request wins, any later one is a 409."""
    ip = client_ip(request)
    session_factory = request.app.state.session_factory
    password_hash = None
    username = None
    if body.mode == "login":
        username = (body.username or "").strip()
        problem = auth.check_username_rules(username) or auth.check_password_rules(body.password or "", username)
        if problem:
            raise _bad_request(problem)
        password_hash = await asyncio.to_thread(auth.hash_password, body.password or "")
    elif not body.confirm:
        raise _bad_request("confirm that you want to run without a login")

    def save() -> bool:
        with session_factory() as session:
            lock_both_files(session)
            if not login_store.choose_mode_if_unset(session, body.mode):
                return False
            if username is not None and password_hash is not None:
                login_store.set_login(session, username, password_hash)
                _start_session(request, response, session)
            session.commit()
            return True

    if not await asyncio.to_thread(save):
        raise HTTPException(status_code=409, detail="a login mode has already been chosen")
    if username is not None:
        security_log.setup_chose_login(ip, username)
    else:
        security_log.setup_chose_none(ip)
    return {"mode": body.mode}


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = client_ip(request)
    app = request.app
    lockout = app.state.login_lockout
    session_factory = app.state.session_factory

    def read():
        with session_factory() as session:
            return login_store.get_login_state(session), login_store.password_hash_of(session)

    state, stored_hash = await asyncio.to_thread(read)
    if state.mode != "login":
        raise HTTPException(status_code=409, detail="login is not turned on")
    left = lockout.begin(ip)
    if left is not None:
        raise _locked(left)
    try:
        # An oversize or unstorable (unpaired surrogate) username or password
        # can't be right: a plain failure, counted, without a password check
        # an attacker could make arbitrarily expensive. The JSON guard leaves
        # this route alone so the attempt reaches the lockout.
        oversize = (
            len(body.username) > auth.MAX_USERNAME_LENGTH
            or len(body.password) > auth.MAX_PASSWORD_LENGTH
            or not (auth.is_storable(body.username) and auth.is_storable(body.password))
        )
        matched = (
            not oversize
            and state.username is not None
            and hmac.compare_digest(body.username.encode("utf-8"), state.username.encode("utf-8"))
        )
        ok = False
        if oversize:
            pass
        elif matched and stored_hash:
            ok = await asyncio.to_thread(auth.verify_password, body.password, stored_hash)
        else:
            await asyncio.to_thread(auth.dummy_verify, body.password)
        if not ok:
            security_log.login_failed(ip, matched)
            started = lockout.record_failure(ip)
            if started is not None:
                security_log.locked_out(ip, started)
                raise _locked(started)
            raise HTTPException(status_code=401, detail="wrong username or password")
        # Failures that finished while this check ran may have tripped the
        # lock: a right password doesn't get past one.
        left = lockout.check(ip)
        if left is not None:
            raise _locked(left)
        lockout.record_success(ip)
    finally:
        lockout.release(ip)

    def save():
        with session_factory() as session:
            lock_both_files(session)
            # A password change or login reset since the read ended every
            # session; one checked against the old password must not start.
            if login_store.get_login_state(session).epoch != state.epoch:
                raise HTTPException(status_code=401, detail="the login changed while signing in; sign in again")
            _start_session(request, response, session)

    await asyncio.to_thread(save)
    security_log.login_ok(ip, state.username or "")
    return {"mode": "login", "signed_in": True, "username": state.username}


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    session_factory = request.app.state.session_factory
    if token:

        def delete():
            with session_factory() as session:
                lock_both_files(session)
                login_store.delete_session(session, token)
                session.commit()

        await asyncio.to_thread(delete)
        security_log.logout(client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
