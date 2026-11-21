"""The cross-file guard recognises the pattern that fails at once with
"database is locked"."""
from tests import _db_guard
from tests._db_guard import TransactionWatch


def _run(*statements):
    watch = TransactionWatch()
    return [v for v in (watch.statement(s) for s in statements) if v]


def test_write_one_file_then_read_and_write_the_other_is_flagged():
    found = _run(
        "BEGIN ",
        "UPDATE history.history_counters SET trend_total_sample_count=? WHERE history.history_counters.id = ?",
        "SELECT devices.id FROM devices WHERE devices.id = ?",
        "UPDATE devices SET database_revision=? WHERE devices.id = ?",
    )
    assert len(found) == 1 and "main" in found[0]


def test_the_same_under_begin_immediate_is_fine():
    assert not _run(
        "BEGIN IMMEDIATE",
        "UPDATE history.history_counters SET trend_total_sample_count=? WHERE history.history_counters.id = ?",
        "SELECT devices.id FROM devices WHERE devices.id = ?",
        "UPDATE devices SET database_revision=? WHERE devices.id = ?",
    )


def test_reads_outside_a_transaction_dont_count():
    assert not _run(
        "SELECT devices.id FROM devices",
        "BEGIN ",
        "UPDATE devices SET database_revision=? WHERE devices.id = ?",
        "COMMIT",
    )


def test_a_read_after_the_files_own_write_is_fine():
    assert not _run(
        "BEGIN ",
        "UPDATE devices SET name=? WHERE devices.id = ?",
        "SELECT devices.id FROM devices",
        "UPDATE devices SET database_revision=? WHERE devices.id = ?",
    )


def test_a_history_read_then_history_write_is_flagged():
    found = _run(
        "BEGIN ",
        "UPDATE bacnet_objects SET trend_sample_cap=? WHERE bacnet_objects.id = ?",
        "SELECT count(*) AS count_1 FROM (SELECT history.trend_samples.id FROM history.trend_samples) AS anon_1",
        "DELETE FROM history.trend_samples WHERE history.trend_samples.id IN (?)",
    )
    assert len(found) == 1 and "history" in found[0]


def test_commit_ends_the_transaction():
    assert not _run(
        "BEGIN ",
        "SELECT devices.id FROM devices",
        "COMMIT",
        "BEGIN ",
        "UPDATE devices SET name=? WHERE devices.id = ?",
    )


def test_a_table_named_inside_a_string_value_is_not_a_read():
    assert not _run(
        "BEGIN ",
        "INSERT INTO history.log_entries (message) VALUES ('ran SELECT name FROM devices, it''s fine')",
        "UPDATE devices SET database_revision=? WHERE devices.id = ?",
    )


def test_bacpack_frames_are_found_wherever_the_package_is_installed():
    package_dir = "/venv/lib/python3.14/site-packages/bacpack"
    assert _db_guard._package_relative(f"{package_dir}/web/api/objects.py", package_dir) == "web/api/objects.py"
    assert _db_guard._package_relative(f"{package_dir}/db.py", package_dir) is None
    assert _db_guard._package_relative("/repo/tests/web/test_objects.py", package_dir) is None
    assert _db_guard._package_relative(r"C:\venv\Lib\site-packages\bacpack\models.py", "C:/venv/Lib/site-packages/bacpack") == "models.py"


def test_each_violation_belongs_to_the_test_that_was_running(monkeypatch):
    monkeypatch.setattr(_db_guard, "VIOLATIONS", [])
    monkeypatch.setattr(_db_guard, "CURRENT_TEST", None)
    _db_guard.record("left over")
    _db_guard.CURRENT_TEST = "tests/x.py::test_b"
    _db_guard.record("found")
    found = _db_guard.take("tests/x.py::test_b")
    assert len(found) == 2
    assert "between tests" in found[0] and "left over" in found[0]
    assert "found" in found[1] and "MainThread" in found[1]
    assert _db_guard.VIOLATIONS == []
