"""Keeping a device's startup objects out of full garbage collections.

A full collection walks every object the process tracks and holds the GIL,
so on a big device it stops the loop for 0.1-0.4 s. Frozen objects are
skipped. What a live swap retires was frozen and sits in reference cycles,
so it can't be freed until a refreeze (unfreeze, collect, freeze again), and
what it adds isn't frozen until then."""
from __future__ import annotations

import gc

MIN_REFREEZE_OBJECTS = 100


def freeze_startup() -> None:
    # Collect first so the build's own garbage isn't frozen with the rest.
    gc.collect()
    gc.freeze()


class Refreezer:
    """Counts objects retired or added since the last freeze and refreezes
    once they reach a tenth of the device's objects (at least
    MIN_REFREEZE_OBJECTS). A Trend Log counts beside its object but not in
    the device's size, so a trended device refreezes a little sooner."""

    def __init__(self, *, collect=gc.collect, freeze=gc.freeze, unfreeze=gc.unfreeze) -> None:
        self._collect = collect
        self._freeze = freeze
        self._unfreeze = unfreeze
        self.pending = 0

    def changed(self, count: int, device_objects: int) -> bool:
        self.pending += count
        if self.pending < max(device_objects // 10, MIN_REFREEZE_OBJECTS):
            return False
        self._unfreeze()
        self._collect()
        self._freeze()
        self.pending = 0
        return True
