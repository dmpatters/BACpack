# Changelog

## 1.0.0

First public release. Simulated BACnet/IP devices, managed from a web UI.

- Devices: several at once, each in its own process with its own IP address and UDP port. Each meets the B-BC profile, and [PICS.md](PICS.md) is the conformance statement.
- Objects: Analog, Binary and Multi-state Input/Output/Value, Schedule, Calendar, Notification Class, Trend Log, Network Port and File. Outputs and Values are commandable.
- Services: Who-Is/I-Am, Who-Has, ReadProperty(Multiple), WriteProperty(Multiple), COV, ReadRange, intrinsic alarming with acknowledgement and GetEventInformation, Add/RemoveListElement, time synchronization, DeviceCommunicationControl, ReinitializeDevice, backup and restore, BBMD and foreign-device registration.
- Values: simulators, schedules that write other objects, and optional connectors for values over HTTP or from other BACnet devices (read, write or proxy).
- Getting points in: a sample building, EDE import, cloning a live controller, a JSON export and import of the whole server, and a CSV point list per device.
- Watching: live values, trend charts, traffic and log views, and a BACnet client tab.
- Install: a systemd service on Linux (`install.sh`) and a Windows service (a setup wizard, or an offline zip). The web UI is HTTPS with a self-signed or your own certificate.
- Upgrades: from 1.0.0 on, every 1.x release opens the databases, exports, backups and CSV point lists an earlier one saved. The write API (`/api/feed`, `/api/manual`, API keys) changes only by adding. See [Upgrading](docs/guide/upgrading.md).
