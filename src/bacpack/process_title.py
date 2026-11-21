"""Names a BACpack process "bacpack: ..." in ps and top instead of the
interpreter's command line. Without the setproctitle package, or on a platform
where it does nothing (Windows names its processes by their executable's
version resource instead), this is a no-op: a title never breaks startup."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEB_TITLE = "bacpack: web"
# What top shows without its command-line toggle: the kernel keeps 15 characters.
WEB_SHORT_NAME = "bacpack"
DEVICE_SHORT_NAME = "bacpack-worker"


def device_title(device_id: int) -> str:
    return f"bacpack: device {device_id}"


def set_process_title(title: str, short_name: str) -> None:
    try:
        import setproctitle

        setproctitle.setproctitle(title)
        setproctitle.setthreadtitle(short_name)
    except Exception:  # ImportError, an old build without setthreadtitle, anything else
        logger.debug("could not set the process title", exc_info=True)
