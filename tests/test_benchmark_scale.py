from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.scale import datasets, scenarios
from tests.scale.datasets import TINY

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import benchmark_scale as bench  # noqa: E402


def test_read_results_missing_file_is_empty(tmp_path):
    assert bench.read_results(tmp_path / "none.jsonl") == []


def test_read_results_malformed_line_names_it(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text('{"machine": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r":2: not JSON"):
        bench.read_results(path)


def test_previous_results_ignores_other_machines():
    results = [
        {"machine": "a", "results": {"x": 1.0}},
        {"machine": "b", "results": {"x": 9.0}},
        {"machine": "a", "results": {"x": 2.0}},
        {"machine": "b", "results": {"x": 8.0}},
    ]
    assert bench.previous_results(results, "a") == {"x": 2.0}
    assert bench.previous_results(results, "c") == {}
    assert bench.previous_results([], "a") == {}


def test_previous_results_survive_a_partial_run():
    # An --only run names only some scenarios; the others keep their last figure.
    results = [
        {"machine": "a", "results": {"x": 1.0, "y": 5.0}},
        {"machine": "a", "results": {"x": 2.0}},
    ]
    assert bench.previous_results(results, "a") == {"x": 2.0, "y": 5.0}


def test_compare_flags_above_ratio():
    previous = {"slow": 1.0, "fine": 1.0}
    rows = {row[0]: row for row in bench.compare({"slow": 1.6, "fine": 1.4, "new": 3.0}, previous)}
    assert rows["slow"][4] is True
    assert rows["fine"][4] is False
    assert rows["new"][2] is None and rows["new"][3] is None and rows["new"][4] is False
    assert bench.compare({"a": 1.0}, {})[0][3] is None


def test_a_malformed_results_file_fails_before_any_timing(tmp_path, monkeypatch):
    results = tmp_path / "r.jsonl"
    results.write_text("not json\n", encoding="utf-8")
    monkeypatch.setenv("BACPACK_BENCHMARK_RESULTS", str(results))
    monkeypatch.setattr(bench, "load_all", lambda: None)
    monkeypatch.setattr(scenarios, "SCENARIOS", {})
    scenarios.scenario("demo.noop", area="trends", dataset="install", writes=False)(lambda ctx: None)

    def never(*args, **kwargs):
        raise AssertionError("timed before reading the results file")

    monkeypatch.setattr(bench, "_time_all", never)
    with pytest.raises(ValueError, match=":1: not JSON"):
        bench.main([])


def test_repeat_must_be_at_least_one(monkeypatch):
    monkeypatch.setattr(bench, "_time_all", lambda *a: pytest.fail("timed with --repeat 0"))
    with pytest.raises(SystemExit):
        bench.main(["--repeat", "0"])


def test_main_appends_one_line_per_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACPACK_SCALE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("BACPACK_BENCHMARK_RESULTS", raising=False)
    monkeypatch.setattr(scenarios, "SCENARIOS", {})
    monkeypatch.setattr(bench, "load_all", lambda: None)
    monkeypatch.setattr(bench, "DATASETS", {"install": TINY, "full_history": TINY})
    results = tmp_path / "out" / "scale-results.jsonl"

    seen = []

    @scenarios.scenario("demo.count", area="trends", dataset="install", writes=False)
    def count(ctx):
        seen.append(ctx.db_path)
        with ctx.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT COUNT(*) FROM devices").scalar()

    assert bench.main(["--repeat", "2", "--results", str(results)]) == 0
    first = capsys.readouterr().out
    assert "no previous run" in first
    assert bench.main(["--repeat", "2", "--results", str(results)]) == 0
    second = capsys.readouterr().out
    assert "no previous run" not in second
    lines = results.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[1])
    assert set(entry["results"]) == {"demo.count"}
    assert entry["repeat"] == 2 and entry["machine"]
    assert len(seen) == 4

    assert bench.main(["--repeat", "1", "--no-save", "--only", "demo.count", "--results", str(results)]) == 0
    assert len(seen) == 5
    assert len(results.read_text(encoding="utf-8").splitlines()) == 2


def test_results_path_defaults_to_the_scale_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("BACPACK_SCALE_CACHE", str(tmp_path))
    monkeypatch.delenv("BACPACK_BENCHMARK_RESULTS", raising=False)
    assert bench.results_path(None) == tmp_path / "scale-results.jsonl"


def test_results_path_env_var_beats_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BACPACK_SCALE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("BACPACK_BENCHMARK_RESULTS", str(tmp_path / "env.jsonl"))
    assert bench.results_path(None) == tmp_path / "env.jsonl"


def test_results_path_argument_beats_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("BACPACK_BENCHMARK_RESULTS", str(tmp_path / "env.jsonl"))
    assert bench.results_path(str(tmp_path / "arg.jsonl")) == tmp_path / "arg.jsonl"
