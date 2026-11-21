"""A log line (or traffic entry) that names an object, device or connector
writes it through log_names.py, so the diagnostics file can find and replace
the name even after it was renamed or deleted.

Two scans of every module under src/bacpack:

- a logging call (`logger.warning`, `self.logger`, `logging.getLogger(...)`,
  `logger.log`, `traffic_log.add`, a client's `self._log`) whose arguments
  carry a name, directly or through a variable assigned from one in the same
  function;
- any f-string, %-format or str.format that puts a name into a string, in a
  module not on the allowlist below. A pre-built message, a local helper that
  returns one, or an exception text that reaches a log all start as one.

"A name" is a `.name` / `.objectName` / `.object_name` / `.device_name` /
`.connector_name` attribute, `["name"]`, `getattr(x, "objectName")` or a
variable called name/object_name/device_name/connector_name, outside a call to
a log_names helper (`log_names.object_ref(...)` or a name imported from it).
"""
from __future__ import annotations

import ast
import pathlib

import bacpack

_SRC = pathlib.Path(bacpack.__file__).parent

_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"}
_LOGGER_RECEIVERS = {"logger", "log", "_log", "self.logger", "self.log"}
_NAME_ATTRS = {"name", "objectName", "object_name", "device_name", "connector_name"}
_NAME_VARIABLES = {"name", "object_name", "device_name", "connector_name"}
_HELPERS = {"quote", "object_ref", "device_ref", "connector_ref", "live_object_ref"}

# Modules whose f-strings may hold a name, with the reason. A path ending in "/"
# is a package.
ALLOWED_FORMATTING = {
    "log_names.py": "the helper itself",
    "migrations.py": "SQL identifiers (table and column names), not BACnet names",
    "db.py": "SQL identifiers (table names)",
    "models.py": "__repr__ of ORM rows",
    "web/": "API response bodies, file names and Content-Disposition headers",
    "login_store.py": "the API key name-clash message returned to the user; `name` is a key name, not a BACnet name",
    "config_connectors.py": "connector import messages returned to the user",
    "config_io.py": "config import messages returned to the user",
    "connector_config.py": "connector rule messages returned by the API and the import",
    "csv_io.py": "CSV import messages returned to the user",
    "clone_plan.py": "clone plan notes shown in the UI",
    "ede_plan.py": "EDE import plan notes and skip reasons shown in the UI",
    "ede_io.py": "EDE file read notes and errors returned to the user",
    "object_config.py": "save/validation messages (API bodies and apply_error for the current config)",
    "objects/reserved_names.py": "name-clash messages from check_device (API bodies), and the Trend Log name rule",
    "device_network.py": "network link validation messages returned by the API and the import",
    "objects/alarm_config.py": "`name` is a property name, not an object name",
    "objects/trend_log.py": "the Trend Log's Description property value",
    "diagnostics.py": "the diagnostics file itself (summary lines, stand-in names)",
    "diagnostics_standins.py": "URL query parameter names, not BACnet names",
}


# (module, source text) -> reason, for a single flagged expression.
ALLOWED_EXPRESSIONS = {
    ("live_config.py", "problem"): "check_device's validation text (see object_config.py above), for the current config",
}


def _allowed(relative: str) -> bool:
    return any(relative == entry or (entry.endswith("/") and relative.startswith(entry)) for entry in ALLOWED_FORMATTING)


def _receiver(func: ast.Attribute) -> str:
    return ast.unparse(func.value)


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = _receiver(func)
    if func.attr in _LEVELS and (receiver in _LOGGER_RECEIVERS or receiver.startswith("logging.getLogger(")):
        return True
    if func.attr == "add" and receiver.split(".")[-1] == "traffic_log":
        return True  # TrafficLog.add(event_type, detail) is a log too
    return func.attr == "_log" and receiver == "self"  # BACnetClient._log writes a traffic entry


def _log_names_imports(tree: ast.AST) -> set:
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "log_names":
            bound.update(alias.asname or alias.name for alias in node.names if alias.name in _HELPERS)
    return bound


def _helper_call(node: ast.AST, imported: set) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return isinstance(func.value, ast.Name) and func.value.id == "log_names" and func.attr in _HELPERS
    return isinstance(func, ast.Name) and func.id in imported


def _names_outside_helpers(node: ast.AST, imported: set = frozenset()):  # pyright: ignore[reportArgumentType]
    if _helper_call(node, imported):
        return
    if isinstance(node, ast.Attribute) and node.attr in _NAME_ATTRS:
        yield node
    elif isinstance(node, ast.Name) and node.id in _NAME_VARIABLES:
        yield node
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "name":
        yield node
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in _NAME_ATTRS
    ):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _names_outside_helpers(child, imported)


def _tainted_variables(tree: ast.AST, imported: set) -> dict:
    """scope (the module, or a function) -> variables assigned from a name inside it."""
    out = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            tainted = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and any(True for _ in _names_outside_helpers(node.value, imported)):
                    tainted.update(t.id for t in node.targets if isinstance(t, ast.Name))
            out[func] = tainted
    return out


def _formatted_parts(node: ast.AST):
    """The expressions a string-formatting node interpolates."""
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                yield value.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        yield node.right
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        yield from node.args
        yield from (k.value for k in node.keywords)


def unformatted_names(source: str, *, check_formatting: bool = True) -> list:
    """(line, source text) for each name that reaches a logging call, or (with
    check_formatting) a formatted string, outside a log_names helper."""
    tree = ast.parse(source)
    imported = _log_names_imports(tree)
    tainted = _tainted_variables(tree, imported)
    found = {}
    for func, variables in tainted.items():
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and _is_log_call(node):
                for arg in [*node.args, *(k.value for k in node.keywords if k.arg != "extra")]:
                    for name in _names_outside_helpers(arg, imported):
                        found[(name.lineno, ast.unparse(name))] = None
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and sub.id in variables:
                            found[(sub.lineno, sub.id)] = None
    if check_formatting:
        for node in ast.walk(tree):
            for part in _formatted_parts(node):
                for name in _names_outside_helpers(part, imported):
                    found[(name.lineno, ast.unparse(name))] = None
    return sorted(found)


def test_the_detector_flags_the_ways_a_name_gets_into_a_message():
    cases = {
        'logger.warning("x %s", obj.objectName)': 1,
        'logger.info(f"x {row.name} y")': 1,
        'logger.error("x %s", rows[i]["name"])': 1,
        'self.logger.warning("x %s", obj.objectName)': 1,
        'logging.getLogger(__name__).warning("x %s", obj.objectName)': 1,
        'logger.log(logging.WARNING, "x %s", obj.objectName)': 1,
        'logger.warn("x %s", obj.objectName)': 1,
        'logger.warning("x %s", getattr(obj, "objectName"))': 1,
        'label = obj.objectName\nlogger.warning("x %s", label)': 1,
        'def f(o):\n    return f"{o.objectName} ({o.objectIdentifier})"': 1,
        'msg = f"object {obj.name} failed"\nlogger.warning(msg)': 2,
        'raise ValueError(f"object {name!r} bad")': 1,
        'text = "object %s" % obj.name': 1,
        'text = "object {}".format(obj.name)': 1,
        'self._log("client-read", f"{binding.address} {obj.objectName}")': 1,
        'app.traffic_log.add("schedule-write", f"{self.objectName} released")': 1,
        'from .log_names import quote\nlogger.warning("%s %s", quote(name), name)': 1,
        'def quote(s):\n    return s\nlogger.warning("x %s", quote(obj.objectName))': 1,
        'logger.warning("x %s", other.quote(name))': 1,
    }
    for source, expected in cases.items():
        assert len(unformatted_names(source)) == expected, source


def test_the_detector_accepts_the_helpers_and_ignores_extra():
    assert unformatted_names('logger.warning("x %s", log_names.object_ref(t, i, row["name"]))') == []
    assert unformatted_names('from .log_names import object_ref as ref\nlogger.warning("x %s", ref(t, i, name))') == []
    assert unformatted_names('logger.info("x", extra={"name": name})') == []
    assert unformatted_names('logger.info("x %s", f"{log_names.live_object_ref(obj)}")') == []


def test_the_detector_catches_the_sites_that_were_converted():
    old_label = 'def _label(obj):\n    return f"{obj.objectName!s} ({obj.objectIdentifier[0]}:{obj.objectIdentifier[1]})"'
    old_error = "def _object_error(row, exc):\n    return f\"object {row['name']!r} ({row['object_type']}): {exc}\""
    old_summary = 'names["removed"].append(self._rows[i]["name"])\nlogger.info("device %d: %s", d, _changes_summary(names))'
    old_sim = 'self._log_once(object_id, f"device {self.device_id}: {name} is a BACnet proxy right now")'
    for source in (old_label, old_error, old_sim):
        assert unformatted_names(source), source
    # a bare append of a name is not a message yet; it is caught where it is formatted
    assert unformatted_names(old_summary) == []


def test_no_log_call_names_an_object_device_or_connector_outside_log_names():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative == "log_names.py":
            continue
        for line, what in unformatted_names(path.read_text(encoding="utf-8"), check_formatting=not _allowed(relative)):
            if (relative, what) not in ALLOWED_EXPRESSIONS:
                offenders.append(f"{relative}:{line}: {what}")
    assert offenders == [], "name these through bacpack.log_names:\n" + "\n".join(offenders)


def test_every_allowlist_entry_names_a_module_that_exists():
    for entry in ALLOWED_FORMATTING:
        assert (_SRC / entry).exists(), entry
