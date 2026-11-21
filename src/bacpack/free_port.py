"""A UDP port to move a device to when its own is taken."""
from __future__ import annotations

import errno
import socket
from typing import Optional, Set

# Just above BACnet's 47808, a hundred ports.
FIRST_PORT = 47809
LAST_PORT = 47908


def suggest_port(host: str, taken: Set[int]) -> Optional[int]:
    """The first port in range not in `taken` that a plain UDP bind to
    (host, port) accepts, or None. The probe sets no reuse option, as the
    device's own startup check, so a port that any program holds is refused;
    `taken` covers the BACpack devices that aren't running. A host this
    machine doesn't have stops the search at once: no port would do."""
    for port in range(FIRST_PORT, LAST_PORT + 1):
        if port in taken:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((host, port))
            return port
        except OSError as exc:
            if exc.errno == errno.EADDRNOTAVAIL:
                return None
        finally:
            probe.close()
    return None


def port_is_free(host: str, port: int) -> bool:
    """Whether a plain UDP bind to (host, port) is accepted, probed as
    suggest_port does."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()
