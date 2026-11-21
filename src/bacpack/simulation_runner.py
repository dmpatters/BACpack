"""Runs a device's simulated objects: one per device child process."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from bacpypes3.primitivedata import ObjectIdentifier
from sqlalchemy import select

from . import log_names, timeutil
from .bacnet import write_feed_value
from .models import BacnetObject
from .simulation import analog_value, binary_value, family, multistate_value, parse_simulation

logger = logging.getLogger("bacpack.simulation")


# --- Runner (owning device's child process) ---------------------------------

RUNNER_TICK_SECONDS = 1.0


@dataclass
class _Tracked:
    """What the runner remembers about one simulated object."""

    raw: dict
    object_type: str
    instance: int
    state: dict = field(default_factory=dict)
    next_due: float = 0.0
    last: Any = None
    last_run: float = 0.0  # the clock at the latest tick that looked at it


class SimulationRunner:
    """One per device child process. Each tick writes each due object's new
    value at FEED_PRIORITY (10), only when it changed. An object no longer
    simulated gets its slot 10 back."""

    def __init__(
        self,
        session_factory,
        app,
        device_id: int,
        *,
        clock: Callable[[], float] = timeutil.host_seconds,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.session_factory = session_factory
        self.app = app
        self.device_id = device_id
        self.clock = clock
        self.rng = rng or random.Random()
        self._task: Optional[asyncio.Task] = None
        self._tracked: Dict[int, _Tracked] = {}
        # object id -> the last problem logged for it, so a problem that
        # repeats every tick is logged once (again only if it changes).
        self._logged: Dict[int, str] = {}
        # The last _load() result, reloaded only after mark_changed(), which
        # DeviceWatch calls on a config change.
        self._rows: list = []
        self._stale = True

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def mark_changed(self) -> None:
        self._stale = True

    def _current_rows(self) -> list:
        if self._stale:
            # Cleared before loading, so a mark during the load isn't lost;
            # set again if the load fails, so the next tick retries.
            self._stale = False
            try:
                self._rows = self._load()
            except Exception:
                self._stale = True
                raise
        return self._rows

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(RUNNER_TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                # One bad tick must not stop this device's simulators for good.
                logger.exception("simulation tick failed for device %d", self.device_id)

    async def tick(self) -> None:
        rows = self._current_rows()
        now = self.clock()
        for object_id, object_type, instance, name, raw in rows:
            try:
                await self._run_one(object_id, object_type, instance, name, raw, now)
            except Exception as exc:
                self._log_once(
                    object_id,
                    f"device {self.device_id}: simulating {log_names.object_ref(object_type, instance, name)} failed: {exc}",
                )
        current = {row[0] for row in rows}
        for object_id in [i for i in self._tracked if i not in current]:
            self._logged.pop(object_id, None)
            await self._release(object_id, self._tracked.pop(object_id))

    def _load(self) -> list:
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    BacnetObject.id,
                    BacnetObject.object_type,
                    BacnetObject.instance_number,
                    BacnetObject.name,
                    BacnetObject.simulation,
                )
                .where(BacnetObject.device_id == self.device_id, BacnetObject.simulation.is_not(None))
                .order_by(BacnetObject.id)
            ).all()
        # IS NOT NULL only pre-filters: a JSON column can hold JSON null rather
        # than SQL NULL once a simulator is turned off, so filter again here.
        return [tuple(row) for row in rows if row.simulation]

    async def _run_one(self, object_id: int, object_type: str, instance: int, name: str, raw: dict, now: float) -> None:
        live = self.app.get_object_id(ObjectIdentifier((object_type, instance)))
        if live is None:
            self._log_once(
                object_id,
                f"device {self.device_id}: {log_names.object_ref(object_type, instance, name)} isn't in the running device "
                "-- its live apply failed, see the device's Process tab",
            )
            return
        if getattr(live, "_proxy_forward", None) is not None:
            # A proxy forwards every write to its remote, and a proxy never
            # sends anything on its own: don't simulate it.
            self._log_once(
                object_id,
                f"device {self.device_id}: {log_names.object_ref(object_type, instance, name)} is a BACnet proxy right now; "
                "not simulating it",
            )
            return
        tracked = self._tracked.get(object_id)
        # A clock that stepped back would leave next_due / a random dwell
        # deadline that far in the future: start over, as on a settings change.
        # A renumbered or re-typed object is a new live object, not yet written;
        # its old number isn't released, since another object may hold it now.
        if (
            tracked is None
            or tracked.raw != raw
            or (tracked.object_type, tracked.instance) != (object_type, instance)
            or now < tracked.last_run
        ):
            tracked = _Tracked(raw=raw, object_type=object_type, instance=instance)
            self._tracked[object_id] = tracked
        tracked.last_run = now
        if now < tracked.next_due:
            return
        settings = parse_simulation(object_type, raw)
        kind_family = family(object_type)
        if kind_family == "analog":
            tracked.next_due = now + settings.interval_s
            value = analog_value(settings, now, tracked.state, self.rng, self._read_source)
        elif kind_family == "binary":
            value = binary_value(settings, now, tracked.state, self.rng)
        else:
            value = multistate_value(settings, now, tracked.state, self.rng, int(live.numberOfStates))
        if value is None:
            self._log_once(
                object_id,
                f"device {self.device_id}: {log_names.object_ref(object_type, instance, name)} has no value to write "
                "(follow source not in the running "
                "device, or none of its states fit the object)",
            )
            return
        if value != tracked.last and await write_feed_value(live, value):
            tracked.last = value
        self._logged.pop(object_id, None)

    def _read_source(self, identifier: Tuple[str, int]) -> Optional[float]:
        source = self.app.get_object_id(ObjectIdentifier(identifier))
        if source is None:
            return None
        return float(source.presentValue)

    async def _release(self, object_id: int, tracked: _Tracked) -> None:
        """Hand a commandable object's slot 10 back. An input keeps its
        last value: releasing would drop it to the class default."""
        live = self.app.get_object_id(ObjectIdentifier((tracked.object_type, tracked.instance)))
        if live is None or getattr(live, "priorityArray", None) is None:
            return
        try:
            # forward=False: a proxy sends nothing on its own, and this
            # slot 10 value never reached a remote.
            await write_feed_value(live, None, forward=False)
        except Exception:
            logger.exception("device %d: releasing simulated object %d failed", self.device_id, object_id)

    def _log_once(self, object_id: int, message: str) -> None:
        if self._logged.get(object_id) != message:
            self._logged[object_id] = message
            logger.warning(message)
