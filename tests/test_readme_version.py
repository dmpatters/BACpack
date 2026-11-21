"""README's "Current release" line names the version in pyproject.toml.

Every release bumps both by hand, and the README line is the one that gets
forgotten."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_current_release_matches_pyproject():
    with open(ROOT / "pyproject.toml", "rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    named = re.findall(r"Current release: \*\*`([^`]+)`\*\*", readme)
    assert named == [version], (
        f"README.md's 'Current release' line says {named}, pyproject.toml says {version}: "
        "update the README line"
    )
