"""web --self-signed / --address, and the web part of status.
Neither command restarts the service."""
from __future__ import annotations

from .. import web_server
from ..db import lock_both_files

_RESTART = (
    "Restart the service for this to take effect (Linux: sudo systemctl restart bacpack; "
    "Windows: Restart-Service BACpack). Nothing was restarted."
)
_DEFAULT_PORT_NOTE = "unknown (8000 by default)"


def register(subparsers) -> None:
    web = subparsers.add_parser("web", help="make a new self-signed certificate and/or set the listen address")
    web.add_argument("--self-signed", action="store_true", help="make a new self-signed certificate (replaces any other)")
    web.add_argument("--address", metavar="ADDR", help="the IPv4 address the web server listens on (0.0.0.0 = all)")
    web.set_defaults(handler=_web)


def _web(args, context) -> int:
    if not args.self_signed and args.address is None:
        print("Pass --self-signed and/or --address ADDR (0.0.0.0 listens on every address).", file=context.out)
        return 2
    if args.address is not None:
        problem = web_server.check_listen_address(args.address)
        if problem is not None:
            print(f"{problem} Nothing was changed.", file=context.out)
            return 1
    if args.address is not None:
        with context.session_factory() as session:
            lock_both_files(session)
            changed = web_server.set_listen_address(session, args.address, web_server.LOCAL)
        print(f"Listen address {'set to' if changed else 'is already'} {args.address}.", file=context.out)
    if args.self_signed:
        info = web_server.make_self_signed(context.data_dir, web_server.LOCAL)
        print("Made a new self-signed certificate.", file=context.out)
        print(f"SHA-256: {info.fingerprint_sha256}", file=context.out)
        print(f"Expires: {info.not_after:%Y-%m-%d} UTC", file=context.out)
    print(_RESTART, file=context.out)
    return 0


def status_lines(context) -> list[str]:
    with context.session_factory() as session:
        address = web_server.get_listen_address(session)
    port = context.environ.get("BACPACK_PORT") or _DEFAULT_PORT_NOTE
    lines = [f"Web:      listens on {address}, port {port}"]
    certificate = web_server.certificate_status(context.data_dir)
    if certificate.problem:
        lines.append(f"Certificate: {certificate.problem}")
    elif certificate.info is None:
        lines.append("Certificate: none yet (made at the first start)")
    else:
        info = certificate.info
        source = "self-signed" if info.generated_by_bacpack else "own"
        lines.append(f"Certificate: {source}, SHA-256 {info.fingerprint_sha256}, expires {info.not_after:%Y-%m-%d} UTC")
        lines.append(f"Addresses: {', '.join(info.sans)}")
    return lines
