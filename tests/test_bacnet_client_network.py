"""End to end over real UDP on loopback: device 1 reads from and writes to
device 2 through a bacnet connector. Static address, not Who-Is: loopback
doesn't carry the broadcast reliably."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.pdu import Address
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.primitivedata import ObjectIdentifier, PropertyIdentifier, Real

from bacpack.bacnet import write_manual_value
from bacpack.bacnet_client import BacnetClient, Binding, to_jsonable
from bacpack.bacnet_connectors import BacnetConnectorRunner
from bacpack.models import BacnetObject, Connector, ConnectorMapping, ConnectorWriteMapping, Device
from bacpack.object_builder import build_application
from bacpack.schedule_external import ExternalScheduleWriter


def _build(session_factory, device_id, bind_address):
    with session_factory() as session:
        device = session.get(Device, device_id)
        try:
            return build_application(device, list(device.objects), bind_address=bind_address)
        except OSError as exc:
            pytest.skip(f"can't bind {bind_address}: {exc}")


def _build_with_config(session_factory, device_id, bind_address):
    """Like _build, but gives Schedules their DB row ids (the writer keys
    status by them), as device_process does."""
    with session_factory() as session:
        device = session.get(Device, device_id)
        try:
            return build_application(
                device, list(device.objects), bind_address=bind_address, trend_session_factory=session_factory
            )
        except OSError as exc:
            pytest.skip(f"can't bind {bind_address}: {exc}")


@pytest.mark.asyncio
async def test_connector_reads_writes_and_relinquishes_over_real_udp(session_factory):
    with session_factory() as session:
        remote = Device(id=2, name="Remote")
        local = Device(id=1, name="Local")
        local_in = BacnetObject(device=local, object_type="analog-value", instance_number=1, name="In")
        local_out = BacnetObject(device=local, object_type="analog-value", instance_number=2, name="Out")
        connector = Connector(
            name="To remote", type="bacnet", url="", device=local,
            remote_device_instance=2, remote_address="127.0.0.2", timeout_seconds=3,
        )
        write_mapping = ConnectorWriteMapping(
            connector=connector, object=local_out,
            remote_object_type="analog-value", remote_object_instance=8, priority=9,
        )
        session.add_all(
            [
                remote,
                BacnetObject(device=remote, object_type="analog-value", instance_number=7, name="AV-7"),
                BacnetObject(
                    device=remote, object_type="analog-value", instance_number=8, name="AV-8",
                    bacnet_writable=True,
                ),
                local, local_in, local_out, connector,
                ConnectorMapping(
                    connector=connector, object=local_in,
                    remote_object_type="analog-value", remote_object_instance=7,
                    remote_property="present-value",
                ),
                write_mapping,
            ]
        )
        session.commit()
        write_mapping_id = write_mapping.id

    remote_app = _build(session_factory, 2, "127.0.0.2/8")
    local_app = _build(session_factory, 1, "127.0.0.1/8")
    try:
        remote_av7 = remote_app.get_object_id(ObjectIdentifier("analog-value,7"))
        remote_av8 = remote_app.get_object_id(ObjectIdentifier("analog-value,8"))
        await write_manual_value(remote_av7, 42.0)
        await write_manual_value(local_app.get_object_id(ObjectIdentifier("analog-value,2")), 17.0)
        runner = BacnetConnectorRunner(session_factory, local_app, 1, client=BacnetClient(local_app))

        await runner.tick()
        await runner.idle()

        assert local_app.get_object_id(ObjectIdentifier("analog-value,1")).presentValue == 42.0
        assert remote_av8.presentValue == 17.0
        with session_factory() as session:
            status = session.query(Connector).one().runtime.client_status
        assert status["last_success"] is True
        assert status["write_status"][str(write_mapping_id)]["error"] is None

        with session_factory() as session:
            session.delete(session.get(ConnectorWriteMapping, write_mapping_id))
            session.commit()
        runner.mark_changed()
        await runner.tick()
        await runner.idle()

        assert remote_av8.presentValue == remote_av8.relinquishDefault
    finally:
        local_app.close()
        remote_app.close()


@pytest.mark.asyncio
async def test_schedule_writes_a_remote_point_over_real_udp(session_factory):
    """A schedule on device 1 writes AV-8 on device 2 at its
    Priority_For_Writing. Schedules bind by Who-Is only, which loopback doesn't
    carry reliably, so the binding is pre-seeded with device 2's address."""
    with session_factory() as session:
        remote = Device(id=2, name="Remote")
        local = Device(id=1, name="Local")
        session.add_all(
            [
                remote,
                BacnetObject(
                    device=remote, object_type="analog-value", instance_number=8, name="AV-8",
                    bacnet_writable=True,
                ),
                local,
                BacnetObject(
                    device=local, object_type="schedule", instance_number=1, name="Sched-1",
                    schedule_value_type="analog", schedule_default="10.0",
                    weekly_schedule=[[{"time": "00:00", "value": "33.0"}] for _ in range(7)],
                    schedule_priority=12,
                    schedule_write_targets=[
                        {"device_instance": 2, "object_type": "analog-value", "instance_number": 8,
                         "property_identifier": "present-value"}
                    ],
                ),
            ]
        )
        session.commit()

    remote_app = _build(session_factory, 2, "127.0.0.2/8")
    local_app = _build_with_config(session_factory, 1, "127.0.0.1/8")
    try:
        client = BacnetClient(local_app)
        client._bindings[client._key(2, None)] = Binding(Address("127.0.0.2"))
        writer = ExternalScheduleWriter(session_factory, local_app, 1, client=client)
        for _ in range(10):  # let the schedule evaluate
            await asyncio.sleep(0.02)

        await writer.tick()
        await writer.idle()

        remote_av8 = remote_app.get_object_id(ObjectIdentifier("analog-value,8"))
        assert remote_av8.presentValue == 33.0
        assert remote_av8.priorityArray[11].real == 33.0  # slot 12
    finally:
        local_app.close()
        remote_app.close()


@pytest.mark.asyncio
async def test_proxy_mirrors_and_passes_writes_through_over_real_udp(session_factory):
    """Device 1's SP proxies device 2's AV-8, and Locked proxies AV-9 (not
    BACnet-writable). Device 2's own application doubles as the BMS: it writes
    device 1 over the wire, and device 1 passes that on to device 2."""
    with session_factory() as session:
        remote = Device(id=2, name="Remote")
        local = Device(id=1, name="Local")
        sp = BacnetObject(device=local, object_type="analog-value", instance_number=1, name="SP")
        locked = BacnetObject(device=local, object_type="analog-value", instance_number=2, name="Locked")
        connector = Connector(
            name="To remote", type="bacnet", url="", device=local,
            remote_device_instance=2, remote_address="127.0.0.2", timeout_seconds=3,
        )
        session.add_all(
            [
                remote,
                BacnetObject(device=remote, object_type="analog-value", instance_number=8, name="AV-8"),
                BacnetObject(
                    device=remote, object_type="analog-value", instance_number=9, name="AV-9",
                    bacnet_writable=False,
                ),
                local, sp, locked, connector,
                ConnectorMapping(
                    connector=connector, object=sp, remote_object_type="analog-value",
                    remote_object_instance=8, remote_property="present-value", direction="proxy",
                ),
                ConnectorMapping(
                    connector=connector, object=locked, remote_object_type="analog-value",
                    remote_object_instance=9, remote_property="present-value", direction="proxy",
                ),
            ]
        )
        session.commit()

    remote_app = _build(session_factory, 2, "127.0.0.2/8")
    local_app = _build(session_factory, 1, "127.0.0.1/8")
    try:
        remote_av8 = remote_app.get_object_id(ObjectIdentifier("analog-value,8"))
        proxy = local_app.get_object_id(ObjectIdentifier("analog-value,1"))
        runner = BacnetConnectorRunner(session_factory, local_app, 1, client=BacnetClient(local_app))
        await runner.tick()
        await runner.idle()
        present_value = PropertyIdentifier("present-value")

        # The BMS writes the proxy at 8: it lands in device 2's slot 8.
        await remote_app.write_property(
            Address("127.0.0.1"), ObjectIdentifier("analog-value,1"), present_value, Real(72.0), None, 8
        )
        assert remote_av8.presentValue == 72.0
        assert to_jsonable(remote_av8.priorityArray[7]) == 72.0

        # Someone writes device 2 directly at 5; the proxy follows on its next read.
        await remote_av8.write_property("presentValue", Real(68.0), priority=5)
        await runner.tick()
        await runner.idle()
        assert proxy.presentValue == 68.0
        assert [to_jsonable(s) for s in proxy.priorityArray] == [to_jsonable(s) for s in remote_av8.priorityArray]

        # A write device 2 refuses comes back to the BMS as device 2's own error.
        with pytest.raises(ErrorRejectAbortNack) as caught:
            await remote_app.write_property(
                Address("127.0.0.1"), ObjectIdentifier("analog-value,2"), present_value, Real(1.0), None, 8
            )
        assert str(caught.value.errorCode) == "write-access-denied"
    finally:
        local_app.close()
        remote_app.close()
