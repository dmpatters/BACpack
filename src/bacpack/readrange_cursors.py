"""Where each client's by-time ReadRange paging stopped."""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from typing import Hashable, Optional, Tuple

# (wire timestamp, TrendSample row id) of the record a reply ended on.
Cursor = Tuple[dt.datetime, int]

MAX_CURSORS = 4096


class ReadRangeCursors:
    """A bounded least-recently-used map from a key (client address, Trend
    Log row id, forward?) to the cursors of that client's last two by-time
    replies, newest first. The older one lets a request sent again after its
    reply was lost continue from where the first attempt started. Touched
    only from the event loop, so it takes no lock."""

    def __init__(self) -> None:
        self._entries: "OrderedDict[Hashable, Tuple[Cursor, ...]]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Hashable) -> Tuple[Cursor, ...]:
        cursors = self._entries.get(key)
        if cursors is None:
            return ()
        self._entries.move_to_end(key)
        return cursors

    def remember(self, key: Hashable, end: Cursor, used: Optional[int]) -> None:
        """Stores a reply's end record as the newest cursor. `used` is the
        index (in get's order) of the cursor the request continued from. The
        newest moves to second place, unless the request used the second
        (a retry): that one stays, and the new end replaces the newest."""
        old = self._entries.get(key, ())
        if used == 1:
            kept = old[1:2]
        else:
            kept = old[:1]
        self._entries[key] = (end, *kept)
        self._entries.move_to_end(key)
        while len(self._entries) > MAX_CURSORS:
            self._entries.popitem(last=False)
