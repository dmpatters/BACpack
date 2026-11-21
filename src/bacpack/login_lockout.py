"""Per-client-IP lockout for failed logins, in memory.

Per IP rather than per account, so nobody else can lock the owner out. Each
IPv6 address is its own key (a /64 key would let any neighbour on the subnet
lock the owner out). A restart clears it, and so does a local reset (the web process sees the settings' login_epoch
change).
"""
from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

MAX_FAILURES = 5
WINDOW_SECONDS = 900
BASE_LOCK_SECONDS = 900
MAX_LOCK_SECONDS = 86400
MAX_TRACKED = 10_000
# How long an attempt refused for want of a free slot is told to wait.
BUSY_RETRY_SECONDS = 1.0


@dataclass
class _Record:
    failures: list = field(default_factory=list)
    locked_until: float = 0.0
    strikes: int = 0
    last_failure: float = 0.0
    in_flight: int = 0


def _key(ip: str) -> str:
    """The address as the lock's key. An IPv4-mapped IPv6 address (what a
    dual-stack listener reports for an IPv4 client) is the plain IPv4 one."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


class LoginLockout:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> Optional[float]:
        """Seconds left on this IP's lock, or None when it isn't locked."""
        with self._lock:
            record = self._records.get(_key(ip))
            if record is None:
                return None
            left = record.locked_until - self._clock()
            return left if left > 0 else None

    def begin(self, ip: str) -> Optional[float]:
        """Starts a password check, or refuses it with the seconds to wait.
        The check and the reservation are one step: attempts still running
        count as failures, so a burst of parallel logins can't all pass the
        lock before the first failure is recorded. Every successful begin is
        paired with release()."""
        with self._lock:
            now = self._clock()
            key = _key(ip)
            record = self._records.get(key)
            if record is None:
                self._make_room(now)
                record = self._records[key] = _Record()
            if record.locked_until > now:
                return record.locked_until - now
            record.failures = [t for t in record.failures if now - t < WINDOW_SECONDS]
            if len(record.failures) + record.in_flight >= MAX_FAILURES:
                return BUSY_RETRY_SECONDS
            record.in_flight += 1
            return None

    def release(self, ip: str) -> None:
        with self._lock:
            record = self._records.get(_key(ip))
            if record is not None and record.in_flight > 0:
                record.in_flight -= 1

    def record_failure(self, ip: str) -> Optional[float]:
        """Counts a failed login. Returns the length of the lock this failure
        started, else None."""
        with self._lock:
            now = self._clock()
            key = _key(ip)
            record = self._records.get(key)
            if record is None:
                self._make_room(now)
                record = self._records[key] = _Record()
            if record.strikes and now - max(record.last_failure, record.locked_until) > MAX_LOCK_SECONDS:
                record.strikes = 0
            record.last_failure = now
            record.failures = [t for t in record.failures if now - t < WINDOW_SECONDS]
            record.failures.append(now)
            if len(record.failures) < MAX_FAILURES:
                return None
            seconds = min(BASE_LOCK_SECONDS * 2**record.strikes, MAX_LOCK_SECONDS)
            record.strikes += 1
            record.failures = []
            record.locked_until = now + seconds
            return float(seconds)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._records.pop(_key(ip), None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _make_room(self, now: float) -> None:
        if len(self._records) < MAX_TRACKED:
            return
        for key in [
            key
            for key, r in self._records.items()
            if r.locked_until <= now and now - r.last_failure > WINDOW_SECONDS and r.in_flight == 0
        ]:
            del self._records[key]
        while len(self._records) >= MAX_TRACKED:
            del self._records[next(iter(self._records))]
