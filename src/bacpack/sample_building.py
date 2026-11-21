"""The packaged sample building: plan the
ports it would get on this server, and add it. Add-only: the whole-server
config import would delete every other device, so this goes through
config_io.create_devices."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import free_port
from .config_io import ParsedConfig, create_devices, parse_config_document
from .config_schemas import ConfigImportError
from .db import lock_both_files
from .device_network import bind_with_port, ports_in_use, split_socket
from .log_names import device_ref, quote
from .models import Device, get_or_create_settings

# The port a BMS searches; read at call time so tests can move it.
BACNET_PORT = 47808

_FOREIGN_GUIDE = "sharing-the-bacnet-port.md#3-another-port-foreign-device-registration"
_AHU_NAME = "Sample AHU-1"
_NO_BIND = "Set the server's default bind address on the Network Settings page first."
_DELETE_FIRST = "delete the old sample devices before loading the sample again"


@dataclass(frozen=True)
class SampleNote:
    text: str
    guide: Optional[str]


@dataclass(frozen=True)
class SampleDevice:
    id: int
    name: str
    role: str
    object_count: int
    bind_address: Optional[str]
    port: Optional[int]


@dataclass
class SamplePlan:
    devices: List[SampleDevice]
    notes: List[SampleNote] = field(default_factory=list)
    refusal: Optional[str] = None
    # The parsed rows with their binds filled in, for create_devices.
    rows: List[dict] = field(default_factory=list)


class SampleRefused(Exception):
    """The sample can't be added; the message says why."""


def load_document() -> ParsedConfig:
    text = (resources.files("bacpack") / "samples" / "sample-building.json").read_text(encoding="utf-8")
    return parse_config_document(json.loads(text))


def _clash(session: Session, rows: List[dict]) -> Optional[str]:
    for row in rows:
        if session.get(Device, row["id"]) is not None:
            return f"{device_ref(row['id'], row['name'])} already exists on this server; {_DELETE_FIRST}"
    for row in rows:
        if session.scalar(select(Device.id).where(Device.name == row["name"]).limit(1)) is not None:
            return f"a device named {quote(row['name'])} already exists on this server; {_DELETE_FIRST}"
    return None


def _notes(host: str, wanted_port: int, ports: List[int]) -> List[SampleNote]:
    """`wanted_port`: the default bind's own port, the one the AHU asks for."""
    ahu_port = ports[0]
    if ahu_port == BACNET_PORT:
        find = SampleNote(
            f"A Who-Is on UDP {BACNET_PORT} finds all four devices: the AHU answers and forwards the Who-Is to the VAVs.",
            None,
        )
    elif ahu_port == wanted_port:
        find = SampleNote(
            f"{_AHU_NAME} is on UDP {ahu_port}, the default bind address's port, so a Who-Is on UDP {BACNET_PORT} "
            f"won't reach it. Register your tool as a foreign device with {_AHU_NAME} at {host}:{ahu_port} and "
            "search, or add it as a remote node.",
            _FOREIGN_GUIDE,
        )
    else:
        find = SampleNote(
            f"UDP {wanted_port} on {host} is taken (by another program or another BACpack device), so {_AHU_NAME} "
            f"is on UDP {ahu_port}. To find all four devices, register your tool as a foreign device with "
            f"{_AHU_NAME} at {host}:{ahu_port} and search again.",
            _FOREIGN_GUIDE,
        )
    # The installers already open BACNET_PORT; the AHU alone is enough only when it is on it.
    ahu_open = ahu_port == BACNET_PORT
    needed = sorted(port for port in ports if port != BACNET_PORT)
    span = f"UDP {needed[0]}" if needed[0] == needed[-1] else f"UDP {needed[0]}-{needed[-1]}"
    firewall = SampleNote(
        f"From another computer, allow {span} in the firewall (the installer opens only {BACNET_PORT})"
        + (", or use only the AHU." if ahu_open else "."),
        "sample-building.md#firewall",
    )
    limit = SampleNote(
        "The VAVs' own broadcasts (their I-Am at startup) stay on their own ports, so a BMS that only listens "
        "passively won't see them. A search finds them.",
        None,
    )
    return [find, firewall, limit]


def plan_sample(session: Session) -> SamplePlan:
    """What loading would do on this server; writes nothing."""
    rows = load_document().devices
    unplanned = [SampleDevice(r["id"], r["name"], r["network_role"], len(r["objects"]), None, None) for r in rows]

    default = get_or_create_settings(session).bind_address or None
    parts = split_socket(default) if default else None
    if default is None or parts is None:
        return SamplePlan(unplanned, refusal=_NO_BIND)
    clash = _clash(session, rows)
    if clash is not None:
        return SamplePlan(unplanned, refusal=clash)

    host, own_port = parts
    taken = ports_in_use(session, host)
    ports: List[int] = []
    for index in range(len(rows)):
        if index == 0 and own_port not in taken and free_port.port_is_free(host, own_port):
            port = own_port
        else:
            found = free_port.suggest_port(host, taken)
            if found is None:
                return SamplePlan(
                    unplanned,
                    refusal=(
                        f"no free UDP port from {free_port.FIRST_PORT} to {free_port.LAST_PORT} on {host} "
                        "(or this computer does not have that address)"
                    ),
                )
            port = found
        taken.add(port)
        ports.append(port)

    binds = [bind_with_port(default, port) for port in ports]
    planned_rows = [{**row, "bind_address": bind} for row, bind in zip(rows, binds, strict=True)]
    devices = [
        SampleDevice(r["id"], r["name"], r["network_role"], len(r["objects"]), bind, port)
        for r, bind, port in zip(rows, binds, ports, strict=True)
    ]
    return SamplePlan(devices, _notes(host, own_port, ports), None, planned_rows)


def save_sample(session: Session) -> SamplePlan:
    """Add the sample. Blocking (waits on the database's write locks): run it
    in a worker thread. Planning happens under the locks, so two loads can't
    both pass the clash check."""
    lock_both_files(session)
    plan = plan_sample(session)
    if plan.refusal is not None:
        session.rollback()
        raise SampleRefused(plan.refusal)
    try:
        create_devices(session, plan.rows)
    except ConfigImportError as exc:
        session.rollback()
        raise SampleRefused(str(exc)) from exc
    session.commit()
    return plan
