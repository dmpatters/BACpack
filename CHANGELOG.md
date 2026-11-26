# Changelog

## 1.0.1

- EDE import: matching the file's Trend Logs to their points no longer walks every point once per log, so a large file with many logs plans (and dry-runs) in a fraction of the time. The matches are the same.
- Proxies: an alarming object proxied from another device goes back to normal (with its to-normal notification) when the remote device answers again. Before, it stayed in FAULT if the first good poll brought a new value, so its alarms could no longer fire.
- Security: a sign-in whose password was checked just before the password was changed (or the login reset) no longer starts a session. Before, it could, after the change had ended every other session.
- Network settings: a bind address, foreign BBMD or BBMD peer with a port outside 1-65535 is refused with a plain message. Before, it was saved; port 0 then bound a random port no client could find, and a port over 65535 failed only when the device started.
- Write API: a feed or manual write with an integer too large for a number answers 400 like other bad values. Before, it answered 500.
- Connectors: an HTTP connector's own Cookie header is kept when the server redirects within the same site (or from http to https on the same host). Before, it was dropped on every redirect, so a server that logs you in by cookie answered the poll with an error. It is still left off after a redirect to another site.
- Connectors: after adding a connector, the add form clears its header rows too. Before, they stayed filled (their values are hidden), so the next connector added was saved with the previous one's header values, even for a URL on another host.
- Alarming tab: clearing a High or Low limit and typing a new one (or typing a negative limit) no longer unticks that side's "limit enabled". Before, the save then turned that side off and its alarm never fired. A side left with no limit is still turned off when you save.
- Alarms: turning event detection off in the Alarming tab or the API while an object is in alarm now resets its acknowledgements and time stamps, as a BACnet write of Event_Detection_Enable already did. Before, the object stayed in every alarm summary (GetEventInformation) with an acknowledgement that couldn't be given, until the device restarted.
- Objects: on the General, Trending and Schedule tabs, a change saved back to its earlier value before the page reloaded the object is now saved. Before, nothing was sent, the tab showed "Saved." and the server kept the other value.
- Startup: restarting the web server while a device keeps the history file busy no longer fails the start (it used to exit and be restarted until the file was free); plain-text connector credentials found then are encrypted on the next start.
- Sample building: after loading, the Devices page says how many devices loaded and started, and names each one that failed to start with its error. Before, the panel just closed.
- DeviceCommunicationControl and ReinitializeDevice: with 10 requests already waiting for the password check, the next is answered at once with an Abort (out of resources) instead of joining a queue without end; refusals are logged once a minute with their count. During a flood of wrong passwords a genuine request may be refused too and should be retried.
- Device settings: after Clear password, a Keep password button goes back to keeping the current BACnet password. Before, only discarding the whole form did.
- COV: a SubscribeCOV with a Lifetime but no Issue Confirmed Notifications is refused (missing required parameter). Before, it was accepted and made an unconfirmed subscription.
- COV: renewing a subscription with the other choice of confirmed or unconfirmed notifications now switches to it. Before, the first choice stayed.
- Docs: a page comparing BACpack with other BACnet device simulators, linked from the README.
- Docs: BACnet/SC added to the roadmap.
- The new-issue page has an "Ask a question" link to Discussions.
- Notification Class: each recipient has a Process ID field. It was always 1, and a front end that expects another process ID refused its alarm notifications.
- About: the PICS link opens a readable page instead of raw Markdown. The text itself is at `/api/pics.md`.
- Alarms: a confirmed event notification that a recipient refuses (Error, Reject or Abort) or never answers is logged as a warning with the reply, at most once a minute per recipient. Before, it looked the same as a delivered one. Each event notification sent is also on the Traffic page.
- Logs: errors that asyncio or bacpypes3 log (an exception in a timer callback or in a background task) are on the Logs page and in the diagnostics file, warnings and up. Before, a device run as a service lost them entirely.
- Web: a request body over 8 MiB is refused with 413 before it's read (uploads still take files up to 64 MiB). Before, there was no limit, and a few large posts to the login page, which needs no login, could use up a small machine's memory.
- Schedule: a BACnet write to Weekly_Schedule or Exception_Schedule with a time finer than a minute is refused with value-out-of-range. Before, it was acknowledged and then saved without its seconds, so a read-back differed from what was written.
- First start no longer fails when the data folder's filesystem refuses hard links (FAT/exFAT, some network shares and container mounts): the encryption key file is created directly there.
- Security: creating an API key asks for the current password when the login is on, so a stolen session or key can't add a key of its own (API: send `current_password` with `POST /api/settings/api-keys`). Changing the password can revoke every API key at the same time (a new box on the form); keys stayed valid after a password change before, and still do unless it's ticked.
- Connectors: changing an HTTP connector's URL to another host, port or scheme clears its saved password, token and header values (unless new ones are entered in the same save), and so does a config import that changes it; before, the next poll sent them to the new host. A move from http to https on the same host and default ports keeps them.
- Security: with the login off, BACpack answers only to its own names (its IP addresses, this computer's name and name.local, localhost and the names on its certificate) and refuses any other with 421, so a web page can't reach it through DNS rebinding. To open it by another name, use a certificate that carries the name or turn the login on.
- Security: a wrong current password (when changing the password, turning the login off or creating an API key) counts toward the same lockout as the login page and is logged; before, it could be guessed without limit.
- Security: turning the login on offers to revoke the API keys made while it was off (ticked by default; API: `revoke_api_keys` on `POST /api/settings/login/enable`); before, they all started working.
- Tests: `pytest -n auto` without `--dist loadgroup` stops the tests that serve BACnet with a message naming the flag, instead of failing them at random with "UDP port already in use".
- Tests: the checks that a save waits on a locked database off the event loop allow a 0.6 s stall instead of 0.3 s, so a slow shared CI runner no longer fails them, and their waits read the database off the loop. A save made on the loop still stalls it for about a second.
- Tests: many tests that could pass on broken code now fail on it (each was checked by breaking the code it covers), several that could fail at random (after midnight, on the first of a month, on a busy machine) no longer do, and the local-time trend checks also run in zones east and west of UTC on Linux.

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
