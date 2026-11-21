"""Time goes through timeutil.py and device_clock.py. Scans every other module
under src/bacpack for the raw primitives those two replace. Exemptions are
whole files only. A bare time.time reference counts too: simulators read the
host clock through timeutil.host_seconds (they stay on host time by design)."""
from __future__ import annotations

import ast
import pathlib

import bacpack

_SRC = pathlib.Path(bacpack.__file__).parent
_EXEMPT = {_SRC / "timeutil.py", _SRC / "device_clock.py"}

_DATETIME_CALLS = {"now", "utcnow", "fromtimestamp", "utcfromtimestamp"}
_BACNET_DATE_TYPES = {"Date", "DateTime", "Time"}
_TIME_FUNCS = {"time", "localtime", "gmtime", "mktime"}


def _bare_hint(name: str) -> str:
    """host_seconds stands in for time.time only."""
    return "use timeutil.host_seconds or device_clock" if name == "time" else "use timeutil or device_clock"


def raw_time_uses(source: str) -> list:
    """(line, what) for each raw time primitive in one module's source."""
    tree = ast.parse(source)
    datetime_modules, datetime_classes, date_classes, time_modules, time_funcs = set(), set(), set(), set(), set()
    bacnet_types, bacpypes3_modules = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "datetime":
                    datetime_modules.add(bound)
                elif alias.name == "time":
                    time_modules.add(bound)
                elif alias.name.startswith("bacpypes3"):
                    bacpypes3_modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if module == "datetime" and alias.name == "datetime":
                    datetime_classes.add(bound)
                elif module == "datetime" and alias.name == "date":
                    date_classes.add(bound)
                elif module == "time" and alias.name in _TIME_FUNCS:
                    time_funcs.add(bound)
                elif module.startswith("bacpypes3") and alias.name in _BACNET_DATE_TYPES:
                    bacnet_types.add(bound)
                elif module.startswith("bacpypes3") and alias.name[:1].islower():
                    bacpypes3_modules.add(bound)

    def is_datetime_class(expr) -> bool:
        return (isinstance(expr, ast.Name) and expr.id in datetime_classes) or (
            isinstance(expr, ast.Attribute)
            and expr.attr == "datetime"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in datetime_modules
        )

    def is_date_class(expr) -> bool:
        return (isinstance(expr, ast.Name) and expr.id in date_classes) or (
            isinstance(expr, ast.Attribute)
            and expr.attr == "date"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in datetime_modules
        )

    def is_bacpypes3_module(expr) -> bool:
        while isinstance(expr, ast.Attribute):
            expr = expr.value
        return isinstance(expr, ast.Name) and expr.id in bacpypes3_modules

    def has_wildcard_tuple(call) -> bool:
        return bool(
            call.args
            and isinstance(call.args[0], ast.Tuple)
            and any(isinstance(e, ast.Constant) and e.value == 255 for e in call.args[0].elts)
        )

    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and id(node) not in called:
            if node.attr in _TIME_FUNCS and isinstance(node.value, ast.Name) and node.value.id in time_modules:
                found.append((node.lineno, f"time.{node.attr} -- {_bare_hint(node.attr)}"))
        elif isinstance(node, ast.Name) and id(node) not in called and node.id in time_funcs:
            found.append((node.lineno, f"time.{node.id} -- {_bare_hint(node.id)}"))
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr in _DATETIME_CALLS and is_datetime_class(func.value):
                    found.append((node.lineno, f"datetime.{func.attr}() -- use timeutil"))
                elif func.attr == "today" and (is_date_class(func.value) or is_datetime_class(func.value)):
                    found.append((node.lineno, f"{func.attr}() -- use timeutil"))
                elif func.attr == "fromtimestamp" and is_date_class(func.value):
                    found.append((node.lineno, "date.fromtimestamp() -- use timeutil"))
                elif func.attr == "astimezone" and not node.args and not node.keywords:
                    found.append((node.lineno, "astimezone() with no zone -- use timeutil.host_local"))
                elif func.attr in _TIME_FUNCS and isinstance(func.value, ast.Name) and func.value.id in time_modules:
                    found.append((node.lineno, f"time.{func.attr}() -- use timeutil or device_clock"))
                elif func.attr in _BACNET_DATE_TYPES and is_bacpypes3_module(func.value) and has_wildcard_tuple(node):
                    found.append((node.lineno, f"{func.attr}((... 255 ...)) -- use timeutil.wildcard_* / bacnet_date_fields"))
            elif isinstance(func, ast.Name):
                if func.id in time_funcs:
                    found.append((node.lineno, f"time.{func.id}() -- use timeutil or device_clock"))
                elif func.id in bacnet_types and has_wildcard_tuple(node):
                    found.append((node.lineno, f"{func.id}((... 255 ...)) -- use timeutil.wildcard_* / bacnet_date_fields"))
            for keyword in node.keywords:
                if keyword.arg == "tzinfo":
                    found.append((node.lineno, "tzinfo= -- use timeutil.to_db/from_db/host_local"))
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, (ast.Add, ast.Sub))
            and any(isinstance(side, ast.Constant) and side.value == 1900 for side in (node.left, node.right))
        ):
            found.append((node.lineno, "year +/- 1900 -- use timeutil.bacnet_year/python_year"))
    return found


def test_scan_catches_each_raw_primitive():
    source = (
        "import datetime as dt\n"  # 1
        "import time as _time\n"  # 2
        "from datetime import datetime, date\n"  # 3
        "from time import time\n"  # 4
        "from bacpypes3.primitivedata import Date\n"  # 5
        "from bacpypes3.basetypes import DateTime\n"  # 6
        "a = dt.datetime.now(dt.timezone.utc)\n"  # 7
        "b = datetime.utcnow()\n"  # 8
        "c = dt.datetime.fromtimestamp(0)\n"  # 9
        "d = date.today()\n"  # 10
        "e = _time.time()\n"  # 11
        "f = time()\n"  # 12
        "g = x.replace(tzinfo=None)\n"  # 13
        "h = year + 1900\n"  # 14
        "i = Date((255, 1, 1, 255))\n"  # 15
        "j = DateTime.now()\n"  # 16: bacpypes3's own clock, allowed
        "k = _time.monotonic()\n"  # 17: a timer, allowed
        "m = Date((126, 1, 1, 0))\n"  # 18: no wildcard, allowed
        "n = dt.timedelta(days=1)\n"  # 19: allowed
        "o = datetime.today()\n"  # 20
        "p = date.fromtimestamp(0)\n"  # 21
        "q = when.astimezone()\n"  # 22: host zone
        "r = when.astimezone(zone)\n"  # 23: explicit zone, allowed
        "s = _time.localtime()\n"  # 24
        "t = _time.gmtime(0)\n"  # 25
        "u = _time.mktime(parts)\n"  # 26
        "import bacpypes3.primitivedata as pd\n"  # 27
        "v = pd.Date((255, 1, 1, 255))\n"  # 28
        "from time import localtime\n"  # 29
        "w = localtime()\n"  # 30
        "x = _time.time\n"  # 31: a bare reference (e.g. a default clock)
        "y = clock or time\n"  # 32: the from-imported function, uncalled
    )
    assert sorted(line for line, _ in raw_time_uses(source)) == [
        7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 21, 22, 24, 25, 26, 28, 30, 31, 32
    ]


def test_a_bare_reference_suggests_host_seconds_only_for_time_time():
    source = "import time\nclock = time.time\nparts = time.localtime\n"
    assert raw_time_uses(source) == [
        (2, "time.time -- use timeutil.host_seconds or device_clock"),
        (3, "time.localtime -- use timeutil or device_clock"),
    ]


def test_no_raw_time_primitives_outside_the_helpers():
    problems = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _EXEMPT:
            continue
        for line, what in raw_time_uses(path.read_text(encoding="utf-8")):
            problems.append(f"  {path.relative_to(_SRC).as_posix()}:{line}: {what}")
    problems.sort()
    assert not problems, (
        "raw time handling outside timeutil.py/device_clock.py -- use a helper there:\n"
        + "\n".join(problems)
    )
