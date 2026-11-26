# BACpack PICS

Protocol Implementation Conformance Statement, laid out per ASHRAE 135 Annex A (the PICS form). Generated from the code by `scripts/make_pics.py`; do not edit by hand.

## 1. Product

- Vendor: BACpack open-source project. The vendor identifier and vendor name are set per device.
- Product name: BACpack
- Product version: 1.0.1
- Description: an open-source BACnet/IP server, a device simulator and gateway for testing BAS/BMS clients and integrations.
- No redundancy or high availability.

## 2. BACnet standardized device profile

BACnet Building Controller (B-BC). 21 of 21 required BIBBs are supported (DM-TS-B and DM-UTC-B are one requirement: either meets it).

Profile: B-BC as defined in Annex L of ANSI/ASHRAE 135-2024. BACpack devices report Protocol_Revision 22. ASHRAE Interpretation IC 135-2016-11 lets a device claim a profile from a newer edition of the standard.

| BIBB | Title | Supported |
|---|---|---|
| DS-RP-A | Data Sharing - ReadProperty - A | yes |
| DS-RP-B | Data Sharing - ReadProperty - B | yes |
| DS-RPM-A | Data Sharing - ReadPropertyMultiple - A | yes |
| DS-RPM-B | Data Sharing - ReadPropertyMultiple - B | yes |
| DS-WP-A | Data Sharing - WriteProperty - A | yes |
| DS-WP-B | Data Sharing - WriteProperty - B | yes |
| DS-WPM-B | Data Sharing - WritePropertyMultiple - B | yes |
| AE-N-I-B | Alarm and Event - Notification Internal - B | yes |
| AE-ACK-B | Alarm and Event - ACK - B | yes |
| AE-INFO-B | Alarm and Event - Information - B | yes |
| AE-CRL-B | Alarm and Event - Configurable Recipient Lists - B | yes |
| SCHED-E-B | Scheduling - External - B | yes |
| T-VMT-I-B | Trending - Viewing and Modifying Trends Internal - B | yes |
| T-ATR-B | Trending - Automated Trend Retrieval - B | yes |
| DM-DDB-A | Device Management - Dynamic Device Binding - A | yes |
| DM-DDB-B | Device Management - Dynamic Device Binding - B | yes |
| DM-DOB-B | Device Management - Dynamic Object Binding - B | yes |
| DM-DCC-B | Device Management - DeviceCommunicationControl - B | yes |
| DM-TS-B | Device Management - TimeSynchronization - B | yes |
| DM-UTC-B | Device Management - UTCTimeSynchronization - B | yes |
| DM-RD-B | Device Management - ReinitializeDevice - B | yes |
| DM-BR-B | Device Management - Backup and Restore - B | yes |

## 3. BACnet services

| Service | Kind | Executes | Initiates |
|---|---|---|---|
| acknowledge-alarm | confirmed | yes | no |
| add-list-element | confirmed | yes | no |
| atomic-read-file | confirmed | yes | no |
| atomic-write-file | confirmed | yes | no |
| confirmed-cov-notification | confirmed | no | yes |
| confirmed-event-notification | confirmed | no | yes |
| device-communication-control | confirmed | yes | no |
| get-event-information | confirmed | yes | no |
| i-am | unconfirmed | yes | yes |
| i-have | unconfirmed | yes | yes |
| read-property | confirmed | yes | yes |
| read-property-multiple | confirmed | yes | yes |
| read-range | confirmed | yes | no |
| reinitialize-device | confirmed | yes | no |
| remove-list-element | confirmed | yes | no |
| subscribe-cov | confirmed | yes | no |
| time-synchronization | unconfirmed | yes | no |
| unconfirmed-cov-notification | unconfirmed | no | yes |
| unconfirmed-event-notification | unconfirmed | no | yes |
| utc-time-synchronization | unconfirmed | yes | no |
| who-has | unconfirmed | yes | no |
| who-is | unconfirmed | yes | yes |
| write-property | confirmed | yes | yes |
| write-property-multiple | confirmed | yes | no |

AddListElement and RemoveListElement apply only to a Notification Class's Recipient_List.

## 4. Segmentation

Values the Device object reports:

- Segmentation supported: segmented-both (transmit and receive)
- Maximum APDU length accepted: 1024
- Maximum segments accepted: 16
- APDU segment timeout: 1000 ms
- APDU timeout: 3000 ms
- Number of APDU retries: 3

## 5. Standard object types

Standard object types only: no proprietary properties and no proprietary object types.

### analog-input

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only while Out_Of_Service is TRUE |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| units | no |  |
| cov-increment | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| high-limit | yes | present only when intrinsic reporting is configured |
| low-limit | yes | present only when intrinsic reporting is configured |
| deadband | yes | present only when intrinsic reporting is configured |
| limit-enable | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### analog-output

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| units | no |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| cov-increment | no |  |
| current-command-priority | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| high-limit | yes | present only when intrinsic reporting is configured |
| low-limit | yes | present only when intrinsic reporting is configured |
| deadband | yes | present only when intrinsic reporting is configured |
| limit-enable | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### analog-value

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| units | no |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| cov-increment | no |  |
| current-command-priority | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| high-limit | yes | present only when intrinsic reporting is configured |
| low-limit | yes | present only when intrinsic reporting is configured |
| deadband | yes | present only when intrinsic reporting is configured |
| limit-enable | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### binary-input

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only while Out_Of_Service is TRUE |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| inactive-text | no |  |
| active-text | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| alarm-value | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### binary-output

Can be created: no. Can be deleted: no.

Intrinsic reporting: not supported.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| inactive-text | no |  |
| active-text | no |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| current-command-priority | no |  |

### binary-value

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| inactive-text | no |  |
| active-text | no |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| current-command-priority | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| alarm-value | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### multi-state-input

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only while Out_Of_Service is TRUE |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| number-of-states | no |  |
| state-text | yes |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| alarm-values | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### multi-state-output

Can be created: no. Can be deleted: no.

Intrinsic reporting: not supported.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| number-of-states | no |  |
| state-text | yes |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| current-command-priority | no |  |

### multi-state-value

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | yes | writable only when the object's BACnet-writable setting is on (a priority write or a relinquish) |
| status-flags | no |  |
| event-state | no |  |
| out-of-service | yes |  |
| number-of-states | no |  |
| state-text | yes |  |
| priority-array | no |  |
| relinquish-default | yes |  |
| current-command-priority | no |  |
| time-delay | yes | present only when intrinsic reporting is configured |
| notification-class | yes | present only when intrinsic reporting is configured |
| alarm-values | yes | present only when intrinsic reporting is configured |
| event-enable | yes | present only when intrinsic reporting is configured |
| acked-transitions | no | present only when intrinsic reporting is configured |
| notify-type | yes | present only when intrinsic reporting is configured |
| event-time-stamps | no | present only when intrinsic reporting is configured |
| event-detection-enable | yes | present only when intrinsic reporting is configured |
| time-delay-normal | yes | present only when intrinsic reporting is configured; writable (present only once set, otherwise Time_Delay applies to both transitions; a write sets it) |

### schedule

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | no |  |
| effective-period | yes | writable only when the object's BACnet-writable setting is on |
| weekly-schedule | yes | writable only when the object's BACnet-writable setting is on; times to the minute (seconds or hundredths are refused) |
| exception-schedule | yes | writable only when the object's BACnet-writable setting is on; times to the minute (seconds or hundredths are refused) |
| schedule-default | yes | writable only when the object's BACnet-writable setting is on |
| list-of-object-property-references | yes | writable only when the object's BACnet-writable setting is on |
| priority-for-writing | yes | writable only when the object's BACnet-writable setting is on |
| status-flags | no |  |
| reliability | no |  |
| out-of-service | no |  |

### calendar

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| present-value | no |  |
| date-list | yes | writable only when the object's BACnet-writable setting is on |

### notification-class

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| notification-class | no |  |
| priority | yes |  |
| ack-required | yes |  |
| recipient-list | yes |  |
| status-flags | no |  |

### device

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | yes |  |
| object-type | no |  |
| property-list | no |  |
| description | yes |  |
| system-status | no |  |
| vendor-name | no |  |
| vendor-identifier | no |  |
| model-name | no |  |
| firmware-revision | no |  |
| application-software-version | no |  |
| location | yes |  |
| protocol-version | no |  |
| protocol-revision | no |  |
| protocol-services-supported | no |  |
| protocol-object-types-supported | no |  |
| object-list | no |  |
| max-apdu-length-accepted | no |  |
| segmentation-supported | no |  |
| max-segments-accepted | no |  |
| local-time | no |  |
| local-date | no |  |
| utc-offset | no |  |
| daylight-savings-status | no |  |
| apdu-segment-timeout | no |  |
| apdu-timeout | no |  |
| number-of-apdu-retries | no |  |
| device-address-binding | no |  |
| database-revision | no |  |
| configuration-files | no |  |
| last-restore-time | no |  |
| backup-failure-timeout | yes |  |
| active-cov-subscriptions | no |  |
| backup-preparation-time | no |  |
| restore-preparation-time | no |  |
| restore-completion-time | no |  |
| backup-and-restore-state | no |  |
| status-flags | no |  |

### network-port

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| status-flags | no |  |
| reliability | no |  |
| out-of-service | no |  |
| network-type | no |  |
| protocol-level | no |  |
| network-number | no |  |
| network-number-quality | no |  |
| changes-pending | no |  |
| mac-address | no |  |
| link-speed | no |  |
| bacnet-ip-mode | no |  |
| ip-address | no |  |
| bacnet-ip-udp-port | no |  |
| ip-subnet-mask | no |  |
| fd-bbmd-address | no | present only when the network role is foreign device |
| fd-subscription-lifetime | no | present only when the network role is foreign device |
| bbmd-broadcast-distribution-table | no | present only when the network role is BBMD |
| bbmd-accept-fd-registrations | no | present only when the network role is BBMD |
| bbmd-foreign-device-table | no | present only when the network role is BBMD |

### trend-log

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| status-flags | no |  |
| event-state | no |  |
| reliability | no |  |
| enable | no |  |
| log-device-object-property | no |  |
| log-interval | no |  |
| stop-when-full | no |  |
| buffer-size | no |  |
| log-buffer | no |  |
| record-count | no |  |
| total-record-count | no |  |
| logging-type | no |  |
| notification-threshold | yes | present only when the point's trend reporting is on |
| records-since-notification | no | present only when the point's trend reporting is on |
| last-notify-record | no | present only when the point's trend reporting is on |
| notification-class | yes | present only when the point's trend reporting is on |
| event-enable | yes | present only when the point's trend reporting is on |
| acked-transitions | no | present only when the point's trend reporting is on |
| notify-type | yes | present only when the point's trend reporting is on |
| event-time-stamps | no | present only when the point's trend reporting is on |
| event-detection-enable | yes | present only when the point's trend reporting is on |

### file

Can be created: no. Can be deleted: no.

| Property | Writable | Condition |
|---|---|---|
| object-identifier | no |  |
| object-name | no |  |
| object-type | no |  |
| property-list | no |  |
| description | no |  |
| file-type | no |  |
| file-size | yes | writable only to 0, and only while a restore is in progress (a client zeroes the file first) |
| modification-date | no |  |
| archive | no |  |
| read-only | no |  |
| file-access-method | no |  |

## 6. Data link layer

- BACnet/IP (Annex J), IPv4 only, one UDP port per device
- BACnet/IP BBMD: a device can act as a BBMD (broadcast distribution table, foreign device table)
- BACnet/IP foreign device: a device can register with a BBMD

## 7. Other Annex A answers

- Device address binding: static binding is not supported (Device_Address_Binding is always empty). Dynamic binding uses Who-Is / I-Am and Who-Has / I-Have
- Networking options: router: no; BBMD: yes; foreign device: yes
- Network security: none, and BACnet/SC is not supported. An optional per-device password guards DeviceCommunicationControl and ReinitializeDevice
- Character sets: ISO 10646 (UTF-8)
- Gateway options: not a protocol gateway in the Annex A sense. Connectors expose values pulled from HTTP endpoints or other BACnet devices as local objects. Proxy mappings also forward writes to the remote BACnet device
