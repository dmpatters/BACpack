"""The web server's own settings and operations:
the listen address and the HTTPS certificate. The one place that combines
tls_certs with validation and Logs-page events; the launcher, the settings API
and bacpack-admin all call it.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil

from . import security_log, timeutil, tls_certs
from .models import ServerSettings, get_or_create_settings
from .tls_certs import CertInfo, CertProblem

DEFAULT_LISTEN_ADDRESS = "0.0.0.0"
LOCAL = "this machine"


def local_ipv4_addresses() -> list[str]:
    found: set[str] = set()
    for addresses in psutil.net_if_addrs().values():
        found.update(a.address for a in addresses if a.family == socket.AF_INET)
    return sorted(found)


def check_listen_address(address: str, local: Sequence[str] | None = None) -> str | None:
    """A readable problem, or None. IPv4 only: the certificate's addresses, the
    firewall rules and the docs are IPv4."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return f"{address!r} is not an IPv4 address (for example 0.0.0.0 or 192.168.1.50)."
    if address != str(ip):
        return f"Write the address as {ip}."
    if ip.is_unspecified or ip.is_loopback:
        return None
    here = local_ipv4_addresses() if local is None else local
    if address not in here:
        return f"This machine does not have the address {address}."
    return None


def get_listen_address(session) -> str:
    """Read-only: callable without the write lock."""
    settings = session.get(ServerSettings, 1)
    return settings.web_listen_address if settings is not None else DEFAULT_LISTEN_ADDRESS


def set_listen_address(session, address: str, who: str) -> bool:
    """The caller holds lock_both_files. Commits here so the log line is
    written only for a change that was saved."""
    problem = check_listen_address(address)
    if problem is not None:
        raise ValueError(problem)
    settings = get_or_create_settings(session)
    if settings.web_listen_address == address:
        return False
    settings.web_listen_address = address
    session.commit()
    security_log.listen_address_changed(who, address)
    return True


@dataclass(frozen=True)
class CertificateStatus:
    info: CertInfo | None
    problem: str | None


def certificate_status(data_dir: Path) -> CertificateStatus:
    try:
        return CertificateStatus(tls_certs.load_installed(data_dir), None)
    except CertProblem as exc:
        return CertificateStatus(None, str(exc))


def make_self_signed(data_dir: Path, who: str, what: str = "regenerated") -> CertInfo:
    cert_pem, key_pem = tls_certs.generate_self_signed(
        socket.gethostname(), local_ipv4_addresses(), timeutil.utc_now()
    )
    tls_certs.write_pair(data_dir, cert_pem, key_pem)
    info = tls_certs.describe_certificate(cert_pem)
    security_log.certificate_changed(who, what, info.fingerprint_sha256)
    return info


def install_own_certificate(data_dir: Path, cert_pem: bytes, key_pem: bytes, who: str) -> CertInfo:
    info = tls_certs.check_pair(cert_pem, key_pem, timeutil.utc_now(), data_dir)
    tls_certs.write_pair(data_dir, cert_pem, key_pem)
    security_log.certificate_changed(who, "own certificate installed", info.fingerprint_sha256)
    return info


def ensure_certificate(data_dir: Path) -> tuple[CertInfo, bool]:
    info, created = tls_certs.ensure_self_signed(
        data_dir, socket.gethostname(), local_ipv4_addresses(), timeutil.utc_now()
    )
    if created:
        security_log.certificate_changed(LOCAL, "generated at first start", info.fingerprint_sha256)
    return info, created
