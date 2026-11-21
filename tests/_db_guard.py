"""Test guard: in a transaction that didn't start with BEGIN IMMEDIATE, reading
a file and then writing it fails at once with "database is locked" when another
process holds that file's write lock. Every connection gets a trace callback; a
violation by BACpack code (a frame in the bacpack package other than db.py,
whose flush listener runs inside every session) is recorded and fails the test.
It doesn't catch lost updates: reads before the implicit BEGIN hold no
snapshot, so two saves can still both write a value computed from the same
read."""
from __future__ import annotations

import re
import threading
import traceback
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine

import bacpack

# (test id or None between tests, thread name, message). The test id is what
# the autouse fixture set when the violation was recorded: a thread left
# running by an earlier test is still blamed on the test running now, so the
# thread name is kept to tell them apart.
VIOLATIONS: list[tuple[str | None, str, str]] = []
CURRENT_TEST: str | None = None

_PACKAGE_DIR = Path(bacpack.__file__).parent.as_posix()

_TABLE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+("?[\w.]+"?)', re.I)
_WRITE = re.compile(r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+("?[\w.]+"?)', re.I)
# The trace callback sees bound values filled in, so a stored string can
# hold SQL text.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _file(table: str) -> str:
    return "history" if table.strip('"').lower().startswith("history.") else "main"


class TransactionWatch:
    """Follows one connection's statements; `statement` returns a message
    when one is the read-then-write pattern."""

    def __init__(self) -> None:
        self.active = False
        self.immediate = False
        self.first: dict = {}

    def statement(self, sql: str):
        upper = sql.lstrip().upper()
        if upper.startswith("BEGIN"):
            self.active, self.immediate, self.first = True, "IMMEDIATE" in upper, {}
            return None
        if upper.startswith(("COMMIT", "ROLLBACK", "END")):
            self.active = False
            return None
        if not self.active or self.immediate or upper.startswith(("PRAGMA", "SAVEPOINT", "RELEASE", "--")):
            return None
        write = _WRITE.match(sql)
        target = _file(write.group(1)) if write else None
        if write and self.first.get(target) == "read":
            self.first[target] = "write"
            return f"wrote {target} after reading it in a plain BEGIN transaction: {sql[:160]}"
        for table in _TABLE.findall(_STRING_LITERAL.sub("''", sql)):
            if table.strip('"').lower().startswith("sqlite_"):
                continue
            self.first.setdefault(_file(table), "write" if write and _file(table) == target else "read")
        return None


def _package_relative(path: str, package_dir: str = _PACKAGE_DIR) -> str | None:
    """`path` relative to the bacpack package, or None outside it or for db.py."""
    path = path.replace("\\", "/")
    prefix = package_dir.replace("\\", "/").rstrip("/") + "/"
    if not path.lower().startswith(prefix.lower()):
        return None
    relative = path[len(prefix):]
    return None if relative == "db.py" else relative


def _bacpack_frames() -> list[str]:
    frames = []
    for frame in traceback.extract_stack():
        relative = _package_relative(frame.filename)
        if relative is not None:
            frames.append(f"{relative}:{frame.lineno} {frame.name}")
    return frames


def record(message: str) -> None:
    VIOLATIONS.append((CURRENT_TEST, threading.current_thread().name, message))


def take(test_id: str) -> list[str]:
    """Every recorded violation as a report line, emptying the list. One
    recorded between tests is reported too, labelled as such, not dropped."""
    found = [
        f"[thread {thread}] {message}" if test == test_id
        else f"[recorded between tests, thread {thread}] {message}" if test is None
        else f"[recorded during {test}, thread {thread}] {message}"
        for test, thread, message in VIOLATIONS
    ]
    VIOLATIONS.clear()
    return found


def _connect(dbapi_connection, connection_record) -> None:
    watch = TransactionWatch()

    def trace(sql: str) -> None:
        message = watch.statement(sql)
        if message:
            frames = _bacpack_frames()
            if frames:
                record(f"{message}\n    via {' <- '.join(reversed(frames[-4:]))}")

    dbapi_connection.set_trace_callback(trace)


def install() -> None:
    if not event.contains(Engine, "connect", _connect):
        event.listen(Engine, "connect", _connect)
