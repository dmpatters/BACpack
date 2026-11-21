"""BACnet connectors: read and write mappings to one remote device, run
inside the owning device's child process.

Read mappings feed local objects at FEED_PRIORITY. A transport failure
relinquishes every read mapping and backs off; a point-level error
relinquishes only that mapping; DCC suppression sends and relinquishes
nothing.

Proxy read mappings mirror the
remote's Present_Value and Priority_Array instead, and pass local writes
through to the remote. A proxy never relinquishes anything on the remote:
the commands there belong to whoever wrote them.

The runner's mixins live in bacnet_connector_relinquish.py and
bacnet_connector_proxy.py.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Dict, List, Optional, Set, Tuple

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.primitivedata import ObjectIdentifier

from . import config_save, timeutil
from .bacnet import write_feed_value
from .bacnet_client import ClientError, Point, error_text, to_jsonable
from .bacnet_connector_types import (
    LOCAL_REFUSED,
    NOT_LIVE,
    ConnectorConfig,
    ReadMapping,
    WriteMapping,
    array_point,
    Commanded,
    new_status,
    prune_stale_forward_status,
    prune_stale_write_status,
    same_target,
    settings_of,
    read_value_for_feed,
)
from .bacnet_connector_proxy import ProxyMixin
from .bacnet_connector_relinquish import RelinquishMixin, unanswered_retry_delay
from .bacnet_proxy import set_reliability
from .connectors import MAX_BACKOFF_SECONDS
from .models import Connector, ConnectorRuntime

logger = logging.getLogger(__name__)

RUNNER_TICK_SECONDS = 1.0
# Longest a clean stop waits to relinquish remote points. Must fit, with
# STOP_CHECK_TICK_SECONDS, inside engine.py's STOP_GRACE_SECONDS before the
# parent kills the child (test_stop_grace_covers_the_childs_whole_stop_budget).
SHUTDOWN_RELINQUISH_SECONDS = 3.0


class BacnetConnectorRunner(ProxyMixin, RelinquishMixin):
    """One per device process. Each tick relinquishes what was removed and
    starts a run per connector; each run is its own task, so one slow or
    dead remote never delays another, or the tick.
    """

    def __init__(self, session_factory, app, device_id: int, *, client) -> None:
        self.session_factory = session_factory
        self.app = app
        self.device_id = device_id
        self.client = client
        self._task: Optional[asyncio.Task] = None
        self._running: Dict[int, asyncio.Task] = {}
        self._background: Set[asyncio.Task] = set()
        # connector id -> loop time its next poll is due
        self._due: Dict[int, float] = {}
        self._status: Dict[int, dict] = {}
        # connector id -> settings_of last seen; a change clears the backoff.
        self._settings: Dict[int, tuple] = {}
        # read mapping id -> (object_type, instance) it currently feeds
        self._fed: Dict[int, tuple] = {}
        # write mapping id -> what was last sent there
        self._commanded: Dict[int, Commanded] = {}
        # id(Commanded) -> a relinquish still owed (most likely
        # DCC-suppressed), kept until sent so shutdown() can still resend it
        # after stop() cancels the background attempt.
        self._pending_relinquish: Dict[int, Commanded] = {}
        # id(Commanded) keys being attempted, so a per-tick retry can't race
        # an attempt already in flight.
        self._relinquishing: Set[int] = set()
        # id(Commanded) -> (failures, loop time of the next attempt) for a
        # pending relinquish the remote didn't answer.
        self._relinquish_retry: Dict[int, Tuple[int, float]] = {}
        # proxy mapping id -> (object_type, instance) whose hook is attached
        self._proxies: Dict[int, tuple] = {}
        # The last _load() result, reloaded only after mark_changed(), which
        # DeviceWatch calls on a config change.
        self._configs: List[ConnectorConfig] = []
        self._stale = True
        # proxy mapping id -> loop time of its latest successful forward; a
        # poll that started before it is stale for that proxy.
        self._forwarded_at: Dict[int, float] = {}
        # connector ids whose last poll dropped a stale proxy result: poll
        # again on the next tick.
        self._reread: Set[int] = set()
        # connector id -> loop time until which forwards fail at once (see
        # FORWARD_UNREACHABLE_SECONDS).
        self._unreachable_until: Dict[int, float] = {}

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for task in list(self._running.values()) + list(self._background):
            task.cancel()
        self._running.clear()
        self._background.clear()

    async def shutdown(self, timeout: float = SHUTDOWN_RELINQUISH_SECONDS) -> None:
        """A clean device stop: relinquish every commanded or pending remote
        point, then stop, giving up after `timeout`. Not used for a crash,
        a kill or a ReinitializeDevice restart (see device_process.py).

        The entries are grabbed before stop(), which cancels any relinquish
        already in flight; resending one is harmless (idempotent)."""
        commanded = list(self._commanded.values()) + list(self._pending_relinquish.values())
        self._commanded.clear()
        self._pending_relinquish.clear()
        self._relinquishing.clear()
        self._relinquish_retry.clear()
        self.stop()
        if not commanded:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*(self._relinquish(c) for c in commanded)), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "device %d: gave up relinquishing %d remote %s after %g s",
                self.device_id,
                len(commanded),
                "point" if len(commanded) == 1 else "points",
                timeout,
            )

    def mark_changed(self) -> None:
        self._stale = True

    def _current_configs(self) -> List[ConnectorConfig]:
        if self._stale:
            # Cleared before loading, so a mark during the load isn't lost;
            # set again if the load fails, so the next tick retries.
            self._stale = False
            try:
                self._configs = self._load()
            except Exception:
                self._stale = True
                raise
        return self._configs

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(RUNNER_TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                # One bad tick must not stop this device's connectors for good.
                logger.exception("bacnet connector tick failed for device %d", self.device_id)

    async def tick(self) -> None:
        configs = self._current_configs()
        self._note_settings(configs)
        self._sync_proxies(configs)
        await self._release_removed(configs)
        self._retry_pending_relinquishes()
        for config in configs:
            task = self._running.get(config.id)
            if task is not None and not task.done():
                continue
            self._running[config.id] = asyncio.ensure_future(self._run_connector(config))

    async def idle(self) -> None:
        """Wait for every run and background relinquish tick() started."""
        tasks = [t for t in list(self._running.values()) + list(self._background) if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _load(self) -> List[ConnectorConfig]:
        configs = []
        with self.session_factory() as session:
            connectors = (
                session.query(Connector)
                .filter_by(device_id=self.device_id, type="bacnet", enabled=True)
                .order_by(Connector.id)
                .all()
            )
            for connector in connectors:
                if connector.remote_device_instance is None:
                    continue
                reads = [
                    ReadMapping(
                        id=m.id,
                        object_type=m.object.object_type,
                        instance=m.object.instance_number,
                        point=Point(
                            m.remote_object_type,
                            m.remote_object_instance,
                            m.remote_property or "present-value",
                            m.remote_array_index,
                        ),
                        scale=m.scale,
                        offset=m.offset,
                        value_map=m.value_map,
                        direction=m.direction or "read",
                    )
                    for m in connector.mappings
                    if m.object is not None and m.remote_object_type is not None
                ]
                writes = [
                    WriteMapping(
                        id=w.id,
                        object_type=w.object.object_type,
                        instance=w.object.instance_number,
                        point=Point(
                            w.remote_object_type,
                            w.remote_object_instance,
                            w.remote_property or "present-value",
                            w.remote_array_index,
                        ),
                        priority=w.priority,
                    )
                    for w in connector.write_mappings
                    if w.object is not None
                ]
                configs.append(
                    ConnectorConfig(
                        id=connector.id,
                        remote_instance=connector.remote_device_instance,
                        remote_address=connector.remote_address,
                        poll_interval=float(connector.poll_interval_seconds),
                        timeout=float(connector.timeout_seconds),
                        rewrite_interval=float(connector.rewrite_interval_seconds or 0),
                        reads=reads,
                        writes=writes,
                    )
                )
        return configs

    def _note_settings(self, configs: List[ConnectorConfig]) -> None:
        """A connector whose remote or timing was edited is polled on this
        tick with its backoff cleared and its old binding dropped: the edit
        is usually the fix. Mapping edits don't count."""
        for config in configs:
            settings = settings_of(config)
            previous = self._settings.get(config.id)
            self._settings[config.id] = settings
            if previous is None or previous == settings:
                continue
            self.client.unbind(previous[0], previous[1])
            status = self._status.get(config.id)
            if status is not None:
                status["consecutive_failures"] = 0
            self._due.pop(config.id, None)

    def _set_due(self, config: ConnectorConfig, when: float) -> None:
        """Schedule the next poll, unless the connector was edited while this
        run was in flight: then poll on the next tick, so the old run's
        outcome doesn't set a backoff the edit cleared."""
        if self._edited_since(config):
            self._due.pop(config.id, None)
        else:
            self._due[config.id] = when

    def _edited_since(self, config: ConnectorConfig) -> bool:
        current = self._settings.get(config.id)
        return current is not None and current != settings_of(config)

    async def _release_removed(self, configs: List[ConnectorConfig]) -> None:
        """Relinquish what a removed mapping left behind -- a read mapping's
        local feed, a write mapping's remote command -- when the mapping is
        deleted or its connector disabled/deleted."""
        live_ids = {config.id for config in configs}
        # Cancel before any await below, or an in-flight run could still
        # re-feed/re-write and re-set `_due` after this decided it's gone.
        for connector_id in [i for i in self._running if i not in live_ids]:
            task = self._running.pop(connector_id)
            task.cancel()
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        for connector_id in [i for i in self._status if i not in live_ids]:
            self._status.pop(connector_id, None)
        for connector_id in [i for i in self._settings if i not in live_ids]:
            self._settings.pop(connector_id, None)
        # By its own keys: a run in flight when its connector disappeared
        # can re-set _due after _status was popped.
        for connector_id in [i for i in self._due if i not in live_ids]:
            self._due.pop(connector_id, None)

        live_reads = {m.id for config in configs for m in config.reads if m.direction == "read"}
        for mapping_id in [i for i in self._fed if i not in live_reads]:
            object_type, instance = self._fed.pop(mapping_id)
            await self._feed_quietly(object_type, instance, None)

        # And every remote point whose write mapping is gone.
        live_writes = {m.id for config in configs for m in config.writes}
        for mapping_id in [i for i in self._commanded if i not in live_writes]:
            self._spawn_relinquish(self._commanded.pop(mapping_id))

    async def _run_connector(self, config: ConnectorConfig) -> None:
        status = self._status.setdefault(config.id, new_status())
        now = asyncio.get_running_loop().time()
        # `|`, not `or`: both prunes must run.
        changed = prune_stale_write_status(config, status) | prune_stale_forward_status(config, status)
        try:
            if config.reads and now >= self._due.get(config.id, 0.0):
                await self._poll(config, status, now)
                changed = True
            if config.writes:
                changed = await self._push(config, status, now) or changed
            if status["suppressed_by_dcc"]:
                status["suppressed_by_dcc"] = False
                changed = True
        except ClientError as exc:
            if exc.kind == "dcc":
                if not status["suppressed_by_dcc"]:
                    status["suppressed_by_dcc"] = True
                    changed = True
            else:
                # Shouldn't happen (_poll/_push handle their own); not
                # re-raised, as the sibling `except Exception` wouldn't catch it.
                self._record_unexpected_failure(config, status, now, exc)
                changed = True
        except ErrorRejectAbortNack as exc:
            # bacpypes3 raises these as BaseException, not Exception.
            # BacnetClient._call normally converts them; one that escapes
            # mustn't end this connector's task.
            self._record_unexpected_failure(config, status, now, exc)
            changed = True
        except Exception:
            logger.exception("device %d: bacnet connector %d run failed", self.device_id, config.id)
        if changed:
            await self._save_status(config.id, status)

    def _record_unexpected_failure(
        self, config: ConnectorConfig, status: dict, now: float, exc: BaseException
    ) -> None:
        """A whole-connector failure that got past _poll/_push's own
        handling: logged and backed off like a transport failure, so a bad
        connector isn't hammered every tick."""
        logger.exception("device %d: bacnet connector %d run failed", self.device_id, config.id)
        status.update(last_success=False, last_error=error_text(exc))
        if not self._edited_since(config):
            status["consecutive_failures"] += 1
        backoff = min(
            config.poll_interval * (2 ** min(status["consecutive_failures"], 20)), MAX_BACKOFF_SECONDS
        )
        self._set_due(config, now + backoff)

    async def _poll(self, config: ConnectorConfig, status: dict, now: float) -> None:
        status["last_poll_at"] = timeutil.iso_utc(timespec="seconds")
        try:
            binding = await self.client.bind(config.remote_instance, config.remote_address, timeout=config.timeout)
            points = [m.point for m in config.reads] + [array_point(m) for m in config.reads if m.direction == "proxy"]
            values = await self.client.read_many(binding, points, timeout=config.timeout)
        except ClientError as exc:
            if exc.kind == "dcc":
                raise
            await self._poll_failed(config, status, now, exc)
            return

        self._unreachable_until.pop(config.id, None)  # it answers again
        status.update(
            last_success=True,
            last_error=None,
            consecutive_failures=0,
            bound_address=str(binding.address),
            no_rpm=binding.no_rpm,
        )
        errors: Dict[str, Optional[str]] = {}
        for mapping in config.reads:
            if mapping.direction == "proxy":
                errors[str(mapping.id)] = self._mirror_one(config, mapping, values, now, status)
                continue
            if mapping.id in self._proxies:
                # Switched to a proxy while this read was in flight: feeding
                # it now would forward a priority-10 write to the remote.
                errors[str(mapping.id)] = status["read_errors"].get(str(mapping.id))
                continue
            value = values.get(mapping.point)
            if self._local(mapping.object_type, mapping.instance) is None:
                errors[str(mapping.id)] = NOT_LIVE
                continue
            if isinstance(value, ClientError):
                # The remote's own error for this point.
                errors[str(mapping.id)] = error_text(value)
                self._fed.pop(mapping.id, None)
                await self._feed_quietly(mapping.object_type, mapping.instance, None)
                continue
            try:
                feed_value = read_value_for_feed(value, mapping)
            except Exception as exc:
                # Point-level: relinquish only this mapping, no backoff.
                errors[str(mapping.id)] = error_text(exc)
                self._fed.pop(mapping.id, None)
                await self._feed_quietly(mapping.object_type, mapping.instance, None)
                continue
            try:
                await self._feed(mapping.object_type, mapping.instance, feed_value)
            except Exception as exc:
                errors[str(mapping.id)] = LOCAL_REFUSED.format(error_text(exc))
                self._fed.pop(mapping.id, None)
                await self._feed_quietly(mapping.object_type, mapping.instance, None)
                continue
            self._fed[mapping.id] = (mapping.object_type, mapping.instance)
            errors[str(mapping.id)] = None
        status["read_errors"] = errors
        if config.id in self._reread:
            # A proxy's result was dropped as stale: read again next tick.
            self._reread.discard(config.id)
            self._due.pop(config.id, None)
        else:
            self._set_due(config, now + config.poll_interval)

    async def _poll_failed(self, config: ConnectorConfig, status: dict, now: float, exc: ClientError) -> None:
        """The whole read failed: relinquish every read mapping. A transport
        failure also drops the binding and backs off; any other whole-request
        error (a reply that couldn't be decoded) keeps the normal interval."""
        for mapping in config.reads:
            if mapping.direction == "proxy":
                # Last values stay, flagged stale; nothing is relinquished.
                # Only while still a live proxy (one removed mid-poll was cleared).
                obj = self._local(mapping.object_type, mapping.instance)
                if obj is not None and mapping.id in self._proxies:
                    set_reliability(obj, "communication-failure")
                continue
            if mapping.id in self._proxies:
                continue  # switched to a proxy mid-poll: no relinquish to forward
            self._fed.pop(mapping.id, None)
            await self._feed_quietly(mapping.object_type, mapping.instance, None)
        status.update(
            last_success=False,
            last_error=error_text(exc),
            read_errors={str(m.id): error_text(exc) for m in config.reads},
        )
        if exc.transport:
            self.client.unbind(config.remote_instance, config.remote_address)
            status["bound_address"] = None
            if not self._edited_since(config):
                status["consecutive_failures"] += 1
            # Cap the exponent too -- see ConnectorPoller._poll_one.
            backoff = min(
                config.poll_interval * (2 ** min(status["consecutive_failures"], 20)), MAX_BACKOFF_SECONDS
            )
            self._set_due(config, now + backoff)
        else:
            self._set_due(config, now + config.poll_interval)

    async def _push(self, config: ConnectorConfig, status: dict, now: float) -> bool:
        """Send each write mapping whose local Present_Value changed, or whose
        re-write is due. Returns True if anything was attempted or the
        status changed."""
        write_status = status["write_status"]
        attempted = False
        for mapping in config.writes:
            obj = self._local(mapping.object_type, mapping.instance)
            if obj is None:
                entry = write_status.setdefault(str(mapping.id), {"error": None, "last_sent_at": None})
                if entry["error"] != NOT_LIVE:
                    entry["error"] = NOT_LIVE
                    attempted = True
                continue
            raw = obj.presentValue
            value = to_jsonable(raw)
            previous = self._commanded.get(mapping.id)
            if previous is not None and not same_target(previous, config, mapping):
                # Edited to another point or priority: clear the old command
                # first. A DCC-suppressed relinquish stays in
                # _pending_relinquish and is retried later.
                del self._commanded[mapping.id]
                self._track_pending(previous)
                await self._attempt_relinquish(previous)
                previous = None
            due = (
                previous is None
                or previous.last_value != value
                or (config.rewrite_interval > 0 and now - previous.last_sent_at >= config.rewrite_interval)
                or (previous.undelivered > 0 and self._retry_undelivered(config, previous, now))
            )
            if not due:
                continue

            attempted = True
            # Recorded before sending: a refused write is retried on the next
            # change or re-write rather than every tick, and a point that may
            # have been written is still relinquished later.
            commanded = self._commanded[mapping.id] = Commanded(
                config.remote_instance, config.remote_address, mapping.point,
                mapping.priority, config.timeout, value, now,
            )
            entry = write_status.setdefault(str(mapping.id), {"error": None, "last_sent_at": None})
            try:
                binding = await self.client.bind(config.remote_instance, config.remote_address, timeout=config.timeout)
                await self.client.write(binding, mapping.point, raw, priority=mapping.priority, timeout=config.timeout)
            except ClientError as exc:
                if exc.kind == "dcc":
                    # Nothing was sent: put back what was there before, so the
                    # value goes out once DCC lifts.
                    if previous is None:
                        del self._commanded[mapping.id]
                    else:
                        self._commanded[mapping.id] = previous
                    raise
                entry["error"] = error_text(exc)
                if exc.transport:
                    commanded.undelivered = (previous.undelivered if previous is not None else 0) + 1
                    commanded.retry_at = now + unanswered_retry_delay(commanded.undelivered)
                    self.client.unbind(config.remote_instance, config.remote_address)
                    status["bound_address"] = None
                    break  # remote unreachable: leave the rest for their own retry
                continue
            entry.update(error=None, last_sent_at=timeutil.iso_utc(timespec="seconds"))
        return attempted

    def _retry_undelivered(self, config: ConnectorConfig, previous: Commanded, now: float) -> bool:
        """A write lost to a transport failure goes again once the
        remote is bound again (a read rebound it), or on its own backoff,
        which is how a connector with no reads finds the remote back."""
        rebound = self.client.binding(config.remote_instance, config.remote_address) is not None
        return rebound or now >= previous.retry_at

    def _local(self, object_type: str, instance: int):
        # None for an object added since the device started: it isn't live
        # until the device is restarted or reloaded.
        return self.app.get_object_id(ObjectIdentifier((object_type, instance)))

    async def _feed(self, object_type: str, instance: int, value) -> None:
        obj = self._local(object_type, instance)
        if obj is not None:
            # forward=False: releasing a read feed (e.g. on a switch to proxy)
            # must not send a relinquish to the remote.
            await write_feed_value(obj, value, forward=False)

    async def _feed_quietly(self, object_type: str, instance: int, value) -> None:
        try:
            await self._feed(object_type, instance, value)
        except Exception:
            logger.exception(
                "device %d: couldn't relinquish the feed on %s,%d", self.device_id, object_type, instance
            )

    async def _save_status(self, connector_id: int, status: dict) -> None:
        # Copied before the await (the dict keeps changing on the loop), and
        # a fresh object so SQLAlchemy sees a new value to write.
        saved = copy.deepcopy(status)

        def save(session) -> None:
            runtime = session.get(ConnectorRuntime, connector_id)
            if runtime is not None:
                runtime.client_status = saved

        await config_save.save_or_raise(self.session_factory, save)
