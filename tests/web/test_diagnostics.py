"""GET /api/traffic and /api/logs."""
from __future__ import annotations

from tests.web._helpers import _add_log_entry, _create_device


def test_api_traffic_endpoint(web_client):
    _create_device(web_client, device_id=1, name="D")
    r = web_client.get("/api/traffic", params={"device_id": 1})
    assert r.status_code == 200
    assert r.json() == []


# --- Application logging in the UI --------------------------------------------


def test_api_logs_endpoint_returns_empty_list_by_default(web_client):
    r = web_client.get("/api/logs")
    assert r.status_code == 200
    assert r.json() == []


def test_api_logs_endpoint_with_no_device_id_returns_everything(web_client):
    _create_device(web_client, device_id=1, name="D")
    _add_log_entry(web_client, device_id=None, message="global")
    _add_log_entry(web_client, device_id=1, message="device-1")

    r = web_client.get("/api/logs")
    assert r.status_code == 200
    messages = {row["message"] for row in r.json()}
    assert messages == {"global", "device-1"}


def test_api_logs_endpoint_filters_to_one_device_plus_global(web_client):
    _create_device(web_client, device_id=1, name="D")
    _create_device(web_client, device_id=2, name="E")
    _add_log_entry(web_client, device_id=None, message="global")
    _add_log_entry(web_client, device_id=1, message="device-1")
    _add_log_entry(web_client, device_id=2, message="device-2")

    r = web_client.get("/api/logs", params={"device_id": 1})
    messages = {row["message"] for row in r.json()}
    assert messages == {"global", "device-1"}


def test_api_logs_endpoint_filters_by_level(web_client):
    _add_log_entry(web_client, level="INFO", message="info-msg")
    _add_log_entry(web_client, level="WARNING", message="warn-msg")
    _add_log_entry(web_client, level="ERROR", message="error-msg")

    r = web_client.get("/api/logs", params={"level": "WARNING"})
    messages = {row["message"] for row in r.json()}
    assert messages == {"warn-msg", "error-msg"}


def test_api_logs_endpoint_respects_limit(web_client):
    for i in range(5):
        _add_log_entry(web_client, message=f"m{i}")

    r = web_client.get("/api/logs", params={"limit": 2})
    assert len(r.json()) == 2


def test_api_logs_gives_each_entry_its_id_newest_first(web_client):
    # The UI keys rows by id, so an open traceback survives a refresh.
    for i in range(3):
        _add_log_entry(web_client, message=f"m{i}")

    rows = web_client.get("/api/logs").json()
    assert [row["message"] for row in rows] == ["m2", "m1", "m0"]
    ids = [row["id"] for row in rows]
    assert all(isinstance(i, int) for i in ids)
    assert ids == sorted(ids, reverse=True)


def test_api_logs_for_a_device_merges_its_rows_and_global_ones_newest_first_up_to_limit(web_client):
    _create_device(web_client, device_id=1, name="D")
    _create_device(web_client, device_id=2, name="E")
    for i, device_id in enumerate([1, None, 2, 1, None, 1, 2, None]):
        _add_log_entry(web_client, device_id=device_id, message=f"m{i}")

    rows = web_client.get("/api/logs", params={"device_id": 1, "limit": 4}).json()
    assert [row["message"] for row in rows] == ["m7", "m5", "m4", "m3"]


def test_a_device_log_page_reads_through_the_index_without_sorting_the_table(web_client):
    from sqlalchemy import event

    _create_device(web_client, device_id=1, name="D")
    _add_log_entry(web_client, device_id=1, message="device-1")
    engine = web_client.app.state.session_factory.kw["bind"]
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "log_entries" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert web_client.get("/api/logs", params={"device_id": 1, "level": "WARNING"}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements
    with engine.connect() as connection:
        for statement, parameters in statements:
            plan = [row[3] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)]
            # A temp B-tree means every row the filter matched was read and sorted to return a page.
            assert not any("TEMP B-TREE" in step or "MULTI-INDEX OR" in step for step in plan), plan


def test_api_logs_rejects_negative_limit(web_client):
    r = web_client.get("/api/logs?limit=-5")
    assert r.status_code == 422


def test_api_logs_rejects_unknown_level(web_client):
    r = web_client.get("/api/logs?level=NOTALEVEL")
    assert r.status_code == 422


# --- Negative `limit` bounds -----------------------------------------------
# get_traffic(limit=-5) used to silently drop the newest 5 entries
# (entries[:-5]); get_object_trend(limit=-1) let SQLite treat a negative
# LIMIT as unlimited, defeating the cap entirely.


def test_traffic_rejects_negative_limit(web_client):
    r = web_client.get("/api/traffic?device_id=1&limit=-5")
    assert r.status_code == 422
