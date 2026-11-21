"""Manually create a demo device + objects in SQLite, to validate the engine
end-to-end without a UI (Phase 1 milestone).

Usage:
    python scripts/create_demo_device.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bacpack.db import init_db, make_engine, make_session_factory  # noqa: E402
from bacpack.models import BacnetObject, Device  # noqa: E402

DEMO_DEVICE_ID = 599001


def main() -> None:
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        existing = session.get(Device, DEMO_DEVICE_ID)
        if existing is not None:
            print(f"Device {existing.id} ({existing.name!r}) already exists, skipping.")
            return

        device = Device(
            id=DEMO_DEVICE_ID,
            name="Demo Device",
            description="BACpack Phase 1 demo device",
        )
        session.add(device)

        session.add_all(
            [
                BacnetObject(
                    device=device,
                    object_type="analog-input",
                    instance_number=1,
                    name="AI-1-Zone-Temp",
                    description="Simulated zone temperature",
                    units="degreesFahrenheit",
                ),
                BacnetObject(
                    device=device,
                    object_type="analog-output",
                    instance_number=1,
                    name="AO-1-Damper-Cmd",
                    description="Simulated damper command",
                    units="percent",
                ),
                BacnetObject(
                    device=device,
                    object_type="binary-value",
                    instance_number=1,
                    name="BV-1-Fan-Enable",
                    description="Simulated fan enable",
                ),
                BacnetObject(
                    device=device,
                    object_type="multi-state-value",
                    instance_number=1,
                    name="MSV-1-Occ-Mode",
                    description="Simulated occupancy mode",
                    state_text=["Unoccupied", "Occupied", "Standby"],
                ),
            ]
        )

        session.commit()
        print(f"Created device {device.id} ({device.name!r}) with {len(device.objects)} objects.")


if __name__ == "__main__":
    main()
