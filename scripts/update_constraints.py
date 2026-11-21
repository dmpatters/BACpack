"""Regenerate packaging/constraints.txt: every runtime dependency of BACpack,
pinned. Run with Python 3.13 on one platform with --fresh (which drops every
existing pin, so a removed dependency goes too), then on the other without it
(which merges that platform's resolution in; a constraint for a package a
platform doesn't install is ignored there).
Usage: python scripts/update_constraints.py [--fresh]"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packaging" / "constraints.txt"
HEADER = (
    "# Exact versions of every BACpack runtime dependency, for both installers\n"
    "# and the Windows bundle. Regenerate with scripts/update_constraints.py --fresh\n"
    "# on one of Windows and Linux, then without it on the other.\n"
)
SKIP = {"bacpack", "pip", "setuptools", "wheel"}


def _resolve() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        # pip builds a local project in place; a copy keeps build/ and
        # *.egg-info out of the checkout (and away from a syncing folder).
        source = Path(tmp) / "source"
        shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(ROOT / name, source / name)
        env_dir = Path(tmp) / "venv"
        venv.create(env_dir, with_pip=True)
        python = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "-q", str(source)], check=True)
        frozen = subprocess.run(
            [str(python), "-m", "pip", "freeze", "--all"], check=True, capture_output=True, text=True
        ).stdout
    pins = {}
    for line in frozen.splitlines():
        name, sep, version = line.partition("==")
        if sep and name.lower() not in SKIP:
            pins[name.lower()] = version
    return pins


def _existing() -> dict:
    if not TARGET.exists():
        return {}
    pins = {}
    for line in TARGET.read_text(encoding="utf-8").splitlines():
        name, sep, version = line.partition("==")
        if sep and not line.startswith("#"):
            pins[name.lower()] = version
    return pins


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--fresh"]):
        raise SystemExit("usage: python scripts/update_constraints.py [--fresh]")
    kept = {} if args == ["--fresh"] else _existing()
    merged = {**kept, **_resolve()}
    body = "".join(f"{name}=={merged[name]}\n" for name in sorted(merged))
    TARGET.write_text(HEADER + body, encoding="utf-8", newline="\n")
    print(f"wrote {len(merged)} pins to {TARGET}")


if __name__ == "__main__":
    main()
