"""Run with the bundle's own bacpack.exe: imports BACpack, checks its SQLite,
and starts and stops one real device process through EngineManager, so a
bundle that can't spawn devices never ships; then makes a certificate and
loads it into an SSL context, so one that can't serve HTTPS never ships."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path


async def _run(db_path: str) -> None:
    from bacpack.db import init_db, make_engine, make_session_factory, sqlite_version_problem
    from bacpack.engine import EngineManager
    from bacpack.models import BacnetObject, Device, get_or_create_settings

    problem = sqlite_version_problem()
    if problem:
        raise SystemExit(problem)
    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        device = Device(id=1, name="Smoke")
        session.add(device)
        session.add(BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV-1"))
        get_or_create_settings(session).bind_address = "127.0.0.1/8:47899"
        session.commit()
    manager = EngineManager(session_factory, db_path=db_path)
    await manager.start(1)
    # The device runs as the bundle's "BACpack device" executable, not the
    # parent's: the name Task Manager shows.
    import psutil

    child_exe = Path(psutil.Process(manager.processes[1].pid).exe()).name.lower()
    if child_exe != "bacpack-worker.exe":
        raise SystemExit(f"the device process runs as {child_exe}, not bacpack-worker.exe")
    await manager.stop(1)
    engine.dispose()


def _check_tls(directory: Path) -> None:
    """The launcher makes a certificate at first start and uvicorn loads it:
    a bundle whose cryptography or OpenSSL is broken must not ship."""
    import ssl

    from bacpack import timeutil, tls_certs

    cert_pem, key_pem = tls_certs.generate_self_signed("smoke", ["127.0.0.1"], timeutil.utc_now())
    tls_certs.write_pair(directory, cert_pem, key_pem)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    folder = tls_certs.tls_dir(directory)
    context.load_cert_chain(folder / tls_certs.CERT_FILE, folder / tls_certs.KEY_FILE)


def main() -> None:
    if Path(sys.executable).name.lower() != "bacpack.exe":
        raise SystemExit(f"run this with the bundle's bacpack.exe, not {sys.executable}")
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_run(str(Path(tmp) / "smoke.db")))
        _check_tls(Path(tmp))
    print("bundle smoke test passed")


if __name__ == "__main__":
    main()
