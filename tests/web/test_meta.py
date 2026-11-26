"""GET /api/version, /api/capabilities, /api/units."""
from __future__ import annotations


def test_list_units_returns_real_engineering_units(web_client):
    r = web_client.get("/api/units")
    assert r.status_code == 200
    units = r.json()["units"]
    assert "degreesFahrenheit" in units
    assert "percent" in units
    assert "noUnits" in units
    assert units == sorted(units)


def test_get_version_returns_the_installed_package_version(web_client):
    from bacpack import bacpack_version

    r = web_client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == bacpack_version()


def test_get_capabilities_works_with_no_devices_running(web_client):
    from bacpack import bacpack_version

    r = web_client.get("/api/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["bacpack_version"] == bacpack_version()
    assert body["bibb_summary"] == {"met": 21, "total": 21}
    assert {s["name"] for s in body["services"]} >= {"who-is", "read-property"}
    assert "trend-log" in body["object_types"]
    assert set(body) == {
        "bacpack_version", "protocol_revision", "segmentation", "max_apdu_length",
        "data_links", "bibb_profile_source", "bibb_summary", "services",
        "object_types", "bibbs",
    }


def test_pics_is_the_conformance_statement_of_the_running_version(web_client):
    from bacpack import bacpack_version
    from bacpack.pics import render_pics

    r = web_client.get("/api/pics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<table>" in r.text
    assert f"Product version: {bacpack_version()}" in r.text

    raw = web_client.get("/api/pics.md")
    assert raw.status_code == 200
    # Shown in the browser, not downloaded: text/markdown is saved to disk by most.
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.headers["content-disposition"].startswith("inline")
    assert raw.text == render_pics()


def test_pics_needs_the_login_like_the_rest_of_the_api(web_client):
    from tests.web._helpers import enable_login, session_cookie

    enable_login(web_client)
    assert web_client.get("/api/pics").status_code == 401
    assert web_client.get("/api/pics.md").status_code == 401
    session_cookie(web_client)
    assert web_client.get("/api/pics").status_code == 200
    assert web_client.get("/api/pics.md").status_code == 200
