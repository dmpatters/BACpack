"""Tests for bacpack.capabilities."""
from __future__ import annotations

from bacpypes3.basetypes import ObjectTypesSupported, ServicesSupported

from bacpack import capabilities
from bacpack.application import LoggingApplication
from bacpack.models import OBJECT_TYPES

EXPECTED_EXECUTED = {
    "acknowledge-alarm": "confirmed",
    "add-list-element": "confirmed",
    "remove-list-element": "confirmed",
    "atomic-read-file": "confirmed",
    "atomic-write-file": "confirmed",
    "subscribe-cov": "confirmed",
    "read-property": "confirmed",
    "read-property-multiple": "confirmed",
    "write-property": "confirmed",
    "write-property-multiple": "confirmed",
    "device-communication-control": "confirmed",
    "reinitialize-device": "confirmed",
    "read-range": "confirmed",
    "get-event-information": "confirmed",
    "i-am": "unconfirmed",
    "i-have": "unconfirmed",
    "who-has": "unconfirmed",
    "who-is": "unconfirmed",
    "time-synchronization": "unconfirmed",
    "utc-time-synchronization": "unconfirmed",
}


def _set_bits(bitstring) -> set[str]:
    return {name for name in str(bitstring).split(";") if name}


def test_executed_services_is_pinned_exactly():
    # Pinned on purpose: a new do_* handler or a bacpypes3 upgrade that
    # changes what every device advertises on the wire must fail here.
    assert capabilities.executed_services(LoggingApplication) == EXPECTED_EXECUTED


def test_services_supported_bitstring_matches_executed_services():
    ss = capabilities.services_supported(LoggingApplication)
    assert isinstance(ss, ServicesSupported)
    assert _set_bits(ss) == set(EXPECTED_EXECUTED)


def test_services_supported_fixes_bacpypes3_unconfirmed_bit_bug():
    # bacpypes3 0.0.106 looks unconfirmed choices up in ConfirmedServiceChoice:
    # Who-Is (8) became add-list-element, Who-Has (7) atomic-write-file,
    # UnconfirmedCOVNotification (2) confirmed-event-notification.
    bits = _set_bits(capabilities.services_supported(LoggingApplication))
    assert {"who-is", "who-has"} <= bits
    # add-list-element (Who-Is's wrong bit) is now legitimately set by
    # AE-CRL-B, like atomic-write-file (Who-Has's).
    assert "confirmed-event-notification" not in bits

def test_cov_notification_receive_handlers_are_not_advertised():
    bits = _set_bits(capabilities.services_supported(LoggingApplication))
    assert "confirmed-cov-notification" not in bits
    assert "unconfirmed-cov-notification" not in bits


def test_service_kind():
    assert capabilities.service_kind("read-property") == "confirmed"
    assert capabilities.service_kind("who-is") == "unconfirmed"
    assert capabilities.service_kind("confirmed-event-notification") == "confirmed"
    assert capabilities.service_kind("unconfirmed-event-notification") == "unconfirmed"


def test_object_types_supported():
    types = capabilities.object_types_supported()
    assert types == list(OBJECT_TYPES) + ["device", "network-port", "trend-log", "file"]
    assert len(types) == 16


def test_hand_written_service_names_are_real_bit_names():
    # Catches typos in the hand-maintained sets.
    real = set(ServicesSupported._bitstring_names)
    assert capabilities.INITIATED_SERVICES <= real
    assert capabilities._NOT_ADVERTISED <= real


def test_object_type_names_are_real_bit_names():
    real = set(ObjectTypesSupported._bitstring_names)
    assert set(capabilities.object_types_supported()) <= real


def _bibb(name):
    return next(b for b in capabilities.BBC_BIBBS if b.name == name)


def _advertised():
    return capabilities.executed_services(LoggingApplication)


def test_bibb_table_has_the_22_b_bc_rows():
    assert [b.name for b in capabilities.BBC_BIBBS] == [
        "DS-RP-A", "DS-RP-B", "DS-RPM-A", "DS-RPM-B", "DS-WP-A", "DS-WP-B", "DS-WPM-B",
        "AE-N-I-B", "AE-ACK-B", "AE-INFO-B", "AE-CRL-B",
        "SCHED-E-B",
        "T-VMT-I-B", "T-ATR-B",
        "DM-DDB-A", "DM-DDB-B", "DM-DOB-B", "DM-DCC-B", "DM-TS-B", "DM-UTC-B", "DM-RD-B", "DM-BR-B",
    ]


def test_bibb_service_names_are_real_bit_names():
    real = set(ServicesSupported._bitstring_names)
    for bibb in capabilities.BBC_BIBBS:
        assert bibb.executes <= real, bibb.name
        assert bibb.initiates <= real, bibb.name


def test_ds_rp_b_is_supported():
    assert capabilities.bibb_status(_bibb("DS-RP-B"), _advertised()) == (True, [])


def test_client_role_bibbs_are_supported():
    # BACpack initiates RP/RPM/WP and Who-Is.
    for name in ("DS-RP-A", "DS-RPM-A", "DS-WP-A", "DM-DDB-A"):
        assert capabilities.bibb_status(_bibb(name), _advertised()) == (True, []), name


def test_sched_e_b_is_supported():
    # Schedules write to other devices.
    assert capabilities.bibb_status(_bibb("SCHED-E-B"), _advertised()) == (True, [])


def test_dm_ts_b_and_dm_utc_b_are_supported():
    assert capabilities.bibb_status(_bibb("DM-TS-B"), _advertised()) == (True, [])
    assert capabilities.bibb_status(_bibb("DM-UTC-B"), _advertised()) == (True, [])


def test_ae_n_i_b_is_supported_via_initiated_event_notifications():
    assert capabilities.bibb_status(_bibb("AE-N-I-B"), _advertised()) == (True, [])


def test_unmet_forces_not_supported_even_when_services_pass():
    import dataclasses

    t_vmt = dataclasses.replace(_bibb("T-VMT-I-B"), unmet="a requirement BACpack doesn't meet")
    supported, missing = capabilities.bibb_status(t_vmt, _advertised())
    assert supported is False
    assert missing == ["a requirement BACpack doesn't meet"]


def test_t_atr_b_is_supported():
    assert capabilities.bibb_status(_bibb("T-ATR-B"), _advertised()) == (True, [])


def test_expected_supported_set():
    supported = {
        b.name for b in capabilities.BBC_BIBBS if capabilities.bibb_status(b, _advertised())[0]
    }
    assert supported == {
        "DS-RP-A", "DS-RP-B", "DS-RPM-A", "DS-RPM-B", "DS-WP-A", "DS-WP-B", "DS-WPM-B",
        "AE-N-I-B", "AE-ACK-B", "AE-INFO-B", "AE-CRL-B",
        "SCHED-E-B",
        "T-VMT-I-B", "T-ATR-B",
        "DM-DDB-A", "DM-DDB-B", "DM-DOB-B", "DM-DCC-B", "DM-TS-B", "DM-UTC-B", "DM-RD-B", "DM-BR-B",
    }


def test_report_shape_and_summary():
    report = capabilities.capabilities_report(LoggingApplication, "9.9.9")
    assert report["bacpack_version"] == "9.9.9"
    assert report["protocol_revision"] == 22
    assert report["segmentation"] == "segmented-both"
    assert report["max_apdu_length"] == 1024
    assert report["data_links"] == list(capabilities.DATA_LINKS)
    assert report["bibb_profile_source"] == "Annex L (2024 edition)"
    # DM-TS-B and DM-UTC-B share an either_group: 22 rows, 21 requirements.
    assert report["bibb_summary"] == {"met": 21, "total": 21}
    assert report["object_types"] == capabilities.object_types_supported()
    assert len(report["bibbs"]) == 22
    assert report["bibbs"][0] == {
        "name": "DS-RP-A",
        "title": "Data Sharing - ReadProperty - A",
        "area": "Data Sharing",
        "supported": True,
        "missing": [],
        "note": None,
    }


def test_report_services_cover_both_directions():
    services = {
        s["name"]: s for s in capabilities.capabilities_report(LoggingApplication, "x")["services"]
    }
    assert services["read-property"] == {
        "name": "read-property", "kind": "confirmed", "executes": True, "initiates": True,
    }
    assert services["i-am"] == {
        "name": "i-am", "kind": "unconfirmed", "executes": True, "initiates": True,
    }
    assert services["confirmed-event-notification"] == {
        "name": "confirmed-event-notification", "kind": "confirmed",
        "executes": False, "initiates": True,
    }
    assert set(services) == set(EXPECTED_EXECUTED) | capabilities.INITIATED_SERVICES


def test_ae_crl_b_is_met():
    supported, missing = capabilities.bibb_status(
        next(b for b in capabilities.BBC_BIBBS if b.name == "AE-CRL-B"), _advertised()
    )
    assert (supported, missing) == (True, [])
