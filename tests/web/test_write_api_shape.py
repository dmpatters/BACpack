"""The write API's request and response shapes, pinned.

From 1.0.0 on `/api/feed`, `/api/manual` and API-key authentication change
additively only: a test here failing means a client script breaks. Adding a
field updates the assertion; removing or renaming one does not pass review.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from bacpack.models import Device
from tests.web._helpers import (
    _create_device,
    _create_object,
    _create_schedule_object,
    enable_login,
    make_key,
)


def _set_running(client, device_id=599001, status="running"):
    with client.app.state.session_factory() as session:
        session.scalars(select(Device).where(Device.id == device_id)).one().runtime.process_status = status
        session.commit()


def _compact(response) -> str:
    """The body's JSON text without whitespace, so the type of each value is visible."""
    return "".join(response.text.split())


@pytest.fixture()
def live(web_client):
    _create_device(web_client)
    _create_object(web_client)
    _set_running(web_client)
    return web_client


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_request_and_response_shape(live, route):
    r = live.post(f"/api/{route}/599001/analog-value/1", json={"value": 21.5})
    assert r.status_code == 200
    # Exact, as raw text: adding a response field means updating this assertion on purpose,
    # and a value echoed as 1 vs true vs 1.0 differs here though Python compares them equal.
    assert _compact(r) == '{"queued":true,"value":21.5}'
    assert live.post(f"/api/{route}/599001/analog-value/1", json={}).status_code == 422
    assert _compact(live.post(f"/api/{route}/599001/analog-value/1", json={"value": None})) == (
        '{"queued":true,"value":null}'
    )


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_object_type_in_the_path_is_case_insensitive(live, route):
    assert live.post(f"/api/{route}/599001/Analog-Value/1", json={"value": 1}).status_code == 200


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_status_codes(live, route):
    url = f"/api/{route}/599001/analog-value/1"
    assert live.post(f"/api/{route}/599001/analog-value/99", json={"value": 1}).status_code == 404
    assert live.post(f"/api/{route}/599999/analog-value/1", json={"value": 1}).status_code == 409
    assert live.post(url, json={"value": "warm"}).status_code == 400
    assert _create_schedule_object(live).status_code == 201
    assert live.post(f"/api/{route}/599001/schedule/1", json={"value": 1}).status_code == 400
    _set_running(live, status="stopped")
    assert live.post(url, json={"value": 1}).status_code == 409


def test_an_input_takes_a_feed_but_not_a_manual_write(live):
    _create_object(live, object_type="analog-input", instance_number=2, name="AI-2")
    assert live.post("/api/feed/599001/analog-input/2", json={"value": 3}).status_code == 200
    assert live.post("/api/manual/599001/analog-input/2", json={"value": 3}).status_code == 400


def test_binary_and_multistate_values(live):
    _create_object(live, object_type="binary-value", instance_number=1, name="BV-1")
    _create_object(live, object_type="multi-state-value", instance_number=1, name="MSV-1", state_text=["a", "b"])
    for value, echoed in (("active", '"active"'), ("inactive", '"inactive"'), (True, "true"), (0, "0"), (1, "1")):
        r = live.post("/api/feed/599001/binary-value/1", json={"value": value})
        assert r.status_code == 200
        assert _compact(r) == '{"queued":true,"value":%s}' % echoed
    assert live.post("/api/feed/599001/binary-value/1", json={"value": 2}).status_code == 400
    assert _compact(live.post("/api/feed/599001/multi-state-value/1", json={"value": 2})) == (
        '{"queued":true,"value":2}'
    )
    assert live.post("/api/feed/599001/multi-state-value/1", json={"value": 3}).status_code == 400


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_api_key_authenticates_the_write_routes(live, route):
    url = f"/api/{route}/599001/analog-value/1"
    enable_login(live)
    key = make_key(live)
    assert live.post(url, json={"value": 1}).status_code == 401
    assert live.post(url, json={"value": 1}, headers={"Authorization": "Bearer bpk_wrong"}).status_code == 401
    r = live.post(url, json={"value": 1}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert _compact(r) == '{"queued":true,"value":1}'


@pytest.mark.parametrize("route", ["feed", "manual"])
def test_error_bodies_carry_a_string_detail(live, route):
    bad_value = live.post(f"/api/{route}/599001/analog-value/1", json={"value": "warm"})
    missing_object = live.post(f"/api/{route}/599001/analog-value/99", json={"value": 1})
    not_live = live.post(f"/api/{route}/599999/analog-value/1", json={"value": 1})
    for response, status in ((bad_value, 400), (missing_object, 404), (not_live, 409)):
        assert response.status_code == status
        assert isinstance(response.json()["detail"], str) and response.json()["detail"]
