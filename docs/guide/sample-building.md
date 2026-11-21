# The sample building

Part of the [BACpack guide](README.md).

Four devices with moving values, a filling trend, a running schedule and a point that alarms now and then. Loading it deletes nothing. Remove it by deleting its four devices (Delete in each device's header). Their trend history goes with them.

## What is in it

Sample AHU-1 (device 4190001) is a BBMD linked to the three VAVs. Its objects:

- Supply Air Temp (a sine around 55 °F, trended every minute), Return Air Temp (a random walk, trended) and Mixed Air Temp (follows the return air).
- Supply Fan Command (binary output) and Supply Fan Status (binary input, switching on and off).
- Supply Fan VFD Speed (analog output, %) and Supply Air Temp Setpoint (analog value, 55 °F).
- Occupancy (binary value), written by the Occupancy Schedule: weekdays 07:00 to 18:00, with a Holidays calendar of three fixed dates.
- Unit Mode (multi-state: Off, Heat, Cool, Economizer).
- Filter Differential Pressure. It drifts past its high limit now and then and alarms through the "Sample alarms" notification class.

Sample VAV-1 to VAV-3 (devices 4190101 to 4190103) each have Zone Temp (trended), Zone Setpoint, Damper Position, Airflow (follows the damper) and Occupied.

## Loading it

1. Set the default bind address on the Network Settings page. The load is refused without one.
2. On the **Devices** page, with no devices yet, click **Load sample building**. The same button is in the **Sample building** section of the Config page.
3. Leave **Start the devices** ticked to run them at once, and click **Load**.

The AHU takes the default bind address's own port, normally 47808, or the next free port if that is held. Each VAV takes the next free port from 47809 up. If a device with one of the sample ids or names already exists, the load is refused and nothing is added.

See [Getting started](getting-started.md) for the full walkthrough.

## How a BMS finds it

A plain Who-Is on UDP 47808 finds all four devices. A BMS that only listens passively does not see the VAVs, because their startup I-Am stays on their own ports.

When the AHU is not on 47808, register your tool as a foreign device with the AHU at the address and port the panel shows, then search again. See option 3 in [Sharing the BACnet port](sharing-the-bacnet-port.md).

## Firewall

The installers open only UDP 47808. Tools on the same computer reach every device. From another computer, allow the sample's other ports (the panel shows the range) or use only the AHU.

Windows, in an administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "BACpack sample" -Direction Inbound -Protocol UDP -LocalPort 47809-47811 -Action Allow
```

Linux with firewalld or ufw:

```bash
sudo firewall-cmd --permanent --add-port=47809-47811/udp && sudo firewall-cmd --reload
sudo ufw allow 47809:47811/udp
```
