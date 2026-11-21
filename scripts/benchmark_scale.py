"""Times every scale scenario and keeps the results.

Run from the repo root:

    .\\.venv\\Scripts\\python.exe scripts\\benchmark_scale.py [--repeat N] [--only PATTERN] [--rebuild] [--no-save] [--results PATH]

Each scenario runs N times (default 3) and its median is kept. One JSON line
per run is appended to the results file (--results, else the
BACPACK_BENCHMARK_RESULTS environment variable, else scale-results.jsonl in the
scale cache folder), and the table compares each median with the previous run on the same machine. Dataset
builds are reported apart and never counted in a scenario's time.
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import gc
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from bacpack import bacpack_version, timeutil  # noqa: E402
from tests.scale import datasets, scenarios  # noqa: E402
from tests.scale.scenarios import execute, load_all, make_context  # noqa: E402


def results_path(argument):
    """The results file: the argument, else BACPACK_BENCHMARK_RESULTS, else
    scale-results.jsonl in the scale cache folder."""
    chosen = argument or os.environ.get("BACPACK_BENCHMARK_RESULTS")
    return Path(chosen).expanduser() if chosen else datasets.cache_root() / "scale-results.jsonl"


DATASETS = {"install": datasets.FIELD, "full_history": datasets.FULL_HISTORY}
FLAG_RATIO = 1.5


def machine_info() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "date": timeutil.iso_utc(timespec="seconds"),
        "bacpack": bacpack_version(),
        "commit": commit,
        "machine": platform.node(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }


def read_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            raise ValueError(f"{path}:{number}: not JSON") from None
    return entries


def previous_results(results: list[dict], machine: str) -> dict[str, float]:
    """Each scenario's latest median on this machine, scenario by scenario,
    so an --only run doesn't hide the others' last figures."""
    latest: dict[str, float] = {}
    for entry in results:
        if entry.get("machine") == machine:
            latest.update(entry.get("results", {}))
    return latest


def compare(current: dict[str, float], previous: dict[str, float], *, flag_ratio: float = FLAG_RATIO):
    """(name, now, before, ratio, flagged) per scenario."""
    rows = []
    for name, now in current.items():
        before = previous.get(name)
        ratio = now / before if before else None
        rows.append((name, now, before, ratio, ratio is not None and ratio > flag_ratio))
    return rows


async def _run_read_only(s, ctx, repeat: int) -> list[float]:
    """Setup once, then `repeat` timed runs on the same prepared context."""
    first, _ = await execute(s, ctx)
    again = replace(s, setup=None)
    return [first] + [(await execute(again, ctx))[0] for _ in range(repeat - 1)]


def _time_read_only(s, db_path: Path, tmp_path: Path, ids, repeat: int) -> list[float]:
    ctx = make_context(db_path, tmp_path, ids)
    try:
        return asyncio.run(_run_read_only(s, ctx, repeat))
    finally:
        ctx.close()


def _time_writing(s, source: Path, work: Path, dataset: str, ids, repeat: int) -> list[float]:
    """A fresh copy of the dataset for every repeat, each in its own
    directory: a connection the previous repeat hasn't finished closing
    would otherwise still hold (and replay) the old copy's -wal."""
    times = []
    for _ in range(repeat):
        copy = datasets.working_copy(source, Path(tempfile.mkdtemp(prefix=f"{dataset}-w-", dir=work)))
        ctx = make_context(copy, work / "scratch", ids)
        try:
            seconds, _ = asyncio.run(execute(s, ctx))
            times.append(seconds)
        finally:
            ctx.close()
            gc.collect()  # frees a straggling connection, so the copy can go
            shutil.rmtree(copy.parent, ignore_errors=True)
    return times


def _time_all(selected: list, repeat: int, rebuild: bool) -> dict[str, float]:
    medians: dict[str, float] = {}
    work = Path(tempfile.mkdtemp(prefix="bacpack-bench-"))
    try:
        for dataset in ("install", "full_history"):
            mine = [s for s in selected if s.dataset == dataset]
            if not mine:
                continue
            sizes = DATASETS[dataset]
            source = datasets.cached(sizes, rebuild=rebuild)
            ids = datasets.ids_for(sizes)
            # Read-only scenarios share one copy, removed before the writing
            # ones start so two copies of a big dataset are never on disk.
            read_only = [s for s in mine if not s.writes]
            if read_only:
                shared = datasets.working_copy(source, work / f"{dataset}-r")
                try:
                    for s in read_only:
                        medians[s.name] = statistics.median(_time_read_only(s, shared, work / "scratch", ids, repeat))
                        print(f"  {s.name}: {medians[s.name]:.3f} s", flush=True)
                finally:
                    shutil.rmtree(shared.parent, ignore_errors=True)
            for s in (s for s in mine if s.writes):
                medians[s.name] = statistics.median(_time_writing(s, source, work, dataset, ids, repeat))
                print(f"  {s.name}: {medians[s.name]:.3f} s", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return medians


def _print_table(rows, machine: str, have_previous: bool) -> None:
    print()
    if not have_previous:
        print(f"no previous run on {machine}")
    width = max([len(r[0]) for r in rows] + [8])
    print(f"{'scenario':<{width}}  {'median s':>9}  {'before s':>9}  {'ratio':>6}")
    for name, now, before, ratio, flagged in rows:
        before_text = f"{before:9.3f}" if before is not None else f"{'-':>9}"
        ratio_text = f"{ratio:6.2f}" if ratio is not None else f"{'-':>6}"
        print(f"{name:<{width}}  {now:9.3f}  {before_text}  {ratio_text}{'  <-- slower' if flagged else ''}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Time the scale scenarios.")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--only", default=None, help="fnmatch pattern on scenario names")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--results", default=None, help="results file to compare with and append to")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    results = results_path(args.results)
    load_all()
    selected = [
        s for name, s in sorted(scenarios.SCENARIOS.items()) if args.only is None or fnmatch.fnmatch(name, args.only)
    ]
    info = machine_info()
    # Read before timing: a bad line found afterwards would lose the whole run.
    previous = previous_results(read_results(results), info["machine"])
    medians = _time_all(selected, args.repeat, args.rebuild) if selected else {}
    for name, seconds in datasets.build_seconds.items():
        print(f"dataset {name} built in {seconds:.1f} s (not counted)")
    _print_table(compare(medians, previous), info["machine"], bool(previous))
    if not args.no_save and medians:
        results.parent.mkdir(parents=True, exist_ok=True)
        with results.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({**info, "repeat": args.repeat, "results": medians}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
