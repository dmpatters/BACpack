"""scripts/make_upgrade_fixtures.py builds a complete set."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import make_upgrade_fixtures as generator  # noqa: E402

from bacpack import models  # noqa: E402


def test_generator_builds_a_complete_set(tmp_path):
    out = tmp_path / "set"
    manifest = generator.build_set(out, provisional=True)

    names = {p.name for p in out.iterdir()}
    assert {
        "bacpack.db", "bacpack-history.db", "secret.key", "config-export.json",
        "points.csv", "manifest.json", manifest["backup_device"]["file"],
    } == names
    assert set(manifest["object_types"]) == set(models.OBJECT_TYPES)
    assert len(manifest["devices"]) >= 2
    assert any(d["bind_address"] or d["network_role"] != "normal" for d in manifest["devices"])
    assert {c["type"] for c in manifest["connectors"]} == {"http-poll", "bacnet"}
    assert {t["mode"] for t in manifest["trend_logs"]} == {"interval", "cov"}
    assert all(t["samples"] > 0 for t in manifest["trend_logs"])
    assert manifest["provisional"] is True
    assert sum(p.stat().st_size for p in out.iterdir()) < 1024 * 1024
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    for file_name, key in (("bacpack.db", "main_schema_version"), ("bacpack-history.db", "history_schema_version")):
        with closing(sqlite3.connect(out / file_name)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == manifest[key]


def test_two_runs_write_the_same_exports(tmp_path):
    first = generator.build_set(tmp_path / "a")
    second = generator.build_set(tmp_path / "b")
    for name in ("config-export.json", "points.csv", first["backup_device"]["file"]):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert json.loads((tmp_path / "a" / "config-export.json").read_text(encoding="utf-8"))["version"] == first["config_version"]
