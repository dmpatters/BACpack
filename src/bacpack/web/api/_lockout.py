"""The refusal both password checks (the login page and the current-password
check) give while the per-address lockout holds."""
from __future__ import annotations

from fastapi import HTTPException


def locked(seconds: float) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="too many failed logins; try again later",
        headers={"Retry-After": str(max(1, round(seconds)))},
    )
