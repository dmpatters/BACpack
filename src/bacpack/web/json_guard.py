"""Refuses a JSON request body that holds an unpaired surrogate.

JSON allows "\\ud800" and pydantic accepts it, but the string can't be encoded
as UTF-8: it fails later at a password hash or the SQLite bind, and a 422 that
echoes it can't be encoded either. Rejecting it before any route runs covers
every text field of every route in one place.

/api/auth/login is left alone: its route counts the attempt as a failed login
for the lockout, which a refusal here would skip.
"""
from __future__ import annotations

import json
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..unicode_text import UNSTORABLE_TEXT, has_unpaired_surrogate

_UNCHECKED_PATHS = frozenset({"/api/auth/login"})
# Only a body that holds such an escape is parsed: normal bodies aren't parsed twice.
_SURROGATE_ESCAPE = re.compile(rb"\\[uU][dD][89a-fA-F][0-9a-fA-F]{2}")
_REFUSAL = {"detail": UNSTORABLE_TEXT}


def _is_json(scope: Scope) -> bool:
    for name, value in scope["headers"]:
        if name == b"content-type":
            media_type = value.split(b";", 1)[0].strip().lower()
            return media_type == b"application/json" or media_type.endswith(b"+json")
    return False


class RejectUnpairedSurrogates:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _UNCHECKED_PATHS or not _is_json(scope):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":  # a disconnect before the body ended
                await self.app(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        if _SURROGATE_ESCAPE.search(body):
            try:
                refused = has_unpaired_surrogate(json.loads(body))
            except (ValueError, RecursionError):
                refused = False  # the route reports bad JSON itself
            if refused:
                await JSONResponse(_REFUSAL, status_code=422)(scope, receive, send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
