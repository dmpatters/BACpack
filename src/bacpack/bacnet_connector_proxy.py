"""Proxy read mappings: the forward
hook on each proxy's local object, passing writes through to the remote,
and mirroring a poll's result locally. A mixin of BacnetConnectorRunner.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from . import timeutil
from .bacnet_client import ClientError, Point, error_text, to_jsonable
from .bacnet_connector_types import (
    LOCAL_REFUSED,
    NOT_LIVE,
    ConnectorConfig,
    ReadMapping,
    array_point,
    new_status,
)
from .bacnet_proxy import clear_mirror, forward_error, mirror, set_reliability


# A write passed through a proxy waits this long, in all, for the remote
# (bind included), then the client that wrote gets communication: timeout.
# Below a common 3 s BMS APDU timeout, so the client hears an answer.
FORWARD_CAP_SECONDS = 2.5


# After a forward found the remote not answering, the connector's other
# forwards fail at once, for this long or until a poll or forward gets
# through: otherwise n writes to proxies on a dead remote wait
# n x FORWARD_CAP_SECONDS.
FORWARD_UNREACHABLE_SECONDS = 10.0


class ProxyMixin:
    """BacnetConnectorRunner's proxy mappings; its state lives on the
    runner."""

    def _sync_proxies(self, configs: List[ConnectorConfig]) -> None:
        """Attach the forward hook to every live proxy's local object (fresh
        each tick, for current settings), and undo every proxy that's gone:
        hook off, mirror cleared, nothing sent to the remote."""
        live = {
            mapping.id: (config, mapping)
            for config in configs
            for mapping in config.reads
            if mapping.direction == "proxy"
        }
        for mapping_id in [i for i in self._proxies if i not in live]:
            object_type, instance = self._proxies.pop(mapping_id)
            self._forwarded_at.pop(mapping_id, None)
            obj = self._local(object_type, instance)
            if obj is not None:
                obj._proxy_forward = None
                clear_mirror(obj)
        for mapping_id, (config, mapping) in live.items():
            obj = self._local(mapping.object_type, mapping.instance)
            if obj is None:
                continue  # reported as NOT_LIVE by the poll
            if mapping_id not in self._proxies:
                # Stale until its first good read.
                set_reliability(obj, "communication-failure")
            self._proxies[mapping_id] = (mapping.object_type, mapping.instance)
            obj._proxy_forward = self._forwarder(config, mapping)

    def _forwarder(self, config: ConnectorConfig, mapping: ReadMapping):
        async def forward(value, priority: int) -> None:
            await self._forward(config, mapping, value, priority)

        return forward

    async def _forward(self, config: ConnectorConfig, mapping: ReadMapping, value, priority: int) -> None:
        """A write to a proxy's local object, passed to the remote at the
        same priority (None relinquishes). Returns once the remote took it;
        raises the ExecutionError the client gets otherwise. Either way the
        outcome is saved as the proxy's last_forward."""
        status = self._status.setdefault(config.id, new_status())
        entry = {"at": timeutil.iso_utc(timespec="seconds"), "priority": priority, "value": to_jsonable(value), "error": None}
        failure: Optional[ClientError] = None
        loop = asyncio.get_running_loop()
        if loop.time() < self._unreachable_until.get(config.id, float("-inf")):
            failure = ClientError("not sent: the remote didn't answer a write just before", kind="transport")
        else:
            try:
                await asyncio.wait_for(self._send_forward(config, mapping, value, priority), FORWARD_CAP_SECONDS)
            except asyncio.TimeoutError:
                failure = ClientError(f"no response within {FORWARD_CAP_SECONDS:g} s", kind="transport")
            except ClientError as exc:
                failure = exc
            if failure is not None and failure.transport:
                self._unreachable_until[config.id] = loop.time() + FORWARD_UNREACHABLE_SECONDS
                self.client.unbind(config.remote_instance, config.remote_address)
                status["bound_address"] = None
        if failure is None:
            self._unreachable_until.pop(config.id, None)
            self._forwarded_at[mapping.id] = loop.time()
            self._due.pop(config.id, None)  # read the remote back on the next tick
        else:
            entry["error"] = error_text(failure)
        status.setdefault("last_forward", {})[str(mapping.id)] = entry
        await self._save_status(config.id, status)
        if failure is not None:
            raise forward_error(failure)

    async def _send_forward(self, config: ConnectorConfig, mapping: ReadMapping, value, priority: int) -> None:
        binding = await self.client.bind(config.remote_instance, config.remote_address, timeout=config.timeout)
        await self.client.write(binding, mapping.point, value, priority=priority, timeout=config.timeout)

    def _mirror_one(
        self, config: ConnectorConfig, mapping: ReadMapping, values: Dict[Point, Any], started: float, status: dict
    ) -> Optional[str]:
        """Apply one proxy's poll result. Returns its read_errors entry."""
        obj = self._local(mapping.object_type, mapping.instance)
        if obj is None:
            return NOT_LIVE
        if mapping.id not in self._proxies:
            # Deleted, disabled or switched to read while this poll was in
            # flight: _sync_proxies already cleared it, don't mirror again.
            return status["read_errors"].get(str(mapping.id))
        if self._forwarded_at.get(mapping.id, float("-inf")) >= started:
            # A write went through while this read was in flight: what came
            # back may predate it. Keep everything and read again next tick.
            self._reread.add(config.id)
            return status["read_errors"].get(str(mapping.id))
        value = values.get(mapping.point)
        array = values.get(array_point(mapping))
        if isinstance(value, ClientError):
            set_reliability(obj, "configuration-error")
            return error_text(value)
        if isinstance(array, ClientError):
            # Any property-class error means no array to mirror: just the
            # value, as for a non-commandable remote point.
            if array.error_class != "property":
                set_reliability(obj, "configuration-error")
                return error_text(array)
            array = None
        # Reliability before the value: a Present_Value change queues the
        # alarm evaluation, and bacpypes3 records no further change once one
        # is queued, so a recovery set after it left the object in FAULT.
        set_reliability(obj, "no-fault-detected")
        try:
            mirror(obj, value, array)
        except Exception as exc:
            set_reliability(obj, "configuration-error")
            return LOCAL_REFUSED.format(error_text(exc))
        return None
