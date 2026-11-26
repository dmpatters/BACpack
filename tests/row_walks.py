"""A dict that counts whole-table walks, to catch a planner that scans every row once per object."""
from __future__ import annotations


class WalkCountingRows(dict):
    """Counts values(), items(), keys() and iteration; a lookup is not a walk."""

    def __init__(self, *args):
        super().__init__(*args)
        self.walks = 0

    def values(self):
        self.walks += 1
        return super().values()

    def items(self):
        self.walks += 1
        return super().items()

    def keys(self):
        self.walks += 1
        return super().keys()

    def __iter__(self):
        self.walks += 1
        return super().__iter__()
