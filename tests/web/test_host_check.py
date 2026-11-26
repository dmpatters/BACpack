"""The host check: with no login (or none chosen yet) a request whose Host is
not one of the server's own names is refused, so DNS rebinding can't reach it."""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import os
import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bacpack import login_store, timeutil, tls_certs
from bacpack.web import host_names
from bacpack.web.host_names import OwnNames
from tests.web._helpers import LOGIN_PASSWORD, enable_login, session_cookie, set_login_mode

ATTACKER = "attacker.example:8000"


@pytest.fixture(autouse=True)
def _machine_names(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Bacbox")
    monkeypatch.setattr(socket, "getfqdn", lambda: "bacbox.corp.example")


def _get(client, host, path="/api/devices"):
    return client.get(path, headers={"Host": host})


def test_unknown_host_is_refused_with_421_on_get_post_and_the_page(web_client):
    r = _get(web_client, ATTACKER)
    assert r.status_code == 421
    assert "its own names" in r.json()["detail"]
    r = web_client.post("/api/devices", json={"id": 1, "name": "A"}, headers={"Host": ATTACKER})
    assert r.status_code == 421
    page = _get(web_client, ATTACKER, "/")
    assert page.status_code == 421
    assert "its own names" in page.text
    assert _get(web_client, ATTACKER, "/openapi.json").status_code == 421
    assert _get(web_client, ATTACKER, "/assets/x.js").status_code == 421


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8000",
        "[::1]:8000",
        "[fe80::1%25eth0]:8000",
        "192.168.1.20",
        "localhost",
        "LocalHost:8000",
        "localhost.:8000",
        "foo.localhost",
        "bacbox",
        "BACBOX:8000",
        "bacbox.corp.example",
        "bacbox.corp.example.",
    ],
)
def test_own_names_are_answered(web_client, host):
    assert _get(web_client, host).status_code == 200


@pytest.mark.parametrize("host", ["bacbox.local", "BACBOX.local:8000"])
def test_the_short_name_with_dot_local_is_answered(web_client, host):
    assert _get(web_client, host).status_code == 200


def test_the_fqdn_short_form_is_allowed_when_gethostname_differs(web_client, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "other")
    monkeypatch.setattr(socket, "getfqdn", lambda: "bms.corp.example")
    web_client.app.state.own_names = OwnNames(web_client.app.state.data_dir, host_names.machine_names())
    assert _get(web_client, "bms").status_code == 200
    assert _get(web_client, "bms.local").status_code == 200
    assert _get(web_client, "other.local").status_code == 200


def test_the_machine_names_are_read_in_the_lifespan_not_on_a_request(web_client, monkeypatch):
    def boom():
        raise AssertionError("looked up on a request")

    monkeypatch.setattr(socket, "getfqdn", boom)
    monkeypatch.setattr(socket, "gethostname", boom)
    assert _get(web_client, "bacbox").status_code == 200
    assert _get(web_client, "unknown.example").status_code == 421


@pytest.mark.parametrize(
    "host, expected",
    [
        ("bacbox.corp.example", 200),
        ("anything.corp.example:8000", 200),
        ("a.b.corp.example", 421),
        ("corp.example", 421),
        (".corp.example", 421),
        ("corp.example.attacker.example", 421),
    ],
)
def test_a_wildcard_san_matches_exactly_one_label(web_client, host, expected, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "unrelated")
    monkeypatch.setattr(socket, "getfqdn", lambda: "unrelated")
    web_client.app.state.own_names = OwnNames(web_client.app.state.data_dir, host_names.machine_names())
    _write_cert(web_client, "*.corp.example")
    assert _get(web_client, host).status_code == expected


def test_certificate_look_alikes_and_short_forms_are_refused(web_client):
    _write_cert(web_client, "bms.example.org")
    assert _get(web_client, "bms.example.org.attacker.example").status_code == 421
    assert _get(web_client, "bms").status_code == 421
    assert _get(web_client, "bms.example.org").status_code == 200


@pytest.mark.parametrize(
    "host",
    ["[::1]evil.example", "[::1].evil.example", "[::1]:80x", "localhost..", "localhost:abc", ".localhost", "a..localhost"],
)
def test_malformed_hosts_are_refused(web_client, host):
    assert _get(web_client, host).status_code == 421


def test_the_refusal_text_per_mode(web_client):
    assert "runs without a login" in _get(web_client, ATTACKER).json()["detail"]
    set_login_mode(web_client, None)
    text = _get(web_client, ATTACKER).json()["detail"]
    assert "No login has been set up yet" in text
    assert "runs without a login" not in text
    assert "No login has been set up yet" in _get(web_client, ATTACKER, "/").text


def test_setup_from_an_unknown_host_is_refused_and_the_mode_stays_unset(web_client):
    set_login_mode(web_client, None)
    r = web_client.post(
        "/api/auth/setup", json={"mode": "none"}, headers={"Host": ATTACKER, "Origin": "http://attacker.example:8000"}
    )
    assert r.status_code == 421
    with web_client.app.state.session_factory() as session:
        assert login_store.get_login_state(session).mode is None


def test_turning_the_login_off_from_a_name_that_would_be_refused_is_409(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    web_client.headers["Origin"] = "http://attacker.example:8000"
    r = web_client.post(
        "/api/settings/login/disable", json={"current_password": LOGIN_PASSWORD}, headers={"Host": ATTACKER}
    )
    assert r.status_code == 409
    assert "IP address" in r.json()["detail"]
    assert web_client.get("/api/settings/login").json()["mode"] == "login"
    web_client.headers["Origin"] = "http://127.0.0.1"
    r = web_client.post(
        "/api/settings/login/disable", json={"current_password": LOGIN_PASSWORD}, headers={"Host": "127.0.0.1"}
    )
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["localhost.attacker.example", "bacbox.attacker.example", "", "[::1"])
def test_look_alikes_and_an_empty_host_are_refused(web_client, host):
    assert _get(web_client, host).status_code == 421


def test_a_missing_host_header_is_refused(web_client):
    # The test client always adds a Host, so call the middleware's own check
    # and then a raw ASGI request without one.
    assert not web_client.app.state.own_names.allows(None)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "path": "/api/devices", "raw_path": b"/api/devices", "query_string": b"", "root_path": "",
        "scheme": "http", "headers": [], "client": ("10.1.1.1", 5000), "server": ("127.0.0.1", 8000),
        "app": web_client.app, "state": {},
    }
    asyncio.run(web_client.app.middleware_stack(scope, receive, send))
    assert sent[0]["status"] == 421


def test_unset_mode_refuses_an_unknown_host_too(web_client):
    set_login_mode(web_client, None)
    assert _get(web_client, ATTACKER, "/api/auth/state").status_code == 421
    assert _get(web_client, ATTACKER, "/api/version").status_code == 421
    assert _get(web_client, "127.0.0.1:8000", "/api/version").status_code == 200


def test_login_mode_does_not_refuse_an_unknown_host(web_client):
    enable_login(web_client)
    session_cookie(web_client)
    assert _get(web_client, ATTACKER).status_code == 200
    assert _get(web_client, ATTACKER, "/api/version").status_code == 200


def _write_cert(web_client, hostname):
    data_dir = web_client.app.state.data_dir
    # Built here: generate_self_signed refuses a wildcard name.
    now = timeutil.utc_now()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname), x509.IPAddress(ipaddress.ip_address("10.0.0.5"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tls_certs.write_pair(data_dir, cert_pem, key_pem)
    path = tls_certs.tls_dir(data_dir) / tls_certs.CERT_FILE
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_a_certificate_name_is_answered_and_a_replaced_certificate_counts_at_once(web_client):
    assert _get(web_client, "bms.example.org").status_code == 421
    _write_cert(web_client, "bms.example.org")
    assert _get(web_client, "bms.example.org:8000").status_code == 200
    assert _get(web_client, "other.example.org").status_code == 421
    _write_cert(web_client, "other.example.org")
    assert _get(web_client, "other.example.org").status_code == 200
    assert _get(web_client, "bms.example.org").status_code == 421


def test_a_broken_certificate_file_adds_no_names(web_client):
    data_dir = web_client.app.state.data_dir
    directory = tls_certs.tls_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / tls_certs.CERT_FILE).write_bytes(b"not a certificate")
    assert _get(web_client, "bms.example.org").status_code == 421


def test_a_refusal_is_logged_once_a_minute_per_name(web_client, caplog, monkeypatch):
    clock = [timeutil.db_utc_now()]
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="bacpack.security"):
        for _ in range(3):
            _get(web_client, ATTACKER)
        _get(web_client, "second.example")
        lines = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
        assert len(lines) == 2
        assert '"attacker.example"' in lines[0] and "testclient" in lines[0]
        assert '"second.example"' in lines[1]
        clock[0] += login_store.LAST_WRITE_MIN_INTERVAL
        _get(web_client, ATTACKER)
        lines = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
        assert len(lines) == 3
