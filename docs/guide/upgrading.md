# Upgrading BACpack

Part of the [BACpack guide](README.md).

From 1.0.0 on, a new release opens what an older one saved.

## What is kept

Through every 1.x release:

- Your data: `bacpack.db`, `bacpack-history.db` and `secret.key` from 1.0.0 or later open and update on the first start with nothing lost.
- Configuration exports, BACnet backup files and CSV point lists written by 1.0.0 or later import into any later release.
- The write API: `POST /api/feed/...`, `POST /api/manual/...` and API-key sign-in keep their request and response shapes. A later release may add fields but never removes one or changes its meaning.
- You can upgrade from any 1.x release straight to any later one.

A release may ask you to do something once. Its [CHANGELOG.md](../../CHANGELOG.md) entry then starts with **Upgrade note:**, for example "stop every device before upgrading". Read the entry for each release you skip. A release never needs you to throw old data away.

## What is not promised

- The rest of the web API, the part the web pages use, can change in a minor release. The CHANGELOG says so.
- Signed-in web sessions. You may be asked to sign in again.
- Installer options and `bacpack-admin` commands. A change gets an upgrade note.
- Python and operating system versions. A release lists what it supports.
- Data saved before 1.0.0.

## Back up first

**Config** exports everything as one file. For a full copy, stop BACpack and copy `bacpack.db`, `bacpack-history.db` and `secret.key` together from the data folder (Linux: `/var/lib/bacpack`; Windows: `C:\ProgramData\BACpack`). Without `secret.key`, saved connector passwords have to be entered again.

## How to upgrade

- Linux: run the new release's `install.sh`. It stops the service, installs and starts it again, keeping the port and the Python it was installed with.
- Windows: run the new release's `BACpack-<version>-setup.exe`, or its `install.ps1`. The setup keeps the port, data and login. It refuses to replace a newer BACpack with an older one.

A service you stopped yourself stays stopped. Start it with `sudo systemctl start bacpack` (Linux) or `Start-Service BACpack` (Windows).

## Going back to an older release

An older release refuses to start on data a newer one has saved. Nothing in the files is changed.

Each start attempt logs a line naming the file, then exits with code 81. The service is not restarted. On Windows the log line also says it is stopping the service. The Windows setup refuses to go back before it changes anything.

To recover, install that newer release or a later one again, or put back the copy you made before the upgrade and start the older release on it.
