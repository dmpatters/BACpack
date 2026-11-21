"""Tests for bacpack.objects.network_port.

Covers BBMD / BDT configuration. The
build_network_port_object tests only check the NetworkPortObject's
configured properties, never call Application.from_object_list() with it
-- that's what actually opens a socket / sends a foreign-device
registration, which isn't safe to do in an automated test.

Marked async/asyncio because constructing a local Object schedules a task
against the running event loop (see bacpypes3.local.object.Object.__init__).
"""
from __future__ import annotations

import pytest

from bacpack.objects.network_port import build_network_port_object, validate_network_settings


@pytest.mark.asyncio
async def test_normal_mode_by_default():
    npo = build_network_port_object("192.168.1.50/24")
    assert str(npo.bacnetIPMode) == "normal"


@pytest.mark.asyncio
async def test_foreign_device_registration_config():
    npo = build_network_port_object(
        "192.168.1.50/24", foreign_bbmd="192.168.1.5:47808", foreign_ttl=120
    )
    assert str(npo.bacnetIPMode) == "foreign"
    assert npo.fdSubscriptionLifetime == 120


@pytest.mark.asyncio
async def test_bbmd_mode_config():
    npo = build_network_port_object(
        "192.168.1.50/24",
        bbmd_peers=["192.168.2.5:47808", "192.168.3.5:47808"],
    )
    assert str(npo.bacnetIPMode) == "bbmd"
    assert npo.bbmdAcceptFDRegistrations
    assert len(npo.bbmdBroadcastDistributionTable) == 2


@pytest.mark.asyncio
async def test_foreign_and_bbmd_are_mutually_exclusive():
    with pytest.raises(ValueError):
        build_network_port_object(
            "192.168.1.50/24",
            foreign_bbmd="192.168.1.5:47808",
            bbmd_peers=["192.168.2.5:47808"],
        )


# --- validate_network_settings -------------------------------------------
# bind_address/foreign_bbmd/bbmd_peers used to save with a 200 no matter
# what -- a typo only surfaced later, at engine-start time (already a clean
# EngineError) or silently at next process boot. See the function's own
# docstring for why it validates through Address/HostNPort/BDTEntry
# directly rather than build_network_port_object -- the latter needs a
# running event loop (any bacpypes3 local Object does), which a sync
# FastAPI route dispatched to a worker thread doesn't have.


def test_validate_network_settings_accepts_valid_bind_address():
    validate_network_settings("192.168.1.50/24")


def test_validate_network_settings_accepts_missing_bind_address():
    # "not configured yet" is a deliberate, valid state -- nothing to
    # validate.
    validate_network_settings(None)
    validate_network_settings("")


def test_validate_network_settings_rejects_bad_bind_address():
    with pytest.raises(ValueError, match="invalid bind_address"):
        validate_network_settings("192.168.1.999/24")


def test_validate_network_settings_accepts_valid_foreign_bbmd():
    validate_network_settings(
        "192.168.1.50/24", network_role="foreign", foreign_bbmd="192.168.1.5:47808"
    )


def test_validate_network_settings_rejects_bad_foreign_bbmd():
    with pytest.raises(ValueError, match="invalid foreign_bbmd"):
        validate_network_settings(
            "192.168.1.50/24", network_role="foreign", foreign_bbmd="not-a-host"
        )


def test_validate_network_settings_ignores_foreign_bbmd_when_role_is_not_foreign():
    # A bad foreign_bbmd value left over from a previous role isn't
    # validated (or acted on) unless network_role is actually "foreign" --
    # mirrors EngineManager.start's own field selection.
    validate_network_settings("192.168.1.50/24", network_role="normal", foreign_bbmd="garbage")


def test_validate_network_settings_accepts_valid_bbmd_peers():
    validate_network_settings(
        "192.168.1.50/24",
        network_role="bbmd",
        bbmd_peers=["192.168.2.5:47808", "192.168.3.5:47808"],
    )


def test_validate_network_settings_rejects_bad_bbmd_peer():
    with pytest.raises(ValueError, match="invalid bbmd_peers entry"):
        validate_network_settings(
            "192.168.1.50/24",
            network_role="bbmd",
            bbmd_peers=["192.168.2.5:47808", "not-a-peer"],
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bind_address": "not-a-host/24"},
        {"bind_address": "[::1]:47808"},
        {"bind_address": "192.168.1.50/24", "network_role": "foreign", "foreign_bbmd": "bbmd.example:47808"},
        {"bind_address": "192.168.1.50/24", "network_role": "bbmd", "bbmd_peers": ["not-a-peer"]},
    ],
)
def test_validate_network_settings_refuses_names_without_a_dns_lookup(monkeypatch, kwargs):
    # bacpypes3's parsers resolve a name (seconds on a DNS timeout, or a
    # wrong host accepted if it resolves); only IPv4 forms reach them.
    import socket

    def no_dns(*args, **kwargs):
        raise AssertionError("validation did a DNS lookup")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, no_dns)

    with pytest.raises(ValueError, match="IPv4"):
        validate_network_settings(**kwargs)


@pytest.mark.parametrize(
    "address", ["192.168.1.50", "192.168.1.50/24", "192.168.1.50:47809", "192.168.1.50/24:47809"]
)
def test_validate_network_settings_accepts_every_ipv4_bind_form(address):
    validate_network_settings(address)


def test_validate_network_settings_ignores_bbmd_peers_when_role_is_not_bbmd():
    validate_network_settings("192.168.1.50/24", network_role="normal", bbmd_peers=["garbage"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, value",
    [("description", "renamed"), ("objectName", "Other-Name"), ("bacnetIPUDPPort", 47999)],
)
async def test_client_writes_are_refused(attr, value):
    from bacpypes3.basetypes import PropertyIdentifier
    from bacpypes3.errors import PropertyError

    npo = build_network_port_object("192.168.1.50/24")
    before = getattr(npo, attr)

    for form in (attr, PropertyIdentifier(attr)):
        with pytest.raises(PropertyError) as exc:
            await npo.write_property(form, value)
        assert exc.value.errorCode == "writeAccessDenied"
    assert getattr(npo, attr) == before
