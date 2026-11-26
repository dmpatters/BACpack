"""Encryption at rest for connector credentials.

A Fernet key lives in <data dir>/secret.key. The column types below encrypt on
write and decrypt on read, so no code path can store a credential in plain
text and no call site handles ciphertext. Only the web process (init_db, a
save) ever creates the key; a device process only reads it, because it loads
connector rows too and must not fail, or invent a new key, when it is gone.

The data dir travels on the engine's dialect (`attach`), which is what a
column type is handed: two engines in one process, on different folders, each
use their own key.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import JSON, String, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.types import TypeDecorator

from .data_files import hand_to_owner, owner_for

logger = logging.getLogger("bacpack.security")

PREFIX = "enc:v1:"
KEY_FILE = "secret.key"
_DIALECT_ATTRIBUTE = "bacpack_data_dir"

_fernets: dict[Path, Fernet] = {}


class Unreadable(str):
    """A stored credential that cannot be decrypted. Always empty, so every
    "is it set" check already treats it as missing, but it keeps the stored
    ciphertext: a save that rewrites the row writes it back unchanged, so a
    good key put back later still opens it."""

    ciphertext: str

    def __new__(cls, ciphertext: str = "") -> "Unreadable":
        self = super().__new__(cls, "")
        self.ciphertext = ciphertext
        return self


class SecretKeyProblem(RuntimeError):
    """secret.key is unusable, so a credential cannot be saved."""

    def __init__(self, key_file: Path):
        self.key_file = key_file
        super().__init__(
            f"The encryption key file {key_file} is unusable, so credentials cannot be saved. "
            "Restore a good secret.key there, or delete it to have a new one made "
            "(every saved connector credential must then be entered again)."
        )


def attach(engine: Engine, data_dir: Path) -> None:
    setattr(engine.dialect, _DIALECT_ATTRIBUTE, Path(data_dir).resolve())


def data_dir_of(engine_or_dialect: Any) -> Path:
    dialect = getattr(engine_or_dialect, "dialect", engine_or_dialect)
    data_dir = getattr(dialect, _DIALECT_ATTRIBUTE, None)
    if data_dir is None:
        raise RuntimeError("this engine was not made by db.make_engine, so it has no secret.key folder")
    return data_dir


def key_path(data_dir: Path) -> Path:
    return data_dir / KEY_FILE


def _load(data_dir: Path) -> Optional[Fernet]:
    """The key, or None when the file is missing or unusable. A missing file
    is not cached: the web process may make it later."""
    cached = _fernets.get(data_dir)
    if cached is not None:
        return cached
    try:
        fernet = Fernet(key_path(data_dir).read_bytes().strip())
    except (OSError, ValueError):
        return None
    _fernets[data_dir] = fernet
    return fernet


def ensure_key(data_dir: Path) -> bool:
    """Make the key file if none exists (True). An existing file is never
    touched; an unusable one is logged."""
    path = key_path(data_dir)
    if path.exists():
        if _load(data_dir) is None:
            logger.error(
                "the encryption key file %s is unusable; connector credentials can't be read or saved. "
                "Restore it, or delete it and enter the credentials again",
                path,
            )
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp makes the file 0600; linking it into place is atomic and fails
    # rather than replaces if another process got there first.
    key = Fernet.generate_key()
    owner = owner_for(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=KEY_FILE + ".", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        if os.name != "nt":
            os.chmod(temp, 0o600)
        hand_to_owner(temp, owner)
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        except OSError:
            # FAT/exFAT, some SMB shares and container bind mounts refuse hard
            # links. Exclusive create still never replaces another process's
            # key; a reader in the moment before the write sees an invalid
            # key, which counts as missing and isn't cached.
            if not _create_in_place(path, key, owner):
                return False
    finally:
        temp.unlink(missing_ok=True)
    _fernets.pop(data_dir, None)
    return True


def _create_in_place(path: Path, key: bytes, owner) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        hand_to_owner(path, owner)
    except BaseException:
        # A half-written key would be "unusable" on every later start.
        path.unlink(missing_ok=True)
        raise
    return True


def encrypt(data_dir: Path, value: str) -> str:
    fernet = _load(data_dir)
    if fernet is None:
        if not key_path(data_dir).exists():
            ensure_key(data_dir)
            fernet = _load(data_dir)
        if fernet is None:
            raise SecretKeyProblem(key_path(data_dir))
    return PREFIX + fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(data_dir: Path, stored: str) -> str:
    """A value without the prefix is returned as is (a row from before the
    column was encrypted). One that can't be decrypted is Unreadable."""
    if not stored.startswith(PREFIX):
        return stored
    fernet = _load(data_dir)
    if fernet is None:
        return Unreadable(stored)
    try:
        return fernet.decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return Unreadable(stored)


def _seal(dialect, value: Any) -> Any:
    if isinstance(value, Unreadable):
        return value.ciphertext
    if not isinstance(value, str) or not value:
        return value
    return encrypt(data_dir_of(dialect), value)


def _open(dialect, value: Any) -> Any:
    return decrypt(data_dir_of(dialect), value) if isinstance(value, str) and value else value


class EncryptedText(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return _seal(dialect, value)

    def process_result_value(self, value, dialect):
        return _open(dialect, value)


class EncryptedHeaders(TypeDecorator):
    """A dict of header name to value; the values are encrypted."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, dict):
            return {name: _seal(dialect, item) for name, item in value.items()}
        return value

    def process_result_value(self, value, dialect):
        if isinstance(value, dict):
            return {name: _open(dialect, item) for name, item in value.items()}
        return value


_SELECT_CREDENTIALS = text("SELECT id, auth_password, auth_token, headers FROM main.connectors")


def _is_plain(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith(PREFIX)


def _headers_of(headers_json: Any) -> Any:
    return json.loads(headers_json) if isinstance(headers_json, str) else headers_json


def _has_plain(rows) -> bool:
    for _, password, token, headers_json in rows:
        if _is_plain(password) or _is_plain(token):
            return True
        headers = _headers_of(headers_json)
        if isinstance(headers, dict) and any(_is_plain(item) for item in headers.values()):
            return True
    return False


def _unreadable_count(data_dir: Path, rows) -> int:
    count = 0
    for _, password, token, headers_json in rows:
        values = [password, token]
        headers = _headers_of(headers_json)
        if isinstance(headers, dict):
            values.extend(headers.values())
        count += sum(
            isinstance(item, str) and item.startswith(PREFIX) and isinstance(decrypt(data_dir, item), Unreadable)
            for item in values
        )
    return count


def _updates_for(data_dir: Path, rows) -> dict[int, dict[str, Any]]:
    """Raises SecretKeyProblem before anything is written."""
    updates: dict[int, dict[str, Any]] = {}
    for row_id, password, token, headers_json in rows:
        row_updates: dict[str, Any] = {}
        for column, value in (("auth_password", password), ("auth_token", token)):
            if _is_plain(value):
                row_updates[column] = encrypt(data_dir, value)
        headers = _headers_of(headers_json)
        if isinstance(headers, dict) and any(_is_plain(item) for item in headers.values()):
            row_updates["headers"] = json.dumps(
                {name: encrypt(data_dir, item) if _is_plain(item) else item for name, item in headers.items()}
            )
        if row_updates:
            updates[row_id] = row_updates
    return updates


def rewrite_plaintext(engine: Engine) -> tuple[int, int]:
    """Encrypt every plain-text connector credential, with raw SQL so the
    column types stay out of it. Returns (rows changed, values that already
    carry the prefix but cannot be decrypted); those are left alone.

    A read-only pass decides whether there is anything to do. Only then is the
    write lock taken, before the rows are read again: a device process may
    commit between a read and a first write, which fails at once with
    "database is locked" instead of waiting. An unusable key leaves the plain
    rows as they are, with one warning, so a start never fails over it. So does
    a file a device process keeps locked past the busy timeout (BEGIN IMMEDIATE
    takes the write lock on both attached files): the rows stay plain until the
    next start. Both cases return (0, 0)."""
    data_dir = data_dir_of(engine)
    with engine.connect() as connection:
        rows = connection.execute(_SELECT_CREDENTIALS).all()
    changed = 0
    if _has_plain(rows):
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                rows = connection.execute(_SELECT_CREDENTIALS).all()
                updates = _updates_for(data_dir, rows)
                for row_id, row_updates in updates.items():
                    assignments = ", ".join(f"{column} = :{column}" for column in row_updates)
                    connection.execute(
                        text(f"UPDATE main.connectors SET {assignments} WHERE id = :id"), {**row_updates, "id": row_id}
                    )
                connection.commit()
                changed = len(updates)
        except SecretKeyProblem as problem:
            logger.warning(
                "connector credentials are still stored as plain text: the encryption key file %s is unusable. "
                "Restore it, or delete it and start again",
                problem.key_file,
            )
            return 0, 0
        except OperationalError as exc:
            message = str(getattr(exc, "orig", exc)).lower()
            if "locked" not in message and "busy" not in message:
                raise
            logger.warning(
                "could not encrypt the plain-text connector credentials (the database is busy), "
                "will retry on the next start: %s",
                exc,
            )
            return 0, 0
        with engine.connect() as connection:
            rows = connection.execute(_SELECT_CREDENTIALS).all()
    unreadable = _unreadable_count(data_dir, rows)
    if unreadable:
        logger.warning(
            "%d connector credentials can't be decrypted with %s; open each connector and enter them again",
            unreadable,
            KEY_FILE,
        )
    return changed, unreadable
