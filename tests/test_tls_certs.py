import datetime as dt
import hashlib
import os
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bacpack import timeutil, tls_certs
from bacpack.tls_certs import CertProblem


@pytest.fixture
def now():
    return timeutil.utc_now()


def _make(now, hostname="bacbox", addresses=("10.0.0.5",)):
    return tls_certs.generate_self_signed(hostname, list(addresses), now)


def _data_dir(tmp_path):
    return tmp_path / "data"


def test_generated_certificate_has_the_sans_ten_years_and_the_marker(now):
    cert_pem, key_pem = _make(now, addresses=("10.0.0.5", "192.168.1.9"))
    info = tls_certs.describe_certificate(cert_pem)
    for san in ("bacbox", "localhost", "127.0.0.1", "10.0.0.5", "192.168.1.9"):
        assert san in info.sans
    assert abs((info.not_after - info.not_before).days - 3650) <= 1
    assert info.generated_by_bacpack
    assert b"PRIVATE KEY" in key_pem


def test_fingerprint_is_uppercase_colon_hex_of_sha256_der(now):
    cert_pem, _ = _make(now)
    cert = x509.load_pem_x509_certificate(cert_pem)
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    expected = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    assert tls_certs.describe_certificate(cert_pem).fingerprint_sha256 == expected


def test_link_local_addresses_are_left_out_of_the_sans(now):
    # An adapter that failed DHCP: nothing reaches the server by 169.254.x.x.
    cert_pem, _ = _make(now, addresses=("169.254.10.2", "192.168.1.9", "10.0.0.5"))
    info = tls_certs.describe_certificate(cert_pem)
    assert "169.254.10.2" not in info.sans
    assert "192.168.1.9" in info.sans
    assert "10.0.0.5" in info.sans


def test_a_hostname_that_is_not_a_valid_dns_name_is_left_out_of_the_sans(now):
    cert_pem, _ = _make(now, hostname="bad host_name!")
    info = tls_certs.describe_certificate(cert_pem)
    assert "bad host_name!" not in info.sans
    assert "localhost" in info.sans


def test_check_pair_accepts_a_chain_with_the_leaf_first(now):
    cert_pem, key_pem = _make(now)
    other_cert, _ = _make(now, hostname="other")
    info = tls_certs.check_pair(cert_pem + other_cert, key_pem, now)
    assert info.fingerprint_sha256 == tls_certs.describe_certificate(cert_pem).fingerprint_sha256


def _foreign_cert(now, *, days_before, days_after):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "mine")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=days_before))
        .not_valid_after(now + dt.timedelta(days=days_after))
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _cases(now):
    cert_pem, key_pem = _make(now)
    _, other_key = _make(now)
    protected = serialization.load_pem_private_key(key_pem, None).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    )
    expired_cert, expired_key = _foreign_cert(now, days_before=400, days_after=-10)
    future_cert, future_key = _foreign_cert(now, days_before=-10, days_after=400)
    return [
        ("garbage", b"not a pem", key_pem, "certificate"),
        ("key as certificate", key_pem, key_pem, "private key"),
        ("certificate as key", cert_pem, cert_pem, "key"),
        ("protected key", cert_pem, protected, "passphrase"),
        ("other key", cert_pem, other_key, "match"),
        ("expired", expired_cert, expired_key, "expired"),
        ("not yet valid", future_cert, future_key, "not valid until"),
        ("too large", cert_pem + b"#" * (tls_certs.MAX_PEM_BYTES + 1), key_pem, "large"),
    ]


@pytest.mark.parametrize("index", range(8))
def test_check_pair_refuses(now, index):
    label, cert_pem, key_pem, word = _cases(now)[index]
    with pytest.raises(CertProblem) as raised:
        tls_certs.check_pair(cert_pem, key_pem, now)
    assert word in str(raised.value).lower(), label
    assert "PRIVATE KEY" not in str(raised.value)


def test_write_pair_creates_tls_dir_and_both_files_and_replaces_atomically(tmp_path, now, monkeypatch):
    data = _data_dir(tmp_path)
    data.mkdir()
    cert_pem, key_pem = _make(now)
    tls_certs.write_pair(data, cert_pem, key_pem)
    tdir = tls_certs.tls_dir(data)
    assert (tdir / tls_certs.CERT_FILE).read_bytes() == cert_pem
    assert (tdir / tls_certs.KEY_FILE).read_bytes() == key_pem

    new_cert, new_key = _make(now, hostname="other")
    real_open = os.open
    calls = {"n": 0}

    def failing_open(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(tls_certs.os, "open", failing_open)
    with pytest.raises(OSError):
        tls_certs.write_pair(data, new_cert, new_key)
    monkeypatch.undo()
    assert (tdir / tls_certs.CERT_FILE).read_bytes() == cert_pem
    assert (tdir / tls_certs.KEY_FILE).read_bytes() == key_pem
    assert sorted(p.name for p in tdir.iterdir()) == [tls_certs.CERT_FILE, tls_certs.KEY_FILE]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_key_is_0600_on_posix(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    tls_certs.write_pair(data, *_make(now))
    tdir = tls_certs.tls_dir(data)
    assert stat.S_IMODE((tdir / tls_certs.KEY_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE(tdir.stat().st_mode) == 0o700


def test_root_hands_the_files_to_the_data_dir_owner(tmp_path, now, monkeypatch):
    data = _data_dir(tmp_path)
    data.mkdir()
    chowned = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((str(path), uid, gid)), raising=False)
    tls_certs.write_pair(data, *_make(now))
    st = data.stat()
    names = {os.path.basename(p) for p, uid, gid in chowned if (uid, gid) == (st.st_uid, st.st_gid)}
    assert "tls" in names
    assert any(n.startswith("cert.pem.") and n.endswith(".tmp") for n in names)
    assert any(n.startswith("key.pem.") and n.endswith(".tmp") for n in names)


def test_load_installed_none_when_empty(tmp_path):
    assert tls_certs.load_installed(_data_dir(tmp_path)) is None


def test_load_installed_refuses_a_lone_cert_or_lone_key_and_a_mismatch(tmp_path, now):
    data = _data_dir(tmp_path)
    tdir = tls_certs.tls_dir(data)
    tdir.mkdir(parents=True)
    cert_pem, key_pem = _make(now)
    (tdir / tls_certs.CERT_FILE).write_bytes(cert_pem)
    with pytest.raises(CertProblem) as raised:
        tls_certs.load_installed(data)
    assert "key.pem" in str(raised.value)
    (tdir / tls_certs.CERT_FILE).unlink()
    (tdir / tls_certs.KEY_FILE).write_bytes(key_pem)
    with pytest.raises(CertProblem) as raised:
        tls_certs.load_installed(data)
    assert "cert.pem" in str(raised.value)
    (tdir / tls_certs.CERT_FILE).write_bytes(_make(now, hostname="other")[0])
    with pytest.raises(CertProblem) as raised:
        tls_certs.load_installed(data)
    assert "match" in str(raised.value)


def test_ensure_self_signed_creates_once_and_never_overwrites(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    first, created = tls_certs.ensure_self_signed(data, "bacbox", ["10.0.0.5"], now)
    assert created
    second, created_again = tls_certs.ensure_self_signed(data, "bacbox", ["10.0.0.5"], now)
    assert not created_again
    assert second.fingerprint_sha256 == first.fingerprint_sha256

    lone = tmp_path / "lone"
    lone.mkdir()
    tdir = tls_certs.tls_dir(lone)
    tdir.mkdir()
    (tdir / tls_certs.KEY_FILE).write_bytes(b"precious")
    with pytest.raises(CertProblem):
        tls_certs.ensure_self_signed(lone, "bacbox", [], now)
    assert (tdir / tls_certs.KEY_FILE).read_bytes() == b"precious"
    assert not (tdir / tls_certs.CERT_FILE).exists()


def test_expired_installed_certificate_still_loads(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    cert_pem, key_pem = _foreign_cert(now, days_before=400, days_after=-10)
    tls_certs.write_pair(data, cert_pem, key_pem)
    info = tls_certs.load_installed(data)
    assert info is not None
    assert info.not_after < now
    assert not info.generated_by_bacpack


def _rsa_pair(now, bits):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "weak")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=100))
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


def test_check_pair_refuses_a_pair_the_tls_library_will_not_load(now):
    cert_pem, key_pem = _rsa_pair(now, 1024)
    with pytest.raises(CertProblem) as raised:
        tls_certs.check_pair(cert_pem, key_pem, now)
    assert "TLS" in str(raised.value) and "PRIVATE KEY" not in str(raised.value)


def test_check_pair_accepts_a_2048_bit_rsa_pair(now):
    cert_pem, key_pem = _rsa_pair(now, 2048)
    assert tls_certs.check_pair(cert_pem, key_pem, now)


def test_load_installed_refuses_an_installed_pair_the_tls_library_will_not_load(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    tls_certs.write_pair(data, *_rsa_pair(now, 1024))
    with pytest.raises(CertProblem) as raised:
        tls_certs.load_installed(data)
    assert "TLS" in str(raised.value)


def test_generated_certificate_is_a_server_certificate(now):
    cert = x509.load_pem_x509_certificate(_make(now)[0])
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.digital_signature
    assert not usage.value.key_cert_sign and not usage.value.content_commitment
    extended = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(extended.value) == [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]


def test_check_pair_allows_five_minutes_of_clock_skew_on_not_before(now):
    near_cert, near_key = _foreign_cert(now, days_before=0, days_after=100)
    start = x509.load_pem_x509_certificate(near_cert).not_valid_before_utc
    assert tls_certs.check_pair(near_cert, near_key, start - dt.timedelta(minutes=4))
    with pytest.raises(CertProblem, match="not valid until"):
        tls_certs.check_pair(near_cert, near_key, start - dt.timedelta(minutes=6))


def test_the_certificate_download_holds_only_certificates(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    cert_pem, key_pem = _make(now)
    tls_certs.write_pair(data, cert_pem + key_pem, key_pem)
    served = tls_certs.read_certificate_pem(data)
    assert b"PRIVATE KEY" not in served
    assert x509.load_pem_x509_certificate(served).public_bytes(serialization.Encoding.PEM) == cert_pem


def test_write_pair_restores_the_old_pair_when_the_second_replace_fails(tmp_path, now, monkeypatch):
    data = _data_dir(tmp_path)
    data.mkdir()
    old = _make(now)
    tls_certs.write_pair(data, *old)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(tls_certs.os, "replace", flaky)
    with pytest.raises(OSError):
        tls_certs.write_pair(data, *_make(now, hostname="new"))
    monkeypatch.undo()
    tdir = tls_certs.tls_dir(data)
    assert (tdir / tls_certs.CERT_FILE).read_bytes() == old[0]
    assert (tdir / tls_certs.KEY_FILE).read_bytes() == old[1]
    assert sorted(p.name for p in tdir.iterdir()) == [tls_certs.CERT_FILE, tls_certs.KEY_FILE]


def test_write_pair_leaves_no_backups_on_success(tmp_path, now):
    data = _data_dir(tmp_path)
    data.mkdir()
    tls_certs.write_pair(data, *_make(now))
    tls_certs.write_pair(data, *_make(now, hostname="again"))
    assert sorted(p.name for p in tls_certs.tls_dir(data).iterdir()) == [tls_certs.CERT_FILE, tls_certs.KEY_FILE]


def test_load_installed_turns_an_unreadable_folder_into_a_cert_problem(tmp_path, monkeypatch):
    import pathlib

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pathlib.Path, "exists", denied)
    with pytest.raises(CertProblem, match="Cannot read"):
        tls_certs.load_installed(_data_dir(tmp_path))


def test_check_pair_keeps_the_key_copy_inside_the_tls_folder_and_removes_it(tmp_path, now, monkeypatch):
    data = _data_dir(tmp_path)
    data.mkdir()
    made = []
    real = tls_certs.tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        made.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    monkeypatch.setattr(tls_certs.tempfile, "TemporaryDirectory", spy)
    assert tls_certs.check_pair(*_make(now), now, data_dir=data)
    assert [Path(d) for d in made] == [tls_certs.tls_dir(data)]
    assert list(tls_certs.tls_dir(data).iterdir()) == []


def test_write_pair_clears_leftover_temp_files(tmp_path, now):
    data = _data_dir(tmp_path)
    tdir = tls_certs.tls_dir(data)
    tdir.mkdir(parents=True)
    (tdir / "key.pem.abc123.tmp").write_bytes(b"old key copy")
    (tdir / "cert.pem.zzz.tmp").write_bytes(b"old")
    leftover_dir = tdir / ".check-xyz"
    leftover_dir.mkdir()
    (leftover_dir / "key.pem").write_bytes(b"old key copy")
    tls_certs.write_pair(data, *_make(now))
    assert sorted(p.name for p in tdir.iterdir()) == [tls_certs.CERT_FILE, tls_certs.KEY_FILE]
