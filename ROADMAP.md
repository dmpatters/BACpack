# Roadmap

Planned after 1.0, in the order we expect to get to it. This is intent, not a promise, and the order can change. [CHANGELOG.md](CHANGELOG.md) lists every release.

1. Equipment simulators. Ready-made AHU, VAV box, heat pump, chilled-water and hot-water systems whose points behave together.
2. Fault injection. Make a device or object misbehave on purpose: a Reliability fault, a stuck value, a chosen BACnet error, slow replies.
3. Alarm and event exerciser. Fire an alarm or event on demand, at a chosen state and priority, or a burst of them, without moving the value across a limit.
4. Scenario runner. A saved, replayable timeline with run, pause and loop.
5. Device duplicate and object templates. Copy a device in one click. Save a group of objects as a template.
6. More value object types: Integer Value, Positive Integer Value, Large Analog Value and CharacterString Value.
7. Loop, Accumulator and Pulse Converter objects.
8. CreateObject and DeleteObject, so BACnet clients can add and remove objects on a BACpack device.
9. Full traffic log. Every BACnet message a device sends or receives, with an export.
10. BACnet/SC (Secure Connect), so a device can join a BACnet/SC network through a hub.

## Suggesting something

Open a [feature request](https://github.com/dmpatters/BACpack/issues/new?template=feature_request.yml) and describe the job you need done. Requests for items already on this list help decide the order.
