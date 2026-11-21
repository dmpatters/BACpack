# Windows service: what was checked

`install.ps1` (one level up) installs BACpack as the Windows service `BACpack`, using [WinSW](https://github.com/winsw/winsw) v2.12.0. It runs in one of two modes:

- Source mode: a Python 3.13+ installed for all users, with the venv in `C:\Program Files\BACpack\.venv`.
- Bundle mode: the release zip's own embeddable Python, offline.

Checked on Windows Server 2025 Core (PowerShell 5.1, network profile Public).

## Virtual account

The service runs as `NT SERVICE\BACpack`, set with `sc.exe config BACpack obj= "NT SERVICE\BACpack"` after `bacpack-service.exe install`. WinSW's `<serviceaccount>` element isn't used.

`%ProgramData%\BACpack` is restricted to SYSTEM, Administrators and the account (inheritance removed, grants by SID).

## HTTPS

The service runs `python -P -m bacpack serve --port N`, HTTPS only. The certificate and key are made at the first start in `<data folder>\tls\`.

WinSW 2.12 restarts on every non-zero exit. For these failures a restart cannot help:

- 79: a broken certificate pair.
- 81: a database saved by a newer BACpack.
- 78: a too-old SQLite.

For these the web process asks Windows to stop its own service. The installer lets `NT SERVICE\BACpack` stop and query the BACpack service, and nothing else. The service shows Stopped and the log line names the fix (for 79, `bacpack-admin.cmd web --self-signed`). Start the service by hand once fixed.

An exit of 0 hangs WinSW 2.12 with the service shown Running, because it needs a Service Control Manager access the virtual account lacks.

A saved listen address that this machine lacks is waited for up to two minutes. The process then exits with 80 and is restarted every 5 s.

## Restart on a non-zero exit

"Restart UI only" exits the web process with code 75. With `<onfailure action="restart" delay="5 sec"/>`, WinSW restarts it in about 5 s. Devices keep running and are reattached.

## Embeddable Python

The embeddable zip ignores `site-packages` until `python314._pth` lists `Lib\site-packages` and `import site`. `pip install --target ...\Lib\site-packages`, run by a full Python, then gives a working interpreter. `bundle_smoke.py` imports BACpack, checks SQLite, and starts and stops one device through `EngineManager`.

## Pinned downloads

SHA-256 values are in `install.ps1` and `build_bundle.ps1`.

- `WinSW-x64.exe` v2.12.0 from the GitHub release: `05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA`.
- `python-3.14.10-embed-amd64.zip` from python.org: `90905E64A840557D8C1BC4F5B74DB3D7EE85E3FFB217611CAC1C7813ACEA4159`. It matches the digest in python.org's `.sigstore` bundle.

The bundle stays on a Python line that still gets Windows builds (3.13 stopped at 3.13.17).

## PowerShell 5.1 traps the scripts avoid

- Windows PowerShell strips double quotes inside arguments to native programs, so every `python -c` snippet uses single quotes only.
- Under `$ErrorActionPreference = 'Stop'`, a native program writing to stderr throws even with `2>$null`. Probes that may fail run with `Continue`.

## The setup wizard

`BACpack-<version>-setup.exe` is built by `build_installer.ps1` from `bacpack.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php) 6.7.4. It runs `build_bundle.ps1`, then compiles the wizard around the bundle.

In `PrepareToInstall` the wizard runs the bundle's own `install.ps1` as its own PowerShell (`ExecAndLogOutput`). A failure stops setup there with exit code 7 and nothing registered. Service, account, permissions, firewall and upgrade rules are `install.ps1`'s alone. The wizard is unsigned and needs administrator rights.

The compiler is checked against a pinned SHA-256 (`A549929319F9120716677C685535CA4671E4B937B5FDC87E2585F32D8EB30E8C`, `innosetup-6.7.4.exe` from the GitHub release `is-6_7_4`). It is installed for the current user only, under `%LOCALAPPDATA%\bacpack-build`, with a per-user entry in Installed apps. `build_installer.ps1 -Clean` removes both.

The wizard's folder (`C:\Program Files\BACpack Setup`) holds only its uninstaller, which runs `install.ps1 -Uninstall` from a temporary copy of the `install.ps1` in `C:\Program Files\BACpack`, as `bacpack-admin.cmd uninstall` does.

A login password made in the wizard reaches `install.ps1` only through the child process's environment variable `BACPACK_LOGIN_PASSWORD`, never on a command line or in a file.

## Process names

`build_bundle.ps1` copies the embeddable `python.exe` to `bacpack.exe` and `bacpack-worker.exe` and rewrites their version resources (FileDescription "BACpack" and "BACpack device") with rcedit v2.0.0 (`3E7801DB1A5EDBEC91B49A24A094AAD776CB4515488EA5A4CA2289C400EADE2A`, a build tool that is not shipped).

The service and `bacpack-admin.cmd` run `bacpack.exe`. The web process starts each device with `bacpack-worker.exe` when that file sits beside its own executable. `bundle_smoke.py` runs as `bacpack.exe` and checks the device child's executable name.

Rewriting the version resources invalidates the Python Software Foundation's signature on the copies. `Get-AuthenticodeSignature` reports `python.exe` as Valid and `bacpack.exe` and `bacpack-worker.exe` as NotSigned.
