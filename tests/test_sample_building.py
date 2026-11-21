"""Planning and saving the sample building. Probes bind real UDP ports, so this
file is in UDP_TEST_FILES."""
from __future__ import annotations

import socket

import pytest
from sqlalchemy import func, select

from bacpack import free_port, sample_building
from bacpack.models import BacnetObject, Device, get_or_create_settings
from bacpack.object_builder import build_application
from bacpack.sample_building import SampleRefused, plan_sample, save_sample

SAMPLE_IDS = [4190001, 4190101, 4190102, 4190103]


@pytest.fixture(autouse=True)
def _range(monkeypatch):
    # Away from the BACnet ports other xdist workers serve on.
    monkeypatch.setattr(free_port, "FIRST_PORT", 52610)
    monkeypatch.setattr(free_port, "LAST_PORT", 52619)
    monkeypatch.setattr(sample_building, "BACNET_PORT", 52605)


def _default_bind(session, bind="127.0.0.1/8:52605"):
    get_or_create_settings(session).bind_address = bind
    session.commit()


def _device_count(session):
    return session.scalar(select(func.count()).select_from(Device))


def test_no_default_bind_is_refused(session):
    plan = plan_sample(session)
    assert plan.refusal == "Set the server's default bind address on the Network Settings page first."
    assert [d.id for d in plan.devices] == SAMPLE_IDS
    assert all(d.bind_address is None and d.port is None for d in plan.devices)
    assert plan.notes == [] and plan.rows == []


def test_the_ahu_takes_the_default_bind_port_and_the_vavs_the_next_free_ones(session):
    _default_bind(session)
    plan = plan_sample(session)
    assert plan.refusal is None
    assert [d.port for d in plan.devices] == [52605, 52610, 52611, 52612]
    assert plan.devices[0].bind_address == "127.0.0.1/8:52605"
    assert plan.devices[1].bind_address == "127.0.0.1/8:52610"
    assert [d.role for d in plan.devices] == ["bbmd", "normal", "normal", "normal"]
    assert [d.object_count for d in plan.devices] == [13, 5, 5, 5]
    assert "finds all four devices" in plan.notes[0].text and "UDP 52605" in plan.notes[0].text
    assert plan.notes[0].guide is None
    assert "UDP 52610-52612 in the firewall" in plan.notes[1].text
    assert plan.notes[1].guide == "sample-building.md#firewall"
    assert "passively" in plan.notes[2].text


def test_a_default_bind_without_a_port_gets_an_explicit_47808_for_the_ahu(session, monkeypatch):
    monkeypatch.setattr(sample_building, "BACNET_PORT", 47808)
    monkeypatch.setattr(sample_building, "free_port", _AlwaysFree)
    _default_bind(session, "127.0.0.1/8")
    plan = plan_sample(session)
    assert plan.devices[0].bind_address == "127.0.0.1/8:47808"
    assert plan.devices[0].port == 47808
    assert "UDP 52610-52612" in plan.notes[1].text


class _AlwaysFree:
    """free_port with the 47808 probe answered yes, so no real port is touched."""

    FIRST_PORT = 52610
    LAST_PORT = 52619
    suggest_port = staticmethod(free_port.suggest_port)

    @staticmethod
    def port_is_free(host, port):
        return True


def test_the_ahu_moves_when_a_device_already_has_the_port(session):
    _default_bind(session)
    session.add(Device(id=1, name="Taken", bind_address="127.0.0.1/8:52605"))
    session.commit()
    plan = plan_sample(session)
    assert [d.port for d in plan.devices] == [52610, 52611, 52612, 52613]
    assert "is taken (by another program or another BACpack device)" in plan.notes[0].text
    assert "127.0.0.1:52610" in plan.notes[0].text
    assert plan.notes[0].guide == "sharing-the-bacnet-port.md#3-another-port-foreign-device-registration"
    assert "UDP 52610-52613" in plan.notes[1].text


def test_a_device_with_no_bind_of_its_own_holds_the_default_port(session):
    _default_bind(session)
    session.add(Device(id=1, name="On default"))
    session.commit()
    assert plan_sample(session).devices[0].port == 52610


def test_the_ahu_moves_when_another_program_holds_the_port(session):
    _default_bind(session)
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", 52605))
    try:
        plan = plan_sample(session)
    finally:
        held.close()
    assert plan.devices[0].port == 52610


def test_the_vavs_skip_existing_devices_and_each_other(session):
    _default_bind(session)
    session.add(Device(id=1, name="Taken", bind_address="127.0.0.1/8:52611"))
    session.commit()
    assert [d.port for d in plan_sample(session).devices] == [52605, 52610, 52612, 52613]


def test_no_free_port_is_refused(session, monkeypatch):
    monkeypatch.setattr(free_port, "LAST_PORT", 52611)
    _default_bind(session)
    plan = plan_sample(session)
    assert "no free UDP port" in plan.refusal
    assert all(d.port is None for d in plan.devices)


def test_a_host_this_computer_does_not_have_is_refused_with_a_reason(session):
    _default_bind(session, "203.0.113.5/24")
    plan = plan_sample(session)
    assert "no free UDP port" in plan.refusal and "203.0.113.5" in plan.refusal


def test_an_existing_device_id_or_name_is_a_clash(session):
    _default_bind(session)
    session.add(Device(id=4190101, name="Mine"))
    session.commit()
    assert "4190101" in plan_sample(session).refusal
    session.delete(session.get(Device, 4190101))
    session.add(Device(id=1, name="Sample VAV-2"))
    session.commit()
    assert '"Sample VAV-2"' in plan_sample(session).refusal


def test_planning_writes_nothing(session):
    _default_bind(session)
    before = _device_count(session)
    plan_sample(session)
    session.rollback()
    assert _device_count(session) == before


# --- save_sample ----------------------------------------------------------------


def test_save_creates_the_four_devices_with_their_objects_links_and_binds(session_factory):
    with session_factory() as session:
        _default_bind(session)
        keep = Device(id=1, name="Keep")
        session.add(keep)
        session.add(BacnetObject(device=keep, object_type="analog-value", instance_number=1, name="AV"))
        session.commit()
    with session_factory() as session:
        plan = save_sample(session)
    with session_factory() as session:
        devices = {d.id: d for d in session.scalars(select(Device)).all()}
        assert sorted(devices) == [1, *SAMPLE_IDS]
        assert [len(devices[i].objects) for i in SAMPLE_IDS] == [13, 5, 5, 5]
        assert [o.name for o in devices[1].objects] == ["AV"]
        ahu = devices[4190001]
        assert ahu.network_role == "bbmd"
        assert sorted(link.linked_device_id for link in ahu.bbmd_links) == SAMPLE_IDS[1:]
        assert [devices[i].bind_address for i in SAMPLE_IDS] == [d.bind_address for d in plan.devices]
        assert all(o.trend_log_instance for o in ahu.objects if o.trended)


async def test_a_saved_device_builds(session_factory):
    with session_factory() as session:
        _default_bind(session)
    with session_factory() as session:
        save_sample(session)
    with session_factory() as session:
        device = session.get(Device, 4190001)
        build_application(device, list(device.objects), trend_session_factory=session_factory)


def test_save_refuses_a_clash_and_writes_nothing(session_factory):
    with session_factory() as session:
        _default_bind(session)
        session.add(Device(id=1, name="Sample VAV-2"))
        session.commit()
    with session_factory() as session:
        with pytest.raises(SampleRefused, match="Sample VAV-2"):
            save_sample(session)
    with session_factory() as session:
        assert _device_count(session) == 1


def test_save_twice_refuses_the_second(session_factory):
    with session_factory() as session:
        _default_bind(session)
    with session_factory() as session:
        save_sample(session)
    with session_factory() as session:
        with pytest.raises(SampleRefused, match="4190001"):
            save_sample(session)
    with session_factory() as session:
        assert _device_count(session) == 4


def test_save_with_no_default_bind_refuses(session_factory):
    with session_factory() as session:
        with pytest.raises(SampleRefused, match="default bind"):
            save_sample(session)
    with session_factory() as session:
        assert _device_count(session) == 0


# --- notes when the default bind's own port is not the BACnet port ----------------


def test_an_ahu_on_a_deliberate_other_port_says_a_who_is_will_not_reach_it(session):
    _default_bind(session, "127.0.0.1/8:52620")
    plan = plan_sample(session)
    assert plan.devices[0].port == 52620
    text = plan.notes[0].text
    assert "Sample AHU-1 is on UDP 52620, the default bind address's port" in text
    assert "a Who-Is on UDP 52605 won't reach it" in text
    assert "127.0.0.1:52620" in text and "remote node" in text
    assert plan.notes[0].guide == "sharing-the-bacnet-port.md#3-another-port-foreign-device-registration"
    assert "is taken" not in text


def test_an_ahu_moved_off_a_deliberate_other_port_names_that_port_as_taken(session):
    _default_bind(session, "127.0.0.1/8:52620")
    session.add(Device(id=1, name="Taken", bind_address="127.0.0.1/8:52620"))
    session.commit()
    plan = plan_sample(session)
    assert plan.devices[0].port == 52610
    assert plan.notes[0].text.startswith("UDP 52620 on 127.0.0.1 is taken")


def test_the_firewall_note_offers_the_ahu_alone_only_when_the_ahu_is_open(session):
    _default_bind(session)
    assert "or use only the AHU" in plan_sample(session).notes[1].text


def test_the_firewall_note_does_not_offer_the_ahu_alone_when_it_is_not_on_the_open_port(session):
    _default_bind(session, "127.0.0.1/8:52620")
    note = plan_sample(session).notes[1].text
    assert "or use only the AHU" not in note
    assert "UDP 52610-52612" not in note and "52620" in note
