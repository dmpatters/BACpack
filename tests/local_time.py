import time

import pytest


def local_target(seconds: float) -> tuple[time.struct_time, float]:
    """A local wall-clock time about `seconds` from now, and its real distance
    from now.

    A BACnet local time carries no DST flag, so a time inside the hour a
    fall-back repeats can mean either copy of it. When now + seconds
    lands there, the target is now - seconds instead: two DST changes are
    never that close together.
    """
    now = time.time()
    for delta in (seconds, -seconds):
        target = time.localtime(now + delta)
        if abs(time.mktime(target[:8] + (-1,)) - (now + delta)) < 1:
            return target, delta
    raise AssertionError(f"no unambiguous local time {seconds} s either side of now")


# East of UTC, west of UTC, southern summer (DST in January), and UTC itself.
ZONES = ["UTC", "Asia/Tokyo", "America/Los_Angeles", "Australia/Sydney"]


@pytest.fixture(params=ZONES)
def host_zone(request, monkeypatch):
    """Run a test in each of several host zones: on a UTC host a conversion
    that sends UTC is otherwise indistinguishable from a right one. Without
    time.tzset only the first zone runs, in the host's own zone."""
    if not hasattr(time, "tzset"):
        if request.param != ZONES[0]:
            pytest.skip("time.tzset is Unix-only; the other zones run on Linux")
        yield None
        return
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()
