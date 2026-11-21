# Security

BACpack may run next to or inside live building systems. This page says what it protects and what it does not.

## What BACpack protects

- Login: the web UI and API are behind a login you choose. The first visit asks you to create one or to run without one. There is one account. Passwords are at least 12 characters and hashed with scrypt. Sessions end after 8 hours without a request.
- Lockout: five failed logins from one address within 15 minutes lock that address out for 15 minutes. The lockout doubles on each repeat, up to 24 hours.
- API keys: scripts use named, revocable keys from the Security page, shown once when created. A key can do everything a signed-in user can.
- HTTPS: the web UI and API are HTTPS only, with a self-signed certificate made at first start or your own. The session cookie is always `Secure`. A cross-site request that would change something is refused.
- Connector credentials: passwords, tokens and header values are stored encrypted, with a key in `secret.key` in the data folder. If that file is lost or replaced, the affected connectors ask for their credentials again.
- Device password: an optional password per device guards DeviceCommunicationControl and ReinitializeDevice, including the BACnet backup and restore states. A wrong or missing password is refused with the BACnet error `security` / `passwordFailure` and logged.
- Local security settings: the login, API keys, sessions, listen address, certificates, `secret.key`, connector credentials and device passwords stay on the install where you set them. Config exports, imports, BACnet backups and restores and the diagnostics download leave them out. Importing a config cannot switch the login off.

## What BACpack does not protect

- BACnet has no authentication or encryption. Anyone who can reach a device's UDP port can read and write its objects. The device password guards only DeviceCommunicationControl and ReinitializeDevice, and BACnet sends it unencrypted.
- There are no roles or multiple users. Whoever signs in, or holds an API key, can do everything.
- Whoever can read the data folder or run as the service account has everything, including `bacpack.db` and `secret.key`. Keep the permissions the installer sets.
- The first visitor claims a fresh install. For an unattended install, make the choice in the installer with `--login-user` / `-LoginUser`, or `--no-login` / `-NoLogin`.
- Compare the self-signed certificate's fingerprint with the one on the Web Server page before you accept it, or install your own.
- Running behind a reverse proxy is untested. The lockout would see only the proxy.
- There is no redundancy or high availability, and no audit trail beyond the Logs page.

## Recommended setup

1. Create a login on the first visit, or install with `--login-user` / `-LoginUser`.
2. If the machine has several interfaces, set the listen address on the Web Server page to the one the web UI should use.
3. Open the web port and the BACnet UDP ports to the networks that need them and no further.
4. Set a BACnet password on any device that real BACnet clients can reach and must not reboot or silence.
5. Back up `secret.key` with the database. Without it, saved connector credentials must be entered again after a restore.
6. Use one API key per script and revoke it when the script is retired.

## Locked out, or HTTPS broken

Run these on the machine itself, as root (Linux) or from an elevated prompt (Windows). They need no running web server.

- Linux: `sudo bacpack-admin reset-login`, `sudo bacpack-admin web --self-signed`, `sudo bacpack-admin web --address ADDR`, `sudo bacpack-admin status`.
- Windows: the same commands with `bacpack-admin.cmd`, from an elevated prompt in `C:\Program Files\BACpack`.

`reset-login` sets a new password and ends every session. Restart the service after the `web` commands.

## Reporting a vulnerability

Report privately: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue. Include the version, how it was installed and the steps to reproduce.
