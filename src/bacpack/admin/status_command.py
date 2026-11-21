"""`status`: every command module's status lines, in COMMAND_MODULES order."""
from __future__ import annotations


def register(subparsers) -> None:
    status = subparsers.add_parser("status", help="show the login state, listen address and certificate")
    status.set_defaults(handler=_status)


def _status(args, context) -> int:
    for module in context.command_modules:
        lines = getattr(module, "status_lines", None)
        if lines is not None:
            for line in lines(context):
                print(line, file=context.out)
    return 0
