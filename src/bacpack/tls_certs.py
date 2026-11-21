"""Certificates for the web server's HTTPS: make a
self-signed one, check an uploaded pair, and keep both files under
<data dir>/tls/. Pure certificate work: the caller passes the time.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import re
import shutil
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .data_files import hand_to_owner, owner_for

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
# Whether a certificate is BACpack's own is read from its subject, not stored.
GENERATED_ORG = "BACpack self-signed"
MAX_PEM_BYTES = 262144

_VALID_YEARS_IN_DAYS = 3650
# A browser whose clock runs a little behind still accepts a new certificate.
_BACKDATE = dt.timedelta(minutes=5)
# A pair made on a machine whose clock runs a little ahead is still accepted.
_CLOCK_SKEW = dt.timedelta(minutes=5)
_DNS_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FIX = "bacpack-admin web --self-signed"
_SCRATCH_PREFIX = ".check-"


class CertProblem(ValueError):
    """A certificate or key that cannot be used; the message is meant for a person."""


@dataclass(frozen=True)
class CertInfo:
    fingerprint_sha256: str
    subject: str
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime
    sans: tuple[str, ...]
    generated_by_bacpack: bool


def tls_dir(data_dir: Path) -> Path:
    return data_dir / "tls"


def _valid_dns_name(name: str) -> bool:
    return 0 < len(name) <= 253 and all(_DNS_LABEL.match(label) for label in name.split("."))


def generate_self_signed(hostname: str, addresses: Sequence[str], now: dt.datetime) -> tuple[bytes, bytes]:
    """Certificate PEM and unencrypted PKCS8 key PEM. ECDSA P-256: quick to
    make at first start and accepted by browsers and every TLS client."""
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    seen: set[str] = set()
    for name in (hostname, "localhost"):
        if _valid_dns_name(name) and name.lower() not in seen:
            seen.add(name.lower())
            sans.append(x509.DNSName(name))
    for text in ("127.0.0.1", *addresses):
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        # A link-local address (an adapter that found no DHCP) is one no client
        # browses to; it only clutters the certificate's address list.
        if ip.is_link_local:
            continue
        if str(ip) not in seen:
            seen.add(str(ip))
            sans.append(x509.IPAddress(ip))
    common_name = hostname if _valid_dns_name(hostname) else "BACpack"
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, GENERATED_ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    start = now - _BACKDATE
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=_VALID_YEARS_IN_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # ECDHE key exchange needs only the signature bit.
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def _fingerprint(cert: x509.Certificate) -> str:
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _leaf(cert_pem: bytes) -> x509.Certificate:
    if b"PRIVATE KEY" in cert_pem:
        raise CertProblem("That is a private key, not a certificate. Put the certificate in the certificate box.")
    return _certificates(cert_pem)[0]


def _certificates(cert_pem: bytes) -> list[x509.Certificate]:
    try:
        certs = x509.load_pem_x509_certificates(cert_pem)
    except ValueError:
        certs = []
    if not certs:
        raise CertProblem("No PEM certificate found. Expected text starting with -----BEGIN CERTIFICATE-----.")
    return certs


def _info(cert: x509.Certificate) -> CertInfo:
    sans: list[str] = []
    try:
        extension = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        pass
    else:
        sans.extend(extension.value.get_values_for_type(x509.DNSName))
        sans.extend(str(ip) for ip in extension.value.get_values_for_type(x509.IPAddress))
    organisations = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return CertInfo(
        fingerprint_sha256=_fingerprint(cert),
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        sans=tuple(sans),
        generated_by_bacpack=any(a.value == GENERATED_ORG for a in organisations),
    )


def describe_certificate(cert_pem: bytes) -> CertInfo:
    return _info(_leaf(cert_pem))


def _load_key(key_pem: bytes):
    try:
        return serialization.load_pem_private_key(key_pem, password=None)
    except TypeError:
        raise CertProblem(
            "The private key is protected by a passphrase. Remove the passphrase first "
            "(for example with: openssl pkey -in key.pem -out key-nopass.pem)."
        )
    except ValueError:
        raise CertProblem("No unencrypted PEM private key found in the key file.")


def _matched(cert_pem: bytes, key_pem: bytes) -> CertInfo:
    cert = _leaf(cert_pem)
    key = _load_key(key_pem)
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    if cert.public_key().public_bytes(der, spki) != key.public_key().public_bytes(der, spki):
        raise CertProblem("The private key does not match the certificate (the first certificate in the file).")
    return _info(cert)


def _ssl_problem(cert_file: Path, key_file: Path) -> None:
    """The pair as the server will load it: OpenSSL's own policy (a too-small
    RSA key, say) can refuse a pair that parses and matches, and uvicorn would
    then exit on every start."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_file, key_file)
    except ssl.SSLError as exc:
        raise CertProblem(f"The system's TLS library refuses this certificate and key: {exc.reason or exc}.")


def _prepare_dir(data_dir: Path) -> tuple[Path, os.stat_result | None]:
    """The tls folder, made if need be (0700), and the owner its files go to."""
    directory = tls_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    owner = owner_for(data_dir)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    hand_to_owner(directory, owner)
    return directory, owner


def check_pair(cert_pem: bytes, key_pem: bytes, now: dt.datetime, data_dir: Path | None = None) -> CertInfo:
    if len(cert_pem) > MAX_PEM_BYTES or len(key_pem) > MAX_PEM_BYTES:
        raise CertProblem(f"The file is too large (limit {MAX_PEM_BYTES // 1024} KiB).")
    info = _matched(cert_pem, key_pem)
    if info.not_after < now:
        raise CertProblem(f"The certificate expired on {info.not_after:%Y-%m-%d %H:%M} UTC.")
    if info.not_before > now + _CLOCK_SKEW:
        raise CertProblem(f"The certificate is not valid until {info.not_before:%Y-%m-%d %H:%M} UTC.")
    # The key is written out for the check: inside the tls folder when there is
    # a data dir, which only the service account can open.
    scratch = _prepare_dir(data_dir)[0] if data_dir is not None else None
    with tempfile.TemporaryDirectory(prefix=_SCRATCH_PREFIX, dir=scratch) as folder:
        cert_file, key_file = Path(folder) / CERT_FILE, Path(folder) / KEY_FILE
        cert_file.write_bytes(cert_pem)
        key_file.write_bytes(key_pem)
        _ssl_problem(cert_file, key_file)
    return info


def write_pair(data_dir: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """Both files replaced via temp names: the key first, then the certificate.
    The old pair is hard-linked aside first and put back if the second replace
    fails; a crash between the two replaces leaves a pair that does not match,
    which the launcher refuses (load_installed)."""
    directory, owner = _prepare_dir(data_dir)
    # Left by a crashed write or check; the key may be in them.
    for leftover in directory.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    for leftover in directory.glob(_SCRATCH_PREFIX + "*"):
        shutil.rmtree(leftover, ignore_errors=True)
    targets = ((KEY_FILE, key_pem, 0o600), (CERT_FILE, cert_pem, 0o644))
    temps: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    replaced: list[Path] = []
    try:
        for name, content, mode in targets:
            # mkstemp makes the file 0600, so the key is never wider.
            fd, temp_name = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=directory)
            temp = Path(temp_name)
            temps.append((temp, directory / name))
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            if os.name != "nt":
                os.chmod(temp, mode)
            hand_to_owner(temp, owner)
        for _, final in temps:
            if final.exists():
                backup = final.with_name(final.name + ".bak")
                backup.unlink(missing_ok=True)
                try:
                    os.link(final, backup)
                except OSError:
                    shutil.copy2(final, backup)
                backups.append((backup, final))
        for temp, final in temps:
            os.replace(temp, final)
            replaced.append(final)
    except BaseException:
        for backup, final in backups:
            if final in replaced:
                os.replace(backup, final)
        raise
    finally:
        for path, _ in temps + backups:
            path.unlink(missing_ok=True)


def read_certificate_pem(data_dir: Path) -> bytes:
    """Only the certificates in cert.pem, re-encoded: a hand-placed file that
    also holds the key must never serve it."""
    certs = _certificates((tls_dir(data_dir) / CERT_FILE).read_bytes())
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def load_installed(data_dir: Path) -> CertInfo | None:
    """None when no pair exists yet. A half pair, an unreadable file or a key
    that does not match raises CertProblem; expiry is not checked (an expired
    own certificate still serves)."""
    directory = tls_dir(data_dir)
    cert_path = directory / CERT_FILE
    key_path = directory / KEY_FILE
    try:
        has_cert, has_key = cert_path.exists(), key_path.exists()
    except OSError as exc:
        raise CertProblem(f"Cannot read the certificate files in {directory}: {exc.strerror or exc}.")
    if not has_cert and not has_key:
        return None
    if not has_cert:
        raise CertProblem(f"{CERT_FILE} is missing from {directory} but {KEY_FILE} is there. Run {_FIX} to make a new pair.")
    if not has_key:
        raise CertProblem(f"{KEY_FILE} is missing from {directory} but {CERT_FILE} is there. Run {_FIX} to make a new pair.")
    try:
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
    except OSError as exc:
        raise CertProblem(f"Cannot read the certificate files in {directory}: {exc.strerror or exc}.")
    try:
        info = _matched(cert_pem, key_pem)
        _ssl_problem(cert_path, key_path)
        return info
    except CertProblem as exc:
        raise CertProblem(f"The certificate files in {directory} are unusable: {exc} Run {_FIX} to make a new pair.")


def ensure_self_signed(
    data_dir: Path, hostname: str, addresses: Sequence[str], now: dt.datetime
) -> tuple[CertInfo, bool]:
    """The installed certificate, else a new self-signed one (True). Never
    overwrites: a broken pair raises instead."""
    installed = load_installed(data_dir)
    if installed is not None:
        return installed, False
    cert_pem, key_pem = generate_self_signed(hostname, addresses, now)
    write_pair(data_dir, cert_pem, key_pem)
    return describe_certificate(cert_pem), True
