# Importing an EDE file

Part of the [BACpack guide](README.md).

BACpack turns an EDE points list into simulated devices that keep the file's device instances, object types, instances and names.

It reads EDE files saved as CSV (`;` or `,` separated, UTF-8 or Windows-1252). Save an `.xlsx` as CSV first. A separate state-texts file is read too. The object-types and units files aren't needed.

## As new devices

1. On the **Devices** page, click **Import EDE**.
2. Choose the **EDE file**, and the **State texts file** if you have one. Every device in the file is listed, all ticked.
3. Untick any you don't want. Each device's **Device instance** and **Device name** start as the file's. If BACpack already has that instance, change the new one's instance and name.
4. Check the preview. Nothing is saved yet. A problem that would stop creation, such as an instance or name already in use, shows in red and **Create** waits until you change it.
5. Click **Create device** (or **Create devices**). Every ticked device is saved stopped, in one go. If one can't be saved, none is.

Every new device uses the server's default bind address and port, so only one can run at a time. Starting a second offers **Use port**.

## Into an existing device

1. Open the device's **Import/Export** tab and choose the EDE file as the **CSV file**. Add the **State texts file** if there is one.
2. Pick **Merge** or **Sync** and click **Preview**. The plan says "Read as an EDE file".
3. When the file holds several devices, choose one under **Device in the file**. The file's device row is ignored, and the device keeps its own instance and name.
4. Check the plan and click **Import**.

**Merge** only adds. An object already on the device (same type and instance) is left as it is and listed.

**Sync** makes the file the device's object list. It updates the objects the file names, adds new ones and deletes the rest, with their trend history. It never clears what the file doesn't carry: a missing column, a blank cell or a dropped value keeps the stored setting. Existing schedules, calendars and notification classes keep their times, dates and recipients. The preview warns when the sync would empty the device.

## What is kept

| | Kept |
|---|---|
| Every object | type, instance, name, description, units (from the unit number) |
| Multi-state and binary objects | state texts, Active and Inactive text (from the state-texts file or columns) |
| Inputs | the file's default value as the **Start value** |
| Outputs and values | the file's default value as Relinquish_Default |
| Values (AV, BV, MSV) | **BACnet writable** off when the file says the value isn't settable or commandable |
| Analog, binary and multi-state alarm types | high and low limits, saved with alarming left off |
| Notification class column | saved when that notification class is in the import or already on the device |
| Device | instance, name, description |

## Stand-ins to fill in

- Trend logs turn on trending, in interval mode with the default interval and sample cap, for the point they log. They keep the log's own instance and name.
- Schedules are created empty: no times, no exceptions, a None default, no write targets. Set them on the object's **Schedule** tab.
- Calendars have no dates until you add one on the **Dates** tab.
- Notification classes get the default priorities and no recipients.

BACpack ties a trend log to a point by name (`AHU1.Temp.Log` logs `AHU1.Temp`), then keyname, then instance. A log it can't tie to exactly one point is listed under **Not imported**.

## What isn't imported

- Alarming. Limits are saved but alarming stays off, since most EDE files name no notification class. Turn it on per object on the **Alarming** tab.
- Object types BACpack doesn't simulate: loops, programs, event enrollments, structured views and proprietary types. They're listed under **Not imported**, with a **Request support for these types** link to a pre-filled GitHub issue. Nothing is sent until you submit it.
- Rows BACpack can't use: a missing or out-of-range instance, a blank name, an object listed twice. Each is listed with the reason and the rest imports. A duplicate name gets ` (2)`, ` (3)`, ... and a note.

A file that isn't an EDE file, or lacks a mandatory column, is refused. The mandatory columns are device instance, object name, object type and object instance.
