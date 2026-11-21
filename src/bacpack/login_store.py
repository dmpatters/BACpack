"""Login state, sessions and API keys in the main file.

Functions that write expect the caller to have called
db.lock_both_files(session) first and to commit. Tokens and keys are stored
only as sha256: both are 256-bit random values, so a plain hash is safe and
cheap to check on every request. A caller passes `now` (naive UTC, the stored
form, from timeutil) wherever time matters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import Callable, Optional, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session

from . import timeutil
from .models import ApiKey, ServerSettings, WebSession, get_or_create_settings

SESSION_IDLE = dt.timedelta(hours=8)
LAST_WRITE_MIN_INTERVAL = dt.timedelta(minutes=1)
KEY_PREFIX = "bpk_"
KEY_DISPLAY_PREFIX_LENGTH = 12
MAX_CREDENTIAL_LENGTH = 200


@dataclass(frozen=True)
class LoginState:
    mode: Optional[str]
    username: Optional[str]
    epoch: int


def get_login_state(session: Session) -> LoginState:
    settings = session.get(ServerSettings, 1)
    if settings is None:
        return LoginState(None, None, 0)
    return LoginState(settings.login_mode, settings.login_username, settings.login_epoch or 0)


def password_hash_of(session: Session) -> Optional[str]:
    settings = session.get(ServerSettings, 1)
    return None if settings is None else settings.login_password_hash


def set_login(session: Session, username: str, password_hash: str) -> None:
    """Login mode with these credentials; every session ends."""
    settings = get_or_create_settings(session)
    settings.login_mode = "login"
    settings.login_username = username
    settings.login_password_hash = password_hash
    settings.login_epoch = (settings.login_epoch or 0) + 1
    delete_all_sessions(session)


def set_username(session: Session, username: str) -> None:
    """A rename keeps every session: only a password change ends them."""
    get_or_create_settings(session).login_username = username


def set_no_login(session: Session) -> None:
    settings = get_or_create_settings(session)
    settings.login_mode = "none"
    settings.login_epoch = (settings.login_epoch or 0) + 1
    delete_all_sessions(session)


def choose_mode_if_unset(session: Session, mode: str) -> bool:
    """Sets the mode only while none is chosen; True when this call won, so
    two setup requests can't both claim a fresh install."""
    get_or_create_settings(session)
    result = cast(
        CursorResult,
        session.execute(
            update(ServerSettings)
            .where(ServerSettings.id == 1, ServerSettings.login_mode.is_(None))
            .values(login_mode=mode, login_epoch=ServerSettings.login_epoch + 1)
        ),
    )
    return result.rowcount == 1


def _hash(raw: str) -> Optional[str]:
    if not raw or len(raw) > MAX_CREDENTIAL_LENGTH:
        return None
    try:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


# --- sessions ----------------------------------------------------------------


def create_session(session: Session, ip: str, user_agent: Optional[str]) -> str:
    now = timeutil.db_utc_now()
    session.execute(delete(WebSession).where(WebSession.last_seen < now - SESSION_IDLE))
    token = secrets.token_urlsafe(32)
    session.add(
        WebSession(
            token_hash=_hash(token), created=now, last_seen=now, client_ip=ip, user_agent=(user_agent or "")[:300]
        )
    )
    return token


def resolve_session(session: Session, token: str, now: dt.datetime) -> Optional[int]:
    """Id of the live session this token belongs to. Read-only: the middleware
    calls it on the event loop, and create_session sweeps expired rows."""
    token_hash = _hash(token)
    if token_hash is None:
        return None
    row = session.scalar(select(WebSession).where(WebSession.token_hash == token_hash))
    if row is None:
        return None
    if now - row.last_seen > SESSION_IDLE:
        return None
    return row.id


def delete_session(session: Session, token: str) -> None:
    token_hash = _hash(token)
    if token_hash is not None:
        session.execute(delete(WebSession).where(WebSession.token_hash == token_hash))


def delete_all_sessions(session: Session) -> None:
    session.execute(delete(WebSession))


def touch_session(session_factory: Callable[[], Session], session_id: int, now: dt.datetime) -> None:
    with session_factory() as session:
        session.execute(update(WebSession).where(WebSession.id == session_id).values(last_seen=now))
        session.commit()


# --- API keys ------------------------------------------------------------------


def create_api_key(session: Session, name: str) -> tuple[ApiKey, str]:
    """The new row and the raw key, which is never stored."""
    name = name.strip()
    taken = session.scalar(select(ApiKey.id).where(ApiKey.name == name, ApiKey.revoked_at.is_(None)))
    if taken is not None:
        raise ValueError(f"a key named {name!r} already exists")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(
        name=name, key_hash=_hash(raw), prefix=raw[:KEY_DISPLAY_PREFIX_LENGTH], created=timeutil.db_utc_now()
    )
    session.add(row)
    session.flush()
    return row, raw


def resolve_api_key(session: Session, key: str) -> Optional[int]:
    key_hash = _hash(key) if key.startswith(KEY_PREFIX) else None
    if key_hash is None:
        return None
    return session.scalar(select(ApiKey.id).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)))


def touch_api_key(session_factory: Callable[[], Session], key_id: int, now: dt.datetime) -> None:
    with session_factory() as session:
        session.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used=now))
        session.commit()


def revoke_api_key(session: Session, key_id: int) -> Optional[ApiKey]:
    row = session.get(ApiKey, key_id)
    if row is None or row.revoked_at is not None:
        return None
    row.revoked_at = timeutil.db_utc_now()
    return row


def list_api_keys(session: Session) -> list[ApiKey]:
    return list(session.scalars(select(ApiKey).where(ApiKey.revoked_at.is_(None)).order_by(ApiKey.id)))


class WriteThrottle:
    """True at most once a minute per key: last_seen and last_used are
    bookkeeping, not worth a write per request."""

    # Past this many keys, entries older than the interval are dropped: they
    # no longer hold anything back.
    MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._last: dict = {}

    def due(self, key, now: dt.datetime) -> bool:
        if len(self._last) > self.MAX_ENTRIES:
            self._last = {k: t for k, t in self._last.items() if now - t < LAST_WRITE_MIN_INTERVAL}
        last = self._last.get(key)
        if last is not None and now - last < LAST_WRITE_MIN_INTERVAL:
            return False
        self._last[key] = now
        return True
