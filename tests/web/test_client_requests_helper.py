"""The shared wait for a ClientRequest answer (web/api/_client_requests.py)."""
from __future__ import annotations

import threading
import time

import pytest

from bacpack.models import ClientRequest, Device
from bacpack.web.api._client_requests import ClientRequestTimedOut, run_client_request
from tests.loop_gap import assert_waited_off_the_loop, run_while_locked
from tests.web._helpers import _create_device


def _mark_running(web_client, device_id=599001):
    with web_client.app.state.session_factory() as session:
        session.get(Device, device_id).runtime.process_status = "running"
        session.commit()


def _answer_once(session_factory, status, result, error):
    def run():
        for _ in range(200):
            with session_factory() as session:
                rows = session.query(ClientRequest).filter_by(status="pending").all()
                for row in rows:
                    row.status, row.result, row.error = status, result, error
                session.commit()
                if rows:
                    return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_an_answer_is_returned_and_the_row_deleted_when_asked(web_client):
    _create_device(web_client)
    _mark_running(web_client)
    session_factory = web_client.app.state.session_factory
    thread = _answer_once(session_factory, "done", {"x": 1}, None)

    answer = await run_client_request(
        session_factory, 599001, "live", {}, wait_seconds=2, not_running="no", delete_when_answered=True
    )
    thread.join(timeout=2)

    assert (answer.ok, answer.result, answer.error) == (True, {"x": 1}, None)
    with session_factory() as session:
        assert session.query(ClientRequest).count() == 0


@pytest.mark.asyncio
async def test_an_error_answer_is_returned_not_raised(web_client):
    _create_device(web_client)
    _mark_running(web_client)
    session_factory = web_client.app.state.session_factory
    thread = _answer_once(session_factory, "error", None, "boom")

    answer = await run_client_request(session_factory, 599001, "live", {}, wait_seconds=2, not_running="no")
    thread.join(timeout=2)

    assert (answer.ok, answer.error) == (False, "boom")
    with session_factory() as session:
        assert session.query(ClientRequest).count() == 1


@pytest.mark.asyncio
async def test_no_answer_times_out_and_marks_the_row(web_client):
    _create_device(web_client)
    _mark_running(web_client)
    session_factory = web_client.app.state.session_factory

    with pytest.raises(ClientRequestTimedOut):
        await run_client_request(session_factory, 599001, "live", {}, wait_seconds=0, not_running="no")

    with session_factory() as session:
        (row,) = session.query(ClientRequest).all()
        assert (row.status, row.error) == ("error", "timed out")


@pytest.mark.asyncio
async def test_a_held_write_lock_doesnt_stall_the_event_loop(web_client):
    # The insert, the polls and the delete wait for the lock in a worker
    # thread.
    _create_device(web_client)
    _mark_running(web_client)
    session_factory = web_client.app.state.session_factory
    thread = _answer_once(session_factory, "done", {"x": 1}, None)

    run = await run_while_locked(
        session_factory,
        lambda: run_client_request(
            session_factory, 599001, "live", {}, wait_seconds=5, not_running="no", delete_when_answered=True
        ),
        history=True,
    )
    thread.join(timeout=5)

    assert run.result.result == {"x": 1}
    assert_waited_off_the_loop(run)
