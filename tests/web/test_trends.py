"""Trend data/CSV/storage endpoints, per-object trend config and sample
caps, and the running trend-sample counter across delete paths."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.web._helpers import _create_device, _create_object
from bacpack.models import get_or_create_history_counters, trend_state_of, TrendSample, TrendState


def test_object_create_and_update_carries_trend_sample_cap(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30,
                          trend_sample_cap=100)
    assert obj["trend_sample_cap"] == 100

    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trend_sample_cap": 50})
    assert r.json()["trend_sample_cap"] == 50


def test_lowering_trend_sample_cap_immediately_prunes_to_fit(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        record_sample(session_factory, obj["id"], v)

    r = web_client.put(f"/api/devices/599001/objects/{obj['id']}", json={"trend_sample_cap": 2})
    assert r.status_code == 200

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend")
    assert [row["value"] for row in r.json()] == [4.0, 5.0]


# --- Historical trending ---------------------------------------------------


def test_object_create_and_update_carries_trend_config(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    assert obj["trended"] is True
    assert obj["trend_mode"] == "interval"
    assert obj["trend_interval_seconds"] == 30

    r = web_client.put(
        f"/api/devices/599001/objects/{obj['id']}", json={"trend_mode": "cov"}
    )
    assert r.json()["trend_mode"] == "cov"


def test_trend_data_endpoint_returns_recorded_samples(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory

    record_sample(session_factory, obj["id"], 1.0)
    record_sample(session_factory, obj["id"], 2.0)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend")
    assert r.status_code == 200
    body = r.json()
    assert [row["value"] for row in body] == [1.0, 2.0]
    assert [row["sequence"] for row in body] == [1, 2]


def test_trend_data_endpoint_respects_limit(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    for v in [1.0, 2.0, 3.0, 4.0]:
        record_sample(session_factory, obj["id"], v)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend?limit=2")
    assert [row["value"] for row in r.json()] == [3.0, 4.0]  # most recent 2, oldest-first


def test_trend_export_csv(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, obj["id"], 42.0)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "sequence,timestamp,value" in r.text
    assert "42.0" in r.text


def test_trends_storage_endpoint_shape_with_no_trends(web_client):
    r = web_client.get("/api/trends/storage")
    assert r.status_code == 200
    body = r.json()
    assert body["total_samples"] == 0
    assert body["total_bytes_estimate"] == 0
    assert body["hard_cap_bytes"] == 1024 * 1024 * 1024
    assert body["effective_limit_bytes"] == body["hard_cap_bytes"]
    assert body["trends"] == []


def test_trends_storage_endpoint_reports_per_object_usage(web_client):
    from bacpack.trending import BYTES_PER_TREND_SAMPLE, record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, obj["id"], 1.0)
    record_sample(session_factory, obj["id"], 2.0)

    r = web_client.get("/api/trends/storage")
    body = r.json()
    assert body["total_samples"] == 2
    assert body["total_bytes_estimate"] == 2 * BYTES_PER_TREND_SAMPLE
    assert len(body["trends"]) == 1
    entry = body["trends"][0]
    assert entry["device_id"] == 599001
    assert entry["object_id"] == obj["id"]
    assert entry["object_name"] == obj["name"]
    assert entry["sample_count"] == 2
    assert entry["bytes_estimate"] == 2 * BYTES_PER_TREND_SAMPLE


def test_trends_storage_endpoint_gives_each_objects_sample_cap(web_client):
    from bacpack.models import MAX_TREND_SAMPLE_CAP
    from bacpack.trending import record_sample

    _create_device(web_client)
    capped = _create_object(
        web_client, trended=True, trend_mode="interval", trend_interval_seconds=30, trend_sample_cap=500
    )
    default = _create_object(
        web_client, instance_number=2, name="AV-2", trended=True, trend_mode="interval", trend_interval_seconds=30
    )
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, capped["id"], 1.0)
    record_sample(session_factory, default["id"], 1.0)

    caps = {t["object_id"]: t["sample_cap"] for t in web_client.get("/api/trends/storage").json()["trends"]}
    assert caps == {capped["id"]: 500, default["id"]: MAX_TREND_SAMPLE_CAP}


def test_trends_storage_endpoint_reflects_configured_soft_limit(web_client):
    web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 5})
    r = web_client.get("/api/trends/storage")
    assert r.json()["effective_limit_bytes"] == 5 * 1024 * 1024


@pytest.mark.usefixtures("quiet_retention")
def test_storage_reports_pruning_while_over_the_limit(web_client, monkeypatch):
    from bacpack import trending

    monkeypatch.setattr(trending, "BYTES_PER_TREND_SAMPLE", 300_000)  # 1 MB = 3 samples
    web_client.put("/api/settings/network", json={"trend_soft_limit_mb": 1})
    # A pass running beside the hand-set counter finds nothing to delete and
    # resyncs it to the real count, 0.
    assert web_client.app.state.trend_pruner.wait_idle(10)
    for total, expected in ((4, True), (3, False)):
        with web_client.app.state.session_factory() as session:
            get_or_create_history_counters(session).trend_total_sample_count = total
            session.commit()
        assert web_client.get("/api/trends/storage").json()["pruning"] is expected


# get_object_trend(limit=-1) used to let SQLite treat a negative LIMIT as
# unlimited, defeating the cap entirely.
def test_trend_data_rejects_negative_limit(web_client):
    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend?limit=-1")
    assert r.status_code == 422


def test_deleting_a_trended_object_decrements_the_running_counter(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, obj["id"], 1.0)
    record_sample(session_factory, obj["id"], 2.0)

    r = web_client.delete(f"/api/devices/599001/objects/{obj['id']}")
    assert r.status_code == 204

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 0


def test_deleting_a_device_decrements_the_running_counter_for_all_its_objects(web_client):
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj_1 = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30,
                            name="AV-1", instance_number=1)
    obj_2 = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30,
                            name="AV-2", instance_number=2)
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, obj_1["id"], 1.0)
    record_sample(session_factory, obj_2["id"], 2.0)
    record_sample(session_factory, obj_2["id"], 3.0)

    r = web_client.delete("/api/devices/599001")
    assert r.status_code == 204

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 0


def test_batch_deleting_a_trended_object_decrements_the_running_counter(web_client):
    # Unlike delete_object, batch_delete_objects used to skip
    # release_trend_samples_for_objects entirely, leaking the counter high
    # with no self-healing.
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    record_sample(session_factory, obj["id"], 1.0)
    record_sample(session_factory, obj["id"], 2.0)
    record_sample(session_factory, obj["id"], 3.0)

    r = web_client.request(
        "DELETE",
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]]},
    )
    assert r.status_code == 200, r.text

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 0


def test_batch_lowering_trend_sample_cap_immediately_prunes_to_fit(web_client):
    # batch_update_objects (the bulk-edit panel's save path) didn't
    # prune-to-fit when trend_sample_cap was lowered, unlike update_object.
    from bacpack.trending import record_sample

    _create_device(web_client)
    obj = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30)
    session_factory = web_client.app.state.session_factory
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        record_sample(session_factory, obj["id"], v)

    r = web_client.patch(
        "/api/devices/599001/objects/batch",
        json={"object_ids": [obj["id"]], "patch": {"trend_sample_cap": 2}},
    )
    assert r.status_code == 200, r.text

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend")
    assert [row["value"] for row in r.json()] == [4.0, 5.0]

    with session_factory() as session:
        from bacpack.models import get_or_create_settings

        settings = get_or_create_settings(session)
        counters = get_or_create_history_counters(session)
    assert counters.trend_total_sample_count == 2


# Trend *page* rendering (linking, "not trended" gating, the uPlot chart
# itself) is now a React concern -- see frontend/src/pages/ObjectTrend.tsx
# and its Vitest coverage. The JSON trend-data endpoints these pages consume
# are already covered above (test_trend_data_endpoint_*, test_trend_export_csv).


def test_trend_export_csv_with_non_ascii_or_quoted_object_name(web_client):
    """The object name went straight into Content-Disposition, which must be
    Latin-1 -- a name like "Zone Δ" failed the response as a 500, and a
    double quote broke the header."""
    _create_device(web_client)
    obj = _create_object(web_client, name='Zone Δ "north"', trended=True)

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/export.csv")
    assert r.status_code == 200, r.text
    disposition = r.headers["content-disposition"]
    assert disposition.isascii()
    assert "filename*=UTF-8''Zone%20%CE%94%20%22north%22-trend.csv" in disposition


# --- Sample counter after deletes ---------------------------------------------
# The bulk-delete event also lowered trend_total_sample_count, but every
# delete path already does that through release_trend_samples_for_objects, so
# the counter went down twice (found by a live check: 200k samples
# deleted, counter dropped from 284471 to 0).

import pytest


def _seed_samples(web_client, object_ids_and_counts):
    from bacpack.models import TrendSample, get_or_create_settings

    with web_client.app.state.session_factory() as session:
        for object_id, count in object_ids_and_counts:
            for i in range(count):
                session.add(TrendSample(object_id=object_id, sequence=i + 1, value=1.0))
        get_or_create_history_counters(session).trend_total_sample_count = sum(c for _, c in object_ids_and_counts)
        session.commit()


def _counter_and_rows(web_client):
    from bacpack.models import TrendSample, get_or_create_settings

    with web_client.app.state.session_factory() as session:
        return get_or_create_history_counters(session).trend_total_sample_count, session.query(TrendSample).count()


@pytest.mark.parametrize(
    "delete",
    [
        lambda c, gone: c.delete(f"/api/devices/599001/objects/{gone}"),
        lambda c, gone: c.request("DELETE", "/api/devices/599001/objects/batch", json={"object_ids": [gone]}),
    ],
    ids=["single", "batch"],
)
def test_deleting_an_object_lowers_the_counter_once(web_client, delete):
    _create_device(web_client)
    kept = _create_object(web_client, name="KEEP", instance_number=1)
    gone = _create_object(web_client, name="GONE", instance_number=2)
    _seed_samples(web_client, [(kept["id"], 3), (gone["id"], 5)])

    assert delete(web_client, gone["id"]).status_code in (200, 204)

    assert _counter_and_rows(web_client) == (3, 3)


def test_deleting_a_device_lowers_the_counter_once(web_client):
    _create_device(web_client)
    _create_device(web_client, device_id=599002, name="Other")
    kept = _create_object(web_client, device_id=599002, name="KEEP")
    gone = _create_object(web_client, name="GONE")
    _seed_samples(web_client, [(kept["id"], 3), (gone["id"], 5)])

    assert web_client.delete("/api/devices/599001").status_code == 204

    assert _counter_and_rows(web_client) == (3, 3)


def test_clearing_trend_sample_cap_prunes_to_the_default_cap(web_client, monkeypatch):
    import bacpack.models as models_module
    from bacpack.trending import record_sample

    _create_device(web_client)
    single = _create_object(web_client, trended=True, trend_mode="interval", trend_interval_seconds=30,
                            trend_sample_cap=10)
    batched = _create_object(web_client, instance_number=2, name="AV-2", trended=True, trend_mode="interval",
                             trend_interval_seconds=30, trend_sample_cap=10)
    session_factory = web_client.app.state.session_factory
    for v in (1.0, 2.0, 3.0, 4.0):
        record_sample(session_factory, single["id"], v)
        record_sample(session_factory, batched["id"], v)
    monkeypatch.setattr(models_module, "MAX_TREND_SAMPLE_CAP", 2)

    r = web_client.put(f"/api/devices/599001/objects/{single['id']}", json={"trend_sample_cap": None})
    assert r.status_code == 200, r.text
    r = web_client.patch(
        "/api/devices/599001/objects/batch", json={"object_ids": [batched["id"]], "patch": {"trend_sample_cap": None}}
    )
    assert r.status_code == 200, r.text

    for obj in (single, batched):
        r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend")
        assert [row["value"] for row in r.json()] == [3.0, 4.0]


def _trended_object(web_client, **overrides):
    _create_device(web_client)
    fields = dict(trended=True, trend_mode="interval", trend_interval_seconds=5)
    fields.update(overrides)
    return _create_object(web_client, **fields)


def test_trend_range_returns_samples_in_the_range(web_client):
    obj = _trended_object(web_client)
    with web_client.app.state.session_factory() as session:
        for seq, second in enumerate((0, 5, 10), start=1):
            session.add(TrendSample(object_id=obj["id"], sequence=seq, timestamp=dt.datetime(2026, 1, 15, 9, 0, second), value=float(seq)))
        session.commit()

    r = web_client.get(
        f"/api/devices/599001/objects/{obj['id']}/trend/range",
        params={"start": "2026-01-15T09:00:04Z", "end": "2026-01-15T09:00:11Z"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["value"] for p in body["samples"]] == [2.0, 3.0]
    assert body["carry_in"]["value"] == 1.0
    assert body["mode"] == "interval" and body["interval_seconds"] == 5 and body["step"] is False


def test_trend_range_unset_interval_reports_the_default(web_client):
    obj = _trended_object(web_client, trend_interval_seconds=None)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params={"start": "2026-01-15T09:00:00Z"})
    assert r.json()["interval_seconds"] == 60


def test_trend_range_of_a_binary_object_is_a_step(web_client):
    obj = _trended_object(web_client, object_type="binary-value", units=None)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params={"start": "2026-01-15T09:00:00Z"})
    assert r.json()["step"] is True


def test_trend_range_of_an_untrended_object_is_empty(web_client):
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params={"start": "2026-01-15T09:00:00Z"})
    assert r.status_code == 200
    assert r.json()["mode"] is None and r.json()["samples"] == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start": "2026-01-15T09:00:00"},  # no offset
        {"start": "2026-01-15T10:00:00Z", "end": "2026-01-15T09:00:00Z"},
        {"start": "2026-01-15T09:00:00Z", "end": "2026-01-15T09:00:00Z"},
        {"start": "2026-01-15T09:00:00Z", "max_points": 9},
        {"start": "2026-01-15T09:00:00Z", "max_points": 5001},
        {"start": "not-a-time"},
        {"last": 3600, "start": "2026-01-15T09:00:00Z"},
        {"last": 3600, "end": "2026-01-15T09:00:00Z"},
        {"last": 0},
        {"last": 366 * 86400 + 1},
    ],
)
def test_trend_range_bad_parameters_are_422(web_client, params):
    obj = _trended_object(web_client)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params=params)
    assert r.status_code == 422, r.text


def test_trend_range_last_ends_at_the_servers_now(web_client, monkeypatch):
    from bacpack import timeutil

    now = dt.datetime(2026, 1, 15, 10, 0, 0)
    monkeypatch.setattr(timeutil, "db_utc_now", lambda: now)
    obj = _trended_object(web_client)
    with web_client.app.state.session_factory() as session:
        session.add(TrendSample(object_id=obj["id"], sequence=1, timestamp=now - dt.timedelta(hours=2), value=1.0))
        session.add(TrendSample(object_id=obj["id"], sequence=2, timestamp=now, value=2.0))
        session.commit()

    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params={"last": 3600})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"] == "2026-01-15T09:00:00+00:00" and body["end"] == "2026-01-15T10:00:00+00:00"
    assert [p["value"] for p in body["samples"]] == [2.0]
    assert body["carry_in"]["value"] == 1.0


def test_trend_range_returns_an_explicit_window_in_utc(web_client):
    obj = _trended_object(web_client)
    r = web_client.get(
        f"/api/devices/599001/objects/{obj['id']}/trend/range",
        params={"start": "2026-01-15T11:00:00+02:00", "end": "2026-01-15T09:30:00Z"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["start"] == "2026-01-15T09:00:00+00:00" and r.json()["end"] == "2026-01-15T09:30:00+00:00"


def test_trend_range_of_an_untrended_object_still_names_its_window(web_client, monkeypatch):
    from bacpack import timeutil

    monkeypatch.setattr(timeutil, "db_utc_now", lambda: dt.datetime(2026, 1, 15, 10, 0, 0))
    _create_device(web_client)
    obj = _create_object(web_client)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range", params={"last": 60})
    assert r.json()["start"] == "2026-01-15T09:59:00+00:00" and r.json()["end"] == "2026-01-15T10:00:00+00:00"


def test_trend_range_says_which_of_last_and_start_to_send(web_client):
    obj = _trended_object(web_client)
    url = f"/api/devices/599001/objects/{obj['id']}/trend/range"
    assert "send last or start" in web_client.get(url).text
    both = web_client.get(url, params={"last": 60, "start": "2026-01-15T09:00:00Z"})
    assert "not both" in both.text


def test_trend_range_unknown_object_is_404(web_client):
    _create_device(web_client)
    r = web_client.get("/api/devices/599001/objects/9999/trend/range", params={"start": "2026-01-15T09:00:00Z"})
    assert r.status_code == 404


def test_trend_range_says_a_raw_plus_in_the_offset_arrived_as_a_space(web_client):
    # A query string reads an unencoded + as a space.
    obj = _trended_object(web_client)
    r = web_client.get(f"/api/devices/599001/objects/{obj['id']}/trend/range?start=2026-01-15T09:00:00+02:00")
    assert r.status_code == 422
    assert "%2B" in r.text


def test_trend_range_describes_how_to_send_an_offset(web_client):
    params = web_client.get("/openapi.json").json()["paths"][
        "/api/devices/{device_id}/objects/{object_id}/trend/range"
    ]["get"]["parameters"]
    start = next(p for p in params if p["name"] == "start")
    assert "%2B" in start["description"]


def test_trend_range_refuses_a_start_or_end_whose_utc_conversion_overflows(web_client):
    obj = _trended_object(web_client)
    url = f"/api/devices/599001/objects/{obj['id']}/trend/range"
    r = web_client.get(url, params={"start": "0001-01-01T00:00:00+05:00"})
    assert r.status_code == 422
    assert "start" in r.json()["detail"]
    r = web_client.get(url, params={"start": "2026-01-15T09:00:00Z", "end": "9999-12-31T23:59:59-05:00"})
    assert r.status_code == 422
    assert "end" in r.json()["detail"]
    assert web_client.get(url, params={"start": "0001-01-01T00:00:00Z"}).status_code == 200
