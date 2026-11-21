"""The interop parsers on text captured from the bacnet-stack 1.7.0 tools."""
from __future__ import annotations

from datetime import date, datetime

from tests.interop import parse

WHOIS = """;Device   MAC (hex)            SNET  SADR (hex)           APDU
;-------- -------------------- ----- -------------------- ----
  599101  7F:00:00:02:BA:CC    0     00                   1024
  599102  7F:00:00:02:BA:CD    0     00                   1024
  599102  7F:00:00:02:BA:CD    0     00                   1024
;
; Total Devices: 2
"""

RPM = """analog-input #1
{
    object-name: "IOP Temp"
    present-value: 21.500000
    status-flags: {false,false,false,false}
}
analog-output #1
{
    priority-array: {Null,
        Null,
        33.000000}
    relinquish-default: 10.000000
}
"""

WPM_BAD = "BACnet Error: property: write-access-denied\nBACnet Error: analog-input 1: present-value\n"

RR = """trend-log #1
{
    log-buffer: {{{2026-11-09-07:19:54.00}, real:50.000000},
        {{2026-11-09-07:19:55.00}, real:51.500000}}
}
"""

GE = """
Sending first GetEventInformation request ...
Recieved Ack. Saved invoke ID was 1, service returned 1
Decode of Ack returned length 65. MoreEvents flag was 0
DeviceID	Type	Instance	eventState
--------------- ------- --------------- ---------------
599101		0	2		HL

1	 Total
"""

EPICS = """List of Objects in Test Device:
{
  {
    object-identifier: (device, 599101)
    object-list: {
        (device, 599101), (file, 1), (network-port, 1),
        (analog-input, 1) }
    max-apdu-length-accepted: 1024
  }
  {
    object-identifier: (analog-input, 1)
    object-name: "x"
  }
}
"""


def test_whois_collapses_duplicates():
    assert parse.parse_whois(WHOIS) == {599101: 1024, 599102: 1024}


def test_rpm_objects_and_continued_values():
    objects = parse.parse_rpm(RPM)
    assert objects[("analog-input", 1)]["object-name"] == '"IOP Temp"'
    assert parse.parse_bool_array(objects[("analog-input", 1)]["status-flags"]) == [False] * 4
    assert parse.parse_array(objects[("analog-output", 1)]["priority-array"]) == ["Null", "Null", "33.000000"]
    assert objects[("analog-output", 1)]["relinquish-default"] == "10.000000"


def test_rp_value():
    assert parse.parse_rp('"IOP-1"\n') == "IOP-1"
    assert parse.parse_rp("22\n") == "22"


def test_errors():
    assert parse.parse_error("BACnet Error: property: write-access-denied\n") == ("property", "write-access-denied")
    assert parse.parse_error("BACnet Error: security: password-failure") == ("security", "password-failure")
    assert parse.parse_error("BACnet Reject: invalid-tag\n") == ("reject", "invalid-tag")
    assert parse.parse_error("\nWriteProperty Acknowledged!\n") is None


def test_wpm_names_the_first_failed_write():
    assert parse.parse_wpm_failed(WPM_BAD) == (
        "property", "write-access-denied", "analog-input", 1, "present-value"
    )
    assert parse.parse_wpm_failed("WriteProperty Acknowledged!") is None


def test_cov_notifications_counted():
    assert parse.count_cov_notifications("x\nUCOV: Received Notification!\nUCOV: Received Notification!\n") == 2


def test_trend_records():
    assert parse.parse_trend_records(RR) == [
        ("2026-11-09-07:19:54.00", "50.000000"),
        ("2026-11-09-07:19:55.00", "51.500000"),
    ]
    assert parse.parse_trend_records("trend-log #1\n{\n    log-buffer: {}\n}\n") == []


def test_event_information():
    assert parse.parse_event_information(GE) == [(599101, 0, 2, "HL")]
    assert parse.parse_event_information("Decode of Ack returned length -1.") == []


def test_who_has():
    text = "I-Have: binary-value 1 from device 599102!\nI-Have: binary-value 1 from device 599101!\n"
    assert parse.parse_who_has(text) == {("binary-value", 1, 599102), ("binary-value", 1, 599101)}


def test_epics_objects_and_errors():
    assert parse.parse_epics_object_list(EPICS) == {("device", 599101), ("file", 1), ("network-port", 1), ("analog-input", 1)}
    assert parse.parse_epics_objects(EPICS) == {("device", 599101), ("analog-input", 1)}
    assert parse.parse_epics_errors(EPICS) == []
    assert parse.parse_epics_errors("ok\nBACnet Error: x\nAbort: y") == ["BACnet Error: x", "Abort: y"]


def test_clock_values():
    assert parse.parse_clock_seconds("07:20:56.12") == 7 * 3600 + 20 * 60 + 56.12
    assert parse.parse_date("Monday, November 9, 2026") == date(2026, 11, 9)
    assert parse.parse_device_datetime("Monday, November 9, 2026", "23:59:58.50") == datetime(2026, 11, 9, 23, 59, 58, 500000)
    assert parse.parse_timestamp("2026/11/09-07:19:53.94") == datetime(2026, 11, 9, 7, 19, 53, 940000)
    assert parse.parse_timestamp("2026-11-09-07:19:54.00") == datetime(2026, 11, 9, 7, 19, 54)
