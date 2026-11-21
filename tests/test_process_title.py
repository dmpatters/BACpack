"""Process names: "bacpack: ..." in ps and top (setproctitle), and the Windows
bundle's renamed interpreters for device processes."""
from __future__ import annotations

import runpy
import sys
import types

import pytest

from bacpack import device_process, engine, process_title


class _FakeSetProcTitle(types.ModuleType):
    def __init__(self):
        super().__init__("setproctitle")
        self.calls = []

    def setproctitle(self, title):
        self.calls.append(("title", title))

    def setthreadtitle(self, name):
        self.calls.append(("thread", name))


def test_set_process_title_sets_the_title_and_the_short_name(monkeypatch):
    fake = _FakeSetProcTitle()
    monkeypatch.setitem(sys.modules, "setproctitle", fake)
    process_title.set_process_title("bacpack: web", "bacpack")
    assert fake.calls == [("title", "bacpack: web"), ("thread", "bacpack")]


def test_a_missing_setproctitle_never_breaks_startup(monkeypatch):
    monkeypatch.setitem(sys.modules, "setproctitle", None)  # import raises ImportError
    process_title.set_process_title("bacpack: web", "bacpack")


def test_a_failing_setproctitle_never_breaks_startup(monkeypatch):
    fake = _FakeSetProcTitle()

    def boom(_title):
        raise OSError("no")

    fake.setproctitle = boom
    monkeypatch.setitem(sys.modules, "setproctitle", fake)
    process_title.set_process_title("bacpack: web", "bacpack")


def test_device_title_names_the_device():
    assert process_title.device_title(599001) == "bacpack: device 599001"


def test_the_device_process_sets_its_title_before_serving(tmp_path, monkeypatch):
    seen = []

    async def serve(session_factory, device_id, *, freeze_gc=False):
        seen.append("serve")

    monkeypatch.setattr(device_process, "prepare_device_process", lambda: None)
    monkeypatch.setattr(device_process, "_serve_device", serve)
    monkeypatch.setattr(device_process, "set_process_title", lambda title, short: seen.append((title, short)))
    device_process.run_device_process(str(tmp_path / "x.db"), 7)
    assert seen == [("bacpack: device 7", "bacpack-worker"), "serve"]


def test_the_web_process_sets_its_title_in_the_entry_point(monkeypatch):
    seen = []
    monkeypatch.setattr(process_title, "set_process_title", lambda title, short: seen.append((title, short)))
    monkeypatch.setattr("bacpack.serve.main", lambda: 0)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("bacpack", run_name="__main__")
    assert exit_info.value.code == 0
    assert seen == [("bacpack: web", "bacpack")]


def test_device_executable_is_the_bundles_renamed_interpreter_only_on_windows(tmp_path):
    (tmp_path / "bacpack.exe").write_bytes(b"")
    assert engine.device_executable(str(tmp_path / "bacpack.exe"), "win32") is None  # no bacpack-worker.exe beside it
    device_exe = tmp_path / "bacpack-worker.exe"
    device_exe.write_bytes(b"")
    assert engine.device_executable(str(tmp_path / "bacpack.exe"), "win32") == str(device_exe)
    assert engine.device_executable(str(tmp_path / "bacpack.exe"), "linux") is None


def test_the_process_factory_spawns_devices_with_that_executable(monkeypatch):
    calls = []

    class FakeContext:
        def set_executable(self, path):
            calls.append(("executable", path))

        def Process(self, **kwargs):  # noqa: N802 -- mirrors multiprocessing's context
            calls.append(("process", kwargs["args"], kwargs["daemon"]))
            return "process"

    monkeypatch.setattr(engine.multiprocessing, "get_context", lambda method: FakeContext() if method == "spawn" else None)
    monkeypatch.setattr(engine, "device_executable", lambda: r"C:\x\bacpack-worker.exe")
    assert engine._default_process_factory("db", 3) == "process"
    assert calls == [("executable", r"C:\x\bacpack-worker.exe"), ("process", ("db", 3), False)]
    calls.clear()
    monkeypatch.setattr(engine, "device_executable", lambda: None)
    engine._default_process_factory("db", 3)
    assert calls == [("process", ("db", 3), False)]
