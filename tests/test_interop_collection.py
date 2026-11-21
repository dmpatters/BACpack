from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.interop.harness import INTEROP_DIR, interop_collection


class _Item:
    def __init__(self, path):
        self.path = path
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker.name)


def _config(*, interop, numprocesses=None, xdist=False):
    options = {"interop": interop, "numprocesses": numprocesses}
    return SimpleNamespace(
        getoption=lambda name, default=None: options.get(name, default),
        pluginmanager=SimpleNamespace(hasplugin=lambda name: xdist),
    )


def test_interop_items_are_skipped_without_the_flag():
    inside, outside = _Item(INTEROP_DIR / "test_x.py"), _Item(Path(__file__))
    interop_collection(_config(interop=False), [inside, outside])
    assert inside.markers == ["interop", "skip"]
    assert outside.markers == []


def test_interop_items_run_with_the_flag():
    inside = _Item(INTEROP_DIR / "test_x.py")
    interop_collection(_config(interop=True), [inside])
    assert inside.markers == ["interop"]


def test_interop_refuses_xdist():
    with pytest.raises(pytest.exit.Exception):
        interop_collection(_config(interop=True, numprocesses=2, xdist=True), [])
