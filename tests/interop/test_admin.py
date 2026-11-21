"""DeviceCommunicationControl, ReinitializeDevice, Who-Has, TimeSynchronization."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import parse, raw
from .helpers import poll, rp, rpm
from .stack import IOP1, IOP2, IOP2_PASSWORD

# A DCC disable carries this duration (minutes) as a safety net; the test
# ends it sooner by restarting the device.
DISABLE_MINUTES = 1


def test_dcc_refuses_a_wrong_or_missing_password(stack):
    t = stack.tools
    for args in ((), ("wrong",)):
        result = t.run("bacdcc", IOP2, 1, DISABLE_MINUTES, *args)
        assert parse.parse_error(result.text) == ("security", "password-failure"), result.describe()
    # Still answering: nothing was disabled.
    assert rp(t, IOP2, "device", IOP2, "object-name") == "IOP-2"


def test_dcc_disable_silences_the_device(stack):
    t = stack.tools
    try:
        t.run("bacdcc", IOP2, 1, DISABLE_MINUTES, IOP2_PASSWORD).expect_ok()
        silent = t.run("bacrp", IOP2, "device", IOP2, "object-name", timeout=8)
        assert silent.timed_out or silent.code != 0, silent.describe()
        # A disabled device ignores Who-Is too (135 DM-DCC).
        assert IOP2 not in parse.parse_whois(t.run("bacwi", IOP2, IOP2, "--timeout", "2000").out)
    finally:
        # bacnet-stack 1.7.0 can't end a disable: bacdcc and bacrd bind with a
        # Who-Is, which a disabled device ignores, and bacrd has no --mac. The
        # product's own restart does it, and doesn't need the device to answer.
        stack.restart(IOP2)
        stack.wait_until_answering((IOP2,))
    assert rp(t, IOP2, "device", IOP2, "object-name") == "IOP-2"


def test_reinitialize_refuses_a_wrong_or_missing_password(stack):
    t = stack.tools
    for args in ((), ("wrong",)):
        result = t.run("bacrd", IOP2, 1, *args)
        assert parse.parse_error(result.text) == ("security", "password-failure"), result.describe()


def test_warm_start_with_the_password_restarts_the_device(stack):
    t = stack.tools
    before = stack.pid(IOP2)
    result = t.run("bacrd", IOP2, 1, IOP2_PASSWORD).expect_ok()
    assert "Acknowledged" in result.out
    # The device only marks itself for a restart; the parent's health-check
    # task (running in the fixture) carries it out.
    poll(lambda: stack.pid(IOP2) not in (None, before), timeout=60, what="a new process for IOP-2")
    stack.wait_until_answering((IOP2,))
    assert rp(t, IOP2, "device", IOP2, "object-name") == "IOP-2"


def test_who_has_by_name_and_by_object_id(stack):
    t = stack.tools
    assert parse.parse_who_has(t.run("bacwh", "IOP2 Value", timeout=15).text) == {("analog-value", 1, IOP2)}
    assert parse.parse_who_has(t.run("bacwh", "IOP Setpoint", timeout=15).text) == {("analog-value", 1, IOP1)}
    # Both devices have binary-value 1.
    both = parse.parse_who_has(t.run("bacwh", "binary-value", 1, timeout=15).text)
    assert {device for _, _, device in both} == {IOP1, IOP2}
    assert {(kind, instance) for kind, instance, _ in both} == {("binary-value", 1)}


def test_time_synchronization_sets_the_device_clock(stack):
    # bacts with no arguments sends the host's own local time.
    t = stack.tools
    t.run("bacts").expect_ok()

    def close():
        got = rpm(t, IOP1, ("device", IOP1, "local-date,local-time"))[("device", IOP1)]
        device_now = parse.parse_device_datetime(got["local-date"], got["local-time"])
        # Whole datetimes, so a run around local midnight compares correctly.
        return abs(device_now - datetime.now()) < timedelta(seconds=10)

    poll(close, timeout=20, what="the device clock within 10 s of the host's local time")


def _answers(tools, device):
    return tools.run("bacrp", device, "device", device, "object-name", timeout=8).code == 0


def test_dcc_enable_and_reinitialize_are_honoured_while_disabled(stack):
    # 135 16.1 and 16.4: a DCC-disabled device still answers these two. The
    # requests are hand-built (tests/interop/raw.py): bacnet-stack 1.7.0 binds
    # by Who-Is, which a disabled device ignores, and bacrd has no --mac.
    t = stack.tools
    to_device = ("127.0.0.2", 47821)

    def disable():
        t.run("bacdcc", IOP2, 1, DISABLE_MINUTES, IOP2_PASSWORD).expect_ok()
        assert not _answers(t, IOP2)

    try:
        disable()
        refused = raw.send_and_receive(raw.reinitialize_device(1, raw.WARMSTART, "wrong"), to_device)
        assert (refused.kind, refused.error_class, refused.error_code) == (
            "error", raw.ERROR_CLASS_SECURITY, raw.ERROR_CODE_PASSWORD_FAILURE
        ), refused
        assert not _answers(t, IOP2)  # a refused request changed nothing

        enabled = raw.send_and_receive(raw.device_communication_control(2, raw.ENABLE, IOP2_PASSWORD), to_device)
        assert (enabled.kind, enabled.service) == ("simple-ack", raw.SERVICE_DEVICE_COMMUNICATION_CONTROL), enabled
        poll(lambda: _answers(t, IOP2), timeout=60, what="IOP-2 answering after DCC enable")

        disable()
        before = stack.pid(IOP2)
        restart = raw.send_and_receive(raw.reinitialize_device(3, raw.WARMSTART, IOP2_PASSWORD), to_device)
        assert (restart.kind, restart.service) == ("simple-ack", raw.SERVICE_REINITIALIZE_DEVICE), restart
        # The health-check task restarts the device, and a restart is enabled.
        poll(lambda: stack.pid(IOP2) not in (None, before), timeout=60, what="a new process for IOP-2")
        poll(lambda: _answers(t, IOP2), timeout=60, what="IOP-2 answering after the restart")
    finally:
        stack.restart(IOP2)
        stack.wait_until_answering((IOP2,))
