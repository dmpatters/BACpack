"""Password hashing and the password and username rules: stdlib
hashlib.scrypt, rather than a dependency for a single credential pair.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

_SALT_BYTES = 16
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

# NIST SP 800-63B: length, not composition.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
MAX_USERNAME_LENGTH = 64


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return f"{salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, derived_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(derived_hex)
    except (ValueError, TypeError):
        return False

    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return hmac.compare_digest(derived, expected)


_dummy_hash: Optional[str] = None


def dummy_verify(password: str) -> None:
    """One full scrypt against a throwaway hash: a wrong username costs the
    same time as a wrong password, so the response doesn't tell which was
    wrong."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def is_storable(text: str) -> bool:
    """False for a string with an unpaired surrogate, which has no UTF-8 form."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


_UNSTORABLE = "can't contain an unpaired surrogate"


def check_username_rules(username: str) -> Optional[str]:
    """What is wrong with this username, or None."""
    stripped = username.strip()
    if not is_storable(stripped):
        return f"username {_UNSTORABLE}"
    if not stripped:
        return "username can't be blank"
    if len(stripped) > MAX_USERNAME_LENGTH:
        return f"username must be at most {MAX_USERNAME_LENGTH} characters"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        return "username can't contain control characters"
    return None


def check_password_rules(password: str, username: Optional[str]) -> Optional[str]:
    """What is wrong with this password, or None."""
    if not is_storable(password):
        return f"password {_UNSTORABLE}"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"password must be at most {MAX_PASSWORD_LENGTH} characters"
    if username is not None and password == username.strip():
        return "password can't be the same as the username"
    return None
