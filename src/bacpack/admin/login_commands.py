"""reset-login, set-login, no-login and the login lines of status."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select

from .. import auth, login_store, security_log
from ..db import lock_both_files
from ..models import WebSession

PASSWORD_ENV = "BACPACK_LOGIN_PASSWORD"
_LOCAL = "this machine"


def register(subparsers) -> None:
    reset = subparsers.add_parser("reset-login", help="set a new login password here (ends every session)")
    reset.add_argument("--username", help="a new username (default: keep the current one)")
    reset.set_defaults(handler=_reset_login)

    setter = subparsers.add_parser("set-login", help="turn the login on; the password is read from BACPACK_LOGIN_PASSWORD or prompted")
    setter.add_argument("--username", required=True)
    setter.set_defaults(handler=_set_login)

    none = subparsers.add_parser("no-login", help="record that BACpack runs without a login")
    none.set_defaults(handler=_no_login)


def _ask_twice(context) -> Optional[str]:
    first = context.prompt("New password: ")
    if first != context.prompt("Type it again: "):
        print("The two passwords differ; nothing was changed.", file=context.out)
        return None
    return first


def _save_login(context, username: str, password: str) -> int:
    problem = auth.check_username_rules(username) or auth.check_password_rules(password, username)
    if problem:
        print(f"{problem}; nothing was changed.", file=context.out)
        return 1
    password_hash = auth.hash_password(password)
    with context.session_factory() as session:
        lock_both_files(session)
        login_store.set_login(session, username, password_hash)
        session.commit()
    security_log.local_reset(username)
    return 0


def _reset_login(args, context) -> int:
    with context.session_factory() as session:
        current = login_store.get_login_state(session).username
    username = (args.username or current or "").strip()
    if not username:
        print("There is no login yet: pass --username NAME.", file=context.out)
        return 1
    password = _ask_twice(context)
    if password is None:
        return 1
    status = _save_login(context, username, password)
    if status == 0:
        print(f"The login for {username!r} has a new password and every session has ended.", file=context.out)
        print("A running BACpack needs no restart.", file=context.out)
    return status


def _set_login(args, context) -> int:
    username = args.username.strip()
    password = context.environ.get(PASSWORD_ENV) or _ask_twice(context)
    if password is None:
        return 1
    status = _save_login(context, username, password)
    if status == 0:
        print(f"Login turned on for {username!r}.", file=context.out)
    return status


def _no_login(args, context) -> int:
    with context.session_factory() as session:
        lock_both_files(session)
        login_store.set_no_login(session)
        session.commit()
    security_log.mode_switched(_LOCAL, "none")
    print("BACpack will run without a login.", file=context.out)
    return 0


def status_lines(context) -> list[str]:
    with context.session_factory() as session:
        state = login_store.get_login_state(session)
        sessions = session.scalar(select(func.count()).select_from(WebSession))
        keys = len(login_store.list_api_keys(session))
    mode = {"login": "login required", "none": "no login", None: "not chosen yet (the setup screen is waiting)"}[state.mode]
    lines = [f"Login:    {mode}"]
    if state.username:
        lines.append(f"Username: {state.username}")
    lines += [f"Sessions: {sessions}", f"API keys: {keys}"]
    return lines
