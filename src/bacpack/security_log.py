"""Login and security events for the Logs page. No function takes a
password, key, token or session id, so none can reach a log line; a failed
login never carries the attempted username either (a password typed into
the username box would land on the Logs page).
"""
from __future__ import annotations

import logging

from .log_names import quote

logger = logging.getLogger("bacpack.security")


def login_ok(ip: str, username: str) -> None:
    logger.info("login: %s signed in from %s", quote(username), ip)


def login_failed(ip: str, username_matched: bool) -> None:
    reason = "wrong password" if username_matched else "unknown username"
    logger.warning("login failed from %s: %s", ip, reason)


def locked_out(ip: str, seconds: float) -> None:
    logger.warning("login locked out: %s for %d minutes after repeated failed logins", ip, round(seconds / 60))


def logout(ip: str) -> None:
    logger.info("logout from %s", ip)


def setup_chose_login(ip: str, username: str) -> None:
    logger.info("setup: login created for %s from %s", quote(username), ip)


def setup_chose_none(ip: str) -> None:
    logger.warning("setup: chose to run without login, from %s", ip)


def mode_switched(ip: str, mode: str) -> None:
    logger.warning("login mode switched to %s from %s", quote(mode), ip)


def password_changed(ip: str) -> None:
    logger.info("login password changed from %s; every session ended", ip)


def username_changed(ip: str, username: str) -> None:
    logger.info("login username changed to %s from %s", quote(username), ip)


def local_reset(username: str) -> None:
    logger.warning("login reset on this machine for %s; every session ended", quote(username))


def listen_address_changed(who: str, address: str) -> None:
    logger.warning("web server listen address set to %s from %s; takes effect at the next restart", address, who)


def certificate_changed(who: str, what: str, fingerprint: str) -> None:
    logger.warning("web server certificate %s from %s; SHA-256 %s", what, who, fingerprint)


def key_created(key_id: int, prefix: str) -> None:
    logger.info("API key %d (%s...) created", key_id, prefix)


def key_revoked(key_id: int, prefix: str) -> None:
    logger.info("API key %d (%s...) revoked", key_id, prefix)


def secret_key_unusable(key_file) -> None:
    logger.error("a credential was not saved: the encryption key file %s is unusable", quote(key_file))


def bacnet_password_failed(source: str, service: str) -> None:
    logger.warning("%s refused: wrong or missing password from %s", service, source)
