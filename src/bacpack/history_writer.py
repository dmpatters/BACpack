"""One writer thread per process for the log and traffic tables, so logging
never does database work on a device's event loop.

Callers build a row and `put` it; the thread writes whatever is queued in one
transaction on the history file. It never logs through the `bacpack` logger
(that would queue a line about failing to write lines): its own failures go
to stderr.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import queue
import sys
import threading
import time
from typing import Optional, Union

from sqlalchemy import insert, text
from sqlalchemy.exc import OperationalError

from . import timeutil
from .models import LogEntry, TrafficLogEntry

BATCH_SIZE = 500
MAX_QUEUED = 10_000
# Bounded so a locked file can't hold a stopping process past the engine's
# STOP_GRACE_SECONDS.
EXIT_FLUSH_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.5


@dataclasses.dataclass(frozen=True)
class LogRow:
    device_id: Optional[int]
    level: str
    logger_name: str
    message: str
    timestamp: dt.datetime


@dataclasses.dataclass(frozen=True)
class TrafficRow:
    device_id: int
    event_type: str
    detail: str
    timestamp: dt.datetime
    # The device's cap on its own rows.
    keep: int


def produced_at(seconds: float) -> dt.datetime:
    """The stored form of a POSIX time, whole seconds like the tables'
    CURRENT_TIMESTAMP default."""
    return timeutil.to_db(timeutil.utc_from_timestamp(seconds)).replace(microsecond=0)


class _Marker:
    def __init__(self) -> None:
        self.done = threading.Event()


_STOP = object()


_current: Optional["HistoryWriter"] = None


def current() -> Optional["HistoryWriter"]:
    """This process's writer, or None before `install`."""
    return _current


def install(writer: "HistoryWriter") -> None:
    """Makes `writer` the process's writer and closes the one it replaces
    (a test's fresh app, a second install)."""
    global _current
    previous, _current = _current, writer
    if previous is not None:
        previous.close()


def flush_current(timeout: Optional[float] = None) -> bool:
    """Flushes this process's writer; True at once when none is installed."""
    writer = _current
    return True if writer is None else writer.flush(timeout)


def close_current(timeout: Optional[float] = None) -> None:
    """Flushes and stops this process's writer, for an exit."""
    global _current
    writer, _current = _current, None
    if writer is not None:
        writer.close(timeout)


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _is_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


class HistoryWriter:
    def __init__(self, session_factory, *, device_id: Optional[int] = None) -> None:
        self._session_factory = session_factory
        self._device_id = device_id
        self._pending: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self._thread = threading.Thread(target=self._run, name="history-writer", daemon=True)
        self._closed = False
        self._abandon = threading.Event()
        self._drop_lock = threading.Lock()
        self._dropped_logs = 0
        self._dropped_traffic = 0
        self._last_error: Optional[str] = None

    @property
    def dropped(self) -> tuple[int, int]:
        """Log lines and traffic entries dropped and not yet reported."""
        with self._drop_lock:
            return self._dropped_logs, self._dropped_traffic

    def start(self) -> None:
        self._thread.start()

    def put(self, row: Union[LogRow, TrafficRow]) -> bool:
        """Queues a row without ever waiting; False when it was dropped."""
        if not self._closed:
            try:
                self._pending.put_nowait(row)
                return True
            except queue.Full:
                pass
        with self._drop_lock:
            if isinstance(row, LogRow):
                self._dropped_logs += 1
            else:
                self._dropped_traffic += 1
        return False

    def flush(self, timeout: Optional[float] = None) -> bool:
        """True once everything put before the call is committed (or dropped
        as unwritable); False when the timeout passed first or the thread
        has stopped."""
        deadline = time.monotonic() + (EXIT_FLUSH_SECONDS if timeout is None else timeout)
        if not self._thread.is_alive():
            return False
        marker = _Marker()
        try:
            self._pending.put(marker, timeout=_left(deadline))
        except queue.Full:
            return False
        return marker.done.wait(_left(deadline))

    def close(self, timeout: Optional[float] = None) -> None:
        """Flushes, then stops the thread, all within one timeout: the engine
        kills a device that takes longer than STOP_GRACE_SECONDS to stop."""
        if self._closed:
            return
        deadline = time.monotonic() + (EXIT_FLUSH_SECONDS if timeout is None else timeout)
        # Flush before abandoning, so a locked batch keeps retrying until the deadline.
        self.flush(_left(deadline))
        self._closed = True
        self._abandon.set()
        try:
            self._pending.put(_STOP, timeout=_left(deadline))
        except queue.Full:
            return
        if self._thread.is_alive():
            self._thread.join(_left(deadline))

    def _run(self) -> None:
        while True:
            item = self._pending.get()
            stopping = item is _STOP
            batch = [] if stopping else [item]
            emptied = False
            # Once stopping, everything left goes in the last batch, including
            # rows a put queued behind the stop while close began.
            while stopping or len(batch) < BATCH_SIZE:
                try:
                    nxt = self._pending.get_nowait()
                except queue.Empty:
                    emptied = True
                    break
                if nxt is _STOP:
                    stopping = True
                else:
                    batch.append(nxt)
            if emptied:
                summary = self._drop_summary()
                if summary is not None:
                    batch.append(summary)
            self._write_with_retry(batch)
            for entry in batch:
                if isinstance(entry, _Marker):
                    entry.done.set()
            if stopping:
                return

    def _drop_summary(self) -> Optional[LogRow]:
        with self._drop_lock:
            logs, traffic = self._dropped_logs, self._dropped_traffic
            self._dropped_logs = self._dropped_traffic = 0
        if not logs and not traffic:
            return None
        return LogRow(
            self._device_id,
            "WARNING",
            "bacpack.history_writer",
            f"{logs} log lines and {traffic} traffic entries were dropped: the log queue was full",
            produced_at(timeutil.host_seconds()),
        )

    def _write_with_retry(self, batch: list) -> None:
        while True:
            try:
                self._write_batch(batch)
            except Exception as exc:
                if _is_lock_error(exc) and not self._abandon.is_set():
                    # Keep the batch: a long prune or restore delays entries, it doesn't lose them.
                    self._abandon.wait(LOCK_RETRY_SECONDS)
                    continue
                self._report(exc)
                return
            self._last_error = None
            return

    def _report(self, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if message != self._last_error:
            self._last_error = message
            print(f"bacpack history writer: dropped a batch of log/traffic rows: {message}", file=sys.stderr)

    def _write_batch(self, batch: list) -> None:
        logs = [
            {
                "device_id": r.device_id,
                "level": r.level,
                "logger_name": r.logger_name,
                "message": r.message,
                "timestamp": r.timestamp,
            }
            for r in batch
            if isinstance(r, LogRow)
        ]
        traffic = [r for r in batch if isinstance(r, TrafficRow)]
        if not logs and not traffic:
            return
        caps: dict[int, int] = {}
        for r in traffic:
            caps[r.device_id] = min(r.keep, caps.get(r.device_id, r.keep))
        with self._session_factory() as session:
            if logs:
                session.execute(insert(LogEntry), logs)
            if traffic:
                session.execute(
                    insert(TrafficLogEntry),
                    [
                        {
                            "device_id": r.device_id,
                            "event_type": r.event_type,
                            "detail": r.detail,
                            "timestamp": r.timestamp,
                        }
                        for r in traffic
                    ],
                )
                for device_id, cap in caps.items():
                    # Older than the cap-th newest row; one statement, no COUNT.
                    session.execute(
                        text(
                            "DELETE FROM history.traffic_log_entries WHERE device_id = :d AND id < "
                            "(SELECT id FROM history.traffic_log_entries WHERE device_id = :d "
                            "ORDER BY id DESC LIMIT 1 OFFSET :n)"
                        ),
                        {"d": device_id, "n": cap - 1},
                    )
            session.commit()
