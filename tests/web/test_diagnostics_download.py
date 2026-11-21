import inspect
import io
import json
import zipfile

from bacpack import bacpack_version
from bacpack.models import DeviceRuntime, LogEntry
from bacpack.web.api import diagnostics as diagnostics_api

from ._helpers import _add_log_entry, _create_connector, _create_device, _create_object

FILES = {"summary.txt", "system.json", "config.json", "logs.json", "traffic.json"}


def _zip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        return {name: z.read(name) for name in z.namelist()}


def test_download_returns_a_zip_with_the_five_files_and_an_attachment_name(web_client):
    _create_device(web_client)
    r = web_client.get("/api/diagnostics/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="bacpack-diagnostics-{bacpack_version()}-')
    assert disposition.endswith('.zip"')
    assert set(_zip(r)) == FILES


def test_download_defaults_to_stand_ins(web_client):
    _create_device(web_client, name="ZZ-Marker-Dev")
    for url in ("/api/diagnostics/download", "/api/diagnostics/download?real_values=false"):
        files = _zip(web_client.get(url))
        assert b"ZZ-Marker-Dev" not in b"".join(files.values())
        assert json.loads(files["config.json"])["devices"][0]["name"] == "device-1"
    real = _zip(web_client.get("/api/diagnostics/download?real_values=true"))
    assert json.loads(real["config.json"])["devices"][0]["name"] == "ZZ-Marker-Dev"


def test_download_of_an_empty_server_works(web_client):
    r = web_client.get("/api/diagnostics/download")
    assert r.status_code == 200
    assert json.loads(_zip(r)["config.json"])["devices"] == []


def test_download_rejects_a_non_boolean_real_values(web_client):
    assert web_client.get("/api/diagnostics/download?real_values=maybe").status_code == 422


def test_download_has_no_credentials_in_either_mode(web_client):
    _create_connector(
        web_client, name="ZZ-Conn", url="http://zz-host.example/x",
        auth_type="basic", auth_username="zz-user", auth_password="ZZ-SECRET-PW",
        headers={"X-Key": "ZZ-SECRET-HDR"},
    )
    for url in ("/api/diagnostics/download", "/api/diagnostics/download?real_values=true"):
        blob = b"".join(_zip(web_client.get(url)).values())
        assert b"ZZ-SECRET-PW" not in blob
        assert b"ZZ-SECRET-HDR" not in blob


def test_download_runs_off_the_event_loop():
    assert not inspect.iscoroutinefunction(diagnostics_api.download_diagnostics)


def test_download_writes_nothing(web_client):
    _create_device(web_client)
    _create_object(web_client)
    _add_log_entry(web_client, message="hello")
    factory = web_client.app.state.session_factory

    def counts():
        with factory() as session:
            return (
                session.query(LogEntry).count(),
                session.query(DeviceRuntime).count(),
                session.query(DeviceRuntime).one().config_seq,
            )

    before = counts()
    web_client.get("/api/diagnostics/download")
    web_client.get("/api/diagnostics/download?real_values=true")
    assert counts() == before
