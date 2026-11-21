import type { BacnetObjectDTO, TrendMode } from "../api/types";
import { isInput } from "./objectTypes";

/** Controlled-input form state for one object -- string-backed even for
 * numeric/list fields so inputs can hold transient/invalid text while being
 * edited. */
export interface ObjectFormState {
  name: string;
  description: string;
  units: string;
  stateText: string[]; // one label per state, multi-state only
  // False locks the object against external BACnet WriteProperty while
  // BACpack's own feed/manual edits keep working -- see BacnetWriteLocked
  // in bacnet.py. Meaningful for every Output/Value type.
  bacnetWritable: boolean;
  outOfServiceDefault: boolean;
  alarmingEnabled: boolean;
  // Plain text regardless of object type, same convention as the API/DB
  // column: a float string for analog, "active"/"inactive" for binary, a
  // 1-based state number for multi-state. Only meaningful for object types
  // with a priority array (see _COMMANDABLE_TYPES in bacnet.py) -- the API
  // silently ignores it for everyone else, same as units for non-analog.
  relinquishDefault: string;
  activeText: string; // binary only, "" = On
  inactiveText: string; // binary only, "" = Off
  startValue: string; // inputs only, relinquishDefault's text form, "" = null
  trended: boolean;
  trendLogInstance: string; // numeric string, "" = the row id
  trendLogName: string; // "" = "<name>-Trend"
  trendMode: TrendMode;
  trendIntervalSeconds: string; // numeric string, "" = null
  trendSampleCap: string; // numeric string, "" = null -- 0 or unset both mean disabled
  // BUFFER_READY reporting -- only sent as on while trended is on.
  trendReportingEnabled: boolean;
  trendNotificationClassInstance: string; // numeric string, "" = null
  trendNotificationThreshold: string; // numeric string; "" = 0, but the input is required while reporting is on
  trendEventDetectionEnable: boolean;
  trendNotifyType: "alarm" | "event";
  // null = never set: the device serves all three transitions on, and an
  // untouched form must not turn it into an explicit list.
  trendEventEnable: [boolean, boolean, boolean] | null;
}

export function toFormState(obj: BacnetObjectDTO): ObjectFormState {
  return {
    name: obj.name,
    description: obj.description ?? "",
    units: obj.units ?? "",
    stateText: obj.state_text ?? [],
    bacnetWritable: obj.bacnet_writable,
    outOfServiceDefault: obj.out_of_service_default,
    alarmingEnabled: obj.alarming_enabled,
    relinquishDefault: obj.relinquish_default ?? "",
    activeText: obj.active_text ?? "",
    inactiveText: obj.inactive_text ?? "",
    startValue: obj.start_value ?? "",
    trended: obj.trended,
    trendLogInstance: obj.trend_log_instance != null ? String(obj.trend_log_instance) : "",
    trendLogName: obj.trend_log_name ?? "",
    trendMode: obj.trend_mode,
    trendIntervalSeconds: obj.trend_interval_seconds != null ? String(obj.trend_interval_seconds) : "",
    trendSampleCap: obj.trend_sample_cap != null ? String(obj.trend_sample_cap) : "",
    trendReportingEnabled: obj.trend_reporting_enabled ?? false,
    trendNotificationClassInstance:
      obj.trend_notification_class_instance != null ? String(obj.trend_notification_class_instance) : "",
    trendNotificationThreshold: String(obj.trend_notification_threshold ?? 10),
    trendEventDetectionEnable: obj.trend_event_detection_enable ?? true,
    trendNotifyType: obj.trend_notify_type ?? "event",
    trendEventEnable: obj.trend_event_enable ?? null,
  };
}

export function isMultiState(objectType: string): boolean {
  return objectType.includes("multi-state");
}

/** The BUFFER_READY reporting part of a save. Reporting needs
 * trended (the API refuses one without the other), so unchecking Trended
 * turns reporting off too. */
function reportingPatch(form: ObjectFormState): Record<string, unknown> {
  return {
    trend_reporting_enabled: form.trended && form.trendReportingEnabled,
    trend_notification_class_instance: form.trendNotificationClassInstance
      ? Number(form.trendNotificationClassInstance)
      : null,
    trend_notification_threshold: Number(form.trendNotificationThreshold || 0),
    trend_event_detection_enable: form.trendEventDetectionEnable,
    trend_notify_type: form.trendNotifyType,
    // Never set = all on, so only a real change is sent.
    trend_event_enable: form.trendEventEnable ?? [true, true, true],
  };
}

/** The keys of `patch` whose value differs from `base`'s, compared as JSON
 * so lists compare by content. A tab sends only these: a value a BACnet
 * client wrote while the tab was open isn't put back (the rule
 * the Alarming tab has used since a038f95). */
export function changedFields(
  patch: Record<string, unknown>,
  base: Record<string, unknown>
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(patch).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(base[key]))
  );
}

function fullPatch(form: ObjectFormState, objectType: string): Record<string, unknown> {
  const patch: Record<string, unknown> = {
    name: form.name,
    description: form.description || null,
    units: form.units || null,
    bacnet_writable: form.bacnetWritable,
    out_of_service_default: form.outOfServiceDefault,
    alarming_enabled: form.alarmingEnabled,
    relinquish_default: form.relinquishDefault || null,
    trended: form.trended,
    trend_mode: form.trendMode,
    trend_interval_seconds: form.trendIntervalSeconds ? Number(form.trendIntervalSeconds) : null,
    trend_sample_cap: form.trendSampleCap ? Number(form.trendSampleCap) : null,
    trend_log_instance: form.trendLogInstance ? Number(form.trendLogInstance) : null,
    trend_log_name: form.trendLogName.trim() || null,
    ...reportingPatch(form),
  };
  if (isMultiState(objectType)) {
    patch.state_text = form.stateText;
  }
  if (objectType.startsWith("binary-")) {
    patch.active_text = form.activeText.trim() || null;
    patch.inactive_text = form.inactiveText.trim() || null;
  }
  if (isInput(objectType)) {
    patch.start_value = form.startValue || null;
  }
  return patch;
}

/** Builds the PUT body for a single-object save. state_text is only
 * included for multi-state objects; omitting a key (not sending it as
 * null) leaves it untouched, since the JSON API's ObjectUpdate uses
 * Pydantic's exclude_unset.
 *
 * `saved` (the form state the tab loaded from): when given, only the
 * fields that differ from it are sent. The tabs don't poll, so resending
 * an unchanged field would put back the old value over anything a BACnet
 * client wrote meanwhile (State_Text, Notification_Threshold, ...). */
export function formStateToPatch(
  form: ObjectFormState,
  objectType: string,
  saved?: ObjectFormState
): Record<string, unknown> {
  const patch = fullPatch(form, objectType);
  return saved ? changedFields(patch, fullPatch(saved, objectType)) : patch;
}

/** One field in the bulk-edit panel: `enabled` gates whether it's applied
 * at all to the selected objects -- unchecked fields are omitted from the
 * PATCH body entirely (see buildBulkPatch), not sent as their default
 * value, so every unchecked field is left alone on every selected object. */
export interface BulkField<T> {
  enabled: boolean;
  value: T;
}

export interface BulkPatchForm {
  description: BulkField<string>;
  units: BulkField<string>;
  bacnetWritable: BulkField<boolean>;
  outOfServiceDefault: BulkField<boolean>;
  alarmingEnabled: BulkField<boolean>;
  trended: BulkField<boolean>;
  trendMode: BulkField<TrendMode>;
  trendIntervalSeconds: BulkField<string>;
  trendSampleCap: BulkField<string>;
}

export function emptyBulkPatchForm(): BulkPatchForm {
  return {
    description: { enabled: false, value: "" },
    units: { enabled: false, value: "" },
    bacnetWritable: { enabled: false, value: true },
    outOfServiceDefault: { enabled: false, value: false },
    alarmingEnabled: { enabled: false, value: false },
    trended: { enabled: false, value: false },
    trendMode: { enabled: false, value: "interval" },
    trendIntervalSeconds: { enabled: false, value: "" },
    trendSampleCap: { enabled: false, value: "" },
  };
}

export function buildBulkPatch(form: BulkPatchForm): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  if (form.description.enabled) patch.description = form.description.value || null;
  if (form.units.enabled) patch.units = form.units.value || null;
  if (form.bacnetWritable.enabled) patch.bacnet_writable = form.bacnetWritable.value;
  if (form.outOfServiceDefault.enabled) patch.out_of_service_default = form.outOfServiceDefault.value;
  if (form.alarmingEnabled.enabled) patch.alarming_enabled = form.alarmingEnabled.value;
  if (form.trended.enabled) {
    patch.trended = form.trended.value;
    // Reporting needs trended, so turning trended off turns reporting off
    // too -- same as reportingPatch does for a single-object save.
    if (!form.trended.value) patch.trend_reporting_enabled = false;
  }
  if (form.trendMode.enabled) patch.trend_mode = form.trendMode.value;
  if (form.trendIntervalSeconds.enabled) {
    patch.trend_interval_seconds = form.trendIntervalSeconds.value
      ? Number(form.trendIntervalSeconds.value)
      : null;
  }
  if (form.trendSampleCap.enabled) {
    patch.trend_sample_cap = form.trendSampleCap.value ? Number(form.trendSampleCap.value) : null;
  }
  return patch;
}
