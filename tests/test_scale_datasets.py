from __future__ import annotations

from dataclasses import replace

import pytest

from bacpack import db as bacpack_db
from bacpack.db import make_engine
from tests.scale import datasets
from tests.scale.datasets import TINY, build, cache_dir, cache_key, cached, check_dataset


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("BACPACK_SCALE_CACHE", str(tmp_path / "cache"))


def _scalar(path, sql):
    engine = make_engine(path)
    try:
        with engine.connect() as connection:
            return connection.exec_driver_sql(sql).scalar()
    finally:
        engine.dispose()


def _run(path, sql):
    engine = make_engine(path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(sql)
    finally:
        engine.dispose()


def test_tiny_install_is_consistent(tmp_path):
    path = tmp_path / "t.db"
    build(path, TINY)
    assert check_dataset(path) == []
    assert _scalar(path, "SELECT COUNT(*) FROM devices") == 3
    assert _scalar(path, f"SELECT COUNT(*) FROM bacnet_objects WHERE device_id = {datasets.BIG_DEVICE_ID}") == 60
    assert _scalar(path, "SELECT COUNT(*) FROM object_runtime") == _scalar(path, "SELECT COUNT(*) FROM bacnet_objects")
    assert _scalar(path, "SELECT COUNT(*) FROM history.traffic_log_entries") == 15
    assert _scalar(path, "SELECT COUNT(*) FROM history.log_entries") == 200
    assert _scalar(path, "SELECT COUNT(*) FROM history.client_requests") == 50
    assert _scalar(path, "SELECT COUNT(*) FROM history.sqlite_master WHERE name LIKE 'ix_trend_samples%'") == 3


def test_tiny_full_history_hits_its_target_exactly(tmp_path):
    sizes = replace(TINY, history_target=500)
    path = tmp_path / "t.db"
    build(path, sizes)
    assert _scalar(path, "SELECT trend_total_sample_count FROM history.history_counters") == 500
    assert _scalar(path, "SELECT COUNT(*) FROM history.trend_samples") == 500
    assert check_dataset(path) == []
    assert len(datasets.ids_for(sizes).extra_trended_ids) == 12


def test_check_dataset_catches_a_wrong_counter(tmp_path):
    path = tmp_path / "t.db"
    build(path, TINY)
    _run(path, "UPDATE history.history_counters SET trend_total_sample_count = trend_total_sample_count + 1")
    problems = check_dataset(path)
    assert len(problems) == 1
    assert "counter" in problems[0]


def test_check_dataset_catches_a_wrong_next_sequence(tmp_path):
    path = tmp_path / "t.db"
    build(path, TINY)
    _run(path, "UPDATE history.trend_state SET next_sequence = next_sequence + 5 WHERE object_id = 1")
    problems = check_dataset(path)
    assert len(problems) == 1
    assert "object 1" in problems[0]
    assert "next_sequence" in problems[0]


def test_cache_key_changes_with_the_schema(monkeypatch):
    before = cache_key(TINY)
    from bacpack import migrations

    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, object()])
    assert cache_key(TINY) != before


def test_cache_key_changes_with_an_index():
    from sqlalchemy import Index

    from bacpack.models import Base

    table = Base.metadata.tables["history.log_entries"]
    before = cache_key(TINY)
    index = Index("ix_scale_key_probe", table.c.level)
    try:
        assert cache_key(TINY) != before
    finally:
        table.indexes.discard(index)
    assert cache_key(TINY) == before


def test_cache_key_changes_with_the_builder_and_sizes(monkeypatch):
    before = cache_key(TINY)
    assert cache_key(replace(TINY, devices=4)) != before
    monkeypatch.setattr(datasets, "BUILDER_VERSION", datasets.BUILDER_VERSION + 1)
    assert cache_key(TINY) != before


def test_a_partial_build_is_never_reused():
    final = cache_dir(TINY)
    partial = final.with_name(final.name + ".partial-999")
    partial.mkdir(parents=True)
    (partial / "bacpack.db").write_text("half a database")
    path = cached(TINY)
    assert check_dataset(path) == []
    assert not partial.exists()


def test_a_failed_build_leaves_no_cache_directory(monkeypatch):
    def broken(path, sizes):
        path.write_text("x")
        raise RuntimeError("disk full")

    monkeypatch.setattr(datasets, "build", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        cached(TINY)
    root = datasets.cache_root()
    assert list(root.iterdir()) == []


def test_cached_reuses_a_finished_build(monkeypatch):
    calls = []
    real = datasets.build
    monkeypatch.setattr(datasets, "build", lambda path, sizes: (calls.append(1), real(path, sizes)))
    first = cached(TINY)
    assert cached(TINY) == first
    assert len(calls) == 1
    cached(TINY, rebuild=True)
    assert len(calls) == 2


def test_an_older_key_for_the_same_dataset_is_removed(monkeypatch):
    first = cached(TINY).parent
    monkeypatch.setattr(datasets, "BUILDER_VERSION", datasets.BUILDER_VERSION + 1)
    second = cached(TINY).parent
    assert first != second
    assert not first.exists()
    assert second.is_dir()


def test_working_copy_copies_both_files(tmp_path):
    src = cached(TINY)
    copy = datasets.working_copy(src, tmp_path / "copy")
    assert check_dataset(copy) == []
    assert bacpack_db.history_db_path(copy).exists()


def test_working_copy_reads_each_copied_file_through(tmp_path, monkeypatch):
    # On XFS the copy is a reflink with none of its pages cached, so write
    # scenarios would time disk reads.
    read = []
    monkeypatch.setattr(datasets, "_read_through", lambda path: read.append((path, path.stat().st_size)))
    src = cached(TINY)
    copy = datasets.working_copy(src, tmp_path / "copy")
    history = bacpack_db.history_db_path(copy)
    assert sorted(read) == sorted([(copy, copy.stat().st_size), (history, history.stat().st_size)])


def test_working_copy_drops_a_previous_copys_wal_files(tmp_path):
    # SQLite would replay a leftover -wal onto the fresh copy, so the copy
    # would start with an earlier run's writes already in it.
    src = cached(TINY)
    dest = tmp_path / "copy"
    dest.mkdir()
    for name in ("bacpack.db-wal", "bacpack.db-shm", "bacpack-history.db-wal", "bacpack-history.db-shm"):
        (dest / name).write_bytes(b"stale")
    copy = datasets.working_copy(src, dest)
    assert sorted(p.name for p in dest.iterdir()) == sorted([copy.name, bacpack_db.history_db_path(copy).name])
