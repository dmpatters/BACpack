# Uninstalling

Uninstalling stops the service, and its devices with it, then removes it. Your data stays unless you ask for it to go.

## Windows

Installed with the setup wizard: go to Settings > Apps > Installed apps > BACpack > Uninstall. It asks whether to delete BACpack's data too (default No). Use this route for a setup install. The command below leaves the Installed apps entry and the Start menu items, and choosing Uninstall on them later removes them.

Installed from the zip or any other way: from an elevated **Command Prompt** run

```
"C:\Program Files\BACpack\bacpack-admin.cmd" uninstall
"C:\Program Files\BACpack\bacpack-admin.cmd" uninstall -Purge
```

The second line deletes the data too. In PowerShell put `&` in front of the quoted path.

## Linux

```bash
sudo bacpack-admin uninstall
sudo bacpack-admin uninstall --purge
```

The second line deletes the data too.

## What stays, what goes

| | Removed | Kept (deleted by purge) |
|---|---|---|
| Windows | the service, `C:\Program Files\BACpack`, the firewall rules the installer made | `C:\ProgramData\BACpack`: devices, trends, logs, settings, certificate |
| Linux | the service, `/opt/bacpack`, the `bacpack` user, the firewall rules the installer added | `/var/lib/bacpack`: the same |

A firewall rule you added yourself is not touched. Installing again picks the kept data up.
