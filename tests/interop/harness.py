"""The collection switch of the interop tests."""
from __future__ import annotations

from pathlib import Path

import pytest

INTEROP_DIR = Path(__file__).resolve().parent


def interop_collection(config, items) -> None:
    """Marks everything under tests/interop/ and skips it unless --interop.
    The suite owns fixed loopback ports, so it never runs under xdist."""
    interop = config.getoption("interop", False)
    if interop and config.pluginmanager.hasplugin("xdist") and config.getoption("numprocesses", None):
        pytest.exit("--interop runs sequentially: add -p no:xdist", returncode=4)
    skip = pytest.mark.skip(reason="interop test: run with --interop")
    for item in items:
        if INTEROP_DIR in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.interop)
            if not interop:
                item.add_marker(skip)
