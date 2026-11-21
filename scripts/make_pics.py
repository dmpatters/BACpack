"""Regenerate PICS.md, BACpack's conformance statement (ASHRAE 135 Annex A),
from the code.

Rerun after any change to services, object types, properties or write rules,
and at each release (the version line changes); tests/test_pics.py fails
until PICS.md matches. Needs the editable install (`pip install -e .`), whose
metadata supplies the version.
Usage: python scripts/make_pics.py"""
from __future__ import annotations

import sys
from pathlib import Path

from bacpack.pics import render_pics

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "PICS.md"


def write_pics(path: Path) -> None:
    # newline="\n": the committed file and the generator's text must match
    # byte for byte on Windows too.
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        out.write(render_pics())


def main(argv: list[str] | None = None) -> int:
    write_pics(TARGET)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
