"""A name another BACpack module imports doesn't start with an underscore,
which says "private to this module". Modules are exempt: web/api/_lookups.py
and the like are private to their package by design. Tests may still import a
module's private names."""
from __future__ import annotations

import ast
import pathlib

import bacpack

_SRC = pathlib.Path(bacpack.__file__).parent


def _package_dir(path: pathlib.Path, node: ast.ImportFrom) -> pathlib.Path:
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
    else:
        base = _SRC.parent
    for part in (node.module or "").split("."):
        if part:
            base = base / part
    return base


def private_imports(path: pathlib.Path) -> list:
    """(line, imported name) for each underscore name `path` imports from
    another BACpack module."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.level and not (node.module or "").startswith("bacpack"):
            continue
        target = _package_dir(path, node)
        for alias in node.names:
            name = alias.name
            if not name.startswith("_") or name.startswith("__"):
                continue
            if (target / f"{name}.py").exists() or (target / name).is_dir():
                continue  # a module, not a name in one
            found.append((node.lineno, name))
    return found


def test_no_module_imports_another_modules_private_name():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if "spa" in path.relative_to(_SRC).parts:
            continue
        for line, name in private_imports(path):
            offenders.append(f"{path.relative_to(_SRC)}:{line} imports {name}")
    assert not offenders, "drop the underscore from a name other modules use:\n" + "\n".join(offenders)


def test_the_guard_sees_a_private_import(tmp_path):
    module = tmp_path / "pkg" / "user.py"
    module.parent.mkdir()
    (module.parent / "_helpers.py").write_text("x = 1\n", encoding="utf-8")
    module.write_text(
        "from .other import _secret, public\nfrom . import _helpers\nfrom .x import __version__\n",
        encoding="utf-8",
    )
    assert private_imports(module) == [(1, "_secret")]
