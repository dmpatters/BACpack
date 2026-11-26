from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bacpack.db import history_db_path, init_db, make_engine, make_session_factory
from bacpack.web.app import create_app
from tests import _db_guard

_db_guard.install()


# Tests that serve BACnet on a fixed UDP port (127.0.0.1:47808 and the
# like; a FakeDeviceProcess runs the real _serve_device). Under pytest-xdist
# (`-n auto --dist loadgroup`) they all run on one worker, so two never
# hold the same port at once. _guard_udp_binds fails a test elsewhere that
# binds one.
UDP_TEST_FILES = frozenset(
    {
        "tests/objects/test_device_writes.py",
        "tests/test_bacnet_client_network.py",
        "tests/test_clone_reader_network.py",
        "tests/test_device_recipient_udp.py",
        "tests/test_device_process.py",
        "tests/test_device_process_subprocess.py",
        "tests/test_fakes.py",
        "tests/test_free_port.py",
        "tests/web/test_clone.py",
        "tests/web/test_object_io.py",
        "tests/test_sample_building.py",
        "tests/web/test_port_suggestion.py",
        "tests/web/test_sample.py",
        "tests/web/test_writes.py",
    }
)
_TESTS_ROOT = Path(__file__).resolve().parent.parent


def _in_udp_file(item) -> bool:
    return Path(item.path).resolve().relative_to(_TESTS_ROOT).as_posix() in UDP_TEST_FILES


def pytest_addoption(parser):
    parser.addoption("--scale", action="store_true", default=False, help="run the field-sized tests in tests/scale")
    parser.addoption(
        "--scale-rebuild", action="store_true", default=False, help="rebuild the cached scale datasets"
    )
    parser.addoption(
        "--interop", action="store_true", default=False, help="run the bacnet-stack tests in tests/interop"
    )
    parser.addoption(
        "--interop-rebuild", action="store_true", default=False, help="rebuild the cached bacnet-stack tools"
    )


@pytest.hookimpl(tryfirst=True)  # before xdist reads the group marks
def pytest_collection_modifyitems(config, items):
    from tests.scale.harness import scale_collection
    from tests.interop.harness import interop_collection

    scale_collection(config, items)
    interop_collection(config, items)
    for item in items:
        if _in_udp_file(item):
            item.add_marker(pytest.mark.xdist_group("bacnet-udp"))


def udp_grouping_problem(option, workerinput, nodeid) -> str | None:
    """Why a test in a UDP file can't run safely here, or None.

    Spread over several xdist workers, UDP tests fail at random with a
    misleading "port already in use"; fail them with the reason instead.
    xdist groups a test by suffixing its node id, so a loadgroup run without
    the suffix means a pytest/xdist pairing broke the grouping.
    """
    if getattr(option, "loadgroup", False):
        if "@bacnet-udp" not in nodeid:
            return "--dist loadgroup did not put this test in the bacnet-udp group: check the pytest/pytest-xdist versions"
        return None
    if workerinput is not None and workerinput.get("workercount", 1) > 1:
        return (
            "run with -n auto --dist loadgroup: tests that serve BACnet on fixed UDP ports "
            "must share one worker, or they clash on the ports"
        )
    return None


@pytest.fixture(autouse=True)
def _guard_udp_binds(request, monkeypatch):
    if _in_udp_file(request.node):
        problem = udp_grouping_problem(
            request.config.option, getattr(request.config, "workerinput", None), request.node.nodeid
        )
        if problem:
            pytest.fail(problem, pytrace=False)
        yield
        return
    bound = []
    real_bind = socket.socket.bind

    def recording_bind(sock, address):
        if sock.type == socket.SOCK_DGRAM and isinstance(address, tuple) and address[1] != 0:
            bound.append(address)
        return real_bind(sock, address)

    monkeypatch.setattr(socket.socket, "bind", recording_bind)
    yield
    if bound:
        pytest.fail(
            f"binds UDP {bound}: add its file to UDP_TEST_FILES in tests/conftest.py",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _guard_cross_file_writes(request):
    """BACpack code that reads a file and then writes it must have started with
    db.lock_both_files."""
    _db_guard.CURRENT_TEST = request.node.nodeid
    try:
        yield
    finally:
        _db_guard.CURRENT_TEST = None
    found = _db_guard.take(request.node.nodeid)
    if found:
        joined = "\n".join(dict.fromkeys(found))
        pytest.fail(f"read-then-write without db.lock_both_files:\n{joined}", pytrace=False)


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory):
    """One database built by the real init_db per test session (per xdist
    worker), copied for each test: a fresh init_db costs ~90 ms on Windows,
    a copy ~1 ms. Checkpointed so the two main files hold everything."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    engine = make_engine(path)
    init_db(engine)
    # A fresh install waits on the setup screen (every /api route 403s); tests
    # run as an install that chose "no login".
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE main.server_settings SET login_mode = 'none' WHERE id = 1")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA main.wal_checkpoint(TRUNCATE)")
        connection.exec_driver_sql("PRAGMA history.wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


def _copy_template(template, path):
    shutil.copyfile(template, path)
    shutil.copyfile(history_db_path(template), history_db_path(path))
    return path


@pytest.fixture()
def session_factory(tmp_path, _template_db):
    engine = make_engine(_copy_template(_template_db, tmp_path / "test.db"))
    return make_session_factory(engine)


@pytest.fixture()
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture()
def web_client(tmp_path, _template_db, monkeypatch):
    from tests.fakes import FakeDeviceProcess

    def process_factory(db_path, device_id):
        from bacpack.db import make_engine, make_session_factory

        engine = make_engine(db_path)
        session_factory = make_session_factory(engine)
        return FakeDeviceProcess(session_factory, device_id)

    # The copy is already current; create_app's own init_db is covered by
    # tests/web/test_app.py's fresh-path test.
    db_path = _copy_template(_template_db, tmp_path / "web-test.db")
    monkeypatch.setattr("bacpack.web.app.init_db", lambda engine: None)
    app = create_app(db_path=str(db_path), process_factory=process_factory)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_log_capture():
    """install_log_capture adds handlers to process-wide loggers, the library
    ones included; a test that installs them must not leave them for the next."""
    yield
    import logging

    from bacpack.log_capture import remove_log_capture_handlers

    remove_log_capture_handlers()
    logging.getLogger("bacpack").setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _reset_device_clock():
    """device_clock is process-wide state: a test that installs it or moves the
    offset must never leak that into the next test. uninstall() is idempotent
    and also resets the offset."""
    yield
    from bacpack import device_clock

    device_clock.uninstall()


@pytest.fixture(autouse=True)
def _guard_gc_freeze():
    """Only a device's own process freezes the GC;
    a test that really froze this process would leave its heap out of every
    later collection."""
    yield
    import gc

    frozen = gc.get_freeze_count()
    gc.unfreeze()
    assert frozen == 0, "a test froze the test process; pass freeze_gc only from run_device_process"


@pytest.fixture
def quiet_retention(monkeypatch):
    """Keeps the daily retention task from running (and kicking the trend
    pruner) at app startup, for a test that counts kicks. Use it through
    `usefixtures` so it is set up before `web_client`."""

    async def idle(*args, **kwargs):
        import asyncio

        await asyncio.sleep(10**6)

    monkeypatch.setattr("bacpack.web.app.run_retention_task", idle)
