"""Reading an uploaded file, shared by the routers that take one (routers
never import each other)."""
from __future__ import annotations

from fastapi import HTTPException, UploadFile

# Every import refuses a bigger upload. An exported object is about 1.75 KB of
# JSON config, so 64 MiB is about 38k objects; parsing a file that size peaks
# near 140 MiB.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024


def read_upload(file: UploadFile) -> bytes:
    """The upload's bytes, read in chunks from its spooled file so an
    oversized one is refused without holding it all in memory. Blocking:
    call from a worker thread."""
    chunks = []
    size = 0
    while chunk := file.file.read(UPLOAD_CHUNK_BYTES):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"file larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB")
        chunks.append(chunk)
    return b"".join(chunks)
