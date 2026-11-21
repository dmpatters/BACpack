"""EDE file builders for the tests, in the shapes real BIG-EU files take.
No third-party sample file is committed."""
from __future__ import annotations

import csv
import io

COLUMNS = [
    "keyname",
    "device obj.-instance",
    "object-name",
    "object-type",
    "object-instance",
    "description",
    "present-value-default",
    "min-present-value",
    "max-present-value",
    "settable",
    "supports COV",
    "hi-limit",
    "low-limit",
    "state-text-reference",
    "unit-code",
    "vendor-specific-address",
]

# Keyword -> column title, for ede_row.
_TITLES = {
    "keyname": "keyname",
    "device": "device obj.-instance",
    "name": "object-name",
    "type": "object-type",
    "instance": "object-instance",
    "description": "description",
    "default": "present-value-default",
    "settable": "settable",
    "commandable": "commandable",
    "command_value": "commandValue",
    "hi": "hi-limit",
    "low": "low-limit",
    "state_ref": "state-text-reference",
    "state_text": "state-text",
    "unit": "unit-code",
    "nc": "notification-class",
}


def ede_row(device=1000, type=0, instance=1, name="AI 1", **cells) -> dict:
    """One data row, as {column title: cell}. Keyword cells use _TITLES."""
    row = {
        "device obj.-instance": device,
        "object-type": type,
        "object-instance": instance,
        "object-name": name,
    }
    for key, value in cells.items():
        row[_TITLES[key]] = value
    return row


def device_row(instance=1000, name="Plant controller", **cells) -> dict:
    return ede_row(device=instance, type=8, instance=instance, name=name, **cells)


def _header_lines(count: int, hashes: bool) -> list[list[str]]:
    lines = [
        ["PROJECT_NAME", "Test project"],
        ["VERSION_OF_REFERENCEFILE", "1.0"],
        ["TIMESTAMP_OF_LAST_CHANGE", "2026-01-01"],
        ["AUTHOR_OF_LAST_CHANGE", "tester"],
        ["VERSION_OF_LAYOUT", "2.1"],
    ][:count]
    return [[("#" if hashes else "") + line[0], *line[1:]] for line in lines]


def ede_text(
    rows: list[dict],
    *,
    sep: str = ";",
    header_lines: int = 5,
    hashes: bool = True,
    columns: list[str] | None = None,
    comments: list[str] = (),
) -> str:
    """An EDE file as text: the header block, optional comment lines, the
    column title line (`#keyname` when `hashes`), then the rows."""
    columns = list(columns if columns is not None else COLUMNS)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=sep, lineterminator="\n")
    for line in _header_lines(header_lines, hashes):
        writer.writerow(line)
    if header_lines >= 6:
        writer.writerow(["#mandatory", "N", "Y", "Y", "Y", "Y"])
    for comment in comments:
        out.write(comment + "\n")
    titles = list(columns)
    if hashes:
        titles[0] = "#" + titles[0]
    writer.writerow(titles)
    for row in rows:
        unknown = set(row) - set(columns)
        if unknown:
            raise ValueError(f"cells for columns the file lacks: {sorted(unknown)}")
        writer.writerow([row.get(column, "") for column in columns])
    return out.getvalue()


def ede_bytes(text: str, encoding: str = "utf-8", bom: bool = False, crlf: bool = False) -> bytes:
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode(encoding)
    return (b"\xef\xbb\xbf" + data) if bom else data


def state_texts_text(rows: list[list], *, sep: str = ";", header: bool = True) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=sep, lineterminator="\n")
    if header:
        writer.writerow(["#Reference", "Text 1", "Text 2"])
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


# Every column the reader knows, for tests that need the optional ones.
WIDE = COLUMNS + ["commandable", "commandValue", "state-text", "notification-class"]


def full_rows() -> list[dict]:
    """Two devices with every shape the import handles: units, limits, a
    notification class, writable cells, state texts (reference, inline,
    missing), a schedule, a calendar, matched and unmatched trend logs,
    duplicate names, a `---` unit, and types BACpack does not build."""
    return [
        device_row(1000, "Plant", description="Plant room"),
        ede_row(type=0, instance=1, name="Temp", unit=62, hi="30", low="5,5", nc=3, keyname="P.T", description="Room"),
        ede_row(type=0, instance=2, name="Humidity", unit="---"),
        ede_row(type=1, instance=1, name="Valve", settable="N", unit=98),
        ede_row(type=2, instance=1, name="SP", default="21,5", settable="N", commandable="Y", unit=62),
        ede_row(type=3, instance=1, name="Door", state_ref="1"),
        ede_row(type=4, instance=1, name="Fan", state_text="[Stopped|Running]"),
        ede_row(type=5, instance=1, name="Enable", default="1"),
        ede_row(type=13, instance=1, name="Mode", state_ref="99"),
        ede_row(type=19, instance=1, name="Season", state_text="[Winter|Summer|Spring]", default="2"),
        ede_row(type=15, instance=3, name="Alarms"),
        ede_row(type=17, instance=1, name="Occupancy"),
        ede_row(type=6, instance=1, name="Holidays"),
        ede_row(type=20, instance=5, name="Temp.Log1", keyname="P.T.L"),
        ede_row(type=20, instance=6, name="Nothing.Matches"),
        ede_row(type=2, instance=10, name="Damper"),
        ede_row(type=2, instance=11, name="Damper"),
        ede_row(type=2, instance=12, name="Damper"),
        ede_row(type=9, instance=1, name="Loop 1"),
        ede_row(type=600, instance=1, name="Vendor thing"),
        ede_row(type=10, instance=1, name="Backup file"),
        device_row(2000, "Chiller"),
        ede_row(device=2000, type=0, instance=1, name="CH Temp"),
    ]
