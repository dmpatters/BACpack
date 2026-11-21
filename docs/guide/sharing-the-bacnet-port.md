# Sharing the BACnet port with Yabe or Workbench

Part of the [BACpack guide](README.md).

BACpack and a BACnet tool such as Yabe or Niagara Workbench both want UDP port 47808 on your computer's address. Whichever starts second fails:

- BACpack: `UDP port 192.168.1.200:47808 is already in use by another program or device`. The device shows as crashed.
- Yabe: `Couldn't start BACnet communication: An attempt was made to access a socket in a way forbidden by its access permissions`.

There are three ways round it. With several devices, option 3 is the easiest.

| | Needs | The tool finds the device by |
|---|---|---|
| 1. A second IP address | a free address on your subnet, administrator rights | a normal search |
| 2. Another port, added as a remote node | nothing | adding it by hand, one device at a time |
| 3. Another port, foreign device registration (recommended) | nothing | a normal search, after one registration |

When a start failed because the port is taken, the device's Process tab offers a free port with a **Use port** button.

## 1. A second IP address on the network adapter

The device gets its own address and keeps port 47808. Tested with Yabe on Windows 11 over Wi-Fi. Linux is not tested. Don't add the address in the adapter's IPv4 properties in Control Panel: that turns DHCP off.

In an administrator prompt (`Wi-Fi` is the adapter's name, from `netsh interface ipv4 show interfaces`):

1. Check the adapter has one address and DHCP is on:

   ```
   netsh interface ipv4 show config "Wi-Fi"
   ```

   It should say `DHCP enabled: Yes`. If it says `No`, run `netsh interface ipv4 set address "Wi-Fi" dhcp` first.

2. Let a fixed address live beside the DHCP one:

   ```
   netsh interface ipv4 set interface "Wi-Fi" dhcpstaticipcoexistence=enabled
   ```

3. Add the second address: an unused one on the tool's subnet that the router won't hand out. `skipassource=true` keeps Windows from using it for outgoing traffic.

   ```
   netsh interface ipv4 add address "Wi-Fi" 192.168.1.221 255.255.255.0 skipassource=true
   ```

4. Run the first command again. It still says `DHCP enabled: Yes`, now with both addresses.

Set the device's bind address on its Process tab to `192.168.1.221/24` (no port, so 47808) and start it. In Yabe, keep the usual channel on your own address and port 47808. A normal Who-Is finds the device.

To undo:

```
netsh interface ipv4 delete address "Wi-Fi" 192.168.1.221
netsh interface ipv4 set interface "Wi-Fi" dhcpstaticipcoexistence=disabled
```

## 2. Another port, added as a remote node

No administrator rights or network change. The device runs on your computer's address on a port other than 47808, one port per device.

1. In BACpack, set the device's bind address to your address with a port, for example `192.168.1.200/24:47810`. Or press **Use port 47810** on the Process tab after a failed start.
2. In Yabe, select the tool's own 47808 channel. Choose **Functions > IP Services > Add Remote BACnet/IP node** and enter `? - 192.168.1.200:47810`: a device instance or `?`, a space, a dash, a space, then the address and port. A bare `192.168.1.200:47810` is refused with "Invalid parameter". The device shows as `[4194303]` until it has answered.

Do not create a channel on 47810 in Yabe. That gives the "access forbidden" error again. Only Yabe has been tested.

## 3. Another port, foreign device registration

One device acts as a BBMD and forwards the tool's broadcasts to the devices linked to it. The tool registers with that device.

1. Give each device its own port, as in option 2 (`192.168.1.200/24:47810`, `:47811`, `:47812`).
2. On the Process tab of the device that will be the BBMD, set **Network role** to **BBMD** and tick the other devices under **Forward broadcasts to**.
3. Save and restart that device with **Restart serving**. Start every linked device too. A stopped one can't answer.
4. In Yabe, open **Foreign Device Registration** under its IP services, enter the BBMD device's address and port (`192.168.1.200:47810`), and register.
5. Send a Who-Is. Every device the BBMD forwards to answers.

A link is one-way. The linked device answers the tool directly, so the tool can discover, read, write and subscribe to it. The linked device's own broadcasts, including its startup I-Am, stay on its own port.

A BBMD's table is rebuilt when it starts. When a linked device is deleted or moves to another port, the BBMD shows **Restart serving** until you restart it. A linked device on the server's default address moves when you change that default in Network Settings. Restart the BBMD then too, since that change doesn't flag it.

## What does not work

- The loopback address (`127.0.0.x`). Yabe offers only adapter addresses.
- A second adapter on another subnet, for example a VirtualBox host-only adapter. The tool's broadcast does not cross subnets.

## The firewall

The installer opens only UDP 47808. Other computers cannot reach a device on another port until the firewall allows that port.

On Windows, an adapter on a Public network profile blocks the devices even when the rule exists for Private. Check with `Get-NetConnectionProfile`.

Allow a port on Windows (administrator PowerShell):

```
New-NetFirewallRule -DisplayName "BACpack UDP 47810" -Direction Inbound -Protocol UDP -LocalPort 47810 -Action Allow -Profile Any
```

`-Profile Any` matches the installer's own rule for 47808. Uninstalling removes only the rules the installer added. Remove one of yours with `Remove-NetFirewallRule -DisplayName "BACpack UDP 47810"`.

On Linux:

```
sudo firewall-cmd --permanent --add-port=47810/udp && sudo firewall-cmd --reload     # firewalld
sudo ufw allow 47810/udp                                                            # ufw
```
