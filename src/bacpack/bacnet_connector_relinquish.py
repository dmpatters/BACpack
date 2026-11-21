"""Relinquishing what a BACnet connector's write mappings commanded on a
remote device: tracked until actually sent, retried with backoff when the
remote doesn't answer. A mixin of BacnetConnectorRunner.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from bacpypes3.apdu import ErrorRejectAbortNack

from .bacnet_client import ClientError, error_text
from .bacnet_connector_types import RESEND, Commanded
from .connectors import MAX_BACKOFF_SECONDS

# The runner's logger name, not this module's: the Logs page shows it.
logger = logging.getLogger("bacpack.bacnet_connectors")


# First retry delay for a relinquish the remote didn't answer, doubling up to
# MAX_BACKOFF_SECONDS; dropping it would leave the command in the remote's
# priority array for good.
RELINQUISH_RETRY_FIRST_SECONDS = 10.0


def unanswered_retry_delay(failures: int) -> float:
    """Seconds before retrying a relinquish, or a write, the remote hasn't
    answered `failures` times in a row."""
    return min(RELINQUISH_RETRY_FIRST_SECONDS * 2 ** min(failures - 1, 20), MAX_BACKOFF_SECONDS)


class RelinquishMixin:
    """BacnetConnectorRunner's relinquish bookkeeping; its state lives on
    the runner (set up in BacnetConnectorRunner.__init__)."""

    def _track_pending(self, commanded: Commanded) -> None:
        """Record `commanded` as needing a relinquish, synchronously, so a
        background attempt cancelled before it runs is still visible to
        shutdown() and the next tick's retry."""
        key = id(commanded)
        self._pending_relinquish[key] = commanded
        self._relinquishing.add(key)

    async def _attempt_relinquish(self, commanded: Commanded) -> bool:
        """One relinquish attempt for a `commanded` already tracked by
        _track_pending; untracked once sent or refused. No answer: retried
        with backoff; DCC suppression: retried on the next tick."""
        key = id(commanded)
        try:
            outcome = await self._relinquish(commanded)
        finally:
            self._relinquishing.discard(key)
        if outcome == "done":
            self._pending_relinquish.pop(key, None)
            self._relinquish_retry.pop(key, None)
        elif outcome == "unreachable":
            failures = self._relinquish_retry.get(key, (0, 0.0))[0] + 1
            delay = unanswered_retry_delay(failures)
            self._relinquish_retry[key] = (failures, asyncio.get_running_loop().time() + delay)
        return outcome == "done"

    def _spawn_attempt(self, commanded: Commanded) -> None:
        task = asyncio.ensure_future(self._attempt_relinquish(commanded))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _spawn_relinquish(self, commanded: Commanded) -> None:
        """Track `commanded` and attempt it once in the background, so one
        slow or dead remote can't delay this tick or another connector's."""
        self._track_pending(commanded)
        self._spawn_attempt(commanded)

    def _retry_pending_relinquishes(self) -> None:
        """One more background try for each pending relinquish not in flight
        and past its retry delay."""
        now = asyncio.get_running_loop().time()
        for key, commanded in list(self._pending_relinquish.items()):
            if key in self._relinquishing:
                continue
            retry = self._relinquish_retry.get(key)
            if retry is not None and now < retry[1]:
                continue
            self._relinquishing.add(key)
            self._spawn_attempt(commanded)

    async def _relinquish(self, commanded: Commanded) -> str:
        """Write NULL at the priority this device commanded. "done" once it
        was sent, isn't needed (a live mapping owns the target) or the
        remote refused it; "suppressed" under DCC; "unreachable" when the
        remote didn't answer."""
        try:
            binding = await self.client.bind(
                commanded.remote_instance, commanded.remote_address, timeout=commanded.timeout
            )
            if self._same_target_commands(commanded):
                # A live write mapping owns this target now (e.g. deleted and
                # added again while this relinquish was pending): a NULL here
                # would undo its write.
                return "done"
            await self.client.write(
                binding, commanded.point, None, priority=commanded.priority, timeout=commanded.timeout
            )
            # A live mapping may have written this same target while the
            # NULL was in flight, so the NULL may have landed after it:
            # re-send that value on the next tick.
            for live in self._same_target_commands(commanded):
                live.last_value = RESEND
            return "done"
        except (ClientError, ErrorRejectAbortNack, Exception) as exc:
            if isinstance(exc, ClientError) and exc.kind == "dcc":
                return "suppressed"
            if isinstance(exc, ClientError) and exc.transport:
                self.client.unbind(commanded.remote_instance, commanded.remote_address)
                # Warned on the first failure and when the delay reaches its
                # cap, not every retry: a remote that never answers would
                # otherwise log every 5 minutes for good.
                failures = self._relinquish_retry.get(id(commanded), (0, 0.0))[0] + 1
                if failures == 1:
                    then = "will retry"
                elif unanswered_retry_delay(failures) == MAX_BACKOFF_SECONDS > unanswered_retry_delay(failures - 1):
                    then = f"still trying, every {MAX_BACKOFF_SECONDS:g} s from now on"
                else:
                    then = None
                if then is not None:
                    logger.warning(
                        "device %d: couldn't reach device %d to relinquish %s at priority %d (%s); %s",
                        self.device_id,
                        commanded.remote_instance,
                        commanded.point,
                        commanded.priority,
                        error_text(exc),
                        then,
                    )
                return "unreachable"
            logger.warning(
                "device %d: couldn't relinquish %s at priority %d on device %d: %s",
                self.device_id,
                commanded.point,
                commanded.priority,
                commanded.remote_instance,
                error_text(exc),
            )
            return "done"

    def _same_target_commands(self, commanded: Commanded) -> List[Commanded]:
        """The live commands (other than `commanded` itself) that write the
        same remote point at the same priority. The address isn't compared:
        it's only the route, and a relinquish retried after the connector
        moved address must still see the live write."""
        return [
            live
            for live in self._commanded.values()
            if live is not commanded
            and live.remote_instance == commanded.remote_instance
            and live.point == commanded.point
            and live.priority == commanded.priority
        ]
