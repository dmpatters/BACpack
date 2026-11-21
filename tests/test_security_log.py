from __future__ import annotations

import inspect
import logging

import pytest

from bacpack import security_log


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.INFO, logger="bacpack.security")


def _last(caplog):
    record = caplog.records[-1]
    return record.levelno, record.getMessage()


@pytest.mark.parametrize(
    "call, level, parts",
    [
        (lambda: security_log.login_ok("1.2.3.4", 'ad"min'), logging.INFO, ["1.2.3.4", '"ad\\"min"']),
        (lambda: security_log.login_failed("1.2.3.4", True), logging.WARNING, ["1.2.3.4", "wrong password"]),
        (lambda: security_log.login_failed("1.2.3.4", False), logging.WARNING, ["unknown username"]),
        (lambda: security_log.locked_out("1.2.3.4", 900), logging.WARNING, ["1.2.3.4", "15 minutes"]),
        (lambda: security_log.logout("1.2.3.4"), logging.INFO, ["1.2.3.4"]),
        (lambda: security_log.setup_chose_login("1.2.3.4", "bob"), logging.INFO, ["bob", "1.2.3.4"]),
        (lambda: security_log.setup_chose_none("1.2.3.4"), logging.WARNING, ["without login"]),
        (lambda: security_log.mode_switched("1.2.3.4", "none"), logging.WARNING, ["none"]),
        (lambda: security_log.password_changed("1.2.3.4"), logging.INFO, ["password changed"]),
        (lambda: security_log.username_changed("1.2.3.4", "bob"), logging.INFO, ["bob"]),
        (lambda: security_log.local_reset("bob"), logging.WARNING, ["reset", "bob"]),
        (lambda: security_log.listen_address_changed("1.2.3.4", "10.0.0.5"), logging.WARNING, ["10.0.0.5", "1.2.3.4"]),
        (
            lambda: security_log.certificate_changed("this machine", "regenerated", "AB:CD"),
            logging.WARNING,
            ["regenerated", "this machine", "AB:CD"],
        ),
        (lambda: security_log.key_created(7, "bpk_abcdefgh"), logging.INFO, ["7", "bpk_abcdefgh"]),
        (lambda: security_log.key_revoked(7, "bpk_abcdefgh"), logging.INFO, ["revoked", "7"]),
        (
            lambda: security_log.bacnet_password_failed("1.2.3.4:47808", "DeviceCommunicationControl"),
            logging.WARNING,
            ["1.2.3.4:47808", "DeviceCommunicationControl", "refused"],
        ),
        (
            lambda: security_log.secret_key_unusable("/data/secret.key"),
            logging.ERROR,
            ["secret.key", "unusable"],
        ),
    ],
)
def test_event_level_and_content(caplog, call, level, parts):
    call()
    got_level, message = _last(caplog)
    assert got_level == level
    for part in parts:
        assert part in message


def test_no_secret_shaped_arguments():
    for name, func in inspect.getmembers(security_log, inspect.isfunction):
        if func.__module__ != security_log.__name__:
            continue
        for parameter in inspect.signature(func).parameters:
            assert not any(word in parameter for word in ("password", "token", "session", "secret")), (name, parameter)
            assert parameter != "key", name
