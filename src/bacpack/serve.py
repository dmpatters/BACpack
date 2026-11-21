"""`python -m bacpack serve`: the HTTPS-only web server the installed services
run. It reads the listen address from settings and
the certificate from <data dir>/tls/, making a self-signed one at the first
start, then runs uvicorn in this same process. The dev command
`uvicorn bacpack.web.app:app` stays plain HTTP.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, MutableMapping

import uvicorn

from . import lifecycle, timeutil, tls_certs, web_server, windows_service
from .db import get_db_path, init_db, make_engine, make_session_factory, sqlite_version_problem
from .log_capture import logs_page_logging
from .migrations import DatabaseTooNew
from .tls_certs import CertProblem

DEFAULT_PORT = 8000
# A saved address can come up after the web server at boot (DHCP, a slow link).
ADDRESS_WAIT_SECONDS = 120
ADDRESS_POLL_SECONDS = 2
# Longest wait for WinSW's Ctrl+C after asking for the service's own stop;
# under WinSW's 90 s stop timeout.
STOP_WAIT_SECONDS = 60
# Tests replace it.
wait_for_stop: Callable[[float], None] = time.sleep
APP = "bacpack.web.app:app"

logger = logging.getLogger("bacpack.serve")

USAGE = "usage: python -m bacpack serve [--port N]"


@dataclass(frozen=True)
class ServePlan:
    host: str
    port: int
    certfile: Path
    keyfile: Path
    fingerprint: str


class ServeRefused(Exception):
    """The web server must not start; the message is the whole explanation."""

    exit_code = lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE


class AddressMissing(ServeRefused):
    """The saved listen address is not on this machine; a retry may succeed."""

    exit_code = lifecycle.ADDRESS_MISSING_EXIT_CODE


class DatabaseTooNewRefused(ServeRefused):
    """The database files were saved by a newer BACpack; restarting cannot help."""

    exit_code = lifecycle.DATABASE_TOO_NEW_EXIT_CODE


def _refuse(message: str, kind: type[ServeRefused] = ServeRefused) -> ServeRefused:
    logger.critical(message)
    return kind(message)


def prepare(
    db_path: str,
    port: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ServePlan:
    data_dir = Path(db_path).resolve().parent
    engine = make_engine(db_path)
    try:
        # The new column of an upgrade has to exist before settings are read.
        try:
            init_db(engine)
        except DatabaseTooNew as exc:
            raise _refuse(str(exc), DatabaseTooNewRefused) from exc
        session_factory = make_session_factory(engine)
        with logs_page_logging(session_factory):
            deadline = monotonic() + ADDRESS_WAIT_SECONDS
            waiting = False
            while True:
                with session_factory() as session:
                    address = web_server.get_listen_address(session)
                try:
                    ipaddress.IPv4Address(address)
                except ValueError:
                    # Waiting cannot fix a value that is not an address.
                    raise _refuse(
                        f"The web server's saved listen address {address!r} is not an IPv4 address. "
                        "Run: bacpack-admin web --address 0.0.0.0"
                    )
                if web_server.check_listen_address(address) is None:
                    break
                if not waiting:
                    waiting = True
                    logger.warning(
                        "waiting up to %d s for the web server's listen address %s to come up on this machine",
                        ADDRESS_WAIT_SECONDS,
                        address,
                    )
                if monotonic() >= deadline:
                    raise _refuse(
                        f"The web server is set to listen on {address}, but this machine does not have that address. "
                        "It will keep trying. To listen on every address instead run: bacpack-admin web --address 0.0.0.0",
                        AddressMissing,
                    )
                sleep(ADDRESS_POLL_SECONDS)
            try:
                info, _ = web_server.ensure_certificate(data_dir)
            except CertProblem as exc:
                raise _refuse(str(exc))
            except OSError as exc:
                raise _refuse(
                    f"Cannot write the certificate files in {tls_certs.tls_dir(data_dir)}: {exc.strerror or exc}."
                )
            if info.not_after < timeutil.utc_now():
                logger.warning(
                    "the web server certificate expired on %s UTC; browsers will refuse it. "
                    "Install a new one on the Web Server page, or run: bacpack-admin web --self-signed",
                    f"{info.not_after:%Y-%m-%d}",
                )
            directory = tls_certs.tls_dir(data_dir)
            return ServePlan(
                address, port, directory / tls_certs.CERT_FILE, directory / tls_certs.KEY_FILE, info.fingerprint_sha256
            )
    finally:
        engine.dispose()


def _port_default(environ: Mapping[str, str]) -> int:
    value = environ.get("BACPACK_PORT", "")
    return int(value) if value.isdigit() else DEFAULT_PORT


def _service_exit_code(code: int, environ: Mapping[str, str], platform: str) -> int:
    """WinSW 2.12 restarts the service on every non-zero exit, and under the
    virtual account it hangs on an exit of 0 (reporting that stop needs a
    Service Control Manager access the account lacks). So for what a restart
    cannot fix the process asks for its own service to be stopped and waits for
    WinSW's Ctrl+C; if the request is refused, the restarts go on."""
    if code not in lifecycle.NOT_RETRIED_EXIT_CODES or platform != "win32":
        return code
    service_id = environ.get(windows_service.SERVICE_ID_ENV)
    if environ.get(lifecycle.SERVICE_ENV) != "1" or not service_id:
        return code
    # WinSW's Ctrl+C can land as soon as the stop is requested, before the wait.
    try:
        problem = windows_service.stop_own_service(service_id)
        if problem is not None:
            print(
                f"BACpack could not stop the Windows service {service_id} ({problem}), so it will be restarted "
                f"(exit code {code}).",
                file=sys.stderr,
                flush=True,
            )
            return code
        print(
            f"BACpack is stopping the Windows service {service_id} (exit code {code}): a restart cannot fix this. "
            "Fix the problem above, then start the service again.",
            file=sys.stderr,
            flush=True,
        )
        wait_for_stop(STOP_WAIT_SECONDS)
    except KeyboardInterrupt:
        pass
    return code


def main(
    argv: list[str] | None = None,
    *,
    run: Callable[..., None] = uvicorn.run,
    environ: MutableMapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "serve":
        print(USAGE, file=sys.stderr)
        return 2
    port = _port_default(environ)
    rest = args[1:]
    if rest:
        if len(rest) != 2 or rest[0] != "--port" or not rest[1].isdigit():
            print(USAGE, file=sys.stderr)
            return 2
        port = int(rest[1])
    if not 1 <= port <= 65535:
        print(f"The port must be 1 to 65535, not {port}.", USAGE, sep=chr(10), file=sys.stderr)
        return 2
    problem = sqlite_version_problem()
    if problem is not None:
        print(problem, file=sys.stderr)
        return _service_exit_code(lifecycle.SQLITE_TOO_OLD_EXIT_CODE, environ, platform)
    db_path = environ.get("BACPACK_DB_PATH") or str(get_db_path())
    try:
        plan = prepare(db_path, port)
    except ServeRefused as refused:
        # Already on the console and the Logs page through the log handlers.
        return _service_exit_code(refused.exit_code, environ, platform)
    lifecycle.mark_running(plan.host, plan.port, plan.fingerprint)
    # An import string, so uvicorn imports the app in this process and sees
    # the markers just set.
    run(
        APP,
        host=plan.host,
        port=plan.port,
        ssl_certfile=str(plan.certfile),
        ssl_keyfile=str(plan.keyfile),
        proxy_headers=False,
    )
    return 0
