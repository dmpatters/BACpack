# Cloning a live controller

Part of the [BACpack guide](README.md).

You need a running BACpack device on the controller's network (it sends the reads from its own address) and a controller that answers Who-Is or has a known address.

## Cloning

1. Open the running device's **BACnet client** tab and click **Who-Is**.
2. Click **Clone** on the controller's row. A big controller takes a few minutes. You can leave the page: **Open clone** on the tab shows progress and **Cancel** stops the read.
3. When the read is done, the panel shows the plan. Nothing is saved yet. It holds:
   - **Device instance** and **Device name**, which start as the controller's. Change them if the clone will ever be on the controller's network.
   - **Bind address**. Blank uses the server's default.
4. Click **Create device**. The new device is saved stopped.

**Discard** throws the read away. Only one clone read per device can run at a time.

Started on the site's network, a clone with the controller's instance would clash with it, and its notification classes would send duplicate alarms to the real front end. Change its instance or recipients first, or start it elsewhere.

## What is copied

| | Copied |
|---|---|
| Every object | type, instance, name, description, units |
| Multi-state objects | state texts |
| Binary objects | Active text and Inactive text |
| Inputs | the present value at clone time, as the **Start value** |
| Outputs and values | Relinquish_Default (not the priority array or the present value) |
| Schedules | weekly schedule, exception schedule, effective period, default value (None included), priority for writing, write targets on objects inside the controller |
| Calendars | date list |
| Alarming (analog inputs/outputs/values, binary inputs/values, multi-state inputs/values) | limits, deadband, limit enable, time delays, event enable, notify type, notification class, alarm values |
| Notification classes | priority, ack required, recipient list as it is |
| Trend Logs | each log of a Present_Value in the same controller marks that point trended (interval or COV), with the log's own instance and name |
| Device | name, description, location, vendor name and identifier |

## What isn't

- Trend history. The clone starts with empty trends.
- Priority slots. An output or value starts at its Relinquish_Default.
- Object types BACpack doesn't simulate: loops, accumulators, programs, files, structured views, Trend Log Multiple, Event Log and proprietary types. They're listed under **Not cloned**, with a **Request support for these types** link to a pre-filled GitHub issue. Nothing is sent until you submit it.
- Schedule write targets on other devices, Trend Logs of other devices or properties, and Trend Logs using triggered logging. Each is listed with the reason.
- Command rights on values the controller doesn't let anyone command. Such a value is cloned with **BACnet writable** off and the plan says so.

## Taking it offline

Clone on the laptop you take home. To move a clone to another BACpack server, export its point list from the device's **Import/Export** tab, create a device with the same instance and name there, and import the file. Don't use the **Config** page's import for one device. It makes the whole server match the file, so it deletes every device the file doesn't have.
