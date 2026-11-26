"""FastAPI app-level wiring, not any one /api router: the SPA-serving
contract (catch-all route, /api and /docs taking precedence, unmatched
/api/* is a real 404) and startup log-capture installation. The SPA's own
component/interaction tests live under frontend/src (Vitest), not here.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bacpack import history_writer
from bacpack.engine import EngineManager
from bacpack.web.app import create_app


# --- SPA serving -----------------------------------------------------------
# The React SPA itself is tested with Vitest under frontend/src -- this only
# covers the FastAPI-side serving contract: /api, /docs, /openapi.json, and
# /assets are matched before the catch-all, and everything else gets
# index.html so React Router can take over client-side. See "Routing &
# serving" in the plan doc's SPA design section.


def test_spa_index_served_at_root(web_client):
    r = web_client.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text


def test_spa_catch_all_serves_index_for_client_routes(web_client):
    # Arbitrary client-side routes (e.g. a deep link or hard refresh on
    # /devices/599001) aren't real server routes -- the catch-all must still
    # return the SPA shell so React Router can render them, not 404.
    r = web_client.get("/devices/599001")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text


def test_api_and_openapi_take_precedence_over_the_spa_catch_all(web_client):
    assert web_client.get("/api/devices").headers["content-type"].startswith("application/json")
    assert web_client.get("/openapi.json").headers["content-type"].startswith("application/json")


def test_docs_is_the_spa_page_and_loads_nothing_from_a_cdn(web_client):
    r = web_client.get("/docs")
    assert r.status_code == 200
    assert '<div id="root">' in r.text
    assert "cdn.jsdelivr.net" not in r.text and "unpkg.com" not in r.text
    assert web_client.get("/openapi.json").status_code == 200
    assert '<div id="root">' in web_client.get("/redoc").text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unknown_api_path_is_404_for_every_method(web_client, method):
    r = web_client.request(method, "/api/no-such-thing")
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_wrong_method_on_a_real_api_route_is_still_405(web_client):
    r = web_client.request("PATCH", "/api/devices")
    assert r.status_code == 405
    assert "allow" in r.headers


def test_app_startup_creates_the_database_on_a_fresh_path(tmp_path):
    # web_client starts from a ready copy with init_db stubbed out; this is
    # the one test of create_app building both files itself.
    import sqlite3

    from sqlalchemy import text
    from starlette.testclient import TestClient

    from bacpack.db import history_db_path
    from bacpack.web.app import create_app

    db_path = tmp_path / "fresh.db"
    with TestClient(create_app(db_path=str(db_path)), base_url="http://127.0.0.1") as client:
        # A fresh install waits for the setup screen.
        assert client.get("/api/devices").status_code == 403
        with client.app.state.session_factory() as session:
            session.execute(text("UPDATE server_settings SET login_mode = 'none'"))
            session.commit()
        assert client.get("/api/devices").json() == []
        assert client.get("/api/settings/network").status_code == 200

    main = sqlite3.connect(db_path)
    history = sqlite3.connect(history_db_path(db_path))
    try:
        assert main.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] > 0
        assert history.execute("SELECT count(*) FROM history_counters").fetchone()[0] == 1
    finally:
        main.close()
        history.close()


def test_app_startup_installs_log_capture(web_client):
    import logging

    logging.getLogger("bacpack.web.app").warning("startup wiring test")
    history_writer.flush_current()

    session_factory = web_client.app.state.session_factory
    with session_factory() as session:
        from bacpack.models import LogEntry

        rows = session.query(LogEntry).filter_by(device_id=None).all()
    assert any(r.message == "startup wiring test" for r in rows)


# --- Unmatched /api/* is a clean 404, not the SPA's index.html -


def test_unmatched_api_path_is_404_not_the_spa_shell(web_client):
    r = web_client.get("/api/totally-not-a-real-endpoint")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_bare_api_path_is_404_not_the_spa_shell(web_client):
    r = web_client.get("/api")
    assert r.status_code == 404


# --- Cross-site request refusal ---------------------------------------------
# A multipart upload needs no CORS preflight, so without this any web page
# could POST a config file and delete every device.

_EMPTY_CONFIG = {"file": ("c.json", b'{"version": 1, "devices": []}', "application/json")}


def _device_count(web_client):
    return len(web_client.get("/api/devices").json())


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_upload_is_refused(web_client, site):
    from tests.web._helpers import _create_device

    _create_device(web_client)
    r = web_client.post(
        "/api/config/import?dry_run=false", files=_EMPTY_CONFIG, headers={"Sec-Fetch-Site": site}
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site request refused"}
    assert _device_count(web_client) == 1


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_or_typed_request_is_allowed(web_client, site):
    r = web_client.post(
        "/api/devices", json={"id": 1, "name": "A"}, headers={"Sec-Fetch-Site": site}
    )
    assert r.status_code == 201


def test_foreign_origin_without_fetch_metadata_is_refused(web_client):
    for method in ("post", "put", "patch", "delete"):
        r = web_client.request(
            method.upper(), "/api/devices/1", json={}, headers={"Origin": "http://evil.example"}
        )
        assert r.status_code == 403, method


def test_matching_origin_is_allowed(web_client):
    r = web_client.post(
        "/api/devices", json={"id": 1, "name": "A"}, headers={"Origin": "http://127.0.0.1"}
    )
    assert r.status_code == 201


def test_origin_matching_only_a_forwarded_host_is_refused(web_client):
    r = web_client.post(
        "/api/devices",
        json={"id": 1, "name": "A"},
        headers={"Origin": "https://bacpack.example.com", "X-Forwarded-Host": "bacpack.example.com"},
    )
    assert r.status_code == 403


def test_origin_with_a_different_port_is_refused(web_client):
    r = web_client.post(
        "/api/devices", json={"id": 1, "name": "A"}, headers={"Origin": "http://127.0.0.1:3000"}
    )
    assert r.status_code == 403


def test_script_request_without_browser_headers_is_allowed(web_client):
    r = web_client.post("/api/config/import?dry_run=true", files=_EMPTY_CONFIG)
    assert r.status_code == 200


def test_cross_site_read_is_not_refused(web_client):
    r = web_client.get("/api/devices", headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"})
    assert r.status_code == 200


def test_deeply_nested_json_body_gets_400(web_client):
    # FastAPI's own body parsing already turns json's
    # RecursionError into 400; pinned so an upgrade can't quietly change it.
    deep = "[" * 100_000 + "]" * 100_000
    r = web_client.put("/api/settings/network", content=deep, headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_a_recursion_error_from_any_route_is_a_400(web_client):
    # The app-level net for any other json.loads reached by
    # a request (e.g. the CSV import's JSON cells).
    from fastapi.routing import APIRoute

    def recurse():
        raise RecursionError("maximum recursion depth exceeded")

    web_client.app.router.routes.insert(0, APIRoute("/api/test-recursion", recurse, methods=["GET"]))
    r = web_client.get("/api/test-recursion")
    assert r.status_code == 400
    assert r.json()["detail"] == "request nested too deeply"


# Ids past SQLite's signed 64-bit INTEGER can't even be bound in a query.
_HUGE = 2**63


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", f"/api/devices/{_HUGE}"),
        ("GET", f"/api/devices/{-_HUGE - 1}"),
        ("GET", f"/api/devices/599001/objects/{_HUGE}"),
        ("GET", f"/api/connectors/{_HUGE}"),
        ("PUT", f"/api/devices/{_HUGE}/clock"),
    ],
)
def test_an_id_too_big_for_sqlite_in_the_path_is_404(web_client, method, path):
    from ._helpers import _create_device

    _create_device(web_client)
    r = web_client.request(method, path, json={"offset_seconds": 0})
    assert r.status_code == 404


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("PATCH", "/api/devices/599001/objects/batch", {"object_ids": [_HUGE], "patch": {"description": "x"}}),
        ("DELETE", "/api/devices/599001/objects/batch", {"object_ids": [_HUGE]}),
        ("POST", "/api/engine/start", {"device_id": _HUGE}),
        ("GET", f"/api/traffic?device_id={_HUGE}", None),
    ],
)
def test_an_id_too_big_for_sqlite_in_a_body_or_query_is_422(web_client, method, path, body):
    from ._helpers import _create_device

    _create_device(web_client)
    r = web_client.request(method, path, json=body)
    assert r.status_code == 422


def _app_without_init(tmp_path, monkeypatch, _template_db):
    from tests.conftest import _copy_template

    monkeypatch.setattr("bacpack.web.app.init_db", lambda engine: None)
    return create_app(db_path=str(_copy_template(_template_db, tmp_path / "lc.db")))


def test_shutdown_stops_devices_after_cancelling_the_resume_task(tmp_path, monkeypatch, _template_db):
    events = []

    async def slow_resume(self, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("resume cancelled")
            raise

    async def stop_all(self):
        events.append("stop all")

    monkeypatch.setattr(EngineManager, "resume_remembered_devices", slow_resume)
    monkeypatch.setattr(EngineManager, "stop_all_for_shutdown", stop_all)
    with TestClient(_app_without_init(tmp_path, monkeypatch, _template_db), base_url="http://127.0.0.1"):
        pass
    assert events == ["resume cancelled", "stop all"]


def test_startup_runs_the_resume_task_after_reattach(tmp_path, monkeypatch, _template_db):
    events = []

    async def reattach(self):
        events.append("reattach")

    async def resume(self, **kwargs):
        events.append("resume")

    monkeypatch.setattr(EngineManager, "reattach_running_devices", reattach)
    monkeypatch.setattr(EngineManager, "resume_remembered_devices", resume)
    with TestClient(_app_without_init(tmp_path, monkeypatch, _template_db), base_url="http://127.0.0.1"):
        pass
    assert events[:2] == ["reattach", "resume"]


def test_the_lifespan_starts_the_trend_pruner(web_client):
    from bacpack.trending import TrendPruner

    pruner = web_client.app.state.trend_pruner
    assert isinstance(pruner, TrendPruner)
    assert pruner.wait_idle(5)


def test_lifespan_shutdown_saves_queued_log_lines(tmp_path, monkeypatch):
    import time

    from bacpack.history_writer import HistoryWriter

    real = HistoryWriter._write_batch

    def slow(self, batch):
        time.sleep(0.3)
        real(self, batch)

    monkeypatch.setattr(HistoryWriter, "_write_batch", slow)
    import logging

    from bacpack.db import make_engine, make_session_factory
    from bacpack.models import LogEntry

    db_path = tmp_path / "shutdown.db"
    app = create_app(db_path=str(db_path))
    with TestClient(app, base_url="http://127.0.0.1"):
        logging.getLogger("bacpack.web.app").warning("logged just before shutdown")

    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            assert any(r.message == "logged just before shutdown" for r in session.query(LogEntry))
    finally:
        engine.dispose()


def test_a_failing_shutdown_step_still_saves_log_lines_and_disposes_the_engine(tmp_path, monkeypatch):
    import logging

    import sqlalchemy.engine

    from bacpack.db import make_engine, make_session_factory
    from bacpack.models import LogEntry
    from bacpack.trending import TrendPruner

    async def failing_stop(self):
        raise RuntimeError("stop failed")

    disposed = []
    real_dispose = sqlalchemy.engine.Engine.dispose

    def spy_dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    pruner_stops = []
    real_stop = TrendPruner.stop

    async def spy_stop(self):
        pruner_stops.append(self)
        return await real_stop(self)

    monkeypatch.setattr(EngineManager, "stop_all_for_shutdown", failing_stop)
    monkeypatch.setattr(sqlalchemy.engine.Engine, "dispose", spy_dispose)
    monkeypatch.setattr(TrendPruner, "stop", spy_stop)

    db_path = tmp_path / "shutdown.db"
    app = create_app(db_path=str(db_path))
    with pytest.raises(RuntimeError, match="stop failed"):
        with TestClient(app, base_url="http://127.0.0.1"):
            logging.getLogger("bacpack.web.app").warning("logged before a failing shutdown")

    assert [e.url.database for e in disposed] == [str(db_path)], "the app's engine was never disposed"
    assert len(pruner_stops) == 1
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            assert any(r.message == "logged before a failing shutdown" for r in session.query(LogEntry))
    finally:
        engine.dispose()


def test_a_failing_connector_poller_stop_still_stops_the_devices_and_disposes_the_engine(tmp_path, monkeypatch):
    import sqlalchemy.engine

    from bacpack.connectors import ConnectorPoller

    def failing_poller_stop(self):
        raise RuntimeError("poller stop failed")

    device_stops = []

    async def stop_all(self):
        device_stops.append(self)

    disposed = []
    real_dispose = sqlalchemy.engine.Engine.dispose

    def spy_dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(ConnectorPoller, "stop", failing_poller_stop)
    monkeypatch.setattr(EngineManager, "stop_all_for_shutdown", stop_all)
    monkeypatch.setattr(sqlalchemy.engine.Engine, "dispose", spy_dispose)

    db_path = tmp_path / "shutdown.db"
    with pytest.raises(RuntimeError, match="poller stop failed"):
        with TestClient(create_app(db_path=str(db_path)), base_url="http://127.0.0.1"):
            pass

    assert len(device_stops) == 1, "the devices were not stopped (nor remembered for the next start)"
    assert [e.url.database for e in disposed] == [str(db_path)]
