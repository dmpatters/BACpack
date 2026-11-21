import time


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
