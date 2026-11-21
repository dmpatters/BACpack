"""SubscribeCOV from bacnet-stack. bacscov 1.7.0 prints only that an
unconfirmed notification arrived, not the values in it, and prints nothing
for a confirmed one: so the notification's value is not checked here, only
that the subscription is accepted and that notifications arrive and stop."""
from __future__ import annotations

import time

from . import parse, raw
from .helpers import REAL, relinquish, write
from .stack import IOP1, IOP2, IOP2_PASSWORD
from .tools import OTHER_CLIENT_PORT

# The writes come from another client port: the subscriber listens on the
# usual one, and a write answered on a shared port could be swallowed by
# the subscriber (or take its notification).
WRITER = {"client_port": OTHER_CLIENT_PORT}

PROCESS = 7


def test_unconfirmed_subscription_notifies_initially_and_on_change(stack):
    t = stack.tools
    wrote = []

    def until(text):
        seen = parse.count_cov_notifications(text)
        if seen >= 1 and not wrote:
            wrote.append(write(t, IOP1, "analog-value", 1, "present-value", 8, REAL, 61.0, **WRITER).expect_ok())
        return seen >= 2

    try:
        result = t.run_until("bacscov", IOP1, "analog-value", 1, PROCESS, "unconfirmed", 60, until=until)
        assert not result.timed_out, result.describe()
        assert "SubscribeCOV Acknowledged!" in result.out
    finally:
        # A killed subscriber leaves its subscription behind, and a later
        # subscriber on the same client port would receive its notifications.
        t.run("bacscov", IOP1, "analog-value", 1, PROCESS, "cancel", line_buffered=True)
        relinquish(t, IOP1, "analog-value", 1, 8, **WRITER)


def test_confirmed_subscription_is_accepted(stack):
    t = stack.tools
    try:
        result = t.run_until(
            "bacscov", IOP1, "analog-value", 1, PROCESS + 1, "confirmed", 60,
            until=lambda text: "SubscribeCOV Acknowledged!" in text,
        )
        assert not result.timed_out, result.describe()
    finally:
        t.run("bacscov", IOP1, "analog-value", 1, PROCESS + 1, "cancel", line_buffered=True)


def test_cancel_ends_the_notifications(stack):
    t = stack.tools
    state = {"cancelled_at": None}

    def until(text):
        seen = parse.count_cov_notifications(text)
        if seen >= 1 and state["cancelled_at"] is None:
            t.run("bacscov", IOP1, "analog-output", 1, PROCESS + 2, "cancel", line_buffered=True).expect_ok()
            write(t, IOP1, "analog-output", 1, "present-value", 8, REAL, 62.0, **WRITER).expect_ok()
            state["cancelled_at"] = time.monotonic()
        # The one wait for something not to happen: a bounded look after the
        # change, no timing is asserted.
        return state["cancelled_at"] is not None and time.monotonic() - state["cancelled_at"] > 4

    try:
        result = t.run_until("bacscov", IOP1, "analog-output", 1, PROCESS + 2, "unconfirmed", 60, until=until)
        assert state["cancelled_at"] is not None, result.describe()
        assert parse.count_cov_notifications(result.out + result.err) == 1, result.describe()
    finally:
        t.run("bacscov", IOP1, "analog-output", 1, PROCESS + 2, "cancel", line_buffered=True)
        relinquish(t, IOP1, "analog-output", 1, 8, **WRITER)


def test_dcc_disable_initiation_stops_cov_notifications_until_enable(stack):
    # IOP-2, not IOP-1: IOP-1 is the BBMD for the whole setup. The DCC requests
    # are hand-built (tests/interop/raw.py): bacdcc takes only enable/disable.
    t = stack.tools
    to_device = ("127.0.0.2", 47821)
    state = {"phase": 0, "quiet_since": None}
    enabled = []

    def dcc(invoke_id, mode):
        reply = raw.send_and_receive(raw.device_communication_control(invoke_id, mode, IOP2_PASSWORD), to_device)
        assert reply.kind == "simple-ack", reply
        return reply

    def until(text):
        seen = parse.count_cov_notifications(text)
        if state["phase"] == 0 and seen >= 1:
            dcc(1, raw.DISABLE_INITIATION)
            write(t, IOP2, "analog-value", 1, "present-value", 8, REAL, 61.0, **WRITER).expect_ok()
            state["phase"], state["quiet_since"] = 1, time.monotonic()
        elif state["phase"] == 1 and time.monotonic() - state["quiet_since"] > 4:
            # The one wait for something not to happen: a bounded look.
            assert seen == 1, "a COV notification went out while DCC disable-initiation was set"
            dcc(2, raw.ENABLE)
            enabled.append(True)
            write(t, IOP2, "analog-value", 1, "present-value", 8, REAL, 62.0, **WRITER).expect_ok()
            state["phase"] = 2
        return state["phase"] == 2 and seen >= 2

    try:
        result = t.run_until("bacscov", IOP2, "analog-value", 1, PROCESS + 3, "unconfirmed", 60, until=until)
        assert state["phase"] == 2 and not result.timed_out, result.describe()
    finally:
        if not enabled:
            try:
                dcc(3, raw.ENABLE)
            except Exception:
                stack.restart(IOP2)
                stack.wait_until_answering((IOP2,))
        t.run("bacscov", IOP2, "analog-value", 1, PROCESS + 3, "cancel", line_buffered=True)
        relinquish(t, IOP2, "analog-value", 1, 8, **WRITER)
