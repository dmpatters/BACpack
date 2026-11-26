"""Tests for bacpack.objects.notification_class."""
from __future__ import annotations

import pytest

from bacpack.objects.notification_class import (
    LocalNotificationClassObject,
    build_recipient_list,
)
from bacpack.objects.notification_class_config import validate_notification_class_config


def test_validate_notification_class_config_accepts_defaults():
    validate_notification_class_config(None, None, None)  # None everywhere is valid


def test_validate_notification_class_config_rejects_wrong_length_priority():
    with pytest.raises(ValueError, match="priority"):
        validate_notification_class_config([100, 100], None, None)


def test_validate_notification_class_config_rejects_out_of_range_priority():
    with pytest.raises(ValueError, match="priority"):
        validate_notification_class_config([100, 999, 100], None, None)


def test_validate_notification_class_config_rejects_wrong_length_ack_required():
    with pytest.raises(ValueError, match="ack_required"):
        validate_notification_class_config(None, [True, True], None)


def test_validate_notification_class_config_rejects_bad_recipient_kind():
    with pytest.raises(ValueError, match="recipient"):
        validate_notification_class_config(
            None,
            None,
            [
                {
                    "recipient": {"kind": "carrier-pigeon"},
                    "valid_days": [True] * 7,
                    "from_time": "00:00",
                    "to_time": "23:59",
                    "transitions": [True, True, True],
                    "confirmed": True,
                    "process_identifier": 1,
                }
            ],
        )


def test_validate_notification_class_config_rejects_wrong_length_valid_days():
    with pytest.raises(ValueError, match="valid_days"):
        validate_notification_class_config(
            None,
            None,
            [
                {
                    "recipient": {"kind": "address", "address": "192.168.1.200"},
                    "valid_days": [True] * 6,
                    "from_time": "00:00",
                    "to_time": "23:59",
                    "transitions": [True, True, True],
                    "confirmed": True,
                    "process_identifier": 1,
                }
            ],
        )


def test_build_recipient_list_empty_for_none():
    result = build_recipient_list(None)
    assert list(result) == []


def test_build_recipient_list_address_recipient():
    result = build_recipient_list(
        [
            {
                "recipient": {"kind": "address", "address": "192.168.1.200"},
                "valid_days": [True] * 7,
                "from_time": "00:00",
                "to_time": "23:59",
                "transitions": [True, True, True],
                "confirmed": True,
                "process_identifier": 1,
            }
        ]
    )
    destinations = list(result)
    assert len(destinations) == 1
    assert destinations[0].recipient.address is not None
    assert destinations[0].issueConfirmedNotifications == True  # a wrapped Boolean, not a native bool -- confirmed via direct probe
    assert destinations[0].processIdentifier == 1


def test_build_recipient_list_device_recipient():
    result = build_recipient_list(
        [
            {
                "recipient": {"kind": "device", "instance_number": 599002},
                "valid_days": [True] * 7,
                "from_time": "00:00",
                "to_time": "23:59",
                "transitions": [True, False, True],
                "confirmed": False,
                "process_identifier": 2,
            }
        ]
    )
    destinations = list(result)
    assert len(destinations) == 1
    # .device is a real ObjectIdentifier, not a bare tuple -- [1] extracts
    # the instance number, the same accessor bacpypes3's own
    # EventAlgorithm.send_notifications uses internally (recipient.device[1]).
    assert destinations[0].recipient.device[1] == 599002
    assert destinations[0].issueConfirmedNotifications == False  # a wrapped Boolean, not a native bool


@pytest.mark.asyncio
async def test_local_notification_class_object_rejects_external_write():
    """Async, matching every other test that constructs a real bacpypes3
    local object in this plan -- _Object.__init__ schedules a fire-and-forget
    asyncio task (_post_init) that needs an already-running event loop, so
    this can't be a plain sync test with a manually-driven event loop.
    Wrapped in a real Application (with a device object -- from_object_list
    raises RuntimeError("missing device object") without one) so _post_init's
    notification-class self-lookup resolves cleanly instead of leaving an
    unretrieved task exception behind."""
    from bacpypes3.app import Application
    from bacpypes3.errors import PropertyError
    from bacpypes3.primitivedata import Unsigned

    from bacpack.models import Device
    from bacpack.objects.device import build_device_object

    device_obj = build_device_object(Device(id=599001, name="Test Device", vendor_identifier=999))
    obj = LocalNotificationClassObject(
        objectIdentifier=("notification-class", 1),
        objectName="NC-1",
        notificationClass=1,
        priority=[100, 100, 100],
        ackRequired=[True, True, True],
        recipientList=build_recipient_list(None),
    )
    Application.from_object_list([device_obj, obj])
    with pytest.raises(PropertyError) as exc:
        await obj.write_property("notificationClass", Unsigned(9))
    assert exc.value.errorCode == "writeAccessDenied"


# --- AE-CRL-B list editing --------------------------------------------------

from bacpypes3.basetypes import Destination, DeviceAddress, Recipient
from bacpypes3.primitivedata import Time

from bacpack.objects.notification_class import (
    ListElementError,
    add_entries,
    address_string,
    destination_to_entry,
    recipient_key,
    remove_entries,
)


def _entry(address="192.168.1.200", pid=1, **overrides):
    entry = {
        "recipient": {"kind": "address", "address": address},
        "valid_days": [True] * 7,
        "from_time": "00:00:00.00",
        "to_time": "23:59:59.99",
        "transitions": [True, True, True],
        "confirmed": True,
        "process_identifier": pid,
    }
    entry.update(overrides)
    return entry


def _destination(recipient, pid=1, from_time="06:00:00.00", to_time="18:00:00.00"):
    return Destination(
        validDays=[1, 1, 1, 1, 1, 0, 0],
        fromTime=Time(from_time),
        toTime=Time(to_time),
        recipient=recipient,
        processIdentifier=pid,
        issueConfirmedNotifications=False,
        transitions=[1, 1, 0],
    )


@pytest.mark.parametrize("text", ["192.168.1.200", "192.168.1.200:47809", "2:192.168.1.200", "5:0x12", "*", "3:*"])
def test_address_string_round_trips(text):
    parsed = DeviceAddress(text)
    again = DeviceAddress(address_string(parsed.networkNumber, parsed.macAddress))
    assert (again.networkNumber, bytes(again.macAddress)) == (parsed.networkNumber, bytes(parsed.macAddress))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("192.168.1.200", "192.168.1.200"),
        ("192.168.1.200:47808", "192.168.1.200"),
        ("192.168.1.200:47809", "192.168.1.200:47809"),
        ("2:192.168.1.200", "2:192.168.1.200"),
        ("*", "*"),
    ],
)
def test_address_string_is_readable(text, expected):
    parsed = DeviceAddress(text)
    assert address_string(parsed.networkNumber, parsed.macAddress) == expected


def test_destination_to_entry_address_recipient():
    entry = destination_to_entry(_destination(Recipient(address=DeviceAddress("2:192.168.1.77")), pid=5))
    assert entry == {
        "recipient": {"kind": "address", "address": "2:192.168.1.77"},
        "valid_days": [True, True, True, True, True, False, False],
        "from_time": "06:00:00.00",
        "to_time": "18:00:00.00",
        "transitions": [True, True, False],
        "confirmed": False,
        "process_identifier": 5,
    }


def test_destination_to_entry_device_recipient():
    entry = destination_to_entry(_destination(Recipient(device=("device", 599002))))
    assert entry["recipient"] == {"kind": "device", "instance_number": 599002}


def test_destination_to_entry_keeps_hundredths():
    entry = destination_to_entry(
        _destination(Recipient(device=("device", 1)), from_time="00:00:00.00", to_time="23:59:59.99")
    )
    assert (entry["from_time"], entry["to_time"]) == ("00:00:00.00", "23:59:59.99")


def test_wildcard_time_is_refused():
    with pytest.raises(ValueError):
        destination_to_entry(_destination(Recipient(device=("device", 1)), to_time="*:*:*.*"))


def test_non_device_object_recipient_is_refused():
    with pytest.raises(ValueError):
        destination_to_entry(_destination(Recipient(device=("analog-input", 5))))


def test_recipient_key_normalizes_the_default_port():
    assert recipient_key(_entry("192.168.1.200")) == recipient_key(_entry("192.168.1.200:47808"))


def test_recipient_key_includes_the_process_identifier():
    assert recipient_key(_entry(pid=1)) != recipient_key(_entry(pid=2))


def test_recipient_key_device_recipient():
    entry = _entry(recipient={"kind": "device", "instance_number": 599002}, pid=3)
    assert recipient_key(entry) == ("device", 599002, 3)


def test_add_entries_appends_a_new_key():
    assert add_entries([_entry()], [_entry("192.168.1.77")]) == [_entry(), _entry("192.168.1.77")]


def test_add_entries_same_device_other_process_is_a_second_entry():
    assert add_entries([_entry(pid=1)], [_entry(pid=2)]) == [_entry(pid=1), _entry(pid=2)]


def test_add_entries_replaces_a_matching_key_in_place():
    current = [_entry("10.0.0.1"), _entry(), _entry("10.0.0.2")]
    updated = _entry(confirmed=False)
    assert add_entries(current, [updated]) == [_entry("10.0.0.1"), updated, _entry("10.0.0.2")]


def test_add_entries_collapses_stored_duplicates_of_the_key():
    updated = _entry(confirmed=False)
    assert add_entries([_entry(), _entry("10.0.0.1"), _entry()], [updated]) == [updated, _entry("10.0.0.1")]


def test_add_entries_later_element_with_same_key_wins():
    first, second = _entry(confirmed=False), _entry(transitions=[True, False, False])
    assert add_entries([], [first, second]) == [second]


def test_add_entries_does_not_mutate_current():
    current = [_entry()]
    add_entries(current, [_entry(confirmed=False)])
    assert current == [_entry()]


def test_unparsable_stored_address_does_not_break_add():
    assert add_entries([_entry("not an address")], [_entry()]) == [_entry("not an address"), _entry()]


def test_remove_entries_removes_every_match_of_the_key():
    current = [_entry(), _entry("10.0.0.1"), _entry("192.168.1.200:47808")]
    assert remove_entries(current, [_entry(confirmed=False)]) == [_entry("10.0.0.1")]


def test_remove_entries_missing_key_fails_at_its_position():
    with pytest.raises(ListElementError) as exc:
        remove_entries([_entry()], [_entry(), _entry("10.0.0.9")])
    assert (exc.value.error_class, exc.value.error_code, exc.value.position) == (
        "services",
        "listElementNotFound",
        2,
    )


def test_remove_entries_same_key_twice_fails_at_the_second():
    with pytest.raises(ListElementError) as exc:
        remove_entries([_entry()], [_entry(), _entry()])
    assert exc.value.position == 2


# --- Recipients are checked the way they're built --------------------------


def _device_entry(instance_number):
    return _entry(recipient={"kind": "device", "instance_number": instance_number})


@pytest.mark.parametrize(
    "entry, field",
    [
        (_entry("not-an-address"), "address"),
        (_entry("localhost"), "address"),
        (_entry("65535:*"), "address"),
        (_entry("5:"), "address"),
        (_entry(from_time="nope"), "from_time"),
        (_entry(to_time="nope"), "to_time"),
        (_entry(from_time="*:00:00"), "from_time"),
        (_entry(to_time="25:00:00"), "to_time"),
        (_entry(from_time=None), "from_time"),
        (_device_entry("abc"), "instance_number"),
        (_device_entry(99999999), "instance_number"),
        (_device_entry(-1), "instance_number"),
        (_device_entry(True), "instance_number"),
    ],
)
def test_validate_refuses_a_recipient_the_build_cant_make(entry, field):
    with pytest.raises(ValueError, match=field):
        validate_notification_class_config(None, None, [entry])


@pytest.mark.parametrize(
    "address",
    ["192.168.1.200", "192.168.1.200:47809", "2:192.168.1.200", "0x12", "5:0x12", "*", "3:*", "5:12"],
)
def test_validate_accepts_every_stored_address_form_and_it_builds(address):
    validate_notification_class_config(None, None, [_entry(address)])
    assert len(list(build_recipient_list([_entry(address)]))) == 1


@pytest.mark.parametrize("instance_number", [0, 4194302])
def test_validate_accepts_a_device_recipient_in_range(instance_number):
    validate_notification_class_config(None, None, [_device_entry(instance_number)])
