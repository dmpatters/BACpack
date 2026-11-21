"""No hidden import cycle: a BACpack import inside a function or class body
is refused, and the top-level imports form no cycle. `db` imports `models`
inside four functions, and `models` imports `db.Base`: the usual SQLAlchemy
layout, allowed by name."""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

import bacpack

_SRC = pathlib.Path(bacpack.__file__).parent

# models imports db.Base, the usual SQLAlchemy layout. migrations shares
# init_db's `from . import migrations, models` line; the count is pinned
# below so the allowance can't grow.
_ALLOWED = {("db.py", "bacpack.models"), ("db.py", "bacpack.migrations")}

_OBJECT_CLASS_MODULES = {
    "bacpack.bacnet",
    "bacpack.objects.alarm_config",
    "bacpack.objects.calendar",
    "bacpack.objects.file",
    "bacpack.objects.network_port",
    "bacpack.objects.notification_class",
    "bacpack.objects.trend_log",
    "bacpack.objects.trend_log_reporting",
    "bacpack.objects.schedule",
    "bacpack.objects.multistate",
    "bacpack.objects.device",
}


def _source_files() -> list:
    return [p for p in sorted(_SRC.rglob("*.py")) if "spa" not in p.relative_to(_SRC).parts]


def _module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(_SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_module(name: str) -> bool:
    base = _SRC.parent.joinpath(*name.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def _targets(path: pathlib.Path, node: ast.AST) -> list:
    """Dotted names of the BACpack modules this import statement loads,
    with the packages it runs on the way (a submodule's own packages are
    already loading, and bacpack's __init__ imports nothing of ours)."""
    found = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("bacpack."):
                found.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            package = _module_name(path).split(".")
            if path.name != "__init__.py":
                package = package[:-1]
            if node.level > 1:
                package = package[: len(package) - (node.level - 1)]
            base = ".".join(package + ([node.module] if node.module else []))
        else:
            base = node.module or ""
            if not base.startswith("bacpack"):
                return []
        if _is_module(base):
            found.append(base)
        for alias in node.names:
            if _is_module(f"{base}.{alias.name}"):
                found.append(f"{base}.{alias.name}")
    own = _module_name(path)
    loaded = set()
    for target in found:
        parts = target.split(".")
        for end in range(2, len(parts) + 1):
            name = ".".join(parts[:end])
            if _is_module(name) and not (own == name or own.startswith(name + ".")):
                loaded.add(name)
    return sorted(loaded)


def _top_level_statements(body: list):
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _top_level_statements(stmt.body)
            yield from _top_level_statements(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _top_level_statements(stmt.body)
            for handler in stmt.handlers:
                yield from _top_level_statements(handler.body)
            yield from _top_level_statements(stmt.orelse)
            yield from _top_level_statements(stmt.finalbody)


def _local_imports(allowed: bool) -> list:
    found = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = {id(s) for s in _top_level_statements(tree.body)}
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in top:
                continue
            for target in _targets(path, node):
                if ((rel, target) in _ALLOWED) == allowed:
                    found.append(f"{rel}:{node.lineno} -> {target}")
    return sorted(found)


def local_imports() -> list:
    """`module:line -> target` for each BACpack import inside a function or
    class body."""
    return _local_imports(allowed=False)


def allowed_local_imports() -> list:
    return _local_imports(allowed=True)


def import_cycles() -> list:
    """Groups of modules that import each other at the top level, apart
    from the allowed pair."""
    graph = {}
    for path in _source_files():
        name = _module_name(path)
        edges = graph.setdefault(name, set())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in _top_level_statements(tree.body):
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                edges.update(t for t in _targets(path, stmt) if t != name)
    graph = {k: {t for t in v if t in graph} for k, v in graph.items()}
    graph.pop("bacpack.__main__", None)

    index = {}
    low = {}
    stack = []
    on_stack = set()
    groups = []
    counter = [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph[v]):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
    for v in sorted(graph):
        if v not in index:
            visit(v)
    return sorted(g for g in groups if g != ["bacpack.db", "bacpack.models"])


def test_no_function_local_bacpack_imports():
    assert local_imports() == [], (
        "import at the top of the module; a cycle it would close is fixed by moving "
        "the shared code into a module of its own"
    )


def test_top_level_imports_have_no_cycle():
    assert import_cycles() == []


@pytest.mark.parametrize(
    "module",
    [
        "bacpack.point_config",
        "bacpack.objects.calendar_config",
        "bacpack.objects.notification_class_config",
        "bacpack.objects.reserved_names",
        "bacpack.objects.trend_log_config",
        "bacpack.simulation",
        "bacpack.object_config",
    ],
)
def test_loads_no_object_classes(module):
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print('\\n'.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not _OBJECT_CLASS_MODULES & set(out)


def test_db_keeps_exactly_its_four_model_imports():
    statements = {entry.split(" -> ")[0] for entry in allowed_local_imports()}
    assert len(statements) == 4


def _fake_tree(tmp_path, monkeypatch, files: dict) -> None:
    root = tmp_path / "bacpack"
    for name, text in {"__init__.py": "", **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)


def test_the_guard_sees_a_cycle_through_a_package_init(tmp_path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch, {
        "top.py": "from .pkg import thing\n",
        "pkg/__init__.py": "from . import inner\n",
        "pkg/inner.py": "from ..top import x\n",
    })
    assert import_cycles() == [["bacpack.pkg", "bacpack.pkg.inner", "bacpack.top"]]


def test_the_guard_counts_the_package_a_submodule_import_runs(tmp_path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch, {
        "top.py": "from .pkg.leaf import y\n",
        "pkg/__init__.py": "from ..top import z\n",
        "pkg/leaf.py": "from . import sibling\n",
        "pkg/sibling.py": "",
    })
    assert import_cycles() == [["bacpack.pkg", "bacpack.top"]]


def test_the_guard_sees_a_local_import_and_a_cycle(tmp_path, monkeypatch):
    root = tmp_path / "bacpack"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "a.py").write_text("from . import b\n\ndef f():\n    from . import c\n", encoding="utf-8")
    (root / "b.py").write_text("from .a import f\n", encoding="utf-8")
    (root / "c.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    assert local_imports() == ["a.py:4 -> bacpack.c"]
    assert import_cycles() == [["bacpack.a", "bacpack.b"]]
