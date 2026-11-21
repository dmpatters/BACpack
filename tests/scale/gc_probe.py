"""One full garbage collection in a fresh process holding only the big device,
frozen after the build as its device process does; the test process's own heap
holds every earlier test's leftovers too. Prints the seconds.

    python -m tests.scale.gc_probe <db path> <device id>
"""
from __future__ import annotations

import asyncio
import gc
import sys
import time

from sqlalchemy import select

from bacpack import gc_freeze
from bacpack.db import make_engine, make_session_factory
from bacpack.models import BacnetObject, Device
from bacpack.object_builder import build_application


async def full_collection_seconds(db_path: str, device_id: int) -> float:
    engine = make_engine(db_path)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = session.get(Device, device_id)
        objects = list(session.scalars(select(BacnetObject).filter_by(device_id=device_id)))
        session.expunge_all()
    app = build_application(device, objects, trend_session_factory=session_factory)
    gc_freeze.freeze_startup()
    start = time.perf_counter()
    gc.collect()
    seconds = time.perf_counter() - start
    del app
    engine.dispose()
    return seconds


if __name__ == "__main__":
    print(asyncio.run(full_collection_seconds(sys.argv[1], int(sys.argv[2]))))
