# A tour of the web UI

Part of the [BACpack guide](README.md).

The web UI is at `https://<host>:8000`. With the login off, open it by its IP address, its computer name (or `name.local`) or `localhost`; another name gets "misdirected request". The [sample building](sample-building.md) gives you four devices to look at.

## Devices

The Devices page (`/`) has a card per device. It also has **New device**, **Load sample building**, **Import EDE** (see [Importing an EDE file](importing-an-ede-file.md)), **Restart all running devices** and, under a service, **Restart UI only**.

## A device's tabs

- **Objects**: the object table, with bulk edit and bulk simulate. A row's **Edit** opens the object's page.
- **Process**: **Restart serving**, bind address, network role and device clock. When another program holds the port, **Use port** offers a free one ([Sharing the BACnet port](sharing-the-bacnet-port.md)).
- **Traffic** and **Logs**: what the device saw and did.
- **Import/Export**: the CSV point list, previewed before you apply it. Object types may be written short (AI, AO, AV, BI, BO, BV, MSI, MSO, MSV). Merge adds only new points. Sync never clears settings the file doesn't carry. An EDE file imports here too.
- **Settings**: name, vendor details, an optional BACnet password and more. Apply with **Restart serving**.
- **BACnet client**: Who-Is, and read and write of other devices from this device's address. **Clone** copies a found device ([Cloning a live controller](cloning-a-live-controller.md)).

The BACnet password is up to 20 characters, applies at once and is never exported or restored. When set, DeviceCommunicationControl and every ReinitializeDevice request, including a BACnet backup or restore, must carry it. Otherwise the device answers `passwordFailure` and logs the attempt.

Object edits apply to a running device. **Restart serving** is needed only after changing the device's name, vendor or bind address.

## An object's page

The tabs are **General**, **Trending** (interval or COV, kept as a native Trend_Log, CSV export) and, where they apply, **Alarming**, **Recipients** (notification classes), **Schedule**, **Dates** (calendars) and **Simulation**.

## Connectors

`/connectors` has HTTP pull connectors (JSON path or plain text, scale, offset or value map, basic or bearer auth) and BACnet connectors (read, write or proxy mappings to one remote device).

Credentials are stored encrypted with the key in `secret.key` in the data folder. Back that file up with the database. A credential whose key is lost has to be entered again. The URL is stored and exported as typed, so keep credentials out of it. Changing a URL to another host, port or scheme clears the saved password, token and header values, so enter them again (a move from http to https on the same host and default ports keeps them).

## Trend Storage, Logs

`/trends` sets retention and size limits for recorded history. `/logs` shows BACpack's own application log.

## Network Settings

`/settings/network` sets the default bind address. Devices read it at start, so use **Restart all running devices** after a change. Stopped devices stay stopped.

## Security

`/settings/security` holds the web UI login and **API keys** for scripts. Sessions end after 8 hours without a request. Five failed logins from one address lock it out for 15 minutes, and wrong current passwords (changing the password, turning the login off, creating a key) count toward the same lock. Turning the login on offers to revoke keys made while it was off.

## Web Server

`/settings/web` sets the address the UI listens on (0.0.0.0 is every address). A change takes effect when the web server restarts. Under a service use **Restart web server**.

The page downloads the public certificate. **Regenerate** makes a new one, for example after the machine's IP changed. **Use your own certificate** takes a PEM certificate (a chain is fine) and its unencrypted key.

## Config

`/config` exports the whole server as one JSON file, or imports one after a dry-run preview. Devices and objects are synced to match the file. Connectors in the file are created or updated by name. Others are left alone.

## About

`/about` shows the version and supported BACnet services and object types. **Download diagnostics** makes a zip for a bug report. Names, addresses and URLs are replaced unless you tick **Use real values**.

## API Docs

`/docs` is Swagger UI over the JSON API, served by BACpack. The schema is at `/openapi.json`.

- `POST /api/feed/...` and `POST /api/manual/...` write a value at priority 10 (feed) or 8 (manual).
- `GET /api/devices/{id}/objects/{object_id}/live` returns an object's live state.
- `.../trend/range?start=&end=` returns its trend between two times.

With a login on, a script sends an API key (create one under **Security**, where creating one asks for your login password; it is shown once. Changing the password there can revoke every key with a tick box). Without one the request gets 401.

```
curl --cacert bacpack-certificate.pem -H "Authorization: Bearer bpk_..." https://<host>:8000/api/devices
```

Download `bacpack-certificate.pem` from **Web Server**. `-k` skips the check, which is unsafe on a hostile network.

In PowerShell, trust the certificate once from an elevated prompt (`Import-Certificate -FilePath bacpack-certificate.pem -CertStoreLocation Cert:\LocalMachine\Root`), then run `Invoke-RestMethod -Headers @{Authorization = "Bearer bpk_..."} https://<host>:8000/api/devices`. PowerShell 7's `-SkipCertificateCheck` is the `-k` there. In Windows PowerShell 5.1 a `ServerCertificateValidationCallback = {$true}` bypass fails with "An unexpected error occurred on a send".
