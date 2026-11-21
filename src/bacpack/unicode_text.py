"""Text that has no UTF-8 form.

JSON allows an escaped lone surrogate and json.loads returns it, but it fails
later at a password hash or the SQLite bind. Every place that decodes JSON
from a client checks the result with has_unpaired_surrogate.
"""
from __future__ import annotations

import re

UNSTORABLE_TEXT = "text contains an unpaired surrogate, which can't be stored; send valid Unicode"

_SURROGATE = re.compile("[\ud800-\udfff]")


def has_unpaired_surrogate(value) -> bool:
    """True if a string anywhere in `value` (dict keys and values, lists)
    holds a lone surrogate. An escaped pair is already one character after
    json.loads, so only a lone half matches."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False
