"""SQLite-persisted config (devices, BACnet objects) plus runtime and history tables.

Priority array contents and present values are deliberately not stored: they
live only in the bacpypes3 objects built at process start, so stale priority
slots can't reassert after a restart.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import ForeignKey, Index, JSON, UniqueConstraint, delete, event, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .secrets_store import EncryptedHeaders, EncryptedText

# bacpypes3 ObjectType names, in the kebab-case form stored in the DB.
OBJECT_TYPES = (
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "multi-state-input",
    "multi-state-output",
    "multi-state-value",
    "schedule",
    "calendar",
    "notification-class",
)

ANALOG_TYPES = {"analog-input", "analog-output", "analog-value"}
BINARY_TYPES = {"binary-input", "binary-output", "binary-value"}
MULTISTATE_TYPES = {"multi-state-input", "multi-state-output", "multi-state-value"}

# Largest per-trend sample cap, and the cap of a trend without one; a Trend
# Log's Buffer_Size is its cap. 32767 for compatibility with some software
# that refuses a larger Buffer_Size.
MAX_TREND_SAMPLE_CAP = 32767


def effective_trend_sample_cap(cap: Optional[int]) -> int:
    return cap or MAX_TREND_SAMPLE_CAP

DEFAULT_ACTIVE_TEXT = "On"
DEFAULT_INACTIVE_TEXT = "Off"

INPUT_TYPES = frozenset({"analog-input", "binary-input", "multi-state-input"})

COMMANDABLE_TYPES = {
    "analog-output", "analog-value", "binary-output", "binary-value",
    "multi-state-output", "multi-state-value",
}

ALARMABLE_TYPES = {
    "analog-input", "analog-output", "analog-value",
    "binary-input", "binary-value",
    "multi-state-input", "multi-state-value",
}


# The highest BACnet object or device instance (ASHRAE 135: 4194303 is
# reserved as "uninitialized").
MAX_INSTANCE = 4194302

# A client that doesn't know a device's instance addresses it as
# device,4194303 ("this device"); the device answers with its real one.
WILDCARD_DEVICE_INSTANCE = MAX_INSTANCE + 1


class Device(Base):
    __tablename__ = "devices"

    # Also the BACnet device instance number (0-MAX_INSTANCE): it must be globally
    # unique anyway, so no separate surrogate key.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[Optional[str]] = mapped_column(default=None)
    location: Mapped[Optional[str]] = mapped_column(default=None)
    vendor_identifier: Mapped[int] = mapped_column(default=999)
    # None falls back to "BACpack" in build_device_object, so no backfill.
    vendor_name: Mapped[Optional[str]] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    # CIDR, e.g. "192.168.1.51/24"; None falls back to ServerSettings.bind_address.
    bind_address: Mapped[Optional[str]] = mapped_column(default=None)

    # One of "normal", "foreign", "bbmd". A BBMD's
    # table is built at start from its links plus these typed peers.
    network_role: Mapped[str] = mapped_column(default="normal", server_default=text("'normal'"))
    foreign_bbmd: Mapped[Optional[str]] = mapped_column(default=None)
    foreign_ttl: Mapped[int] = mapped_column(default=60, server_default=text("60"))
    bbmd_peers: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    bbmd_links: Mapped[list["DeviceLink"]] = relationship(
        foreign_keys="DeviceLink.device_id",
        order_by="DeviceLink.linked_device_id",
        cascade="all, delete-orphan",
    )

    # DM-DCC-B. Written by the child's
    # DeviceCommunicationControl handler; reset to "enable" on every process
    # start, as a real device's comms-disabled state doesn't survive a reboot.
    dcc_state: Mapped[str] = mapped_column(default="enable")
    # scrypt hash of the optional DCC/ReinitializeDevice password; it is only
    # ever compared. Never exported, cloned or restored, and no change
    # trigger watches it: the device process reads it per request.
    bacnet_password_hash: Mapped[Optional[str]] = mapped_column(default=None)

    # Seconds added to host time. Persisted, unlike
    # priority arrays: a real controller's clock is battery-backed and survives
    # its own reboot. Written by the child, or by the API for a stopped device.
    clock_offset_seconds: Mapped[float] = mapped_column(default=0.0)
    # Database_Revision/Last_Restore_Time.
    # database_revision is bumped by db._bump_database_revision on every object
    # create/delete/rename; last_restore_time is in device-local time.
    database_revision: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    last_restore_time: Mapped[Optional[dt.datetime]] = mapped_column(default=None)
    # Backup_Failure_Timeout, seconds: written over BACnet by the
    # backup client and saved by the child itself, so no change trigger
    # (migrations.TRIGGERS) watches it.
    backup_failure_timeout: Mapped[int] = mapped_column(default=60, server_default=text("60"))

    objects: Mapped[list["BacnetObject"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    # Always present: see _new_device_runtime.
    runtime: Mapped["DeviceRuntime"] = relationship(
        back_populates="device", cascade="all, delete-orphan", lazy="selectin"
    )
    # passive_deletes: these can hold hundreds of thousands of rows, which an
    # ORM cascade would load one by one; _bulk_delete_device_history (below)
    # removes them in one statement.
    traffic_log_entries: Mapped[list["TrafficLogEntry"]] = relationship(
        primaryjoin="Device.id == foreign(TrafficLogEntry.device_id)",
        back_populates="device",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    log_entries: Mapped[list["LogEntry"]] = relationship(
        primaryjoin="Device.id == foreign(LogEntry.device_id)",
        back_populates="device",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # BACnet client role: a "bacnet" connector
    # runs in its device's process.
    bacnet_connectors: Mapped[list["Connector"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    client_requests: Mapped[list["ClientRequest"]] = relationship(
        primaryjoin="Device.id == foreign(ClientRequest.device_id)",
        back_populates="device",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"Device(id={self.id!r}, name={self.name!r})"


class DeviceLink(Base):
    """A BBMD device forwarding to another device of this server: the linked
    device's socket joins the BBMD's broadcast distribution table. One-way;
    the linked device is untouched. The migrations' delete trigger removes a
    row when either device goes."""

    __tablename__ = "device_links"
    __table_args__ = (UniqueConstraint("device_id", "linked_device_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    linked_device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))


class DeviceRuntime(Base):
    """Process tracking and a pending clock change, written while the device
    runs. One row per device. Not config:
    never exported."""

    __tablename__ = "device_runtime"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    # Persisted so the parent can reattach to a still-running child after its
    # own restart (daemon=False -- see EngineManager).
    process_pid: Mapped[Optional[int]] = mapped_column(default=None)
    process_started_at: Mapped[Optional[dt.datetime]] = mapped_column(default=None)
    # Linux only: boot id and start time counted from boot, which a wall-clock
    # step doesn't move (process_started_at, from psutil, does) -- see
    # process_identity.py and engine._is_same_process. Null on Windows.
    process_boot_id: Mapped[Optional[str]] = mapped_column(default=None)
    process_started_since_boot: Mapped[Optional[float]] = mapped_column(default=None)
    # "stopped" | "running" | "stopping" (graceful stop requested, child
    # hasn't confirmed) | "crashed" (died without a user-initiated stop).
    process_status: Mapped[str] = mapped_column(default="stopped")
    # Set by the child with "crashed"; EngineManager.start() reports it when a
    # spawn fails during its startup window.
    process_error: Mapped[Optional[str]] = mapped_column(default=None)
    # Set when a requested parent shutdown stopped this device, or at startup
    # for a "running" row whose process is gone (a power-off); the next
    # startup restarts it. Cleared by a successful start or an explicit stop.
    resume_on_start: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    # Web-UI hand-off to a running child; PendingWritePoller applies and clears it.
    pending_clock_offset: Mapped[Optional[float]] = mapped_column(default=None)
    # Bumped only by SQLite triggers (migrations.TRIGGERS) when config this
    # device reads changes or a write is queued for it; DeviceWatch compares
    # it each tick.
    change_seq: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Bumped with change_seq by config triggers but not by a queued write:
    # DeviceWatch reloads objects and runners only when this moves.
    config_seq: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Written by ObjectReloader: the
    # change_seq last applied, the last apply's failure, and whether saved
    # device settings differ from what is served. Reset by _mark_running.
    applied_seq: Mapped[Optional[int]] = mapped_column(default=None)
    apply_error: Mapped[Optional[str]] = mapped_column(default=None)
    restart_pending: Mapped[bool] = mapped_column(default=False, server_default=text("0"))

    device: Mapped["Device"] = relationship(back_populates="runtime")


class BacnetObject(Base):
    __tablename__ = "bacnet_objects"
    __table_args__ = (
        UniqueConstraint("device_id", "object_type", "instance_number"),
        UniqueConstraint("device_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    object_type: Mapped[str] = mapped_column(nullable=False)
    instance_number: Mapped[int] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[Optional[str]] = mapped_column(default=None)

    # Analog only; also a display label for an analog-valued schedule.
    units: Mapped[Optional[str]] = mapped_column(default=None)

    # Whether an external BACnet client may write presentValue. False makes
    # WriteProperty fail with writeAccessDenied (BacnetWriteLocked in
    # bacnet.py) while BACpack's own feed/manual writes still work. Whether
    # an object has a priority array at all is fixed by object_type.
    # bacnet_writable/relinquish_default are ignored for Input types,
    # schedule and calendar (not commandable). out_of_service_default applies
    # to schedule but not calendar, which has no Out_Of_Service property.
    bacnet_writable: Mapped[bool] = mapped_column(default=True)
    out_of_service_default: Mapped[bool] = mapped_column(default=False)
    relinquish_default: Mapped[Optional[str]] = mapped_column(default=None)
    # Binary only: Active_Text/Inactive_Text. NULL reads as DEFAULT_ACTIVE_TEXT/
    # DEFAULT_INACTIVE_TEXT, so a row saved before they were settings, or never
    # given any, keeps showing On/Off.
    active_text: Mapped[Optional[str]] = mapped_column(default=None)
    inactive_text: Mapped[Optional[str]] = mapped_column(default=None)
    # Input types only: the value the input shows at device start, stored like
    # relinquish_default. NULL means 0, inactive, state 1.
    start_value: Mapped[Optional[str]] = mapped_column(default=None)

    # Multi-state only: state labels, index 0 = state 1. Also a display label
    # for a multi-state-valued schedule.
    state_text: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # --- Schedule / Calendar objects only ---------------------------------

    # Schedule only: "analog" | "binary" | "multi-state" -- how presentValue,
    # every time-value and schedule_default are encoded.
    schedule_value_type: Mapped[Optional[str]] = mapped_column(default=None)
    # Schedule only, required: Schedule_Default, same plain-string convention
    # as relinquish_default.
    schedule_default: Mapped[Optional[str]] = mapped_column(default=None)
    # Schedule only: 7 entries (Mon-Sun), each a list of
    # {"time": "HH:MM", "value": "<str>"|None}; None is BACnet Null
    # (relinquish to schedule_default).
    weekly_schedule: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Schedule only: list of {"period": {...}, "priority": 1-16,
    # "time_values": [...]} -- see validate_schedule_config
    # (objects/schedule_config.py) for period's shapes.
    exception_schedule: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Schedule only: Effective_Period, "YYYY-MM-DD". Both unset is a fully
    # wildcarded (always in effect) DateRange; a single open side gets a
    # concrete limit (objects/schedule_config.effective_period).
    schedule_effective_start: Mapped[Optional[str]] = mapped_column(default=None)
    schedule_effective_end: Mapped[Optional[str]] = mapped_column(default=None)
    # Schedule only: Priority_For_Writing. Default is deliberately lower
    # precedence than FEED_PRIORITY(10)/MANUAL_PRIORITY(8), so a feed or
    # manual override wins over the schedule.
    schedule_priority: Mapped[int] = mapped_column(default=14)
    # Schedule only: List_Of_Object_Property_References, list of
    # {"object_type", "instance_number", "property_identifier"}. An entry with
    # a `device_instance` targets another device, sent by
    # ExternalScheduleWriter.
    schedule_write_targets: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Calendar only: Date_List, the same {"kind": ...} entries as a schedule
    # exception's period (see validate_calendar_config).
    date_list: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # Trending config. Opt-in per object.
    trended: Mapped[bool] = mapped_column(default=False)
    # One of "interval" | "cov". Only consulted when trended=True.
    trend_mode: Mapped[str] = mapped_column(default="interval")
    # Used when trend_mode="interval"; ignored for "cov".
    trend_interval_seconds: Mapped[Optional[int]] = mapped_column(default=None)
    # 1-MAX_TREND_SAMPLE_CAP; None means MAX_TREND_SAMPLE_CAP.
    trend_sample_cap: Mapped[Optional[int]] = mapped_column(default=None)
    # The Trend Log's Object_Identifier instance and Object_Name. NULL means the
    # row id and "<name>-Trend" (objects/reserved_names); set means fixed, so a
    # clone of a real controller keeps the identity its graphics bind to.
    trend_log_instance: Mapped[Optional[int]] = mapped_column(default=None)
    trend_log_name: Mapped[Optional[str]] = mapped_column(default=None)
    # BUFFER_READY reporting on this object's Trend Log, T-ATR-B. Opt-in.
    trend_reporting_enabled: Mapped[bool] = mapped_column(default=False)
    # Notification_Threshold; 0 means never notify.
    trend_notification_threshold: Mapped[int] = mapped_column(default=10)
    # Separate from notification_class_instance: alarms and the log's
    # BUFFER_READY may go to different classes.
    trend_notification_class_instance: Mapped[Optional[int]] = mapped_column(default=None)
    # 3 bools [to-offnormal, to-fault, to-normal]; None means all-True.
    trend_event_enable: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    trend_notify_type: Mapped[str] = mapped_column(default="event")  # "alarm" | "event"
    trend_event_detection_enable: Mapped[bool] = mapped_column(default=True)

    # Intrinsic reporting, opt-in, only for
    # ALARMABLE_TYPES: Binary/Multi-state Output have no working bacpypes3
    # event algorithm (the design doc's CommandFailure gap). Event state is
    # never persisted; a device restart resets every alarm to Normal.
    alarming_enabled: Mapped[bool] = mapped_column(default=False)
    # A notification-class instance_number on the same device -- a plain
    # number, like BACnet's own Notification_Class, not a DB foreign key.
    # Required when alarming_enabled.
    notification_class_instance: Mapped[Optional[int]] = mapped_column(default=None)
    alarm_time_delay: Mapped[int] = mapped_column(default=0)
    # None falls back to alarm_time_delay for the to-normal transition.
    alarm_time_delay_normal: Mapped[Optional[int]] = mapped_column(default=None)
    alarm_notify_type: Mapped[str] = mapped_column(default="alarm")  # "alarm" | "event"
    # 3 bools [to-offnormal, to-fault, to-normal]; None means all-True.
    alarm_event_enable: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Analog only.
    alarm_high_limit: Mapped[Optional[float]] = mapped_column(default=None)
    alarm_low_limit: Mapped[Optional[float]] = mapped_column(default=None)
    alarm_deadband: Mapped[float] = mapped_column(default=0.0)
    # 2 bools [low-limit-enable, high-limit-enable]; None means all-True.
    alarm_limit_enable: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Binary only: the present-value state considered "alarm".
    alarm_value: Mapped[Optional[bool]] = mapped_column(default=None)
    # Multi-state only: 1-based state numbers considered alarm states.
    alarm_values: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Event_Detection_Enable: False keeps
    # the alarm config but stops detection.
    alarm_event_detection_enable: Mapped[bool] = mapped_column(default=True)

    # Notification Class objects only; instance_number is the
    # Notification_Class number other objects reference.
    # 3 ints [offnormal, fault, normal] each 0-255; None means [100,100,100].
    notification_priority: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # 3 bools [offnormal, fault, normal]; None means all-True.
    notification_ack_required: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # List of {recipient: {"kind": "device", "instance_number": N} |
    # {"kind": "address", "address": "192.168.1.200[:47808]"}, valid_days:
    # [7 bools], from_time, to_time, transitions: [3 bools], confirmed:
    # bool, process_identifier: int}. None/[] means no recipients configured.
    notification_recipient_list: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # Value simulator: {"kind": "sine", ...}
    # checked by simulation.parse_simulation, or None.
    simulation: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    device: Mapped[Device] = relationship(back_populates="objects")
    # passive_deletes: see Device.log_entries; _bulk_delete_object_samples
    # removes them.
    trend_samples: Mapped[list["TrendSample"]] = relationship(
        primaryjoin="BacnetObject.id == foreign(TrendSample.object_id)",
        back_populates="object",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # None until first needed -- create through trend_state_of.
    trend_state: Mapped[Optional["TrendState"]] = relationship(
        primaryjoin="BacnetObject.id == foreign(TrendState.object_id)",
        back_populates="object",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # Cascade: foreign keys aren't enforced (db.py), so a mapping would
    # otherwise outlive its object with ConnectorMapping.object None.
    connector_mapping: Mapped[Optional["ConnectorMapping"]] = relationship(
        back_populates="object", cascade="all, delete-orphan"
    )
    connector_write_mappings: Mapped[list["ConnectorWriteMapping"]] = relationship(
        back_populates="object", cascade="all, delete-orphan"
    )
    # Always present: see _new_object_runtime.
    runtime: Mapped["ObjectRuntime"] = relationship(
        back_populates="object", cascade="all, delete-orphan", lazy="selectin"
    )

    def queue_manual_write(self, value) -> None:
        self.runtime.pending_manual_value = value
        self.runtime.pending_manual_queued = True

    def queue_feed_write(self, value) -> None:
        self.runtime.pending_feed_value = value
        self.runtime.pending_feed_queued = True

    def __repr__(self) -> str:
        return (
            f"BacnetObject(device_id={self.device_id!r}, "
            f"object_type={self.object_type!r}, "
            f"instance_number={self.instance_number!r}, name={self.name!r})"
        )


class TrendSample(Base):
    """One sample of an object's presentValue: the source for both the Trends
    UI and the BACnet Trend_Log objects' ReadRange responses."""

    __tablename__ = "trend_samples"
    __table_args__ = (
        UniqueConstraint("object_id", "sequence"),
        Index("ix_trend_samples_object_timestamp", "object_id", "timestamp"),
        # One log's rows in id order without a sort, for ReadRange.
        Index("ix_trend_samples_object_id", "object_id", "id"),
        # For the globally-oldest-sample query (trending.prune_global_to_limit),
        # which the object_id-led index above doesn't serve.
        Index("ix_trend_samples_timestamp", "timestamp"),
        {"schema": "history"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # A plain integer: SQLite can't enforce a foreign key across files.
    # The before_delete listeners below
    # remove these rows with their owner.
    object_id: Mapped[int] = mapped_column()
    # Per-object counter (TrendState.next_sequence), unlike the shared id.
    sequence: Mapped[int] = mapped_column(nullable=False)
    timestamp: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    # Binary as 0/1, multi-state as its state number, analog as-is.
    value: Mapped[float] = mapped_column(nullable=False)

    # Device clock offset when written. `timestamp`
    # stays true UTC; only BACnet output adds this, so history keeps its
    # old-clock times after a sync.
    clock_offset: Mapped[float] = mapped_column(default=0.0)
    # "value" | "time-change" (a BACnet time-change record; `value` holds the
    # clock jump in seconds). The web views show "value" rows only.
    kind: Mapped[str] = mapped_column(default="value")

    object: Mapped[BacnetObject] = relationship(
        primaryjoin="foreign(TrendSample.object_id) == BacnetObject.id", back_populates="trend_samples"
    )

    def __repr__(self) -> str:
        return (
            f"TrendSample(object_id={self.object_id!r}, sequence={self.sequence!r}, "
            f"value={self.value!r})"
        )


class TrendState(Base):
    """Per-object trend log counters. In the history file next to the
    samples, so a sample insert, its sequence bump and the global count
    commit in one file without the main file's write lock."""

    __tablename__ = "trend_state"
    __table_args__ = {"schema": "history"}

    object_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    # Next Trend_Log sequence number. Persisted because per spec sequence
    # numbers only reset when the log is cleared, not on restart. Wraps at
    # trending._MAX_SEQUENCE; Total_Record_Count = next_sequence - 1.
    next_sequence: Mapped[int] = mapped_column(default=1)
    # Last_Notify_Record for BUFFER_READY, persisted so a restart neither
    # skips nor repeats records.
    last_notify_record: Mapped[int] = mapped_column(default=0)

    # The log's ordered tail, for ReadRange byTime (trend_buffer.advance_tail):
    # rows from ordered_from_id on are
    # ordered by stored time within tolerances; tail_last_id is the newest row
    # the bookkeeping saw, and the tail is trusted only while it is the log's
    # newest. 0 / NULL until a first row is written.
    ordered_from_id: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    tail_last_id: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    tail_max_timestamp: Mapped[Optional[dt.datetime]] = mapped_column(default=None)
    tail_min_offset: Mapped[Optional[float]] = mapped_column(default=None)
    tail_max_offset: Mapped[Optional[float]] = mapped_column(default=None)

    object: Mapped["BacnetObject"] = relationship(
        primaryjoin="foreign(TrendState.object_id) == BacnetObject.id", back_populates="trend_state"
    )


class HistoryCounters(Base):
    """Single-row running totals for the history file.
    trend_total_sample_count avoids a COUNT(*) scan over millions of rows;
    every path that inserts or deletes a TrendSample must keep it in sync
    (trending._adjust_total_sample_count)."""

    __tablename__ = "history_counters"
    __table_args__ = {"schema": "history"}

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    trend_total_sample_count: Mapped[int] = mapped_column(default=0)


class TrafficLogEntry(Base):
    """Persisted mirror of a device's TrafficLog ring buffer (traffic.py), so
    the parent can read what the device's child process logged. Pruned to
    TrafficLog's maxlen per device with each batch the process's
    history_writer commits."""

    __tablename__ = "traffic_log_entries"
    __table_args__ = (Index("ix_traffic_log_entries_device_id", "device_id"), {"schema": "history"})

    id: Mapped[int] = mapped_column(primary_key=True)
    # Plain integer: no foreign key across files (see TrendSample.object_id).
    device_id: Mapped[int] = mapped_column()
    timestamp: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    event_type: Mapped[str] = mapped_column(nullable=False)
    detail: Mapped[str] = mapped_column(nullable=False)

    device: Mapped[Device] = relationship(
        primaryjoin="foreign(TrafficLogEntry.device_id) == Device.id", back_populates="traffic_log_entries"
    )

    def __repr__(self) -> str:
        return f"TrafficLogEntry(device_id={self.device_id!r}, event_type={self.event_type!r})"


class LogEntry(Base):
    """BACpack's own Python `logging` output, captured by
    log_capture.SQLiteLogHandler. device_id is
    None for parent-process events with no owning device. Pruned by
    ServerSettings.log_retention_days."""

    __tablename__ = "log_entries"
    __table_args__ = (Index("ix_log_entries_device_id", "device_id"), {"schema": "history"})

    id: Mapped[int] = mapped_column(primary_key=True)
    # Plain integer: no foreign key across files (see TrendSample.object_id).
    device_id: Mapped[Optional[int]] = mapped_column(default=None)
    timestamp: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    level: Mapped[str] = mapped_column(nullable=False)
    logger_name: Mapped[str] = mapped_column(nullable=False)
    message: Mapped[str] = mapped_column(nullable=False)

    device: Mapped[Optional[Device]] = relationship(
        primaryjoin="foreign(LogEntry.device_id) == Device.id", back_populates="log_entries"
    )

    def __repr__(self) -> str:
        return f"LogEntry(device_id={self.device_id!r}, level={self.level!r})"


class Connector(Base):
    """One external connection polled on an interval: "http-poll" or "bacnet"."""

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False, unique=True)
    type: Mapped[str] = mapped_column(default="http-poll")
    enabled: Mapped[bool] = mapped_column(default=True)

    url: Mapped[str] = mapped_column(nullable=False)
    poll_interval_seconds: Mapped[int] = mapped_column(default=30)
    timeout_seconds: Mapped[int] = mapped_column(default=10)
    # "json" | "plain-text" (the whole body is the value).
    response_format: Mapped[str] = mapped_column(default="json")

    # "none" | "basic" | "bearer". The password, token and header values are
    # encrypted at rest by their column types (secrets_store).
    auth_type: Mapped[str] = mapped_column(default="none")
    auth_username: Mapped[Optional[str]] = mapped_column(default=None)
    auth_password: Mapped[Optional[str]] = mapped_column(EncryptedText, default=None)
    auth_token: Mapped[Optional[str]] = mapped_column(EncryptedText, default=None)
    headers: Mapped[Optional[dict]] = mapped_column(EncryptedHeaders, default=None)
    # False skips the TLS certificate check (self-signed lab endpoints).
    # http-poll only.
    verify_tls: Mapped[bool] = mapped_column(default=True)

    # type="bacnet": one remote device, talked to from device_id's process.
    # url is stored as "" for these: it's NOT NULL, and rebuilding the table
    # to relax that isn't worth the risk to device processes started before
    # an upgrade.
    device_id: Mapped[Optional[int]] = mapped_column(ForeignKey("devices.id"), default=None)
    remote_device_instance: Mapped[Optional[int]] = mapped_column(default=None)
    # "IP[:port]", "network:IP[:port]", "network:0xMAC" or "network:MAC" (one
    # decimal octet); None = bind by Who-Is. See bacnet_client.parse_address.
    remote_address: Mapped[Optional[str]] = mapped_column(default=None)
    # Write mappings are re-sent this often even when unchanged; 0 = on change only.
    rewrite_interval_seconds: Mapped[int] = mapped_column(default=60)

    mappings: Mapped[list["ConnectorMapping"]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )
    write_mappings: Mapped[list["ConnectorWriteMapping"]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )
    # Always present: see _new_connector_runtime.
    runtime: Mapped["ConnectorRuntime"] = relationship(
        back_populates="connector", cascade="all, delete-orphan", lazy="selectin"
    )
    device: Mapped[Optional[Device]] = relationship(back_populates="bacnet_connectors")

    def __repr__(self) -> str:
        return f"Connector(id={self.id!r}, name={self.name!r}, url={self.url!r})"


class ObjectRuntime(Base):
    """The cross-process write queue and an external schedule's delivery
    status. One row per object. Not config:
    never exported."""

    __tablename__ = "object_runtime"

    object_id: Mapped[int] = mapped_column(ForeignKey("bacnet_objects.id"), primary_key=True)
    # Write queue: the parent can't reach a live object in a child process,
    # so the API and ConnectorPoller queue writes here and the child's
    # PendingWritePoller applies them.
    # One slot per priority, so a manual and a feed write in one tick don't
    # replace each other. *_queued is the sentinel because a queued None
    # means "relinquish".
    pending_manual_value: Mapped[Optional[Any]] = mapped_column(JSON, default=None)
    pending_manual_queued: Mapped[bool] = mapped_column(default=False)
    pending_feed_value: Mapped[Optional[Any]] = mapped_column(JSON, default=None)
    pending_feed_queued: Mapped[bool] = mapped_column(default=False)
    # Schedule only, written by ExternalScheduleWriter: per target, keyed
    # "device:type:instance:property", {"last_sent_at", "last_sent_value",
    # "error", "suppressed_by_dcc"}. Read-only in the API.
    schedule_write_status: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    object: Mapped["BacnetObject"] = relationship(back_populates="runtime")


class ConnectorRuntime(Base):
    """A bacnet connector's status, written by its device's process
    (BacnetConnectorRunner). One row per connector."""

    __tablename__ = "connector_runtime"

    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"), primary_key=True)
    client_status: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    connector: Mapped["Connector"] = relationship(back_populates="runtime")


class ConnectorMapping(Base):
    """One value from a connector's polled response, written into one local
    object. object_id is unique so two connectors can't silently fight over
    the same FEED_PRIORITY slot."""

    __tablename__ = "connector_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"))
    object_id: Mapped[int] = mapped_column(ForeignKey("bacnet_objects.id"), unique=True)

    # Dot-path, e.g. "data.temperature"; required for response_format="json".
    json_path: Mapped[Optional[str]] = mapped_column(default=None)
    scale: Mapped[float] = mapped_column(default=1.0)
    offset: Mapped[float] = mapped_column(default=0.0)
    # e.g. {"open": 1, "closed": 0}; applied before scale/offset.
    value_map: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    # bacnet connectors: the remote point polled (kebab-case names). mode is
    # "poll" only; COV subscriptions are not implemented.
    remote_object_type: Mapped[Optional[str]] = mapped_column(default=None)
    remote_object_instance: Mapped[Optional[int]] = mapped_column(default=None)
    remote_property: Mapped[Optional[str]] = mapped_column(default=None)
    remote_array_index: Mapped[Optional[int]] = mapped_column(default=None)
    mode: Mapped[str] = mapped_column(default="poll")
    # "read" | "proxy": a proxy mirrors
    # the remote's Present_Value and Priority_Array and passes local writes
    # through at the same priority.
    direction: Mapped[str] = mapped_column(default="read")

    connector: Mapped[Connector] = relationship(back_populates="mappings")
    object: Mapped["BacnetObject"] = relationship(back_populates="connector_mapping")

    def __repr__(self) -> str:
        return (
            f"ConnectorMapping(connector_id={self.connector_id!r}, "
            f"object_id={self.object_id!r}, json_path={self.json_path!r})"
        )


class ConnectorWriteMapping(Base):
    """Sends one local object's Present_Value to one remote property at
    `priority`, on change and every Connector.rewrite_interval_seconds.
    The remote target's per-connector uniqueness is checked in the API, not a
    UniqueConstraint: SQLite treats NULL remote_array_index values as
    distinct, so the constraint would miss the common case."""

    __tablename__ = "connector_write_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"))
    object_id: Mapped[int] = mapped_column(ForeignKey("bacnet_objects.id"))
    remote_object_type: Mapped[str] = mapped_column(nullable=False)
    remote_object_instance: Mapped[int] = mapped_column(nullable=False)
    remote_property: Mapped[str] = mapped_column(default="present-value")
    remote_array_index: Mapped[Optional[int]] = mapped_column(default=None)
    priority: Mapped[int] = mapped_column(default=8)

    connector: Mapped[Connector] = relationship(back_populates="write_mappings")
    object: Mapped["BacnetObject"] = relationship(back_populates="connector_write_mappings")

    def __repr__(self) -> str:
        return (
            f"ConnectorWriteMapping(connector_id={self.connector_id!r}, "
            f"object_id={self.object_id!r}, remote={self.remote_object_type},"
            f"{self.remote_object_instance} {self.remote_property} @ {self.priority})"
        )


class ClientRequest(Base):
    """A discovery/ad-hoc BACnet client request from the web UI: inserted
    "pending", claimed and answered by the device's process
    (client_requests.py). Pruned after a day."""

    __tablename__ = "client_requests"
    __table_args__ = (
        Index("ix_client_requests_device_status", "device_id", "status"),
        {"schema": "history"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Plain integer: no foreign key across files (see TrendSample.object_id).
    device_id: Mapped[int] = mapped_column()
    # "who-is" | "object-list" | "identify" | "read" | "write" | "live" |
    # "clone-read"
    kind: Mapped[str] = mapped_column(nullable=False)
    params: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    # "pending" -> "running" -> "done" | "error"
    status: Mapped[str] = mapped_column(default="pending")
    result: Mapped[Optional[Any]] = mapped_column(JSON, default=None)
    error: Mapped[Optional[str]] = mapped_column(default=None)
    # A "clone-read" job only: how far
    # the read has got, {"done": n, "total": n}, and the web UI's cancel flag,
    # which the device's process reads each time it saves progress.
    progress: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    cancel_requested: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(default=None)

    device: Mapped[Device] = relationship(
        primaryjoin="foreign(ClientRequest.device_id) == Device.id", back_populates="client_requests"
    )

    def __repr__(self) -> str:
        return f"ClientRequest(id={self.id!r}, kind={self.kind!r}, status={self.status!r})"


class ServerSettings(Base):
    """Single-row table (id is always 1) for server-wide settings."""

    __tablename__ = "server_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    # CIDR, e.g. "192.168.1.50/24": the default for a device without its own
    # bind_address. Never auto-detected; None means not configured yet.
    bind_address: Mapped[Optional[str]] = mapped_column(default=None)

    # One of "normal", "foreign", "bbmd" -- see build_network_port_object().
    network_role: Mapped[str] = mapped_column(default="normal")
    foreign_bbmd: Mapped[Optional[str]] = mapped_column(default=None)
    foreign_ttl: Mapped[int] = mapped_column(default=60)
    bbmd_peers: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # Retired HTTP Basic auth columns: never read, kept because a column drop
    # would break older device children that select every column.
    auth_enabled: Mapped[bool] = mapped_column(default=False)
    auth_username: Mapped[Optional[str]] = mapped_column(default=None)
    auth_password_hash: Mapped[Optional[str]] = mapped_column(default=None)

    # None = not chosen yet (first-use setup screen); "login" or "none".
    login_mode: Mapped[Optional[str]] = mapped_column(default=None)
    login_username: Mapped[Optional[str]] = mapped_column(default=None)
    login_password_hash: Mapped[Optional[str]] = mapped_column(default=None)
    # Bumped by every credential change, local reset and mode switch: the web
    # process clears its in-memory lockout when it sees a new value.
    login_epoch: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    # Read only by the launcher at start, so a change
    # waits for a restart. Not in the config export: it belongs to this machine.
    web_listen_address: Mapped[str] = mapped_column(default="0.0.0.0", server_default=text("'0.0.0.0'"))

    # Not used to choose what runs: every device runs in its own process.
    # Only reported by the settings API.
    active_device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("devices.id"), default=None
    )

    # Global trend retention; None keeps forever.
    trend_retention_days: Mapped[Optional[int]] = mapped_column(
        default=90, server_default=text("90")
    )

    # Soft size limit (MB) for all trend samples. 0 or None disables it, leaving
    # only trending.TREND_HARD_CAP_BYTES. Validated 0-1024 in the API.
    trend_soft_limit_mb: Mapped[Optional[int]] = mapped_column(default=None)

    # Log retention in days. Shorter than trend retention: logs are
    # chattier. None keeps forever.
    log_retention_days: Mapped[Optional[int]] = mapped_column(
        default=14, server_default=text("14")
    )

    def __repr__(self) -> str:
        return f"ServerSettings(bind_address={self.bind_address!r}, network_role={self.network_role!r})"


def get_or_create_settings(session) -> ServerSettings:
    settings = session.get(ServerSettings, 1)
    if settings is None:
        settings = ServerSettings(id=1)
        session.add(settings)
        session.flush()
    return settings


def get_or_create_history_counters(session) -> HistoryCounters:
    counters = session.get(HistoryCounters, 1)
    if counters is None:
        counters = HistoryCounters(id=1)
        session.add(counters)
        session.flush()
    return counters


def trend_state_of(obj: BacnetObject) -> TrendState:
    """obj's trend counters, created on first need."""
    if obj.trend_state is None:
        obj.trend_state = TrendState()
    return obj.trend_state


# --- Bulk history deletes -----------------------------------------------------
# With passive_deletes the ORM doesn't load these rows, and foreign keys aren't
# enforced (db.py), so they are deleted here in the flush's own transaction,
# covering every delete path at once.


@event.listens_for(BacnetObject, "before_delete")
def _bulk_delete_object_samples(mapper, connection, target) -> None:
    # Rows only: every delete path already lowers trend_total_sample_count
    # via trending.release_trend_samples_for_objects; doing it here too would
    # count the delete twice.
    connection.execute(delete(TrendSample).where(TrendSample.object_id == target.id))
    connection.execute(delete(TrendState).where(TrendState.object_id == target.id))


@event.listens_for(Device, "before_delete")
def _bulk_delete_device_history(mapper, connection, target) -> None:
    for model in (LogEntry, TrafficLogEntry, ClientRequest):
        connection.execute(delete(model).where(model.device_id == target.id))


# --- Runtime rows ----------------------------
# One runtime row per owner, created with it, so `x.runtime` is never None.


@event.listens_for(Device, "init")
def _new_device_runtime(target, args, kwargs) -> None:
    if "runtime" not in kwargs:
        target.runtime = DeviceRuntime()


@event.listens_for(BacnetObject, "init")
def _new_object_runtime(target, args, kwargs) -> None:
    if "runtime" not in kwargs:
        target.runtime = ObjectRuntime()


@event.listens_for(Connector, "init")
def _new_connector_runtime(target, args, kwargs) -> None:
    if "runtime" not in kwargs:
        target.runtime = ConnectorRuntime()


LOGIN_MODES = ("login", "none")


class WebSession(Base):
    """A signed-in browser session. Only the sha256 of the cookie token is
    stored."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(unique=True)
    created: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    client_ip: Mapped[Optional[str]] = mapped_column(default=None)
    user_agent: Mapped[Optional[str]] = mapped_column(default=None)


class ApiKey(Base):
    """A named, revocable key for programs. Only the sha256 is stored; prefix
    is the first characters of the key, for display."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    key_hash: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str] = mapped_column()
    created: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_used: Mapped[Optional[dt.datetime]] = mapped_column(default=None)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(default=None)
