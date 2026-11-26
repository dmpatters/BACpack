"""A request body over the cap is refused with 413 before any route, or the
login check, reads it: /api/auth/login is open to anyone who reaches the web
port, and a JSON body is read whole into memory."""
from __future__ import annotations

import re

from bacpack.web import body_limit
from bacpack.web.api._uploads import MAX_UPLOAD_BYTES

from tests.web._helpers import _create_device, enable_login

JSON = {"content-type": "application/json"}


def _chunked(payload: bytes, size: int = 65536):
    # A generator body goes out chunked, with no Content-Length.
    def chunks():
        for start in range(0, len(payload), size):
            yield payload[start : start + size]

    return chunks()


def _device_body(name_length: int) -> bytes:
    return b'{"id":599001,"name":"' + b"n" * name_length + b'"}'


def test_the_caps():
    assert body_limit.MAX_BODY_BYTES == 8 * 1024 * 1024
    # An EDE import takes two files, each up to the upload cap.
    assert body_limit.MAX_UPLOAD_BODY_BYTES == 2 * MAX_UPLOAD_BYTES + 1024 * 1024


def test_a_login_body_over_the_cap_is_413_without_a_login(web_client):
    enable_login(web_client)
    body = b'{"username":"admin","password":"' + b"p" * body_limit.MAX_BODY_BYTES + b'"}'
    r = web_client.post("/api/auth/login", content=body, headers=JSON)
    assert r.status_code == 413
    assert r.json() == {"detail": "request body larger than 8 MiB"}


def test_a_body_over_the_cap_is_413_and_saves_nothing(web_client, monkeypatch):
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", 1000)
    r = web_client.post("/api/devices", content=_device_body(1000), headers=JSON)
    assert r.status_code == 413
    assert web_client.get("/api/devices").json() == []


def test_a_chunked_body_over_the_cap_is_413_and_saves_nothing(web_client, monkeypatch):
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", 1000)
    r = web_client.post("/api/devices", content=_chunked(_device_body(5000), 300), headers=JSON)
    assert r.status_code == 413
    assert web_client.get("/api/devices").json() == []


def test_a_chunked_body_over_the_cap_on_a_route_without_the_json_guard(web_client, monkeypatch):
    # Not JSON, so only the route reads it.
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", 1000)
    headers = {"content-type": "text/plain"}
    r = web_client.post("/api/devices", content=_chunked(_device_body(5000), 300), headers=headers)
    assert r.status_code == 413


def test_a_body_at_the_cap_reaches_the_route(web_client, monkeypatch):
    body = _device_body(100)
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", len(body))
    assert web_client.post("/api/devices", content=body, headers=JSON).status_code == 201
    body = b'{"id":599002,"name":"m"}'
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", len(body))
    assert web_client.post("/api/devices", content=_chunked(body, 5), headers=JSON).status_code == 201


def test_an_upload_route_takes_the_upload_cap(web_client, monkeypatch):
    _create_device(web_client)
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", 1000)
    monkeypatch.setattr(body_limit, "MAX_UPLOAD_BODY_BYTES", 100_000)
    csv_text = "object_type,instance_number,name\n" + "".join(
        f"analog-value,{i},AV-{i}\n" for i in range(1, 200)
    )
    files = {"file": ("objects.csv", csv_text, "text/csv")}
    r = web_client.post("/api/devices/599001/objects/import", files=files)
    assert r.status_code != 413, r.text
    files = {"file": ("objects.csv", csv_text * 40, "text/csv")}
    assert web_client.post("/api/devices/599001/objects/import", files=files).status_code == 413


def test_a_json_route_does_not_take_the_upload_cap(web_client, monkeypatch):
    # A JSON route reads its body whole whatever the content type says.
    monkeypatch.setattr(body_limit, "MAX_BODY_BYTES", 1000)
    monkeypatch.setattr(body_limit, "MAX_UPLOAD_BODY_BYTES", 100_000)
    headers = {"content-type": "multipart/form-data; boundary=x"}
    assert web_client.post("/api/devices", content=_device_body(5000), headers=headers).status_code == 413


def test_the_upload_paths_are_the_routes_that_take_a_form(web_client):
    takes_form = set()
    for path, operations in web_client.app.openapi()["paths"].items():
        sample = re.sub(r"\{[^}]+\}", "1", path)
        content = set()
        for operation in operations.values():
            content |= set(operation.get("requestBody", {}).get("content", {}))
        if content & {"multipart/form-data", "application/x-www-form-urlencoded"}:
            takes_form.add(sample)
        else:
            assert not body_limit.is_upload_path(sample), path
    assert takes_form, "no upload routes found"
    for sample in takes_form:
        assert body_limit.is_upload_path(sample), sample
