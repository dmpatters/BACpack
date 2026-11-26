"""The names BACpack answers to while no login protects it.

A page on another site can be made to resolve its own name to this machine
(DNS rebinding), and then its requests carry that name as Host and pass the
cross-site check. With the login on the session cookie stops it; without a
login the only defence is to refuse a Host that isn't one of our own names.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

from .. import tls_certs


def host_of(header: str) -> str | None:
    """The host part of a Host header, lowercase, with the port and one
    trailing dot removed; an IPv6 literal keeps its brackets. None for a
    header that isn't a host with an optional numeric port."""
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        rest = value[end + 1:]
        if rest and not (rest.startswith(":") and (rest[1:] == "" or rest[1:].isdigit())):
            return None
        return value[: end + 1]
    host, colon, port = value.partition(":")
    if colon and port and not port.isdigit():
        return None
    host = host.removesuffix(".")
    if not host or any(not label for label in host.split(".")):
        return None
    return host


def _is_ip_literal(host: str) -> bool:
    candidate = host[1:-1].partition("%")[0] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def machine_names() -> frozenset[str]:
    """This computer's hostname and fqdn, their short forms, and each short
    form with .local (mDNS answers it, public DNS can't, so it can't be
    rebound). getfqdn can wait on a reverse lookup: call it off the event loop."""
    names: set[str] = set()
    for getter in (socket.gethostname, socket.getfqdn):
        try:
            name = getter().strip().lower().rstrip(".")
        except OSError:
            continue
        if name:
            short = name.partition(".")[0]
            names.update((name, short, short + ".local"))
    return frozenset(names)


class OwnNames:
    """One per app. The certificate's names are read again whenever cert.pem
    changes on disk, so a replaced certificate counts without a restart."""

    def __init__(self, data_dir: Path, machine: frozenset[str]) -> None:
        self._data_dir = data_dir
        self._machine = machine
        self._cert_stamp: tuple[int, int] | None = None
        self._cert: frozenset[str] = frozenset()

    def _certificate_names(self) -> frozenset[str]:
        path = tls_certs.tls_dir(self._data_dir) / tls_certs.CERT_FILE
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._cert_stamp, self._cert = None, frozenset()
            return self._cert
        if stamp != self._cert_stamp:
            try:
                info = tls_certs.describe_certificate(path.read_bytes())
                names = frozenset(s.lower().rstrip(".") for s in info.sans if not _is_ip_literal(s))
            except (OSError, tls_certs.CertProblem):
                names = frozenset()
            self._cert_stamp, self._cert = stamp, names
        return self._cert

    def _on_certificate(self, host: str) -> bool:
        names = self._certificate_names()
        if host in names:
            return True
        # A wildcard covers exactly one more label on the left (RFC 6125).
        _, dot, rest = host.partition(".")
        return bool(dot) and "*." + rest in names

    def allows(self, host_header: str | None) -> bool:
        if host_header is None:
            return False
        host = host_of(host_header)
        if host is None:
            return False
        if _is_ip_literal(host) or host == "localhost" or host.endswith(".localhost"):
            return True
        return host in self._machine or self._on_certificate(host)
