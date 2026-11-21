"""The BACpack side of the interop suite: a temp data dir, the committed
config imported through config_io (the Config page's code path), and both
devices running as real child processes under EngineManager, as in the
product. Not the web API: that adds the login gate and nothing under test."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import psutil

from bacpack import config_io
from bacpack.auth import hash_password
from bacpack.db import init_db, lock_both_files, make_engine, make_session_factory
from bacpack.engine import EngineManager
from bacpack.models import Device

from .bacnet_stack import InteropSetupError
from .tools import Tools

CONFIG_PATH = Path(__file__).resolve().parent / "interop-config.json"
IOP1 = 599101
IOP2 = 599102
IOP2_PASSWORD = "iop-secret"
READY_TIMEOUT = 60
HEALTH_INTERVAL = 1.0


class Stack:
    def __init__(self, data_dir: Path, tools: Tools) -> None:
        self.tools = tools
        self.db_path = data_dir / "bacpack.db"
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="interop-loop", daemon=True)
        self._health = None
        self._old_env = os.environ.get("BACPACK_DB_PATH")
        self.engine = None
        self.session_factory = None
        self.manager = None

    def run(self, coro, timeout=120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self) -> None:
        os.environ["BACPACK_DB_PATH"] = str(self.db_path)
        self.engine = make_engine(self.db_path)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        parsed = config_io.parse_config_document(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        with self.session_factory() as session:
            lock_both_files(session)
            config_io.sync_config(session, parsed.devices, dry_run=False)
            # A password is never in a config export, so the harness sets it.
            session.get(Device, IOP2).bacnet_password_hash = hash_password(IOP2_PASSWORD)
            session.commit()
        self._thread.start()
        self.manager = EngineManager(self.session_factory, db_path=str(self.db_path))
        for device_id in (IOP1, IOP2):
            self.run(self.manager.start(device_id))
        # ReinitializeDevice only marks a device for restart; the parent's
        # health-check task carries it out.
        self._health = asyncio.run_coroutine_threadsafe(
            self.manager.run_health_check_task(interval_seconds=HEALTH_INTERVAL), self.loop
        )
        self.wait_until_answering()

    def whois_text(self) -> str:
        return self.tools.run("bacwi", "--timeout", "2000", timeout=15).text

    def wait_until_answering(self, device_ids=(IOP1, IOP2), timeout: float = READY_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = self.whois_text()
            if all(str(device_id) in text for device_id in device_ids):
                return
            time.sleep(1)
        status = self.manager.status()
        raise InteropSetupError(
            f"devices {list(device_ids)} did not answer Who-Is within {timeout:.0f} s.\n"
            f"last bacwi output:\n{text}\nstatus: {status}"
        )

    def restart(self, device_id: int) -> None:
        self.run(self.manager.restart(device_id))

    def pid(self, device_id: int):
        for row in self.manager.status():
            if row["device_id"] == device_id:
                return row["process_pid"]
        return None

    def stop(self) -> None:
        """Never depends on a device answering: the manager stops children
        by process, then anything left of this process's children is killed."""
        try:
            if self._health is not None:
                self._health.cancel()
            if self.manager is not None and self._thread.is_alive():
                try:
                    self.run(self.manager.stop_all_for_shutdown(), timeout=60)
                except Exception:
                    pass
        finally:
            for child in psutil.Process().children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            if self._thread.is_alive():
                self.loop.call_soon_threadsafe(self.loop.stop)
                self._thread.join(10)
            if not self._thread.is_alive():
                self.loop.close()
            if self.engine is not None:
                self.engine.dispose()
            if self._old_env is None:
                os.environ.pop("BACPACK_DB_PATH", None)
            else:
                os.environ["BACPACK_DB_PATH"] = self._old_env
