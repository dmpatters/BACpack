"""A JSON body with an unpaired surrogate is refused up front (422): it would
fail later at .encode("utf-8") or the SQLite bind, and the 422 echo of it too."""
from __future__ import annotations

import json

from tests.web._helpers import _create_device

JSON = {"content-type": "application/json"}


def _post(client, path, body: bytes):
    return client.post(path, content=body, headers=JSON)


def test_a_surrogate_in_a_text_field_is_422_and_stores_nothing(web_client):
    r = _post(web_client, "/api/devices", b'{"id":599001,"name":"\\ud800"}')
    assert r.status_code == 422
    assert web_client.get("/api/devices").json() == []


def test_a_surrogate_in_a_non_text_field_is_422(web_client):
    assert _post(web_client, "/api/devices", b'{"id":"\\udfff","name":"x"}').status_code == 422


def test_a_surrogate_in_a_key_or_nested_value_is_422(web_client):
    _create_device(web_client)
    assert _post(web_client, "/api/devices", b'{"id":5,"name":"x","\\ud800":1}').status_code == 422
    body = b'{"object_type":"analog-value","instance_number":1,"name":"a","description":["\\ud800"]}'
    assert _post(web_client, "/api/devices/599001/objects", body).status_code == 422


def test_a_valid_escaped_pair_is_accepted(web_client):
    r = _post(web_client, "/api/devices", b'{"id":599001,"name":"smile \\ud83d\\ude00"}')
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "smile \U0001f600"


def test_invalid_json_and_other_content_types_are_left_to_the_route(web_client):
    r = _post(web_client, "/api/devices", b'{"name":"\\ud800"')
    assert r.status_code == 422
    assert "unpaired" not in r.text
    r = web_client.post("/api/devices", content=b'{"name":"\\ud800"}', headers={"content-type": "text/plain"})
    assert "unpaired" not in r.text


def test_a_chunked_body_is_read_whole_and_replayed(web_client):
    payload = json.dumps({"id": 599001, "name": "n" * 200_000}).encode()

    def chunks():
        for start in range(0, len(payload), 16384):
            yield payload[start : start + 16384]

    assert web_client.post("/api/devices", content=chunks(), headers=JSON).status_code == 201

    def chunks_with_surrogate():
        yield b'{"id":599002,"name":"' + b"n" * 50_000
        yield b'\\ud800"}'

    assert web_client.post("/api/devices", content=chunks_with_surrogate(), headers=JSON).status_code == 422


def test_a_422_that_echoes_a_surrogate_is_still_sent(web_client):
    # The login route is outside the guard; its missing password gives a 422
    # whose echoed input holds the lone surrogate.
    r = _post(web_client, "/api/auth/login", b'{"username":"\ud800"}')
    assert r.status_code == 422
    assert "ud800" in r.text


def test_a_422_that_echoes_a_surrogate_key_is_still_sent(web_client):
    r = _post(web_client, "/api/auth/login", b'{"\ud800":1,"username":"a"}')
    assert r.status_code == 422
    assert "ud800" in r.text
