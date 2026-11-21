"""Fixtures of the interop tests. They run only under --interop (the files in
this directory are skipped otherwise), so a normal run builds nothing."""
from __future__ import annotations

import pytest

from . import bacnet_stack
from .stack import Stack
from .tools import Tools


@pytest.fixture(scope="session")
def tools(request):
    return Tools(bacnet_stack.bin_dir(rebuild=request.config.getoption("--interop-rebuild")))


@pytest.fixture(scope="session")
def stack(tools, tmp_path_factory):
    running = Stack(tmp_path_factory.mktemp("interop"), tools)
    try:
        running.start()
        yield running
    finally:
        running.stop()
