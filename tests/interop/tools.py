"""Runs the bacnet-stack tools against the loopback devices. The tools bind
INADDR_ANY, so they can't share a port with a device on this machine; they
register as a foreign device with IOP-1's BBMD and every broadcast is
relayed."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CLIENT_PORT = 47899
# A second client port, for a tool that must not share the first: a COV
# notification goes to the most recently bound socket on a shared port.
OTHER_CLIENT_PORT = 47898
BBMD_ADDRESS = "127.0.0.2"
BBMD_PORT = 47820
REGISTRATION_SECONDS = 300
DEFAULT_TIMEOUT = 20


@dataclass
class Result:
    code: int | None
    out: str
    err: str
    cmd: list[str]
    timed_out: bool = False

    def describe(self) -> str:
        return (
            f"command: {' '.join(self.cmd)}\nexit: {self.code}{' (timed out)' if self.timed_out else ''}\n"
            f"stdout:\n{self.out}\nstderr:\n{self.err}"
        )

    def expect_ok(self) -> "Result":
        if self.code != 0 or self.timed_out:
            raise AssertionError(self.describe())
        return self

    @property
    def text(self) -> str:
        return self.out + self.err


class Tools:
    def __init__(self, bin_dir: Path, *, stdbuf: tuple[str, ...] = ("stdbuf", "-oL")) -> None:
        self.bin_dir = Path(bin_dir)
        self._stdbuf = stdbuf

    def env(self, client_port: int = CLIENT_PORT) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            BACNET_IFACE="lo",
            BACNET_IP_PORT=str(client_port),
            BACNET_BBMD_ADDRESS=BBMD_ADDRESS,
            BACNET_BBMD_PORT=str(BBMD_PORT),
            BACNET_BBMD_TIMETOLIVE=str(REGISTRATION_SECONDS),
        )
        return env

    def command(self, tool: str, *args, line_buffered: bool = False) -> list[str]:
        # Piped output is block-buffered: a tool ended by a timeout would
        # lose it, so tools that may be ended run under stdbuf -oL.
        prefix = list(self._stdbuf) if line_buffered else []
        return [*prefix, str(self.bin_dir / tool), *(str(a) for a in args)]

    def run(
        self,
        tool: str,
        *args,
        timeout: float = DEFAULT_TIMEOUT,
        line_buffered: bool = False,
        client_port: int = CLIENT_PORT,
    ) -> Result:
        cmd = self.command(tool, *args, line_buffered=line_buffered)
        try:
            done = subprocess.run(cmd, env=self.env(client_port), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return Result(None, _text(exc.stdout), _text(exc.stderr), cmd, timed_out=True)
        return Result(done.returncode, done.stdout, done.stderr, cmd)

    def run_until(
        self,
        tool: str,
        *args,
        until: Callable[[str], bool],
        total_timeout: float = 30,
        poll: float = 0.2,
    ) -> Result:
        """For a tool that never exits on its own (bacscov): collect its
        line-buffered output until `until(text)` holds or the limit passes,
        then end it. Output goes to temp files, so no pipe fills up."""
        cmd = self.command(tool, *args, line_buffered=True)

        # bacscov reports a notification on stderr and the rest on stdout:
        # both go to one file, so the predicate sees them in order.
        with tempfile.TemporaryFile("w+") as out:
            proc = subprocess.Popen(cmd, env=self.env(), stdout=out, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + total_timeout
            met = False
            try:
                while time.monotonic() < deadline:
                    out.flush()
                    out.seek(0)
                    if until(out.read()):
                        met = True
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(poll)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            out.seek(0)
            return Result(proc.returncode, out.read(), "", cmd, timed_out=not met)


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
