from __future__ import annotations

import os
import signal

from bacpack import device_process


def test_device_ignores_ctrl_c(monkeypatch):
    calls = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: calls.append((sig, handler)))
    monkeypatch.setattr(os, "dup2", lambda *a: None)
    monkeypatch.delenv("BACPACK_SERVICE", raising=False)
    device_process.prepare_device_process()
    assert (signal.SIGINT, signal.SIG_IGN) in calls


def test_device_keeps_its_console_outside_a_service(monkeypatch):
    dups = []
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(os, "dup2", lambda src, dst: dups.append(dst))
    monkeypatch.delenv("BACPACK_SERVICE", raising=False)
    device_process.prepare_device_process()
    assert dups == []


def test_device_releases_inherited_stdio_under_a_service(monkeypatch):
    dups = []
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(os, "dup2", lambda src, dst: dups.append(dst))
    monkeypatch.setenv("BACPACK_SERVICE", "1")
    device_process.prepare_device_process()
    assert sorted(dups) == [0, 1, 2]
