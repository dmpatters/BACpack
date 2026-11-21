"""Load a device + its objects from SQLite and run it as a live BACnet/IP
device, responding to Who-Is/I-Am, WriteProperty, and SubscribeCOV.

Usage:
    python scripts/run_server.py --device-id 599001 --address 192.168.1.50/24

    # register as a foreign device with a BBMD elsewhere:
    python scripts/run_server.py --device-id 599001 --address 192.168.1.50/24 \\
        --foreign 192.168.1.5:47808 --ttl 60

    # act as a BBMD ourselves, with peer BBMDs in the Broadcast Distribution Table:
    python scripts/run_server.py --device-id 599001 --address 192.168.1.50/24 \\
        --bbmd-peer 192.168.2.5:47808 --bbmd-peer 192.168.3.5:47808

The bind address is required and not auto-detected: on a multi-homed machine
(say a LAN and a VPN interface) a wrong guess silently breaks discovery.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bacpack.object_builder import build_application  # noqa: E402
from bacpack.db import make_engine, make_session_factory  # noqa: E402
from bacpack.models import Device  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--device-id", type=int, required=True, help="BACnet device instance number"
    )
    parser.add_argument(
        "--address",
        required=True,
        help="CIDR address to bind, e.g. 192.168.1.50/24",
    )
    parser.add_argument(
        "--foreign",
        metavar="HOST[:PORT]",
        help="register as a foreign device with this BBMD (mutually exclusive with --bbmd-peer)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=60,
        help="foreign device registration lifetime in seconds (default: 60)",
    )
    parser.add_argument(
        "--bbmd-peer",
        metavar="HOST[:PORT]",
        action="append",
        dest="bbmd_peers",
        help="act as a BBMD; repeat to add peer BBMD entries to the Broadcast "
        "Distribution Table (mutually exclusive with --foreign)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    engine = make_engine()
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        device = session.get(Device, args.device_id)
        if device is None:
            raise SystemExit(
                f"no device with id {args.device_id} -- run scripts/create_demo_device.py first"
            )
        objects = list(device.objects)
        # Detach the data we need before the session closes.
        session.expunge_all()

    app = build_application(
        device,
        objects,
        bind_address=args.address,
        foreign_bbmd=args.foreign,
        foreign_ttl=args.ttl,
        bbmd_peers=args.bbmd_peers,
    )
    role = (
        f"foreign device of {args.foreign}"
        if args.foreign
        else f"BBMD with {len(args.bbmd_peers)} peer(s)"
        if args.bbmd_peers
        else "normal"
    )
    print(
        f"Device {device.id} ({device.name!r}) is up on {args.address} ({role}), "
        f"serving {len(objects)} objects."
    )
    print("Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()
    finally:
        app.close()


def main() -> None:
    args = parse_args()
    if args.foreign and args.bbmd_peers:
        raise SystemExit("--foreign and --bbmd-peer are mutually exclusive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
