"""process_identity: the Linux boot id and a process's start time since
boot, which a wall-clock step doesn't move. Both are None off Linux."""
from __future__ import annotations

import os
import sys

import psutil

from bacpack.process_identity import read_boot_id, started_since_boot

_LINUX = sys.platform.startswith("linux")


def test_read_boot_id_is_stable_on_linux_and_none_elsewhere():
    if _LINUX:
        boot_id = read_boot_id()
        assert boot_id and boot_id == read_boot_id()
    else:
        assert read_boot_id() is None


def test_started_since_boot_matches_psutil_on_linux_and_is_none_elsewhere():
    since_boot = started_since_boot(os.getpid())
    if _LINUX:
        expected = psutil.Process(os.getpid()).create_time() - psutil.boot_time()
        assert since_boot is not None and abs(since_boot - expected) < 2.0
    else:
        assert since_boot is None


def test_started_since_boot_is_none_for_a_pid_that_does_not_exist():
    assert started_since_boot(2**30 - 1) is None
