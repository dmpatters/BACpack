"""How a parent recognises a device process it didn't spawn itself, beyond
psutil's create_time().

On Linux psutil derives create_time() from the wall clock, so an NTP step
moves every live device's creation time and a restarted parent would judge
them all dead. The boot id and the start time counted from boot don't move.
Both are Linux-only (None elsewhere): Windows creation times don't move with
the clock.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import psutil

from . import timeutil

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
# /proc/<pid>/stat field 22, counted from the field after the ")" that ends
# the command name (which can itself contain spaces and parentheses).
_STARTTIME_INDEX = 22 - 3


def read_boot_id() -> Optional[str]:
    """This boot's id, or None off Linux. Changes on every reboot, so a pid
    reused after a reboot never matches a row written before it."""
    try:
        return _BOOT_ID_PATH.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def record_identity(runtime, pid: int) -> None:
    """Set a DeviceRuntime's pid and the identity engine._is_same_process
    checks it by. Raises psutil.NoSuchProcess if `pid` is gone. The OS
    creation time, not now(): a now() taken after a slow start lagged by
    seconds, enough for a restarted parent to judge a live child dead."""
    created = psutil.Process(pid).create_time()
    runtime.process_pid = pid
    runtime.process_started_at = timeutil.to_db(timeutil.utc_from_timestamp(created))
    runtime.process_boot_id = read_boot_id()
    runtime.process_started_since_boot = started_since_boot(pid)


def started_since_boot(pid: int) -> Optional[float]:
    """Seconds from boot to `pid`'s start, read from /proc (clock ticks since
    boot), not from psutil, which caches a create_time() built from the wall
    clock. None off Linux, or when the process is gone or unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[_STARTTIME_INDEX]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None
