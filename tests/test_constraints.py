"""packaging/constraints.txt pins every runtime dependency, so both
installers and the Windows bundle install a tested set."""
from __future__ import annotations

import re
from importlib.metadata import requires
from pathlib import Path

CONSTRAINTS = Path(__file__).resolve().parents[1] / "packaging" / "constraints.txt"


def _pins():
    pins = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        assert version, f"not an exact pin: {line}"
        pins[re.sub(r"[-_.]+", "-", name).lower()] = version
    return pins


def test_every_direct_dependency_is_pinned():
    pins = _pins()
    for requirement in requires("bacpack"):
        if "extra ==" in requirement:
            continue
        name = re.split(r"[\s<>=!\[;]", requirement, maxsplit=1)[0]
        assert re.sub(r"[-_.]+", "-", name).lower() in pins, f"{name} is not in constraints.txt"


def test_uvicorn_has_no_standard_extras():
    assert not any("uvicorn[" in r for r in requires("bacpack"))


def _update_constraints(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "update_constraints", CONSTRAINTS.parents[1] / "scripts" / "update_constraints.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "constraints.txt"
    target.write_text("# header\nstale==2.0\nshared==1.0\n", encoding="utf-8")
    monkeypatch.setattr(module, "TARGET", target)
    monkeypatch.setattr(module, "_resolve", lambda: {"shared": "1.1", "fresh": "3.0"})
    return module, target


def test_update_constraints_merges_into_the_other_platforms_pins(tmp_path, monkeypatch):
    module, target = _update_constraints(tmp_path, monkeypatch)
    module.main([])
    assert target.read_text(encoding="utf-8").endswith("fresh==3.0\nshared==1.1\nstale==2.0\n")


def test_update_constraints_fresh_drops_a_removed_dependency(tmp_path, monkeypatch):
    module, target = _update_constraints(tmp_path, monkeypatch)
    module.main(["--fresh"])
    text = target.read_text(encoding="utf-8")
    assert "stale" not in text
    assert text.endswith("fresh==3.0\nshared==1.1\n")
