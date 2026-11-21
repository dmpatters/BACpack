"""Applies manual/feed writes queued from outside the device's process (the
web API, the parent's ConnectorPoller). Only the child can write its live
objects, so callers queue on the object's runtime row
(BacnetObject.queue_manual_write/queue_feed_write) and this applies them
when DeviceWatch sees the change (there is no cross-process IPC for manual or feed
writes).

It also applies a web-UI device clock change (DeviceRuntime.pending_clock_offset)
the same way.
"""
from __future__ import annotations

import json
import logging
from typing import Set

from bacpypes3.errors import ExecutionError
from bacpypes3.primitivedata import ObjectIdentifier
from sqlalchemy import String, or_, type_coerce, update

from . import config_save, log_names
from .bacnet import write_feed_value, write_manual_value
from .bacnet_client import error_text
from .clock_sync import apply_clock_offset
from .models import BacnetObject, Device, DeviceRuntime, ObjectRuntime

logger = logging.getLogger(__name__)

# ObjectRuntime.pending_<slot>_value / pending_<slot>_queued -- see models.py.
_SLOTS = ("manual", "feed")


class PendingWritePoller:
    """One per running device's child process (device_process.py)."""

    def __init__(self, session_factory, app, device_id: int) -> None:
        self.session_factory = session_factory
        self.app = app
        self.device_id = device_id
        # Objects already warned about as not live, so a connector feeding
        # one every poll logs it once per process, not once per tick.
        self._warned_not_live: Set[int] = set()

    async def apply_due(self) -> None:
        """Apply every queued write and a pending clock change once. Called
        by DeviceWatch when the device's change_seq moves or a clock change
        is pending."""
        with self.session_factory() as session:
            device = session.get(Device, self.device_id)
            if device is None:
                return
            pending_clock = device.runtime.pending_clock_offset
            # Only the rows with a queued write, not every object of the device.
            rows = (
                session.query(
                    ObjectRuntime,
                    BacnetObject.name,
                    BacnetObject.object_type,
                    BacnetObject.instance_number,
                )
                .join(BacnetObject, BacnetObject.id == ObjectRuntime.object_id)
                .filter(BacnetObject.device_id == self.device_id)
                .filter(or_(ObjectRuntime.pending_manual_queued, ObjectRuntime.pending_feed_queued))
                .order_by(ObjectRuntime.object_id)
                .all()
            )
            due = [
                (runtime.object_id, name, object_type, instance_number, slot, getattr(runtime, f"pending_{slot}_value"))
                for runtime, name, object_type, instance_number in rows
                for slot in _SLOTS
                if getattr(runtime, f"pending_{slot}_queued")
            ]

        if pending_clock is not None:
            await self._apply_clock(pending_clock)

        # Manual (priority 8) and feed (priority 10) are separate slots, so
        # the order they apply in doesn't matter. Each is cleared
        # compare-and-clear (see _clear), so a newer write queued while this
        # one was applying stays pending for the next tick.
        for object_id, name, object_type, instance_number, slot, value in due:
            live_obj = self.app.get_object_id(ObjectIdentifier((object_type, instance_number)))
            if live_obj is None:
                # Not in the running device: its live apply failed (the
                # reloader runs first, so a just-added object is live).
                # Cleared, with a warning.
                if object_id not in self._warned_not_live:
                    self._warned_not_live.add(object_id)
                    logger.warning(
                        "device %d: %s isn't in the running device: its live apply failed "
                        "(see the device's Process tab); this write is dropped",
                        self.device_id,
                        log_names.object_ref(object_type, instance_number, name),
                    )
            else:
                try:
                    if slot == "manual":
                        await write_manual_value(live_obj, value)
                    else:
                        await write_feed_value(live_obj, value)
                except ExecutionError as exc:
                    # The object (or a proxy's remote) answered with a BACnet
                    # error: an expected outcome, one line will do.
                    logger.warning(
                        "device %d: pending write for object %d refused: %s",
                        self.device_id,
                        object_id,
                        error_text(exc),
                    )
                except Exception:
                    # One write that no longer applies mustn't stop the
                    # other objects' pending writes.
                    logger.exception(
                        "device %d: failed to apply pending write for object %d",
                        self.device_id,
                        object_id,
                    )
            await self._clear(object_id, slot, value)

    async def _clear(self, object_id: int, slot: str, applied_value) -> None:
        # One UPDATE whose WHERE holds the compare: it runs under the write
        # lock, so a value the lock's holder committed while this waited
        # stays queued. A JSON column holds text, and None may be SQL NULL
        # or JSON null.
        value = getattr(ObjectRuntime, f"pending_{slot}_value")
        same_value = type_coerce(value, String) == json.dumps(applied_value)
        if applied_value is None:
            same_value = or_(same_value, value.is_(None))

        def clear(session) -> None:
            session.execute(
                update(ObjectRuntime)
                .where(
                    ObjectRuntime.object_id == object_id,
                    getattr(ObjectRuntime, f"pending_{slot}_queued").is_(True),
                    same_value,
                )
                .values({f"pending_{slot}_value": None, f"pending_{slot}_queued": False})
            )

        await config_save.save_or_raise(self.session_factory, clear)

    async def _apply_clock(self, new_offset: float) -> None:
        await apply_clock_offset(self.session_factory, self.app, self.device_id, new_offset, source="web UI")

        # Compare-and-clear in one UPDATE, under the write lock: a newer
        # value the UI wrote while this one was being applied or saved stays
        # pending for the next tick instead of being lost.
        def clear(session) -> None:
            session.execute(
                update(DeviceRuntime)
                .where(DeviceRuntime.device_id == self.device_id, DeviceRuntime.pending_clock_offset == new_offset)
                .values(pending_clock_offset=None)
            )

        await config_save.save_or_raise(self.session_factory, clear)
