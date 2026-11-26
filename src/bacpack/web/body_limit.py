"""Refuses a request body over a size cap with 413, before any route reads
it.

A JSON route reads its body whole into memory (whatever the content type
says), and /api/auth/login is open to anyone who reaches the web port, so
without a cap a few large posts can exhaust a small machine's memory. The
real JSON bodies are far smaller: a batch edit of 5,000 objects is about
40 KB. The routes that take a file upload get a cap that fits their files;
Starlette spools those to disk, not memory.
"""
from __future__ import annotations

import re

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api._uploads import MAX_UPLOAD_BYTES

MAX_BODY_BYTES = 8 * 1024 * 1024
# An EDE import takes two files (objects and state texts), plus form fields.
MAX_UPLOAD_BODY_BYTES = 2 * MAX_UPLOAD_BYTES + 1024 * 1024

_UPLOAD_PATHS = re.compile(r"/api/(ede/(read|plan|create)|devices/[^/]+/objects/import|config/import)")


def is_upload_path(path: str) -> bool:
    return _UPLOAD_PATHS.fullmatch(path) is not None


class _TooLarge(HTTPException):
    """An HTTPException, so FastAPI passes it on instead of turning a failed
    body read into a 400, and its 413 reaches the client from inside a route
    too."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body larger than {limit // (1024 * 1024)} MiB")


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None  # the server refuses a malformed one itself
    return None


class LimitRequestBody:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = MAX_UPLOAD_BODY_BYTES if is_upload_path(scope["path"]) else MAX_BODY_BYTES
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _refuse(_TooLarge(limit), scope, receive, send)
            return

        # A chunked body has no length up front: count it as it arrives.
        received = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge(limit)
            return message

        async def watched_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, watched_send)
        except _TooLarge as too_large:
            # Raised outside a route: by the JSON guard, which reads the body first.
            if started:
                raise
            await _refuse(too_large, scope, receive, send)


async def _refuse(too_large: _TooLarge, scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": too_large.detail}, status_code=413)(scope, receive, send)
