// Mirrors the plain-dict shapes the web/api/ package's _*_dict() helpers
// return -- see web/schemas.py's module docstring ("Responses are plain
// dicts... not worth a parallel set of response models").

export type NetworkRole = "normal" | "foreign" | "bbmd";

export interface DeviceSummary {
  id: number;
  name: string;
  description: string | null;
  location: string | null;
  vendor_identifier: number;
  vendor_name: string | null;
  // Seconds a BACnet backup or restore may sit idle before the device ends
  // it. Always sent by the server; optional so older fixtures still type-check.
  backup_failure_timeout?: number;
  // Whether a DCC/ReinitializeDevice password is set; the password itself is
  // never returned. Optional so older fixtures still type-check.
  bacnet_password_set?: boolean;
  object_count: number;
  bind_address: string | null;
  // The device's network role. Always sent by the server; optional so older
  // fixtures still type-check.
  network_role?: NetworkRole;
  foreign_bbmd?: string | null;
  foreign_ttl?: number;
  bbmd_peers?: string[] | null;
  // Device instance numbers a BBMD forwards to.
  bbmd_links?: number[];
}

// GET /api/devices/{id}/port-suggestion: null when no free port was found.
export interface PortSuggestion {
  bind_address: string | null;
  port: number | null;
}

export type ProcessStatus = "running" | "stopped" | "starting" | "stopping" | "crashed" | "reinitializing";

export type DccState = "enable" | "disable" | "disableInitiation";

// Time sync. device_time is the device's own local wall time as host-local ISO
// 8601 with its UTC offset ("2026-09-25T14:03:12-04:00"); read its first 19
// characters rather than parsing it, so a browser in another time zone still
// shows device time.
export interface DeviceClockState {
  clock_offset_seconds: number;
  clock_change_pending: boolean;
  device_time: string;
}

export interface DeviceProcessStatus extends DeviceClockState {
  device_id: number;
  process_status: ProcessStatus;
  process_pid: number | null;
  process_started_at: string | null;
  process_error: string | null;
  // The start failed because the UDP port was taken (a free one can be suggested).
  // Always sent by the server; optional so older fixtures still type-check.
  port_in_use?: boolean;
  // Live object edits: the running device's last failed apply,
  // and whether saved device settings wait for a restart. Always sent by
  // the server; optional so older fixtures still type-check.
  apply_error?: string | null;
  restart_pending?: boolean;
  bind_address: string | null;
  dcc_state: DccState;
  // DM-BR-B (backup and restore). last_restore_time is device-local ISO with
  // its offset, null when never restored.
  database_revision: number;
  last_restore_time: string | null;
}

// POST /api/engine/restart-all: which running devices came back,
// and each one that didn't with its reason.
export interface RestartAllResult {
  restarted: number[];
  failed: { device_id: number; error: string }[];
}

export type TrendMode = "interval" | "cov";

// Schedule/calendar -- see models.py's BacnetObject for the
// field-by-field docstrings this mirrors. "value" fields (schedule_default,
// TimeValue.value) share one plain-string convention across all three
// families: a float string for analog, "active"/"inactive" for binary, a
// 1-based state number for multi-state -- same as relinquish_default
// already uses elsewhere in this codebase.
export type ScheduleValueType = "analog" | "binary" | "multi-state";

export interface ScheduleTimeValue {
  time: string; // "HH:MM"
  value: string | null; // null = relinquish to schedule_default at this time
}

export type ScheduleDaySchedule = ScheduleTimeValue[];

// One date_list entry, or one schedule exception's embedded period -- shape
// depends on `kind`. "calendar-reference" is only valid as an exception
// period (points at a calendar object on the same device), never inside a
// calendar's own date_list -- see bacnet.py's validate_calendar_config /
// _validate_schedule_period for the real semantic gate (kept loose here,
// same division of labor as everywhere else in this API).
export type CalendarEntryKind = "date" | "date-range" | "week-and-day" | "calendar-reference";

export interface CalendarEntry {
  kind: CalendarEntryKind;
  year?: string | number | null; // "date" -- "*" or a year
  month?: string | number | null; // "date" | "week-and-day" -- "*" or 1-12
  day?: string | number | null; // "date" -- "*" or 1-31
  start?: string | null; // "date-range" -- "YYYY-MM-DD"
  end?: string | null; // "date-range" -- "YYYY-MM-DD"
  week_of_month?: string | number | null; // "week-and-day" -- "*", 1-5, or "last"
  day_of_week?: string | number | null; // "date" | "week-and-day" -- "*" or 1-7, Monday=1
  instance_number?: number | null; // "calendar-reference"
}

export interface ScheduleException {
  period: CalendarEntry;
  priority: number; // 1-16; among overlapping exceptions 1 wins (BACnet's highest)
  time_values: ScheduleTimeValue[];
}

export interface ScheduleWriteTarget {
  object_type: string;
  instance_number: number;
  property_identifier: string;
  // Another device; absent = this device.
  device_instance?: number | null;
}

export interface ScheduleWriteStatusEntry {
  last_sent_at: string | null;
  last_sent_value: unknown;
  error: string | null;
  suppressed_by_dcc: boolean;
}

// Value simulator settings.
// The server checks each kind's fields and fills in its defaults.
export interface Simulation {
  kind: string;
  [setting: string]: string | number | number[];
}

export interface BacnetObjectDTO {
  id: number;
  device_id: number;
  object_type: string;
  instance_number: number;
  name: string;
  description: string | null;
  units: string | null;
  bacnet_writable: boolean;
  out_of_service_default: boolean;
  relinquish_default: string | null;
  state_text: string[] | null;
  // Binary only; null = "On"/"Off".
  active_text?: string | null;
  inactive_text?: string | null;
  // Input only: the Present_Value at device start, in relinquish_default's
  // text form; null = 0 / inactive / state 1.
  start_value?: string | null;
  trended: boolean;
  // null = the row id and "<name>-Trend".
  trend_log_instance?: number | null;
  trend_log_name?: string | null;
  trend_mode: TrendMode;
  trend_interval_seconds: number | null;
  trend_sample_cap: number | null;
  // BUFFER_READY reporting. trend_last_notify_record is read-only.
  trend_reporting_enabled: boolean;
  trend_notification_threshold: number;
  trend_notification_class_instance: number | null;
  trend_event_enable: [boolean, boolean, boolean] | null;
  trend_notify_type: "alarm" | "event";
  trend_event_detection_enable: boolean;
  trend_last_notify_record: number;
  // Schedule only, all null for every other object_type.
  schedule_value_type: ScheduleValueType | null;
  schedule_default: string | null;
  weekly_schedule: ScheduleDaySchedule[] | null;
  exception_schedule: ScheduleException[] | null;
  schedule_effective_start: string | null;
  schedule_effective_end: string | null;
  schedule_write_targets: ScheduleWriteTarget[] | null;
  // Schedule only: runtime status of each remote write target, keyed
  // "device:type:instance:property" (written by the device process).
  schedule_write_status?: Record<string, ScheduleWriteStatusEntry> | null;
  // The connector whose read mapping feeds this object; null if none.
  fed_by_connector_id?: number | null;
  simulation?: Simulation | null;
  fed_by_connector_name?: string | null;
  // Present (default 14) on every object regardless of type -- the DB
  // column has no per-type default, only schedule objects act on it.
  schedule_priority: number;
  // Calendar only, null for every other object_type.
  date_list: CalendarEntry[] | null;
  // Alarming. Meaningful only for the 7 alarmable point types; null/false for
  // everything else.
  alarming_enabled: boolean;
  notification_class_instance: number | null;
  alarm_time_delay: number;
  alarm_time_delay_normal: number | null;
  alarm_notify_type: "alarm" | "event";
  alarm_event_enable: [boolean, boolean, boolean] | null;
  alarm_high_limit: number | null;
  alarm_low_limit: number | null;
  alarm_deadband: number;
  alarm_limit_enable: [boolean, boolean] | null;
  alarm_value: boolean | null;
  alarm_values: number[] | null;
  alarm_event_detection_enable: boolean;
  // Notification Class object only, null for every other object_type.
  notification_priority: [number, number, number] | null;
  notification_ack_required: [boolean, boolean, boolean] | null;
  notification_recipient_list: NotificationRecipientEntry[] | null;
}

export interface NotificationRecipientEntry {
  recipient:
    | { kind: "device"; instance_number: number }
    | { kind: "address"; address: string };
  valid_days: boolean[];
  from_time: string;
  to_time: string;
  transitions: [boolean, boolean, boolean];
  confirmed: boolean;
  process_identifier: number;
}

export type AuthType = "none" | "basic" | "bearer";
export type ResponseFormat = "json" | "plain-text";

export type ConnectorType = "http-poll" | "bacnet";

export interface ConnectorDTO {
  id: number;
  name: string;
  type: ConnectorType;
  enabled: boolean;
  url: string; // "" for a bacnet connector
  poll_interval_seconds: number;
  timeout_seconds: number;
  response_format: ResponseFormat;
  auth_type: AuthType;
  auth_username: string | null;
  // Names only: header values (API keys) aren't returned.
  header_names: string[];
  // What an http-poll connector needs before it polls: "password", "token",
  // "header <name>".
  missing_credentials: string[];
  // The saved ones among them that can't be decrypted (secret.key lost or
  // replaced); same labels. Optional so older fixtures still type-check.
  unreadable_credentials?: string[];
  // False skips the TLS certificate check; http-poll only.
  verify_tls: boolean;
  mapping_count: number;
  // bacnet connectors.
  device_id: number | null;
  remote_device_instance: number | null;
  remote_address: string | null;
  rewrite_interval_seconds: number;
  write_mapping_count: number;
}

export interface ConnectorMappingDTO {
  id: number;
  connector_id: number;
  object_id: number;
  json_path: string | null;
  scale: number;
  offset: number;
  value_map: Record<string, number> | null;
  remote_object_type: string | null;
  remote_object_instance: number | null;
  remote_property: string | null;
  remote_array_index: number | null;
  mode: string;
  // "proxy": mirrors the remote's value and priority array and
  // passes writes through.
  direction: "read" | "proxy";
}

export interface ConnectorWriteMappingDTO {
  id: number;
  connector_id: number;
  object_id: number;
  remote_object_type: string;
  remote_object_instance: number;
  remote_property: string;
  remote_array_index: number | null;
  priority: number;
}

// http-poll status is the parent's in-memory poller; bacnet status is written
// by the device's process and carries the extra fields. last_poll_at is a UTC
// ISO timestamp for both.
export interface ConnectorStatusDTO {
  last_poll_at: string | null;
  last_success: boolean | null;
  last_error: string | null;
  consecutive_failures: number;
  bound_address?: string | null;
  no_rpm?: boolean;
  suppressed_by_dcc?: boolean;
  read_errors?: Record<string, string | null>;
  write_status?: Record<string, { error: string | null; last_sent_at: string | null }>;
  last_forward?: Record<string, { at: string; priority: number; value: unknown; error: string | null }>;
}

// BACnet client tool.
export interface DiscoveredDevice {
  instance: number;
  address: string;
  vendor_id: number;
  max_apdu: number;
  segmentation: string;
}

export interface RemoteObject {
  object_type: string;
  instance: number;
  name: string | null;
}

// A found device's own Object_Name and Vendor_Name; null where it didn't answer.
export type RemoteIdentity = { name: string | null; vendor_name: string | null };

export interface RemoteTarget {
  instance: number;
  address?: string | null;
  timeout_seconds?: number;
}

// A clone read of a remote controller.
export interface CloneJob {
  job_id: number;
  status: "pending" | "running" | "done" | "error";
  progress: { phase: "object-list" | "objects"; done: number; total: number };
  error: string | null;
  created_at: string;
  instance: number | null;
  address: string | null;
}

export interface ClonePlanEntry {
  object_type: string;
  instance: number;
  name: string;
}

export type PlanSkipped = ClonePlanEntry & { reason: string; code: string };
export type PlanNote = ClonePlanEntry & { note: string };

export interface ClonePlanResult {
  plan: {
    device: {
      id: number;
      name: string;
      description: string | null;
      location: string | null;
      vendor_name: string | null;
      vendor_identifier: number | null;
    };
    vendor_name: string | null;
    object_count: number;
    counts: Record<string, number>;
    skipped: PlanSkipped[];
    notes: PlanNote[];
    skipped_types: Record<string, number>;
    // A refusal Create would give (a device id in use, a reserved name).
    problem: string | null;
  };
  // Nulls unless the server's default bind address is already served.
  suggested_bind: { bind_address: string | null; port: number | null };
}

// An EDE file's devices (POST /api/ede/read).
export interface EdeFileDevice {
  instance: number;
  name: string;
  object_count: number;
  // The BACpack device that already has this instance as its id.
  existing_device: { id: number; name: string } | null;
}

export interface EdeReadResult {
  project_name: string | null;
  version_of_layout: string | null;
  columns_not_used: string[];
  file_notes: string[];
  devices: EdeFileDevice[];
}

// One ticked device of the file, created with this id and name.
export interface EdeDeviceChoice {
  instance: number;
  id: number;
  name: string;
}

export interface EdeDevicePlan {
  instance: number;
  device: { id: number; name: string; description: string | null };
  object_count: number;
  counts: Record<string, number>;
  skipped: PlanSkipped[];
  notes: PlanNote[];
  skipped_types: Record<string, number>;
  // A refusal Create would give (an id or name in use, repeated, reserved).
  problem: string | null;
}

export interface EdePlanResult {
  plans: EdeDevicePlan[];
  vendor_name: string | null;
  plan_digest: string;
}

// What an object import says about an EDE file it read.
export interface EdeImportInfo {
  read_as: "EDE";
  project_name: string | null;
  version_of_layout: string | null;
  file_notes: string[];
  devices: { instance: number; name: string; object_count: number }[];
  // The file's device the plan is for; null until one is picked.
  device: number | null;
}

export interface RemotePointRequest extends RemoteTarget {
  object_type: string;
  object_instance: number;
  property?: string;
  array_index?: number | null;
}

// Connector mapping JSON path picker.
export interface ConnectorPreviewField {
  path: string;
  value: unknown;
  is_array: boolean;
}

export type ConnectorPreviewDTO =
  | { ok: true; response_format: "json"; fields: ConnectorPreviewField[] }
  | { ok: true; response_format: "plain-text"; raw_text: string }
  | { ok: false; error: string };

export interface NetworkSettingsDTO {
  bind_address: string | null;
  trend_retention_days: number | null;
  trend_soft_limit_mb: number | null;
}

export type LoginMode = "login" | "none" | null;

export interface AuthState {
  mode: LoginMode;
  signed_in: boolean;
  username: string | null;
}

export interface LoginSettings {
  mode: LoginMode;
  username: string | null;
}

// The installed version; listen_address is the address the HTTPS launcher
// bound, null for a hand-run dev server.
export interface VersionInfo {
  version: string;
  service: boolean;
  listen_address?: string | null;
}

export interface WebCertificate {
  fingerprint_sha256: string;
  subject: string;
  issuer: string;
  not_before: string;
  not_after: string;
  sans: string[];
  source: "self-signed" | "own";
  expired: boolean;
}

export interface WebServerSettings {
  listen_address: string;
  running: { address: string; port: number; fingerprint: string } | null;
  certificate: WebCertificate | null;
  certificate_problem: string | null;
  restart_pending: boolean;
}

export interface ApiKeyInfo {
  id: number;
  name: string;
  // The first characters of the key, to tell keys apart.
  prefix: string;
  created: string;
  last_used: string | null;
}

// The only response that carries the full key.
export interface NewApiKey extends ApiKeyInfo {
  key: string;
}

export interface TrafficEntry {
  timestamp: string;
  event_type: string;
  detail: string;
}

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export interface LogEntry {
  id: number;
  timestamp: string;
  level: LogLevel;
  logger_name: string;
  message: string;
  device_id: number | null;
}

export interface TrendStorageEntry {
  device_id: number;
  object_id: number;
  object_name: string;
  sample_count: number;
  bytes_estimate: number;
  sample_cap: number;
}

export interface TrendStorageDTO {
  total_samples: number;
  total_bytes_estimate: number;
  effective_limit_bytes: number;
  hard_cap_bytes: number;
  pruning: boolean;
  trends: TrendStorageEntry[];
}

// An object augmented with its parent device's name, for pickers/lists that
// span every device (e.g. the connector mapping picker) -- computed
// client-side (see fetchAllObjectsWithDevice in endpoints.ts) since no
// backend endpoint returns objects across all devices at once.
export interface ObjectWithDevice extends BacnetObjectDTO {
  deviceName: string;
}

// File-based config import: config_io.py's sync_config plan shapes.
export interface ObjectSyncSummary {
  created: string[];
  updated: string[];
  deleted: string[];
}

// A CSV import's dry run (POST .../objects/import?dry_run=true).
export type CsvImportPlan = {
  dry_run: true;
  mode: "merge" | "sync";
  created: string[];
  updated: string[];
  deleted: string[];
  // The file's columns that aren't BACpack's, left out of the import.
  skipped_columns: string[];
  // Absent when an EDE file with several devices still needs one picked.
  plan_digest?: string;
  // An EDE file only: the rows left out, what was changed, and the
  // unsupported types (for the request-support link).
  skipped?: PlanSkipped[];
  notes?: PlanNote[];
  skipped_types?: Record<string, number>;
  ede?: EdeImportInfo;
  // An EDE file with several devices: preview again with one picked.
  needs_device?: boolean;
};

export interface ConfigSyncPlan {
  dry_run: boolean;
  devices_created: number[];
  devices_updated: number[];
  devices_deleted: number[];
  objects: Record<string, ObjectSyncSummary>;
  // Connectors are created or updated by name, never deleted by an import.
  connectors_created: string[];
  connectors_updated: string[];
  // Per connector: the mappings the import creates, updates and removes.
  connector_mappings: Record<string, ObjectSyncSummary>;
  // Per connector: the password, token and header values it still needs.
  needs_credentials: Record<string, string[]>;
  settings_changed: boolean;
  // Dry run only: sent back on Apply, which refuses if the plan changed.
  plan_digest?: string;
}

// GET /api/capabilities -- see bacpack.capabilities.
export interface CapabilityService {
  name: string;
  kind: "confirmed" | "unconfirmed";
  executes: boolean;
  initiates: boolean;
}

export interface CapabilityBibb {
  name: string;
  title: string;
  area: string;
  supported: boolean;
  missing: string[];
  note: string | null;
}

export interface Capabilities {
  bacpack_version: string;
  protocol_revision: number;
  segmentation: string;
  max_apdu_length: number;
  data_links: string[];
  bibb_profile_source: string;
  bibb_summary: { met: number; total: number };
  services: CapabilityService[];
  object_types: string[];
  bibbs: CapabilityBibb[];
}

export interface LiveObjectDTO {
  object_id: number;
  object_type: string;
  instance: number;
  present_value: unknown;
  priority_array: unknown[] | null;
  controlling_priority: number | null;
  relinquish_default: unknown;
  status_flags: string[] | null;
  event_state: string | null;
  reliability: string | null;
  out_of_service: boolean | null;
}

export interface DeviceLiveDTO {
  read_at: string;
  objects: LiveObjectDTO[];
}

export interface ObjectLiveDTO extends LiveObjectDTO {
  read_at: string;
}

export interface TrendPointDTO {
  timestamp: string;
  value: number;
}

export interface TrendGapDTO {
  start: string;
  end: string;
}

export interface TrendRangeDTO {
  mode: TrendMode | null;
  interval_seconds: number | null;
  step: boolean;
  samples: TrendPointDTO[];
  carry_in: TrendPointDTO | null;
  gaps: TrendGapDTO[];
  thinned: boolean;
  count_in_range: number;
  earliest_sample: string | null;
  // The window the server used: for `last`, it picks "now".
  start: string;
  end: string;
}

// The sample building (GET /api/sample, POST /api/sample/load).
export interface SampleNote {
  text: string;
  // "<file>#<anchor>" under docs/guide, or null.
  guide: string | null;
}

export interface SampleDevicePlan {
  id: number;
  name: string;
  role: string;
  object_count: number;
  bind_address: string | null;
  port: number | null;
}

export interface SamplePlan {
  devices: SampleDevicePlan[];
  // Why it can't be loaded, or null.
  refusal: string | null;
  notes: SampleNote[];
}

export interface SampleLoadedDevice {
  id: number;
  name: string;
  bind_address: string | null;
  port: number | null;
  // Null when the load did not start the devices.
  started: boolean | null;
  error: string | null;
}

export interface SampleLoadResult {
  devices: SampleLoadedDevice[];
}
