"""Service lifecycle constants and the process exit the lifecycle uses."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import history_writer

SERVICE_ENV = "BACPACK_SERVICE"

# Non-zero, so systemd's Restart=on-failure and WinSW's onfailure restart
# the web process; devices are left running and reattached.
RESTART_UI_EXIT_CODE = 75
# packaging/bacpack.service lists it in RestartPreventExitStatus: restarting
# can't fix an old SQLite.
SQLITE_TOO_OLD_EXIT_CODE = 78
# The launcher found a certificate pair that is unusable or that the TLS
# library refuses. packaging/bacpack.service lists it in
# RestartPreventExitStatus: restarting cannot fix it.
WEB_CONFIG_BROKEN_EXIT_CODE = 79
# The saved listen address is not on this machine (yet, after a boot). Not in
# RestartPreventExitStatus: the next attempt may find the network up.
ADDRESS_MISSING_EXIT_CODE = 80
# The database was saved by a newer BACpack.
# packaging/bacpack.service lists it in RestartPreventExitStatus: restarting
# cannot fix it.
DATABASE_TOO_NEW_EXIT_CODE = 81
# What a restart cannot fix: systemd is told so by RestartPreventExitStatus;
# on Windows serve.main stops its own service.
NOT_RETRIED_EXIT_CODES = (SQLITE_TOO_OLD_EXIT_CODE, WEB_CONFIG_BROKEN_EXIT_CODE, DATABASE_TOO_NEW_EXIT_CODE)

# Set by `python -m bacpack serve` in its own environment, before uvicorn
# starts in the same process: the app reads them to know it runs HTTPS-only
# and what it was started with.
HTTPS_ENV = "BACPACK_HTTPS"
RUNNING_ADDRESS_ENV = "BACPACK_RUNNING_ADDRESS"
RUNNING_PORT_ENV = "BACPACK_RUNNING_PORT"
RUNNING_FINGERPRINT_ENV = "BACPACK_RUNNING_FINGERPRINT"


@dataclass(frozen=True)
class RunningWeb:
    address: str
    port: int
    fingerprint: str


def service_mode() -> bool:
    """True when a service manager runs this process and will restart it."""
    return os.environ.get(SERVICE_ENV) == "1"


def https_only() -> bool:
    """True under the launcher, which serves nothing but HTTPS."""
    return os.environ.get(HTTPS_ENV) == "1"


def mark_running(address: str, port: int, fingerprint: str) -> None:
    os.environ[HTTPS_ENV] = "1"
    os.environ[RUNNING_ADDRESS_ENV] = address
    os.environ[RUNNING_PORT_ENV] = str(port)
    os.environ[RUNNING_FINGERPRINT_ENV] = fingerprint


def running_web() -> RunningWeb | None:
    """What the launcher started this process with; None when it did not."""
    address = os.environ.get(RUNNING_ADDRESS_ENV)
    port = os.environ.get(RUNNING_PORT_ENV)
    fingerprint = os.environ.get(RUNNING_FINGERPRINT_ENV)
    if not address or not fingerprint or not port or not port.isdigit():
        return None
    return RunningWeb(address, int(port), fingerprint)


def exit_process(code: int) -> None:
    """Leave at once, skipping the lifespan shutdown and multiprocessing's
    exit-time join of device children. Tests replace this attribute."""
    history_writer.flush_current()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
