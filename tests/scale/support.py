"""Helpers the scenario modules share: an application for some of a
dataset's objects, a web client on the dataset, and time conversions."""
from __future__ import annotations

import datetime as dt
from contextlib import ExitStack
from unittest import mock

from sqlalchemy import select
from starlette.testclient import TestClient

from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from bacpack.web.app import create_app

from .scenarios import Context


def application_for(ctx: Context, object_ids, *, device_id: int | None = None):
    """A served-shape application holding just these objects of one device,
    with a Trend Log for each trended one. Built inside the session that
    loaded the rows."""
    device_id = ctx.ids.big_device_id if device_id is None else device_id
    with ctx.session_factory() as session:
        device = session.get(Device, device_id)
        rows = list(session.scalars(select(BacnetObject).where(BacnetObject.id.in_(list(object_ids)))))
        return build_application(device, rows, trend_session_factory=ctx.session_factory)


def web_client(ctx: Context) -> TestClient:
    """A client on the dataset, started once per context and closed with it.
    init_db and the retention task are stubbed: their cost has scenarios of
    their own, and a prune at startup would change the data under test."""

    def never_spawn(db_path, device_id):
        raise AssertionError("a scale scenario started a device process")

    if "client" not in ctx.state:
        # A dataset is a fresh install, which the login middleware holds at the
        # first-use setup screen (403 setup_required); tests/conftest.py does the same.
        with ctx.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE main.server_settings SET login_mode = 'none' WHERE id = 1")
        stack = ExitStack()
        stack.enter_context(mock.patch("bacpack.web.app.init_db", lambda engine: None))

        async def idle(*args, **kwargs):
            import asyncio

            await asyncio.sleep(10**6)

        stack.enter_context(mock.patch("bacpack.web.app.run_retention_task", idle))
        app = create_app(db_path=str(ctx.db_path), process_factory=never_spawn)
        client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1"))
        ctx.state["client"] = client
        ctx.on_close(stack.close)
    return ctx.state["client"]


def iso_z(stored: str | dt.datetime) -> str:
    """A stored (naive UTC) timestamp as an ISO 8601 string with Z."""
    if isinstance(stored, str):
        stored = dt.datetime.fromisoformat(stored)
    return stored.strftime("%Y-%m-%dT%H:%M:%SZ")


def sample_span(ctx: Context, object_id: int) -> tuple[dt.datetime, dt.datetime]:
    with ctx.engine.connect() as connection:
        first, last = connection.exec_driver_sql(
            "SELECT MIN(timestamp), MAX(timestamp) FROM history.trend_samples WHERE object_id = ?",
            (object_id,),
        ).one()
    return dt.datetime.fromisoformat(first), dt.datetime.fromisoformat(last)


def web_engine(ctx: Context):
    """The SQLAlchemy engine the web app itself runs on (not ctx.engine), for
    a StatementRecorder around a request."""
    return web_client(ctx).app.state.session_factory.kw["bind"]
