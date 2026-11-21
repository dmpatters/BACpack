"""THIRD-PARTY-NOTICES.txt keeps up with the pins and ships in the bundle;
regenerate it with scripts/third_party_notices.py."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "THIRD-PARTY-NOTICES.txt"


def _pins():
    for line in (ROOT / "packaging" / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, version = line.split("==")
            yield name, version


def test_every_pinned_dependency_is_listed_at_its_pin():
    listed = {
        line.split(":")[0].lower()
        for line in NOTICES.read_text(encoding="utf-8").splitlines()
        if ": " in line
    }
    stale = [f"{n}=={v}" for n, v in _pins() if f"{n} {v}".lower() not in listed]
    assert not stale, f"rerun scripts/third_party_notices.py for {stale}"


def test_the_windows_bundle_ships_the_notices():
    text = (ROOT / "packaging" / "windows" / "build_bundle.ps1").read_text(encoding="utf-8")
    assert "THIRD-PARTY-NOTICES.txt" in text
