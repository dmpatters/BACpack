from __future__ import annotations

import pytest

from bacpack.config_connectors import ObjectAfter, objects_after_sync, plan_connectors
from bacpack.config_io import parse_config_document
from bacpack.config_schemas import ConfigImportError
from bacpack.models import BacnetObject, Connector, ConnectorMapping, ConnectorWriteMapping, Device
from tests.test_config_connectors import _bacnet_connector, _device, _http_connector, _obj

THREE = (("analog-value", 1, "AV-1"), ("analog-value", 2, "AV-2"), ("analog-value", 3, "AV-3"))


def _file_device(device_id=599001, objects=THREE, simulated=()):
    rows = []
    for object_type, instance, name in objects:
        row = {"object_type": object_type, "instance_number": instance, "name": name}
        if object_type == "schedule":
            row["schedule_value_type"] = "analog"
            row["schedule_default"] = "0"
        if (object_type, instance) in simulated:
            row["simulation"] = {"kind": "sine", "center": 72, "amplitude": 3, "period_s": 600}
        rows.append(row)
    return {"id": device_id, "name": f"Dev-{device_id}", "objects": rows}


def _http(name="Weather", instance=1, device=599001, **overrides):
    return {
        "name": name,
        "type": "http-poll",
        "url": "https://example.invalid/forecast",
        "mappings": [{"device": device, "object": f"analog-value:{instance}", "json_path": "a"}],
        **overrides,
    }


def _bacnet(name="Room", device=599001, remote=17, **overrides):
    return {
        "name": name,
        "type": "bacnet",
        "device": device,
        "remote_device_instance": remote,
        "mappings": [],
        "write_mappings": [],
        **overrides,
    }


def _plan(session, devices, connectors):
    parsed = parse_config_document({"devices": devices, "connectors": connectors})
    after = objects_after_sync(session, parsed.devices)
    return plan_connectors(session, parsed.connectors, after, {d["id"] for d in parsed.devices})


def _refused(session, devices, connectors, fragment):
    with pytest.raises(ConfigImportError) as exc:
        _plan(session, devices, connectors)
    assert fragment in str(exc.value), str(exc.value)
    return str(exc.value)


def test_objects_after_sync_whole_server_is_exactly_the_file(session):
    _device(session, 599002)
    parsed = parse_config_document(
        {"devices": [_file_device(599001, simulated=[("analog-value", 2)])]}
    )
    after = objects_after_sync(session, parsed.devices)
    assert after == {
        (599001, "analog-value", 1): ObjectAfter("analog-value", False),
        (599001, "analog-value", 2): ObjectAfter("analog-value", True),
        (599001, "analog-value", 3): ObjectAfter("analog-value", False),
    }


def test_objects_after_sync_restore_keeps_other_devices_from_the_db(session):
    _device(session, 599002, objects=(("binary-value", 5, "BV-5"),))
    _device(session, 599001)
    parsed = parse_config_document({"devices": [_file_device(599001, objects=(("analog-value", 9, "AV-9"),))]})

    after = objects_after_sync(session, parsed.devices, only_device_id=599001)

    assert set(after) == {(599001, "analog-value", 9), (599002, "binary-value", 5)}


def test_plan_refuses_a_changed_type(session):
    device = _device(session)
    _http_connector(session, device)
    _refused(
        session,
        [_file_device()],
        [_bacnet(name="Weather")],
        "connector 'Weather' is an http-poll connector here; the file has it as a bacnet connector on device 599001, "
        "and a connector's type and device can't change",
    )


def test_plan_refuses_a_changed_device(session):
    one = _device(session)
    _device(session, 599002)
    _bacnet_connector(session, one)
    _refused(
        session,
        [_file_device(599001), _file_device(599002)],
        [_bacnet(device=599002)],
        "connector 'Room' is a bacnet connector on device 599001 here; the file has it on device 599002, "
        "and a connector's type and device can't change",
    )


def test_plan_refuses_a_bacnet_connector_on_a_device_that_will_not_exist(session):
    _refused(session, [_file_device()], [_bacnet(device=599009)], "connector 'Room': device 599009")


def test_plan_refuses_a_mapping_onto_an_object_the_file_drops(session):
    _refused(
        session,
        [_file_device(objects=THREE[:1])],
        [_http(instance=2)],
        "connector 'Weather': no object 599001 analog-value:2 after this import",
    )
    _refused(
        session,
        [_file_device(599002)],
        [_http(device=599001)],
        "connector 'Weather': no object 599001 analog-value:1 after this import",
    )
    _refused(
        session,
        [_file_device(objects=THREE[:1])],
        [_bacnet(write_mappings=[{"object": "analog-value:3", "remote_object_type": "analog-value", "remote_object_instance": 1}])],
        "connector 'Room': no object 599001 analog-value:3 after this import",
    )


def test_plan_refuses_a_mapping_onto_a_device_the_file_deletes_even_when_the_db_has_it(session):
    _device(session, 599001)
    _device(session, 599002)
    _refused(
        session,
        [_file_device(599001)],
        [_http(device=599002)],
        "connector 'Weather': no object 599002 analog-value:1 after this import",
    )


def test_plan_refuses_a_calculated_target(session):
    devices = [_file_device(objects=(("schedule", 1, "Sched"), ("analog-value", 1, "AV-1")))]
    _refused(
        session,
        devices,
        [_http(mappings=[{"device": 599001, "object": "schedule:1", "json_path": "a"}])],
        "connector 'Weather': schedule objects can't be a connector-mapping target",
    )


def test_plan_refuses_a_simulated_target(session):
    _refused(
        session,
        [_file_device(simulated=[("analog-value", 1)])],
        [_http()],
        "connector 'Weather': object 599001 analog-value:1 is simulated; turn its simulator off",
    )


def test_plan_refuses_two_connectors_in_the_file_feeding_one_object(session):
    message = _refused(
        session, [_file_device()], [_http("A"), _http("B")], "599001 analog-value:1 would be fed by both 'A' and 'B'"
    )
    assert "analog-value:1 would" in message


def test_plan_refuses_a_file_mapping_onto_an_object_a_left_alone_connector_feeds(session):
    device = _device(session)
    _http_connector(session, device)
    _refused(
        session,
        [_file_device()],
        [_http("Other")],
        "599001 analog-value:1 would be fed by both 'Other' and 'Weather'",
    )


def test_plan_refuses_two_sources_across_a_renamed_connector(session):
    device = _device(session)
    _http_connector(session, device)
    stored = session.query(Connector).one()
    stored.name = "Weather 2"
    session.flush()

    message = _refused(
        session, [_file_device()], [_http("Weather")], "599001 analog-value:1 would be fed by both 'Weather' and 'Weather 2'"
    )
    assert "'Weather 2'" in message


def test_plan_refuses_a_simulator_on_an_object_a_left_alone_connector_feeds(session):
    device = _device(session)
    _http_connector(session, device)
    _refused(
        session,
        [_file_device(simulated=[("analog-value", 1)])],
        [],
        "599001 analog-value:1 is fed by 'Weather' and also has a simulator",
    )


def test_plan_ignores_a_left_alone_connector_whose_mapped_object_goes(session):
    device = _device(session)
    _http_connector(session, device)
    plan = _plan(session, [_file_device(objects=THREE[1:])], [_http("Other", instance=2)])
    assert plan.created == ["Other"]


def test_plan_ignores_a_left_alone_connector_whose_device_goes(session):
    one = _device(session)
    _device(session, 599002)
    _bacnet_connector(session, one)
    plan = _plan(session, [_file_device(599002)], [])
    assert plan.created == [] and plan.updated == []


def test_plan_applies_the_echo_guard_against_a_left_alone_connector(session):
    device = _device(session, objects=THREE)
    left_alone = _bacnet_connector(session, device)
    left_alone.write_mappings[0].remote_object_type = "analog-output"
    left_alone.write_mappings[0].remote_object_instance = 5
    session.flush()
    proxy = {
        "object": "analog-value:3",
        "remote_object_type": "analog-output",
        "remote_object_instance": 5,
        "direction": "proxy",
    }
    _refused(
        session,
        [_file_device()],
        [_bacnet(name="Mirror", mappings=[proxy])],
        "(connector 'Room') already writes analog-output,5 present-value",
    )


def test_plan_applies_the_echo_guard_from_the_write_side(session):
    device = _device(session, objects=THREE)
    mirror = _bacnet_connector(session, device, name="Mirror")
    mirror.mappings[0].direction = "proxy"
    mirror.mappings[0].object = _obj(device, "analog-value", 3)
    mirror.mappings[0].remote_object_type = "analog-value"
    mirror.mappings[0].remote_object_instance = 8
    mirror.write_mappings[0].remote_object_instance = 9
    session.flush()
    write = {"object": "analog-value:2", "remote_object_type": "analog-value", "remote_object_instance": 8}
    _refused(
        session,
        [_file_device()],
        [_bacnet(name="Writer", write_mappings=[write])],
        "(connector 'Mirror') proxies analog-value,8 present-value",
    )


def test_plan_leaves_a_connector_missing_from_the_file_alone(session):
    device = _device(session)
    _http_connector(session, device)
    plan = _plan(session, [_file_device()], [])
    assert plan.created == [] and plan.updated == [] and plan.mappings == {}


def test_plan_keys_mapping_changes(session):
    device = _device(session, objects=THREE)
    connector = _bacnet_connector(session, device)
    connector.mappings.append(
        ConnectorMapping(
            object=_obj(device, "analog-value", 3),
            remote_object_type="analog-input",
            remote_object_instance=7,
            remote_property="present-value",
        )
    )
    connector.write_mappings.append(
        ConnectorWriteMapping(
            object=_obj(device, "analog-value", 3),
            remote_object_type="analog-output",
            remote_object_instance=4,
            remote_property="priority-array",
            remote_array_index=3,
        )
    )
    session.flush()
    file_connector = _bacnet(
        mappings=[
            {"object": "analog-value:1", "remote_object_type": "analog-input", "remote_object_instance": 1},
            {"object": "analog-value:2", "remote_object_type": "analog-input", "remote_object_instance": 2},
        ],
        write_mappings=[
            {"object": "analog-value:2", "remote_object_type": "analog-value", "remote_object_instance": 2, "priority": 5},
            {"object": "analog-value:3", "remote_object_type": "analog-output", "remote_object_instance": 6},
        ],
    )

    plan = _plan(session, [_file_device()], [file_connector, _http("Weather", instance=3)])

    assert plan.created == ["Weather"]
    assert plan.updated == ["Room"]
    assert plan.mappings["Room"] == {
        "created": ["599001 analog-value:2", "write analog-output,6 present-value"],
        "updated": ["599001 analog-value:1", "write analog-value,2 present-value"],
        "deleted": ["599001 analog-value:3", "write analog-output,4 priority-array[3]"],
    }
    assert plan.mappings["Weather"] == {"created": ["599001 analog-value:3"], "updated": [], "deleted": []}


def test_needs_credentials(session):
    device = _device(session)
    stored = _http_connector(session, device)
    stored.auth_password = None
    stored.headers = {"X-Have": "value", "X-Empty": ""}
    session.flush()
    http = _http(
        auth_type="basic",
        headers={"X-Have": None, "X-Empty": None, "X-New": None},
        mappings=[],
    )
    bearer = _http("Bearer", auth_type="bearer", mappings=[])
    kept = _http("Kept", auth_type="none", mappings=[])

    plan = _plan(session, [_file_device()], [http, bearer, kept])

    assert plan.needs_credentials == {
        "Weather": ["password", "header X-Empty", "header X-New"],
        "Bearer": ["token"],
    }


def test_needs_credentials_ignores_a_stored_password(session):
    device = _device(session)
    _http_connector(session, device)
    plan = _plan(session, [_file_device()], [_http(auth_type="basic", headers={"X-Api-Key": None})])
    assert plan.needs_credentials == {}


def test_plan_writes_nothing(session):
    device = _device(session)
    _http_connector(session, device)
    before = (session.query(Connector).count(), session.query(ConnectorMapping).count(), session.query(BacnetObject).count())
    _plan(session, [_file_device()], [_http("Other", instance=2), _http()])
    assert not session.new and not session.dirty and not session.deleted
    assert before == (session.query(Connector).count(), session.query(ConnectorMapping).count(), session.query(BacnetObject).count())
    assert session.query(Device).count() == 1
