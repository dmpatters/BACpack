"""Parsers for the text the bacnet-stack 1.7.0 tools print. Each is tested on
output captured from the real tools (tests/test_interop_parse.py)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_WHOIS_ROW = re.compile(r"^\s*(\d+)\s+([0-9A-Fa-f:]+)\s+\d+\s+\S+\s+(\d+)\s*$")
_OBJECT_HEADER = re.compile(r"^(\S+) #(\d+)$")
_PROPERTY_LINE = re.compile(r"^ {4}(\S+): (.*)$")
_ERROR = re.compile(r"BACnet Error: ([\w-]+): ([\w-]+)\s*$")
_REJECT_ABORT = re.compile(r"BACnet (Reject|Abort)[^:]*: (.*)$")
_WPM_FAILED = re.compile(r"BACnet Error: ([\w-]+) (\d+): ([\w-]+)\s*$")
_RECORD = re.compile(r"\{\{([^{}]*)\}, ([\w-]+):([^},]*)\}")
_GE_ROW = re.compile(r"^(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s*$")
_I_HAVE = re.compile(r"I-Have: (\S+) (\d+) from device (\d+)!")
_IDENT = re.compile(r"\(([\w-]+), (\d+)\)")


def parse_whois(text: str) -> dict[int, int]:
    """device instance -> max APDU. Broadcast answers may repeat; a dict
    collapses them."""
    found = {}
    for line in text.splitlines():
        match = _WHOIS_ROW.match(line)
        if match:
            found[int(match.group(1))] = int(match.group(3))
    return found


def parse_rpm(text: str) -> dict[tuple[str, int], dict[str, str]]:
    """(object type, instance) -> {property: value text}; a value continued
    on following lines (arrays) is joined with single spaces."""
    objects: dict[tuple[str, int], dict[str, str]] = {}
    current = None
    last = None
    for line in text.splitlines():
        header = _OBJECT_HEADER.match(line)
        if header:
            current = objects.setdefault((header.group(1), int(header.group(2))), {})
            last = None
            continue
        if current is None or line.strip() in ("{", "}", ""):
            continue
        prop = _PROPERTY_LINE.match(line)
        if prop:
            last = prop.group(1)
            current[last] = prop.group(2).strip()
        elif last is not None and line.startswith("        "):
            current[last] += " " + line.strip()
    return objects


def parse_rp(text: str) -> str:
    """The value a bacrp prints, without the quotes of a string."""
    value = text.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def parse_error(text: str) -> tuple[str, str] | None:
    """(error class, error code) of a refused request, or ("reject"|"abort",
    reason); None when the tool printed neither."""
    match = _ERROR.search(text.replace("\r", ""))
    if match:
        return match.group(1), match.group(2)
    match = _REJECT_ABORT.search(text)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return None


def parse_wpm_failed(text: str) -> tuple[str, str, str, int, str] | None:
    """bacwpm prints the error, then the write that failed first:
    (class, code, object type, instance, property)."""
    lines = text.replace("\r", "").splitlines()
    for i, line in enumerate(lines):
        failed = _WPM_FAILED.search(line)
        if failed and i > 0:
            first = _ERROR.search(lines[i - 1])
            if first:
                return first.group(1), first.group(2), failed.group(1), int(failed.group(2)), failed.group(3)
    return None


def count_cov_notifications(text: str) -> int:
    """bacscov prints only that an unconfirmed notification arrived, not its
    values, and prints nothing for a confirmed one (a 1.7.0 limit)."""
    return text.count("Received Notification!")


def parse_trend_records(text: str) -> list[tuple[str, str]]:
    """(timestamp text, value text) of each record bacrr prints, in order."""
    return [(m.group(1), m.group(3)) for m in _RECORD.finditer(text)]


def parse_event_information(text: str) -> list[tuple[int, int, int, str]]:
    """(device, object type number, instance, event state abbreviation) per row."""
    rows = []
    for line in text.splitlines():
        match = _GE_ROW.match(line.replace("\t", " "))
        if match:
            rows.append((int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4)))
    return rows


def parse_who_has(text: str) -> set[tuple[str, int, int]]:
    """{(object type, instance, device)} of the I-Have answers."""
    return {(m.group(1), int(m.group(2)), int(m.group(3))) for m in _I_HAVE.finditer(text)}


def parse_epics_object_list(text: str) -> set[tuple[str, int]]:
    start = text.find("object-list: {")
    if start < 0:
        return set()
    end = text.find("}", start)
    return {(m.group(1), int(m.group(2))) for m in _IDENT.finditer(text[start:end])}


def parse_epics_objects(text: str) -> set[tuple[str, int]]:
    """The objects the EPICS lists with their own properties."""
    return {
        (m.group(1), int(m.group(2)))
        for m in re.finditer(r"^\s*object-identifier: \(([\w-]+), (\d+)\)", text, re.MULTILINE)
    }


def parse_epics_errors(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.search(r"error|reject|abort", line, re.IGNORECASE)]


def parse_bool_array(value: str) -> list[bool]:
    return [item.strip().lower() == "true" for item in value.strip("{} ").split(",") if item.strip()]


def parse_array(value: str) -> list[str]:
    """A bacrpm array value such as "{Null, 33.000000, Null}"."""
    return [item.strip() for item in value.strip("{} ").split(",") if item.strip()]


def parse_clock_seconds(value: str) -> float:
    """A printed time "07:20:56.12" as seconds since midnight."""
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_date(value: str):
    """A printed date "Monday, November 9, 2026"."""
    return datetime.strptime(value.split(", ", 1)[1], "%B %d, %Y").date()


def parse_device_datetime(date_text: str, time_text: str) -> datetime:
    """A device's printed Local_Date and Local_Time as one datetime."""
    seconds = parse_clock_seconds(time_text)
    return datetime.combine(parse_date(date_text), datetime.min.time()) + timedelta(seconds=seconds)


def parse_timestamp(value: str) -> datetime:
    """"2026/11/09-07:19:53.94" (a bacrpm event time stamp) or
    "2026-11-09-07:19:54.00" (a trend record)."""
    text = value.strip().replace("/", "-")
    date_part, time_part = text.rsplit("-", 1)
    seconds = time_part.split(".")
    stamp = datetime.strptime(f"{date_part} {seconds[0]}", "%Y-%m-%d %H:%M:%S")
    return stamp.replace(microsecond=int(seconds[1]) * 10000 if len(seconds) > 1 else 0)
