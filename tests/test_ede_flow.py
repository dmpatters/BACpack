"""The whole EDE path on a file with every shape: what the planner
returns must save, build, round-trip and account for every row."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from bacpypes3.basetypes import EngineeringUnits

from bacpack import object_config
from bacpack.config_io import export_config, parse_config, sync_config
from bacpack.config_schemas import ObjectCreate
from bacpack.ede_io import read_ede, read_state_texts, resolve_state_texts
from bacpack.ede_plan import plan_device, unsupported_type_name
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application
from tests.ede_fixtures import WIDE, ede_bytes, ede_text, full_rows, state_texts_text

TABLE = read_state_texts(state_texts_text([["1", "Closed", "Open"]]).encode())


def _file():
    file = read_ede(ede_bytes(ede_text(full_rows(), columns=WIDE)))
    resolve_state_texts(file, TABLE)
    return file


def _plan(instance=1000):
    return plan_device(_file().rows, device_instance=instance, device_id=599050, device_name="Plant")


def test_every_planned_row_passes_check_object_and_check_device():
    plan = _plan()
    assert plan.problem is None and len(plan.objects) > 10
    for row in plan.objects:
        object_config.check_object(row)
        ObjectCreate.model_validate(row)
    assert object_config.check_device("Plant", plan.objects) is None


@pytest.mark.asyncio
async def test_planned_rows_build_into_a_running_application():
    plan = _plan()
    device = Device(id=599050, name="Plant", vendor_identifier=999)
    objects = [BacnetObject(**row) for row in plan.objects]
    app = build_application(device, objects)
    for row in plan.objects:
        assert app.get_object_name(row["name"]) is not None, row["name"]
    assert await app.get_object_name("Temp").read_property("units") == EngineeringUnits("degreesCelsius")
    assert await app.get_object_name("Season").read_property("stateText") == ["Winter", "Summer", "Spring"]


def test_a_plan_round_trips_through_config_export_and_import(session):
    plan = _plan()
    device = Device(id=599050, name="Plant", vendor_identifier=999)
    for row in plan.objects:
        device.objects.append(BacnetObject(**row))
    session.add(device)
    session.commit()
    parsed = parse_config(export_config([device]))
    summary = sync_config(session, parsed, dry_run=True)
    assert summary["devices_created"] == [] and summary["devices_deleted"] == []
    objects = summary["objects"][599050]
    assert objects["created"] == [] and objects["deleted"] == []


def test_nothing_in_the_plan_is_silently_changed():
    file = _file()
    plan = _plan()
    planned = {(r["object_type"], r["instance_number"]): r for r in plan.objects}
    skipped = {(s["object_type"], s["instance"]) for s in plan.skipped}
    noted_types = {(n["object_type"], n["instance"]) for n in plan.notes}
    for row in file.rows:
        if row.device_instance != 1000 or row.object_type == "device":
            continue
        key = (row.object_type, row.instance)
        kind = unsupported_type_name(row.object_type.removeprefix("unsupported:"))
        if key in planned:
            if planned[key]["name"] != row.name:
                assert key in noted_types, f"{row.name} was renamed without a note"
            for text in row.notes:
                assert any(n["note"] == text for n in plan.notes), text
        elif row.object_type in ("file", "network-port"):
            assert key in noted_types
        elif row.object_type == "trend-log" and key not in skipped:
            assert row.name in {r["trend_log_name"] for r in plan.objects}, f"{row.name} vanished"
        else:
            assert key in skipped or (kind, row.instance) in skipped, f"{row.name} vanished"


def test_the_full_file_plans_what_the_design_says():
    plan = _plan()
    names = {r["name"]: r for r in plan.objects}
    assert {"Damper", "Damper (2)", "Damper (3)"} <= set(names)
    assert names["Temp"]["trended"] is True and names["Temp"]["trend_log_name"] == "Temp.Log1"
    assert names["Temp"]["alarming_enabled"] is False and names["Temp"]["notification_class_instance"] == 3
    assert names["Door"]["inactive_text"] == "Closed" and names["Mode"]["state_text"] == ["State 1", "State 2"]
    assert not names["Holidays"]["date_list"]
    assert {s["name"] for s in plan.skipped} == {"Nothing.Matches", "Loop 1", "Vendor thing"}
    assert plan.skipped_types == {"event-enrollment (9)": 1, "600": 1}


def test_the_second_device_plans_apart():
    plan = _plan(2000)
    assert [r["name"] for r in plan.objects] == ["CH Temp"]


def test_no_third_party_sample_files_are_committed():
    root = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("needs a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True, encoding="utf-8"
    ).stdout.splitlines()
    samples = [
        path for path in tracked
        if path.lower().endswith(".ede") or ("ede" in Path(path).name.lower() and path.lower().endswith((".csv", ".xlsx", ".xls")))
    ]
    assert samples == []
