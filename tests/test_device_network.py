"""A device's network role, BBMD table and link rules."""
from __future__ import annotations

import pytest

from bacpack.device_network import NetworkConfig, check_network, effective_bind, resolve_network, same_network, socket_of
from bacpack.models import Device, DeviceLink, get_or_create_settings


def _add(session, device_id, name, bind=None, **fields):
    device = Device(id=device_id, name=name, bind_address=bind, **fields)
    session.add(device)
    session.flush()
    return device


def _check(session, device, **overrides):
    values = dict(role="bbmd", foreign_bbmd=None, foreign_ttl=60, bbmd_peers=None, links=[])
    values.update(overrides)
    return check_network(session, device, **values)


# --- addresses ---------------------------------------------------------------

@pytest.mark.parametrize(
    "bind, expected",
    [
        ("192.168.1.200/24", "192.168.1.200:47808"),
        ("192.168.1.200/24:47810", "192.168.1.200:47810"),
        ("192.168.1.200:47810", "192.168.1.200:47810"),
        ("192.168.1.200", "192.168.1.200:47808"),
    ],
)
def test_socket_of_writes_one_socket_one_way(bind, expected):
    assert socket_of(bind) == expected


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("192.168.1.200/24", "192.168.1.221/24", True),
        ("192.168.1.200/24:47810", "192.168.1.221/24:47810", True),
        ("192.168.1.200/24", "192.168.1.200/24:47810", False),
        ("192.168.56.1/24", "192.168.1.200/24", False),
        ("192.168.1.200/24", "192.168.1.221", True),
        ("192.168.1.200", "192.168.1.221", False),
    ],
)
def test_same_network_needs_the_same_port_and_subnet(a, b, expected):
    assert same_network(a, b) is expected


# --- resolve_network ---------------------------------------------------------

def test_a_bbmds_table_is_itself_its_links_then_typed_peers(session):
    bbmd = _add(session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd", bbmd_peers=["10.0.0.9:47808"])
    _add(session, 2, "E", "192.168.1.200/24:47811")
    _add(session, 3, "F", "192.168.1.200/24:47812")
    bbmd.bbmd_links = [DeviceLink(linked_device_id=3), DeviceLink(linked_device_id=2)]
    session.flush()

    network = resolve_network(session, bbmd)

    assert network.bdt == ("192.168.1.200:47810", "192.168.1.200:47811", "192.168.1.200:47812", "10.0.0.9:47808")
    assert network.foreign_bbmd is None


def test_the_same_socket_written_two_ways_is_one_entry(session):
    bbmd = _add(
        session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd",
        bbmd_peers=["192.168.1.200:47811", "192.168.1.200:47810"],
    )
    _add(session, 2, "E", "192.168.1.200:47811")
    bbmd.bbmd_links = [DeviceLink(linked_device_id=2)]
    session.flush()

    assert resolve_network(session, bbmd).bdt == ("192.168.1.200:47810", "192.168.1.200:47811")


def test_a_stored_peer_with_a_prefix_still_resolves_to_its_socket(session):
    bbmd = _add(
        session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd", bbmd_peers=["10.0.0.5/24:47808"]
    )

    assert resolve_network(session, bbmd).bdt == ("192.168.1.200:47810", "10.0.0.5:47808")


def test_a_linked_device_on_the_server_default_bind_uses_the_defaults_socket(session):
    get_or_create_settings(session).bind_address = "192.168.1.200/24"
    bbmd = _add(session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd")
    _add(session, 2, "E")
    bbmd.bbmd_links = [DeviceLink(linked_device_id=2)]
    session.flush()

    assert resolve_network(session, bbmd).bdt == ("192.168.1.200:47810", "192.168.1.200:47808")


def test_a_linked_device_with_no_bind_anywhere_is_left_out(session):
    bbmd = _add(session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd")
    _add(session, 2, "E")
    bbmd.bbmd_links = [DeviceLink(linked_device_id=2)]
    session.flush()

    assert resolve_network(session, bbmd).bdt == ("192.168.1.200:47810",)


@pytest.mark.parametrize(
    "linked_fields",
    [
        {"network_role": "foreign", "foreign_bbmd": "10.0.0.5"},
        # Reached by changing the server default, which no device save checks.
        {"bind_address": None},
    ],
)
def test_a_linked_device_that_no_longer_qualifies_is_left_out(session, linked_fields):
    get_or_create_settings(session).bind_address = "192.168.1.221/24:47810"
    bbmd = _add(session, 1, "D", "192.168.1.200/24:47810", network_role="bbmd")
    linked = _add(session, 2, "E", "192.168.1.200/24:47811")
    bbmd.bbmd_links = [DeviceLink(linked_device_id=2)]
    for field, value in linked_fields.items():
        setattr(linked, field, value)
    session.flush()

    assert resolve_network(session, bbmd).bdt == ("192.168.1.200:47810",)


def test_only_a_bbmd_has_a_table(session):
    device = _add(session, 1, "D", "192.168.1.200/24:47810", bbmd_peers=["10.0.0.9"])
    assert resolve_network(session, device) == NetworkConfig(foreign_bbmd=None, foreign_ttl=60, bdt=None)


@pytest.mark.parametrize("role", ["normal", "bbmd"])
def test_the_ttl_counts_only_for_a_foreign_device(session, role):
    device = _add(session, 1, "D", "192.168.1.200/24:47810", network_role=role, foreign_ttl=300)
    assert resolve_network(session, device).foreign_ttl == 60


def test_a_foreign_device_carries_its_bbmd_and_ttl(session):
    device = _add(
        session, 1, "D", "192.168.1.200/24", network_role="foreign", foreign_bbmd="10.0.0.5:47808", foreign_ttl=120
    )
    assert resolve_network(session, device) == NetworkConfig(foreign_bbmd="10.0.0.5:47808", foreign_ttl=120, bdt=None)


def test_effective_bind_falls_back_to_the_server_default(session):
    get_or_create_settings(session).bind_address = "192.168.1.200/24"
    own = _add(session, 1, "D", "10.0.0.5/24")
    default = _add(session, 2, "E")
    assert effective_bind(session, own) == "10.0.0.5/24"
    assert effective_bind(session, default) == "192.168.1.200/24"


# --- check_network -----------------------------------------------------------

def test_a_valid_set_passes(session):
    device = _add(session, 1, "D", "192.168.1.200/24:47810")
    _add(session, 2, "E", "192.168.1.200/24:47811")
    assert _check(session, device, links=[2], bbmd_peers=["10.0.0.9:47808"]) is None


def test_an_unknown_role_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24")
    assert "role" in _check(session, device, role="router")


def test_a_foreign_device_needs_its_bbmd_address(session):
    device = _add(session, 1, "D", "192.168.1.200/24")
    assert "BBMD" in _check(session, device, role="foreign")
    assert _check(session, device, role="foreign", foreign_bbmd="10.0.0.5") is None


def test_a_bad_foreign_bbmd_or_peer_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24")
    assert "foreign_bbmd" in _check(session, device, role="foreign", foreign_bbmd="not-an-address")
    assert "bbmd_peers" in _check(session, device, bbmd_peers=["not-an-address"])


@pytest.mark.parametrize("peer", ["10.0.0.5/24:47808", "10.0.0.5/24"])
@pytest.mark.parametrize("role", ["bbmd", "normal"])
def test_a_bbmd_peer_with_a_prefix_is_refused(session, role, peer):
    device = _add(session, 1, "D", "192.168.1.200/24")

    message = _check(session, device, role=role, bbmd_peers=[peer])

    assert "bbmd_peers" in message and "/prefix" in message


def test_a_bbmd_peer_is_an_address_or_address_and_port(session):
    device = _add(session, 1, "D", "192.168.1.200/24")

    assert _check(session, device, bbmd_peers=["10.0.0.5:47808", "10.0.0.6"]) is None


def test_a_link_to_itself_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24:47810")
    message = _check(session, device, links=[1])
    assert "D (1)" in message and "itself" in message


def test_a_link_to_a_missing_device_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24:47810")
    assert "599" in _check(session, device, links=[599])


def test_a_link_to_a_foreign_device_is_refused_naming_it(session):
    device = _add(session, 1, "ZZ-330-D", "192.168.1.200/24:47810")
    _add(session, 599105, "ZZ-330-E", "192.168.1.200/24:47811", network_role="foreign", foreign_bbmd="10.0.0.5")
    assert _check(session, device, links=[599105]) == "ZZ-330-E (599105) is a foreign device; it can't be linked"


def test_a_link_to_a_device_on_the_same_subnet_and_port_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24")
    _add(session, 2, "E", "192.168.1.221/24")
    message = _check(session, device, links=[2])
    assert "E (2)" in message and "already on this device's network" in message


def test_a_link_to_a_device_with_no_bind_anywhere_is_refused(session):
    device = _add(session, 1, "D", "192.168.1.200/24:47810")
    _add(session, 2, "E")
    message = _check(session, device, links=[2])
    assert "E (2)" in message and "bind address" in message


def test_a_link_to_a_default_bind_device_on_the_same_network_is_refused(session):
    get_or_create_settings(session).bind_address = "192.168.1.200/24"
    device = _add(session, 1, "D", "192.168.1.221/24")
    _add(session, 2, "E")
    assert "already on this device's network" in _check(session, device, links=[2])


# --- ports and binds shared with the sample building ---------------------------

def test_bind_with_port_replaces_the_port_and_keeps_the_prefix():
    from bacpack.device_network import bind_with_port

    assert bind_with_port("127.0.0.1/8", 52610) == "127.0.0.1/8:52610"
    assert bind_with_port("127.0.0.1:47808", 52610) == "127.0.0.1:52610"
    assert bind_with_port("not an address", 52610) is None


def test_ports_in_use_counts_a_device_on_the_server_default(session):
    from bacpack.device_network import ports_in_use

    get_or_create_settings(session).bind_address = "127.0.0.1/8:52605"
    _add(session, 1, "On default")
    _add(session, 2, "Own", "127.0.0.2/8:52606")
    assert ports_in_use(session, "127.0.0.1") == {52605}
    assert ports_in_use(session, "127.0.0.1", ignore_device_id=1) == set()
