"""One real `python -m bacpack serve` process answering HTTPS."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time

from tests.web._helpers import LOGIN_PASSWORD


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _https(port, method, path, body=None, headers=None):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    try:
        payload = json.dumps(body) if body is not None else None
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        peer = connection.sock.getpeercert(binary_form=True)
        return response, data, peer
    finally:
        connection.close()


def _wait_until_up(port, process, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, "the server exited early"
        try:
            return _https(port, "GET", "/api/version")
        except OSError:
            time.sleep(0.25)
    raise AssertionError("the server did not answer in time")


def test_serve_answers_https_with_the_generated_certificate(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "BACPACK_SERVICE"}
    env["BACPACK_DB_PATH"] = str(tmp_path / "bacpack.db")
    env.pop("BACPACK_PORT", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "bacpack", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        response, data, peer = _wait_until_up(port, process)
        assert response.status == 200
        assert json.loads(data)["listen_address"] == "0.0.0.0"

        origin = {"Origin": f"https://127.0.0.1:{port}"}
        setup, _, _ = _https(
            port,
            "POST",
            "/api/auth/setup",
            {"mode": "login", "username": "owner", "password": LOGIN_PASSWORD},
            origin,
        )
        assert setup.status == 200
        cookie_header = setup.getheader("set-cookie")
        assert "secure" in cookie_header.lower()

        session = cookie_header.split(";")[0]
        settings_response, settings_data, _ = _https(port, "GET", "/api/settings/web", headers={"Cookie": session})
        assert settings_response.status == 200
        shown = json.loads(settings_data)["certificate"]["fingerprint_sha256"]
        digest = hashlib.sha256(peer).hexdigest().upper()
        assert shown == ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))

        plain = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            plain.request("GET", "/api/version")
            reply = plain.getresponse()
            assert reply.status >= 400 or not reply.read().startswith(b"{")
        except (OSError, http.client.HTTPException):
            pass
        finally:
            plain.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
