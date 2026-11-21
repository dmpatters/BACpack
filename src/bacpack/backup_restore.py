"""The DM-BR-B backup/restore session for one device: its state, the config
file's bytes and the Backup_Failure_Timeout timer. No BACnet I/O, so the
Device object, the File object and the application's handlers share one
instance.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bacpypes3.errors import ExecutionError

from . import device_clock

logger = logging.getLogger(__name__)

CONFIG_FILE_INSTANCE = 1
# Seconds; the default for a new device's saved Backup_Failure_Timeout,
# which a client can write.
BACKUP_FAILURE_TIMEOUT = 60
RESTORE_COMPLETION_TIME = 10  # seconds
# Matches the config import's limit (about 38k objects), so a big device's own
# backup restores; it also stops a runaway client exhausting memory.
MAX_RESTORE_BYTES = 64 * 1024 * 1024

_IDLE = "idle"
_BACKUP = "performingABackup"
_RESTORE = "performingARestore"
_SESSION_WORDS = {_BACKUP: "backup", _RESTORE: "restore"}


def refusal(error_class: str, error_code: str, reason: Optional[str] = None) -> ExecutionError:
    """The Error sent back, plus a plain `reason`: the wire only
    carries the class/code, and YABE shows one generic message for any
    Reinitialize failure, so LoggingApplication logs the reason to the
    Traffic and Logs pages."""
    error = ExecutionError(errorClass=error_class, errorCode=error_code)
    error.reason = reason
    return error


class BackupRestore:
    def __init__(self, timeout: float = BACKUP_FAILURE_TIMEOUT) -> None:
        self.timeout = timeout
        self.state = _IDLE
        self.modified: Optional[float] = None
        # "backup"/"restore" when the last session ended on
        # Backup_Failure_Timeout, so a late endBackup can say so.
        self.timed_out: Optional[str] = None
        self._data = b""
        self._timer: Optional[asyncio.TimerHandle] = None

    @property
    def size(self) -> int:
        return len(self._data)

    # --- Session transitions (ReinitializeDevice) -------------------------

    def start_backup(self, snapshot: bytes) -> None:
        self.require(_IDLE)
        self.timed_out = None
        self.state = _BACKUP
        self._data = snapshot
        self.modified = device_clock.now()
        self._touch()

    def end_backup(self) -> None:
        self.require(_BACKUP)
        self.reset()

    def start_restore(self) -> None:
        self.require(_IDLE)
        self.timed_out = None
        self.state = _RESTORE
        self._data = b""
        self.modified = None
        self._touch()

    def abort_restore(self) -> None:
        self.require(_RESTORE)
        self.reset()

    def take_restore_data(self) -> bytes:
        """endRestore: hands back the buffer and ends the session, whether
        or not the caller then manages to apply it."""
        self.require(_RESTORE)
        data = self._data
        self.reset()
        return data

    def reset(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.state = _IDLE
        self._data = b""
        self.modified = None
        self.timed_out = None

    # --- File access (AtomicReadFile / AtomicWriteFile / File_Size) -------

    def read(self, start: int, count: int) -> tuple[bytes, bool]:
        if start < 0 or start > len(self._data):
            raise refusal(
                "services",
                "invalidFileStartPosition",
                f"start position {start} is outside the file ({len(self._data)} octets)",
            )
        chunk = self._data[start : start + count]
        if self.state != _IDLE:
            self._touch()
        return chunk, start + len(chunk) >= len(self._data)

    def write(self, start: int, chunk: bytes) -> int:
        """Returns the actual start position (resolves -1, append)."""
        if self.state != _RESTORE:
            raise refusal("object", "fileAccessDenied", self._wrong_state_reason(_RESTORE))
        if start == -1:
            start = len(self._data)
        if start < 0 or start > len(self._data):
            raise refusal(
                "services",
                "invalidFileStartPosition",
                f"start position {start} is past the end of the data written so far "
                f"({len(self._data)} octets)",
            )
        data = self._data[:start] + chunk + self._data[start + len(chunk) :]
        if len(data) > MAX_RESTORE_BYTES:
            raise refusal(
                "object",
                "fileFull",
                f"the restore file would be larger than {MAX_RESTORE_BYTES // (1024 * 1024)} MiB",
            )
        self._data = data
        self.modified = device_clock.now()
        self._touch()
        return start

    def truncate(self, size: int) -> None:
        """A File_Size write: some clients zero the file before writing."""
        if self.state != _RESTORE:
            raise refusal("property", "writeAccessDenied")
        if size != 0:
            raise refusal("property", "valueOutOfRange")
        self._data = b""
        self.modified = device_clock.now()
        self._touch()

    @property
    def system_status(self) -> str:
        """System_Status for the session: a client that predates
        Backup_And_Restore_State has only this to see a session by."""
        return {_BACKUP: "backupInProgress", _RESTORE: "downloadInProgress"}.get(self.state, "operational")

    def require(self, state: str) -> None:
        """configurationInProgress, with its reason, unless the session is
        in `state`."""
        if self.state != state:
            raise refusal("device", "configurationInProgress", self._wrong_state_reason(state))

    # --- Internals ---------------------------------------------------------

    def _wrong_state_reason(self, wanted: str) -> str:
        if self.state != _IDLE:
            return f"a {_SESSION_WORDS[self.state]} is still in progress"
        reason = f"no {_SESSION_WORDS[wanted]} is in progress"
        if self.timed_out is not None:
            reason += (
                f" (the last {self.timed_out} timed out after {self.timeout:g} s "
                "without activity, Backup_Failure_Timeout)"
            )
        return reason

    def _touch(self) -> None:
        """(Re)starts Backup_Failure_Timeout. No running loop (a plain sync
        caller) means no timer, which only ever happens in tests."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = loop.call_later(self.timeout, self._timed_out)

    def _timed_out(self) -> None:
        self._timer = None
        kind = _SESSION_WORDS.get(self.state)
        self.reset()
        self.timed_out = kind
        if kind is not None:
            logger.warning(
                "%s timed out after %g s without activity (Backup_Failure_Timeout); session ended",
                kind,
                self.timeout,
            )
