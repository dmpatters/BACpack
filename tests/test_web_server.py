import logging

import pytest

from bacpack import timeutil, tls_certs, web_server
from bacpack.models import ServerSettings, get_or_create_settings
from bacpack.tls_certs import CertProblem

LOCAL_ADDRESSES = ["10.0.0.5", "127.0.0.1"]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.INFO, logger="bacpack.security")


@pytest.fixture
def data(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "10.0.0.5"])
def test_check_listen_address_accepts(address):
    assert web_server.check_listen_address(address, LOCAL_ADDRESSES) is None


@pytest.mark.parametrize(
    "address", ["10.0.0.6", "::", "::1", "localhost", "", "0.0.0.0/0", "10.0.0.5:8000", " 10.0.0.5"]
)
def test_check_listen_address_refuses(address):
    assert web_server.check_listen_address(address, LOCAL_ADDRESSES)


def test_local_ipv4_addresses_include_loopback_and_no_ipv6():
    addresses = web_server.local_ipv4_addresses()
    assert "127.0.0.1" in addresses
    assert all(":" not in a for a in addresses)


def test_set_listen_address_changes_logs_and_reports_change(session, caplog):
    assert web_server.set_listen_address(session, "127.0.0.1", "10.1.1.1") is True
    assert get_or_create_settings(session).web_listen_address == "127.0.0.1"
    records = [r for r in caplog.records if r.name == "bacpack.security"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "127.0.0.1" in records[0].getMessage() and "10.1.1.1" in records[0].getMessage()
    caplog.clear()
    assert web_server.set_listen_address(session, "127.0.0.1", "10.1.1.1") is False
    assert not caplog.records


def test_set_listen_address_logs_only_after_the_commit_succeeds(session, caplog, monkeypatch):
    def failing_commit():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        web_server.set_listen_address(session, "127.0.0.1", "10.1.1.1")
    assert not caplog.records


def test_set_listen_address_refuses_and_changes_nothing(session, caplog):
    with pytest.raises(ValueError, match="IPv4"):
        web_server.set_listen_address(session, "::", "10.1.1.1")
    assert get_or_create_settings(session).web_listen_address == "0.0.0.0"
    assert not caplog.records


def test_fresh_settings_default_to_all_addresses(session):
    session.query(ServerSettings).delete()
    session.commit()
    assert get_or_create_settings(session).web_listen_address == "0.0.0.0"


def test_certificate_status_reports_problem_instead_of_raising(data):
    tdir = tls_certs.tls_dir(data)
    tdir.mkdir()
    (tdir / tls_certs.KEY_FILE).write_bytes(b"x")
    status = web_server.certificate_status(data)
    assert status.info is None
    assert "cert.pem" in status.problem


def test_certificate_status_is_empty_before_first_start(data):
    status = web_server.certificate_status(data)
    assert status.info is None and status.problem is None


def test_make_self_signed_replaces_an_own_certificate_and_logs_the_fingerprint_but_no_key_text(data, caplog):
    now = timeutil.utc_now()
    own_cert, own_key = tls_certs.generate_self_signed("other", [], now)
    tls_certs.write_pair(data, own_cert, own_key)
    info = web_server.make_self_signed(data, "this machine")
    installed = tls_certs.load_installed(data)
    assert installed.fingerprint_sha256 == info.fingerprint_sha256
    assert info.fingerprint_sha256 != tls_certs.describe_certificate(own_cert).fingerprint_sha256
    text = caplog.text
    assert info.fingerprint_sha256 in text
    assert "PRIVATE KEY" not in text
    assert own_key.decode().splitlines()[1] not in text


def test_install_own_certificate_refuses_a_bad_pair_and_keeps_the_old_one(data):
    web_server.make_self_signed(data, web_server.LOCAL)
    before = (tls_certs.tls_dir(data) / tls_certs.CERT_FILE).read_bytes()
    with pytest.raises(CertProblem):
        web_server.install_own_certificate(data, b"junk", b"junk", web_server.LOCAL)
    assert (tls_certs.tls_dir(data) / tls_certs.CERT_FILE).read_bytes() == before


def test_install_own_certificate_installs_a_good_pair_and_logs(data, caplog):
    cert_pem, key_pem = tls_certs.generate_self_signed("mine", [], timeutil.utc_now())
    info = web_server.install_own_certificate(data, cert_pem, key_pem, "10.1.1.1")
    assert tls_certs.load_installed(data).fingerprint_sha256 == info.fingerprint_sha256
    assert "own certificate installed" in caplog.text


def test_ensure_certificate_logs_creation_once(data, caplog):
    _, created = web_server.ensure_certificate(data)
    assert created
    _, created = web_server.ensure_certificate(data)
    assert not created
    messages = [r.getMessage() for r in caplog.records if "generated at first start" in r.getMessage()]
    assert len(messages) == 1
