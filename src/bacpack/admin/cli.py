"""The bacpack-admin entry point: opens the database the way the web process
does (BACPACK_DB_PATH), logs to the Logs page, and hands each command an
AdminContext. A command module exposes `register(subparsers)` and puts a
handler `(args, context) -> int` in `set_defaults(handler=...)`; one with
status lines also has `status_lines(context)`, which `status` prints in
COMMAND_MODULES order.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TextIO

from ..db import get_db_path, init_db, make_engine, make_session_factory, sqlite_version_problem
from ..log_capture import logs_page_logging
from ..migrations import DatabaseTooNew
from . import login_commands, status_command, web_commands

COMMAND_MODULES = (login_commands, web_commands, status_command)


@dataclass
class AdminContext:
    session_factory: Callable
    data_dir: Path
    prompt: Callable[[str], str]
    environ: Mapping[str, str]
    out: TextIO
    command_modules: tuple = COMMAND_MODULES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bacpack-admin", description="Local BACpack administration.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in COMMAND_MODULES:
        module.register(subparsers)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] = os.environ,
    out: TextIO | None = None,
) -> int:
    stream: TextIO = out if out is not None else sys.stdout
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    problem = sqlite_version_problem()
    if problem is not None:
        print(problem, file=stream)
        return 1
    db_path = environ.get("BACPACK_DB_PATH") or get_db_path()
    engine = make_engine(db_path)
    try:
        # The installer stops the service first; run by hand against a running
        # install, the schema is already current and this is a no-op.
        try:
            init_db(engine)
        except DatabaseTooNew as exc:
            print(str(exc), file=stream)
            return 2
        session_factory = make_session_factory(engine)
        with logs_page_logging(session_factory):
            return args.handler(args, AdminContext(session_factory, Path(db_path).resolve().parent, prompt, environ, stream))
    finally:
        engine.dispose()
