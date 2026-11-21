"""The unit the scale tests and the benchmark share: a named action on a
dataset. Tests assert bounds on a scenario's
result; the benchmark times it, so the two can't measure different things."""
from __future__ import annotations

import importlib
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from bacpack.db import make_engine, make_session_factory

from .datasets import DatasetIds

AREAS = ("trends", "devices", "history", "uptime", "event_loop")
_AREA_MODULES = tuple(f"tests.scale.scenarios_{area}" for area in AREAS)


@dataclass
class Context:
    db_path: Path
    session_factory: Any
    engine: Any
    ids: DatasetIds
    tmp_path: Path
    # Whatever a scenario's setup prepared (an application, a client).
    state: dict = field(default_factory=dict)
    _closers: list = field(default_factory=list)

    def on_close(self, closer: Callable[[], Any]) -> None:
        self._closers.append(closer)

    def close(self) -> None:
        while self._closers:
            self._closers.pop()()
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


@dataclass(frozen=True)
class Scenario:
    name: str
    area: str
    dataset: Literal["install", "full_history"]
    writes: bool
    run: Callable[[Context], Any]
    # Untimed preparation, run by `execute` before the timed action.
    setup: Callable[[Context], Any] | None = None


SCENARIOS: dict[str, Scenario] = {}


def scenario(
    name: str,
    *,
    area: str,
    dataset: Literal["install", "full_history"],
    writes: bool = False,
    setup: Callable[[Context], Any] | None = None,
):
    def register(run):
        if name in SCENARIOS:
            raise ValueError(f"scenario {name!r} is already registered")
        SCENARIOS[name] = Scenario(name, area, dataset, writes, run, setup)
        return run

    return register


def make_context(db_path: Path, tmp_path: Path, ids: DatasetIds) -> Context:
    engine = make_engine(db_path)
    return Context(db_path, make_session_factory(engine), engine, ids, tmp_path)


def load_all() -> None:
    """Import every area's scenario module so the benchmark sees them
    without importing test files."""
    for module in _AREA_MODULES:
        importlib.import_module(module)


async def execute(s: Scenario, ctx: Context) -> tuple[float, Any]:
    if s.setup is not None:
        prepared = s.setup(ctx)
        if inspect.isawaitable(prepared):
            await prepared
    start = time.perf_counter()
    result = s.run(ctx)
    if inspect.isawaitable(result):
        result = await result
    return time.perf_counter() - start, result
