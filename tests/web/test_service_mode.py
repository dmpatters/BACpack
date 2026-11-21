from __future__ import annotations

from bacpack import lifecycle


def test_version_reports_service_mode_off(web_client, monkeypatch):
    monkeypatch.delenv("BACPACK_SERVICE", raising=False)
    body = web_client.get("/api/version").json()
    assert body["service"] is False
    assert body["version"]


def test_version_reports_service_mode_on(web_client, monkeypatch):
    monkeypatch.setenv("BACPACK_SERVICE", "1")
    assert web_client.get("/api/version").json()["service"] is True


def test_restart_ui_is_refused_without_a_service_manager(web_client, monkeypatch):
    monkeypatch.delenv("BACPACK_SERVICE", raising=False)
    exits = []
    monkeypatch.setattr(lifecycle, "exit_process", exits.append)
    r = web_client.post("/api/engine/restart-ui")
    assert r.status_code == 409
    assert exits == []


def test_restart_ui_answers_then_exits_for_a_restart(web_client, monkeypatch):
    monkeypatch.setenv("BACPACK_SERVICE", "1")
    exits = []
    monkeypatch.setattr(lifecycle, "exit_process", exits.append)
    r = web_client.post("/api/engine/restart-ui")
    assert r.status_code == 202
    assert r.json() == {"restarting": True}
    assert exits == [lifecycle.RESTART_UI_EXIT_CODE]


def test_version_reports_the_running_listen_address_or_none(web_client, monkeypatch):
    for name in (lifecycle.RUNNING_ADDRESS_ENV, lifecycle.RUNNING_PORT_ENV, lifecycle.RUNNING_FINGERPRINT_ENV):
        monkeypatch.delenv(name, raising=False)
    assert web_client.get("/api/version").json()["listen_address"] is None
    monkeypatch.setenv(lifecycle.RUNNING_ADDRESS_ENV, "10.0.0.5")
    monkeypatch.setenv(lifecycle.RUNNING_PORT_ENV, "8000")
    monkeypatch.setenv(lifecycle.RUNNING_FINGERPRINT_ENV, "AB")
    assert web_client.get("/api/version").json()["listen_address"] == "10.0.0.5"
