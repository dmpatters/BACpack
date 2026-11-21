"""Ownership of files BACpack writes into its data folder."""
from __future__ import annotations

import os
from pathlib import Path


def owner_for(data_dir: Path) -> os.stat_result | None:
    """Run as root (sudo bacpack-admin), new files go to the data dir's owner,
    who is the service account that has to read them. None otherwise."""
    geteuid = getattr(os, "geteuid", None)
    return data_dir.stat() if geteuid is not None and geteuid() == 0 else None


def hand_to_owner(path: Path, owner: os.stat_result | None) -> None:
    chown = getattr(os, "chown", None)
    if owner is not None and chown is not None:
        chown(path, owner.st_uid, owner.st_gid)
