"""Bridges Python's `logging` into the LogEntry table so BACpack's own
messages show in the UI. Each process persists
its own records through SQLite, like traffic.py; only INFO and above.
"""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import contextmanager
from typing import Optional

from . import history_writer, timeutil
from .db import delete_in_chunks
from .history_writer import HistoryWriter, LogRow, produced_at
from .models import LogEntry, get_or_create_settings


class SQLiteLogHandler(logging.Handler):
    """Queues each record for the process's HistoryWriter;
    no database work in the caller."""

    def __init__(self, writer: HistoryWriter, *, device_id: Optional[int] = None) -> None:
        super().__init__()
        self._writer = writer
        self._device_id = device_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            device_id = getattr(record, "device_id", self._device_id)
            # Formatted here, while the traceback still exists.
            message = self.format(record) if record.exc_info else record.getMessage()
            self._writer.put(
                LogRow(
                    device_id,
                    record.levelname,
                    record.name,
                    message,
                    produced_at(record.created),
                )
            )
        except Exception:
            # Never let a formatting or queueing failure out of emit().
            # handleError, not logger.exception, which could re-enter this
            # very handler.
            self.handleError(record)


def install_log_capture(session_factory, *, device_id: Optional[int] = None) -> None:
    """Wire SQLiteLogHandler into the `bacpack` logger for this process
    (device_id=None in the parent), and at WARNING into `asyncio` and
    `bacpypes3`. Idempotent: replaces the handlers it installed before,
    since tests create a fresh app per test.

    The WARNING StreamHandler keeps console output: Python's lastResort
    handler stops firing once any handler exists in a logger's chain.
    """
    logger = logging.getLogger("bacpack")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        if isinstance(h, (SQLiteLogHandler, logging.StreamHandler)):
            logger.removeHandler(h)

    writer = HistoryWriter(session_factory, device_id=device_id)
    writer.start()
    history_writer.install(writer)
    db_handler = SQLiteLogHandler(writer, device_id=device_id)
    db_handler.setLevel(logging.INFO)
    logger.addHandler(db_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    logger.addHandler(console_handler)

    for name in _LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        _remove_library_handlers(library)
        library_db = _LibraryDBHandler(writer, device_id=device_id)
        library_db.setLevel(logging.WARNING)
        library.addHandler(library_db)
        library_console = _LibraryConsoleHandler()
        library_console.setLevel(logging.WARNING)
        library.addHandler(library_console)


# An exception in a loop callback or an unawaited task is logged on `asyncio`
# (with its traceback, by the loop's default exception handler); bacpypes3
# logs on `bacpypes3.*`. Under a service a device process's stderr is the
# null device, so without these they'd be lost. WARNING and up only: the
# library debug output stays off.
_LIBRARY_LOGGERS = ("asyncio", "bacpypes3")


class _LibraryDBHandler(SQLiteLogHandler):
    pass


class _LibraryConsoleHandler(logging.StreamHandler):
    """A handler anywhere in the chain turns off Python's lastResort output,
    so the console needs its own."""


def _remove_library_handlers(library: logging.Logger) -> None:
    for handler in list(library.handlers):
        if isinstance(handler, (_LibraryDBHandler, _LibraryConsoleHandler)):
            library.removeHandler(handler)


def remove_log_capture_handlers() -> None:
    """Take out every handler install_log_capture added, on `bacpack` and on
    the library loggers."""
    logger = logging.getLogger("bacpack")
    for handler in list(logger.handlers):
        if isinstance(handler, (SQLiteLogHandler, logging.StreamHandler)):
            logger.removeHandler(handler)
    for name in _LIBRARY_LOGGERS:
        _remove_library_handlers(logging.getLogger(name))


@contextmanager
def logs_page_logging(session_factory):
    """install_log_capture for a short-lived process (the launcher, bacpack-admin):
    on exit the queued records are written and the handlers removed."""
    install_log_capture(session_factory)
    try:
        yield
    finally:
        history_writer.close_current()
        remove_log_capture_handlers()


def prune_old_log_entries(session_factory) -> int:
    """Delete LogEntry rows older than ServerSettings.log_retention_days.
    None means "keep forever". Returns the number of rows deleted."""
    with session_factory() as session:
        settings = get_or_create_settings(session)
        if settings.log_retention_days is None:
            return 0

        cutoff = timeutil.db_cutoff(dt.timedelta(days=settings.log_retention_days))
    return delete_in_chunks(session_factory, LogEntry, LogEntry.timestamp < cutoff)
