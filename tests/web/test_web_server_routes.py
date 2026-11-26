"""The web server settings API (web/api/web_server.py)."""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization

from bacpack import history_writer, lifecycle, timeutil, tls_certs, web_server
from bacpack.models import LogEntry, get_or_create_settings
from tests.test_tls_certs import _foreign_cert
from tests.web._helpers import enable_login, set_login_mode

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture
def client(web_client, tmp_path, monkeypatch):
    data = tmp_path / "tls-data"
    data.mkdir()
    web_client.app.state.data_dir = data
    set_login_mode(web_client, "none")
    monkeypatch.setattr(web_server, "local_ipv4_addresses", lambda: ["10.0.0.5", "127.0.0.1"])
    for name in (lifecycle.RUNNING_ADDRESS_ENV, lifecycle.RUNNING_PORT_ENV, lifecycle.RUNNING_FINGERPRINT_ENV):
        monkeypatch.delenv(name, raising=False)
    return web_client


def _files(client):
    directory = tls_certs.tls_dir(client.app.state.data_dir)
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def _post_pair(client, cert_pem, key_pem):
    return client.post(
        "/api/settings/web/certificate",
        json={"certificate_pem": cert_pem.decode(), "key_pem": key_pem.decode()},
        headers=ORIGIN,
    )


def _install_own(client):
    """A certificate without BACpack's marker, so its source reads as own."""
    cert_pem, key_pem = _foreign_cert(timeutil.utc_now(), days_before=1, days_after=400)
    r = _post_pair(client, cert_pem, key_pem)
    assert r.status_code == 200, r.text
    return r.json(), cert_pem, key_pem


def test_get_reports_listen_address_and_no_certificate_before_first_start(client):
    assert client.get("/api/settings/web").json() == {
        "listen_address": "0.0.0.0",
        "running": None,
        "certificate": None,
        "certificate_problem": None,
        "restart_pending": False,
    }


def test_get_reports_self_signed_source_fingerprint_sans_and_expiry(client):
    made = client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN).json()
    body = client.get("/api/settings/web").json()
    cert = body["certificate"]
    assert body == made
    assert cert["source"] == "self-signed" and cert["expired"] is False
    assert {"localhost", "127.0.0.1", "10.0.0.5"} <= set(cert["sans"])
    assert cert["fingerprint_sha256"].count(":") == 31
    assert cert["not_after"].endswith("+00:00")


def test_put_listen_address_saves_logs_and_refuses_bad_ones(client):
    r = client.put("/api/settings/web", json={"listen_address": "10.0.0.5"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["listen_address"] == "10.0.0.5"
    for bad in ("::", "192.0.2.9", "abc"):
        r = client.put("/api/settings/web", json={"listen_address": bad}, headers=ORIGIN)
        assert r.status_code == 400 and r.json()["detail"]
    with client.app.state.session_factory() as session:
        assert get_or_create_settings(session).web_listen_address == "10.0.0.5"
    history_writer.flush_current()
    with client.app.state.session_factory() as session:
        messages = [e.message for e in session.query(LogEntry).filter(LogEntry.logger_name == "bacpack.security")]
    assert any("10.0.0.5" in m and "listen address" in m for m in messages)


def test_restart_pending_only_when_running_differs(client, monkeypatch):
    client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN)
    fingerprint = client.get("/api/settings/web").json()["certificate"]["fingerprint_sha256"]
    assert client.get("/api/settings/web").json()["restart_pending"] is False

    monkeypatch.setattr(lifecycle, "running_web", lambda: lifecycle.RunningWeb("0.0.0.0", 8000, fingerprint))
    body = client.get("/api/settings/web").json()
    assert body["restart_pending"] is False
    assert body["running"] == {"address": "0.0.0.0", "port": 8000, "fingerprint": fingerprint}

    client.put("/api/settings/web", json={"listen_address": "127.0.0.1"}, headers=ORIGIN)
    assert client.get("/api/settings/web").json()["restart_pending"] is True
    client.put("/api/settings/web", json={"listen_address": "0.0.0.0"}, headers=ORIGIN)
    assert client.get("/api/settings/web").json()["restart_pending"] is False

    client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN)
    assert client.get("/api/settings/web").json()["restart_pending"] is True


def test_certificate_download_is_the_public_certificate_only(client):
    assert client.get("/api/settings/web/certificate.pem").status_code == 404
    client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN)
    r = client.get("/api/settings/web/certificate.pem")
    assert r.status_code == 200
    assert r.content.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" not in r.content
    assert "attachment" in r.headers["content-disposition"]


def test_a_download_never_carries_a_key_placed_in_the_certificate_file(client):
    cert_pem, key_pem = tls_certs.generate_self_signed("x", [], timeutil.utc_now())
    tls_certs.write_pair(client.app.state.data_dir, cert_pem + key_pem, key_pem)
    r = client.get("/api/settings/web/certificate.pem")
    assert r.status_code == 200 and b"PRIVATE KEY" not in r.content


def test_an_oversized_field_is_refused_before_it_is_parsed(client):
    big = "x" * (tls_certs.MAX_PEM_BYTES + 1)
    r = client.post("/api/settings/web/certificate", json={"certificate_pem": big, "key_pem": "k"}, headers=ORIGIN)
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_a_refused_upload_never_sends_the_key_back(client):
    marker = "SECRET-KEY-BODY"
    big_key = "-----BEGIN PRIVATE KEY-----\n" + marker * 40000
    for body in (
        {"certificate_pem": "c", "key_pem": big_key},
        {"key_pem": "-----BEGIN PRIVATE KEY-----\n" + marker},
        {"certificate_pem": "c", "key_pem": ["-----BEGIN PRIVATE KEY-----" + marker]},
        [{"key_pem": marker}],
    ):
        r = client.post("/api/settings/web/certificate", json=body, headers=ORIGIN)
        assert r.status_code in (400, 422), r.text
        assert marker not in r.text and '"input"' not in r.text


def test_regenerate_gives_a_new_fingerprint_and_logs_it(client):
    first = client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN).json()["certificate"]
    second = client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN).json()["certificate"]
    assert first["fingerprint_sha256"] != second["fingerprint_sha256"]
    history_writer.flush_current()
    with client.app.state.session_factory() as session:
        messages = [e.message for e in session.query(LogEntry)]
    assert any(second["fingerprint_sha256"] in m for m in messages)


def test_upload_own_certificate_then_go_back_to_self_signed(client):
    body, cert_pem, _ = _install_own(client)
    assert body["certificate"]["source"] == "own"
    assert _files(client)["cert.pem"] == cert_pem
    back = client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN).json()
    assert back["certificate"]["source"] == "self-signed"


def _bad_uploads():
    now = timeutil.utc_now()
    cert_pem, key_pem = tls_certs.generate_self_signed("x", [], now)
    _, other_key = tls_certs.generate_self_signed("y", [], now)
    protected = serialization.load_pem_private_key(key_pem, None).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"pw")
    )
    expired_cert, expired_key = _foreign_cert(now, days_before=400, days_after=-10)
    return [
        (key_pem, key_pem),
        (cert_pem, protected),
        (cert_pem, other_key),
        (expired_cert, expired_key),
        (b"garbage", b"garbage"),
        (cert_pem + b"#" * (tls_certs.MAX_PEM_BYTES + 1), key_pem),
    ]


def test_bad_uploads_give_400_and_keep_the_installed_pair(client):
    client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN)
    before = _files(client)
    for cert_pem, key_pem in _bad_uploads():
        r = _post_pair(client, cert_pem, key_pem)
        assert r.status_code == 400, r.text
        assert "PRIVATE KEY" not in r.text
        assert _files(client) == before
    r = client.post("/api/settings/web/certificate", json={"certificate_pem": "x"}, headers=ORIGIN)
    assert r.status_code == 422


def test_no_response_or_log_holds_private_key_text(client):
    texts = [
        client.get("/api/settings/web").text,
        client.post("/api/settings/web/certificate/self-signed", headers=ORIGIN).text,
        client.get("/api/settings/web/certificate.pem").text,
        client.put("/api/settings/web", json={"listen_address": "127.0.0.1"}, headers=ORIGIN).text,
    ]
    body, _, key_pem = _install_own(client)
    texts.append(str(body))
    texts.append(client.get("/api/settings/web").text)
    for cert_pem, key in _bad_uploads():
        texts.append(_post_pair(client, cert_pem, key).text)
    history_writer.flush_current()
    with client.app.state.session_factory() as session:
        texts.extend(e.message for e in session.query(LogEntry))
    secret = key_pem.decode().splitlines()[1]
    assert all("PRIVATE KEY" not in t and secret not in t for t in texts)


def test_web_routes_need_a_login_when_login_is_on(client):
    enable_login(client)
    assert client.get("/api/settings/web").status_code == 401
    assert client.get("/api/settings/web/certificate.pem").status_code == 401
    r = client.post("/api/settings/web/certificate", json={"certificate_pem": "a", "key_pem": "b"}, headers=ORIGIN)
    assert r.status_code == 401
