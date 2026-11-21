from __future__ import annotations

from bacpack import login_lockout as ll
from bacpack.login_lockout import LoginLockout


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _fail(lock, ip="1.1.1.1", times=5):
    result = None
    for _ in range(times):
        result = lock.record_failure(ip)
    return result


def test_fifth_failure_in_window_locks_for_15_minutes():
    clock = Clock()
    lock = LoginLockout(clock)
    assert _fail(lock, times=4) is None
    assert lock.check("1.1.1.1") is None
    assert lock.record_failure("1.1.1.1") == 900
    assert lock.check("1.1.1.1") == 900
    clock.now += 901
    assert lock.check("1.1.1.1") is None


def test_failures_outside_the_window_do_not_count():
    clock = Clock()
    lock = LoginLockout(clock)
    _fail(lock, times=4)
    clock.now += 901
    assert lock.record_failure("1.1.1.1") is None


def test_second_lock_is_30_minutes_then_doubles_to_the_24_hour_cap():
    clock = Clock()
    lock = LoginLockout(clock)
    lengths = []
    for _ in range(9):
        lengths.append(_fail(lock))
        clock.now += lengths[-1] + 1
    assert lengths[:4] == [900, 1800, 3600, 7200]
    assert max(lengths) == ll.MAX_LOCK_SECONDS == lengths[-1]


def test_strikes_reset_after_24_hours_without_a_failure():
    clock = Clock()
    lock = LoginLockout(clock)
    _fail(lock)
    clock.now += 86400 + 1000
    assert _fail(lock) == 900


def test_success_clears_the_ip():
    lock = LoginLockout(Clock())
    _fail(lock, times=4)
    lock.record_success("1.1.1.1")
    assert lock.record_failure("1.1.1.1") is None


def test_other_ips_are_unaffected():
    lock = LoginLockout(Clock())
    _fail(lock)
    assert lock.check("2.2.2.2") is None
    assert lock.record_failure("2.2.2.2") is None


def test_clear_unlocks_everyone():
    lock = LoginLockout(Clock())
    _fail(lock)
    lock.clear()
    assert lock.check("1.1.1.1") is None


def test_tracking_is_bounded(monkeypatch):
    monkeypatch.setattr(ll, "MAX_TRACKED", 50)
    clock = Clock()
    lock = LoginLockout(clock)
    for i in range(51):
        lock.record_failure(f"10.0.0.{i}")
    assert len(lock._records) <= 50
    clock.now += 10_000
    for i in range(60):
        lock.record_failure(f"10.1.0.{i}")
    assert len(lock._records) <= 50


def test_begin_reserves_so_attempts_in_flight_count_toward_the_limit():
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        assert lock.begin("1.1.1.1") is None
    # Five verifications are already running: a sixth waits, though none has failed yet.
    assert lock.begin("1.1.1.1") is not None
    lock.release("1.1.1.1")
    assert lock.begin("1.1.1.1") is None


def test_a_failure_after_begin_still_locks_and_release_is_harmless_after_success():
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        assert lock.begin("1.1.1.1") is None
        lock.record_failure("1.1.1.1")
        lock.release("1.1.1.1")
    assert lock.begin("1.1.1.1") is not None
    lock.record_success("1.1.1.1")
    lock.release("1.1.1.1")
    assert lock.begin("1.1.1.1") is None


def test_ipv6_hosts_in_one_slash_64_are_locked_independently():
    # A neighbour on the same subnet (or link-local) must not lock the owner out.
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        lock.record_failure("2001:db8:1:2::1")
    assert lock.check("2001:db8:1:2::1") is not None
    assert lock.check("2001:db8:1:2::2") is None
    assert lock.check("fe80::1") is None


def test_an_ipv4_mapped_address_shares_the_key_of_the_plain_ipv4_address():
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        lock.record_failure("::ffff:10.0.0.9")
    assert lock.check("10.0.0.9") is not None


def test_two_ipv4_mapped_addresses_do_not_share_a_lock():
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        lock.record_failure("::ffff:10.0.0.9")
    assert lock.check("::ffff:10.0.0.10") is None


def test_ipv4_and_unparseable_keys_are_kept_as_they_are():
    lock = LoginLockout(Clock())
    for _ in range(ll.MAX_FAILURES):
        lock.record_failure("unknown")
    assert lock.check("unknown") is not None
    assert lock.check("10.0.0.1") is None
