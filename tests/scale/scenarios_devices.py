"""Area 2, devices and objects: the 5,000-object device's startup, its
Object_List and ReadPropertyMultiple, a live edit, config and CSV round
trips, a BACnet backup and restore, and the list endpoints of a 50-device
install."""
from __future__ import annotations

import csv
import io
import json

from bacpypes3.apdu import ReadPropertyMultipleRequest, ReadPropertyRequest
from bacpypes3.basetypes import PropertyReference, ReadAccessSpecification
from sqlalchemy import select

from bacpack.live_config import ObjectReloader
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application

from .bacnet_requests import read_config_file, reinitialize, send, write_config_file
from .scenarios import Context, scenario
from .support import web_client

RPM_OBJECTS = 500
LIVE_EDIT_DESCRIPTION = "edited by the scale test"


def load_big_device(ctx: Context):
    """The device and its objects as the device process loads them."""
    with ctx.session_factory() as session:
        device = session.get(Device, ctx.ids.big_device_id)
        objects = list(session.scalars(select(BacnetObject).filter_by(device_id=ctx.ids.big_device_id)))
        session.expunge_all()
    return device, objects


def build_big_app(ctx: Context):
    device, objects = load_big_device(ctx)
    return build_application(device, objects, trend_session_factory=ctx.session_factory)


def big_app(ctx: Context) -> None:
    ctx.state["device"], ctx.state["objects"] = load_big_device(ctx)
    ctx.state["app"] = build_application(
        ctx.state["device"], ctx.state["objects"], trend_session_factory=ctx.session_factory
    )


@scenario("devices.build_big_device", area="devices", dataset="install")
def build_big_device(ctx: Context):
    """Device startup: load the 5,000 rows and build the application."""
    return build_big_app(ctx)


@scenario("devices.object_list_whole", area="devices", dataset="install", setup=big_app)
async def object_list_whole(ctx: Context):
    return await send(
        ctx.state["app"], ReadPropertyRequest(objectIdentifier=("device", ctx.ids.big_device_id), propertyIdentifier="objectList")
    )


@scenario("devices.object_list_by_index", area="devices", dataset="install", setup=big_app)
async def object_list_by_index(ctx: Context):
    return await send(
        ctx.state["app"],
        ReadPropertyRequest(
            objectIdentifier=("device", ctx.ids.big_device_id),
            propertyIdentifier="objectList",
            propertyArrayIndex=2500,
        ),
    )


@scenario("devices.rpm_all_many", area="devices", dataset="install", setup=big_app)
async def rpm_all_many(ctx: Context):
    specs = [
        ReadAccessSpecification(
            objectIdentifier=("analog-value", instance),
            listOfPropertyReferences=[PropertyReference(propertyIdentifier="all")],
        )
        for instance in range(1, RPM_OBJECTS + 1)
    ]
    return await send(ctx.state["app"], ReadPropertyMultipleRequest(listOfReadAccessSpecs=specs))


def live_edit_object_id(ctx: Context) -> int:
    return ctx.ids.big_object_ids[len(ctx.ids.big_object_ids) // 2]


def edit_rows(ctx: Context, object_ids) -> int:
    """Edit descriptions in SQLite as the web UI would; the config triggers
    bump change_seq. Returns the new change_seq."""
    with ctx.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE bacnet_objects SET description = ? WHERE id = ?",
            [(LIVE_EDIT_DESCRIPTION, object_id) for object_id in object_ids],
        )
        return connection.exec_driver_sql(
            "SELECT change_seq FROM device_runtime WHERE device_id = ?", (ctx.ids.big_device_id,)
        ).scalar()


def live_edit_setup(ctx: Context) -> None:
    big_app(ctx)
    ctx.state["reloader"] = ObjectReloader(
        ctx.session_factory, ctx.state["app"], ctx.state["device"], ctx.state["objects"]
    )
    ctx.state["change_seq"] = edit_rows(ctx, [live_edit_object_id(ctx)])


@scenario("devices.live_edit_one_object", area="devices", dataset="install", writes=True, setup=live_edit_setup)
async def live_edit_one_object(ctx: Context):
    return await ctx.state["reloader"].apply(ctx.state["change_seq"])


def _client(ctx: Context) -> None:
    web_client(ctx)


def _config_document(ctx: Context) -> bytes:
    """The install's exported config, every big-device object edited so an
    apply has 5,000 updates to make."""
    document = ctx.state["client"].get("/api/config/export").json()
    for device in document["devices"]:
        if device["id"] == ctx.ids.big_device_id:
            for row in device["objects"]:
                row["description"] = LIVE_EDIT_DESCRIPTION
    return json.dumps(document).encode("utf-8")


@scenario("devices.config_export_50", area="devices", dataset="install", setup=_client)
def config_export_50(ctx: Context):
    return ctx.state["client"].get("/api/config/export")


def _config_import_setup(ctx: Context) -> None:
    web_client(ctx)
    ctx.state["document"] = _config_document(ctx)


def post_config(ctx: Context, **params):
    return ctx.state["client"].post(
        "/api/config/import", files={"file": ("config.json", ctx.state["document"], "application/json")}, params=params
    )


@scenario("devices.config_import_dry_run_50", area="devices", dataset="install", setup=_config_import_setup)
def config_import_dry_run_50(ctx: Context):
    return post_config(ctx, dry_run="true")


def _config_apply_setup(ctx: Context) -> None:
    _config_import_setup(ctx)
    ctx.state["digest"] = post_config(ctx, dry_run="true").json()["plan_digest"]


@scenario(
    "devices.config_import_apply_50", area="devices", dataset="install", writes=True, setup=_config_apply_setup
)
def config_import_apply_50(ctx: Context):
    return post_config(ctx, dry_run="false", plan_digest=ctx.state["digest"])


def csv_url(ctx: Context, tail: str) -> str:
    return f"/api/devices/{ctx.ids.big_device_id}/objects/{tail}"


@scenario("devices.csv_export_big", area="devices", dataset="install", setup=_client)
def csv_export_big(ctx: Context):
    return ctx.state["client"].get(csv_url(ctx, "export.csv"))


def _edited_csv(ctx: Context) -> str:
    text = ctx.state["client"].get(csv_url(ctx, "export.csv")).text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    for row in rows:
        row["description"] = LIVE_EDIT_DESCRIPTION
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def post_csv(ctx: Context, **params):
    return ctx.state["client"].post(
        csv_url(ctx, "import"),
        files={"file": ("objects.csv", ctx.state["csv"].encode("utf-8"), "text/csv")},
        params={"mode": "sync", **params},
    )


def _csv_setup(ctx: Context) -> None:
    web_client(ctx)
    ctx.state["csv"] = _edited_csv(ctx)


@scenario("devices.csv_sync_dry_run_big", area="devices", dataset="install", setup=_csv_setup)
def csv_sync_dry_run_big(ctx: Context):
    return post_csv(ctx, dry_run="true")


def _csv_apply_setup(ctx: Context) -> None:
    _csv_setup(ctx)
    ctx.state["digest"] = post_csv(ctx, dry_run="true").json()["plan_digest"]


@scenario("devices.csv_sync_apply_big", area="devices", dataset="install", writes=True, setup=_csv_apply_setup)
def csv_sync_apply_big(ctx: Context):
    return post_csv(ctx, dry_run="false", plan_digest=ctx.state["digest"])


@scenario("devices.backup_big", area="devices", dataset="install", writes=False, setup=big_app)
async def backup_big(ctx: Context):
    """startBackup, read the whole config file, endBackup."""
    app = ctx.state["app"]
    await reinitialize(app, "startBackup")
    data = await read_config_file(app)
    await reinitialize(app, "endBackup")
    return data


def _restore_setup(ctx: Context) -> None:
    big_app(ctx)
    ctx.state["backup"] = ctx.state["app"]._backup_snapshot()


@scenario("devices.restore_big", area="devices", dataset="install", writes=True, setup=_restore_setup)
async def restore_big(ctx: Context):
    """startRestore, write the file, endRestore (which applies it)."""
    app = ctx.state["app"]
    await reinitialize(app, "startRestore")
    await write_config_file(app, ctx.state["backup"])
    await reinitialize(app, "endRestore")
    return len(ctx.state["backup"])


@scenario("devices.list_api_50", area="devices", dataset="install", setup=_client)
def list_api_50(ctx: Context):
    return ctx.state["client"].get("/api/devices")


@scenario("devices.engine_status_50", area="devices", dataset="install", setup=_client)
def engine_status_50(ctx: Context):
    return ctx.state["client"].get("/api/engine/status")


@scenario("devices.objects_list_api_big", area="devices", dataset="install", setup=_client)
def objects_list_api_big(ctx: Context):
    return ctx.state["client"].get(f"/api/devices/{ctx.ids.big_device_id}/objects")
