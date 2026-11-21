"""Free-port suggestions for a device's bind address,
shared by the device API and the
clone plan."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ...device_network import BIND_FORM, bind_with_port, ports_in_use, split_socket
from ...free_port import suggest_port
from ...models import get_or_create_settings

NONE = {"bind_address": None, "port": None}


def server_default_bind(session: Session) -> Optional[str]:
    return get_or_create_settings(session).bind_address or None


def suggest_bind(
    session: Session, bind: Optional[str], *, ignore_device_id: Optional[int] = None, only_if_taken: bool = False
) -> dict:
    """{"bind_address", "port"}: a free port on `bind`'s host, the other
    devices' ports skipped. With `only_if_taken`, nothing is suggested while
    no other device uses `bind`'s own port (the default would do)."""
    match = BIND_FORM.fullmatch(bind) if bind else None
    if match is None:
        return dict(NONE)
    host = match.group(1)
    taken = ports_in_use(session, host, ignore_device_id)
    own = split_socket(bind) if bind else None
    if only_if_taken and own is not None and own[1] not in taken:
        return dict(NONE)
    port = suggest_port(host, taken)
    if port is None:
        return dict(NONE)
    return {"bind_address": bind_with_port(bind, port) if bind else None, "port": port}
