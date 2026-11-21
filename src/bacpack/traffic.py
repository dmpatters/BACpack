"""Traffic log for the Traffic page: a bounded ring buffer of BACnet
activity. The device runs in a child process the parent can't reach, so
with a writer and device_id every add() also queues a TrafficLogEntry row
for the parent to read.
"""
from __future__ import annotations

import collections
import logging
from typing import Deque, Dict, List, Optional

from . import timeutil
from .history_writer import HistoryWriter, TrafficRow, produced_at

logger = logging.getLogger(__name__)


def object_id_text(object_identifier) -> str:
    """"analog-value:1", the notation the UI uses everywhere; a bacpypes3
    ObjectIdentifier's str() is "analog-value,1"."""
    return f"{object_identifier[0]}:{object_identifier[1]}"


class TrafficLog:
    def __init__(
        self,
        maxlen: int = 500,
        *,
        writer: Optional[HistoryWriter] = None,
        device_id: Optional[int] = None,
    ) -> None:
        self.maxlen = maxlen
        self._entries: Deque[Dict[str, str]] = collections.deque(maxlen=maxlen)
        self._writer = writer
        self._device_id = device_id

    def add(self, event_type: str, detail: str) -> None:
        now = timeutil.host_seconds()
        entry = {
            "timestamp": timeutil.iso_utc(timeutil.utc_from_timestamp(now), timespec="seconds"),
            "event_type": event_type,
            "detail": detail,
        }
        self._entries.append(entry)
        if self._writer is not None and self._device_id is not None:
            try:
                self._writer.put(
                    TrafficRow(self._device_id, event_type, detail, produced_at(now), self.maxlen)
                )
            except Exception:
                logger.exception("traffic log persistence failed; in-memory entry retained")

    def recent(self, limit: int = 200) -> List[Dict[str, str]]:
        """Newest first."""
        entries = list(self._entries)
        entries.reverse()
        return entries[:limit]

    def clear(self) -> None:
        self._entries.clear()
