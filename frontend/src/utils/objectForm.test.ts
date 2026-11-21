import { describe, expect, it } from "vitest";
import {
  buildBulkPatch,
  changedFields,
  emptyBulkPatchForm,
  formStateToPatch,
  isMultiState,
  toFormState,
} from "./objectForm";
import type { BacnetObjectDTO } from "../api/types";

const baseObject: BacnetObjectDTO = {
  id: 1,
  device_id: 599001,
  object_type: "analog-value",
  instance_number: 1,
  name: "AV-1",
  description: null,
  units: "percent",
  bacnet_writable: true,
  out_of_service_default: false,
  relinquish_default: null,
  state_text: null,
  trended: false,
  trend_mode: "interval",
  trend_interval_seconds: null,
  trend_sample_cap: null,
  trend_reporting_enabled: false,
  trend_notification_threshold: 10,
  trend_notification_class_instance: null,
  trend_event_enable: null,
  trend_notify_type: "event",
  trend_event_detection_enable: true,
  trend_last_notify_record: 0,
  schedule_value_type: null,
  schedule_default: null,
  weekly_schedule: null,
  exception_schedule: null,
  schedule_effective_start: null,
  schedule_effective_end: null,
  schedule_write_targets: null,
  schedule_priority: 14,
  date_list: null,
  alarming_enabled: false,
  notification_class_instance: null,
  alarm_time_delay: 0,
  alarm_time_delay_normal: null,
  alarm_notify_type: "alarm",
  alarm_event_enable: null,
  alarm_high_limit: null,
  alarm_low_limit: null,
  alarm_deadband: 0,
  alarm_event_detection_enable: true,
  alarm_limit_enable: null,
  alarm_value: null,
  alarm_values: null,
  notification_priority: null,
  notification_ack_required: null,
  notification_recipient_list: null,
};

describe("formStateToPatch", () => {
  it("omits state_text for non-multi-state objects", () => {
    const form = toFormState(baseObject);
    const patch = formStateToPatch(form, baseObject.object_type);
    expect("state_text" in patch).toBe(false);
  });

  it("includes state_text for multi-state objects", () => {
    const msv = { ...baseObject, object_type: "multi-state-value" };
    const form = { ...toFormState(msv), stateText: ["Off", "Low", "High"] };
    const patch = formStateToPatch(form, msv.object_type);
    expect(patch.state_text).toEqual(["Off", "Low", "High"]);
  });

  it("sends null instead of empty string for blank optional fields", () => {
    const form = toFormState(baseObject);
    const patch = formStateToPatch(form, baseObject.object_type);
    expect(patch.description).toBeNull();
    expect(patch.trend_interval_seconds).toBeNull();
  });
});

describe("trend_sample_cap", () => {
  it("carries trend_sample_cap through toFormState/formStateToPatch", () => {
    const obj = { ...baseObject, trend_sample_cap: 200 };
    const form = toFormState(obj);
    expect(form.trendSampleCap).toBe("200");

    const patch = formStateToPatch(form, obj.object_type);
    expect(patch.trend_sample_cap).toBe(200);
  });

  it("formStateToPatch sends null for an empty trend_sample_cap", () => {
    const form = toFormState({ ...baseObject, trend_sample_cap: null });
    const patch = formStateToPatch(form, baseObject.object_type);
    expect(patch.trend_sample_cap).toBeNull();
  });

  it("buildBulkPatch includes trend_sample_cap only when enabled", () => {
    const form = emptyBulkPatchForm();
    form.trendSampleCap = { enabled: true, value: "50" };
    const patch = buildBulkPatch(form);
    expect(patch).toEqual({ trend_sample_cap: 50 });
  });

  it("buildBulkPatch turns reporting off along with trended", () => {
    const form = emptyBulkPatchForm();
    form.trended = { enabled: true, value: false };
    expect(buildBulkPatch(form)).toEqual({ trended: false, trend_reporting_enabled: false });
  });

  it("buildBulkPatch leaves reporting alone when turning trended on", () => {
    const form = emptyBulkPatchForm();
    form.trended = { enabled: true, value: true };
    expect(buildBulkPatch(form)).toEqual({ trended: true });
  });
});

describe("isMultiState", () => {
  it("matches all three multi-state object types", () => {
    expect(isMultiState("multi-state-input")).toBe(true);
    expect(isMultiState("multi-state-output")).toBe(true);
    expect(isMultiState("multi-state-value")).toBe(true);
    expect(isMultiState("analog-value")).toBe(false);
  });
});

describe("toFormState", () => {
  it("keeps State_Text as a list, so a label holding a comma stays one state", () => {
    const msv = { ...baseObject, object_type: "multi-state-value", state_text: ["Low, fan off", "High"] };
    expect(toFormState(msv).stateText).toEqual(["Low, fan off", "High"]);
  });
});

describe("alarmingEnabled", () => {
  it("toFormState includes alarmingEnabled", () => {
    const obj = { ...baseObject, alarming_enabled: true };
    expect(toFormState(obj).alarmingEnabled).toBe(true);
  });

  it("formStateToPatch includes alarming_enabled", () => {
    const form = toFormState({ ...baseObject, alarming_enabled: true });
    const patch = formStateToPatch(form, "analog-input");
    expect(patch.alarming_enabled).toBe(true);
  });

  it("buildBulkPatch omits alarming_enabled when not enabled", () => {
    const patch = buildBulkPatch(emptyBulkPatchForm());
    expect(patch).not.toHaveProperty("alarming_enabled");
  });

  it("buildBulkPatch includes alarming_enabled when enabled", () => {
    const form = emptyBulkPatchForm();
    form.alarmingEnabled = { enabled: true, value: true };
    const patch = buildBulkPatch(form);
    expect(patch.alarming_enabled).toBe(true);
  });
});

describe("buildBulkPatch", () => {
  it("omits every field left unchecked", () => {
    const patch = buildBulkPatch(emptyBulkPatchForm());
    expect(patch).toEqual({});
  });

  it("includes only the checked fields, with correct API field names", () => {
    const form = emptyBulkPatchForm();
    form.description = { enabled: true, value: "batch-set" };
    form.bacnetWritable = { enabled: true, value: false };
    form.trendIntervalSeconds = { enabled: true, value: "60" };

    const patch = buildBulkPatch(form);
    expect(patch).toEqual({
      description: "batch-set",
      bacnet_writable: false,
      trend_interval_seconds: 60,
    });
  });

  it("sends null for a checked-but-blank optional field, not an empty string", () => {
    const form = emptyBulkPatchForm();
    form.units = { enabled: true, value: "" };
    form.trendIntervalSeconds = { enabled: true, value: "" };

    const patch = buildBulkPatch(form);
    expect(patch).toEqual({ units: null, trend_interval_seconds: null });
  });
});

describe("trend reporting fields", () => {
  const base = {
    id: 5, device_id: 1, object_type: "analog-value", instance_number: 1, name: "AV-1",
    description: null, units: "percent", bacnet_writable: true, out_of_service_default: false,
    relinquish_default: null, state_text: null, trended: true, trend_mode: "interval",
    trend_interval_seconds: 60, trend_sample_cap: null, alarming_enabled: false,
    trend_reporting_enabled: true, trend_notification_threshold: 3,
    trend_notification_class_instance: 1, trend_event_enable: null, trend_notify_type: "event",
    trend_event_detection_enable: true, trend_last_notify_record: 12,
  } as unknown as BacnetObjectDTO;

  it("round-trips through the form", () => {
    const patch = formStateToPatch(toFormState(base), "analog-value");
    expect(patch).toMatchObject({
      trend_reporting_enabled: true,
      trend_notification_threshold: 3,
      trend_notification_class_instance: 1,
      trend_event_detection_enable: true,
    });
    expect(patch).not.toHaveProperty("trend_last_notify_record");
  });

  it("sends reporting off whenever Trended is off", () => {
    const form = { ...toFormState(base), trended: false };
    expect(formStateToPatch(form, "analog-value").trend_reporting_enabled).toBe(false);
  });

  it("defaults the fields for a DTO that predates them", () => {
    const old = { ...base } as Record<string, unknown>;
    for (const key of Object.keys(old)) if (key.startsWith("trend_notif") || key.startsWith("trend_rep")) delete old[key];
    const form = toFormState(old as unknown as BacnetObjectDTO);
    expect(form.trendReportingEnabled).toBe(false);
    expect(form.trendNotificationThreshold).toBe("10");
  });
});

describe("formStateToPatch with the saved state", () => {
  const saved = toFormState({
    ...baseObject,
    trended: true,
    trend_reporting_enabled: true,
    trend_notification_threshold: 5,
    trend_notification_class_instance: 1,
  });

  it("leaves unchanged reporting fields out", () => {
    const patch = formStateToPatch({ ...saved, name: "AV-renamed" }, "analog-value", saved);
    expect(patch.name).toBe("AV-renamed");
    expect(patch).not.toHaveProperty("trend_notification_threshold");
    expect(patch).not.toHaveProperty("trend_notification_class_instance");
    expect(patch).not.toHaveProperty("trend_reporting_enabled");
    expect(patch).not.toHaveProperty("trend_event_detection_enable");
  });

  it("includes a changed one", () => {
    const patch = formStateToPatch({ ...saved, trendNotificationThreshold: "8" }, "analog-value", saved);
    expect(patch.trend_notification_threshold).toBe(8);
  });

  it("sends reporting off when Trended is turned off", () => {
    const patch = formStateToPatch({ ...saved, trended: false }, "analog-value", saved);
    expect(patch.trend_reporting_enabled).toBe(false);
  });
});

describe("changedFields", () => {
  it("keeps only the keys whose value differs, comparing lists by content", () => {
    expect(
      changedFields(
        { name: "A", state_text: ["Off", "On"], description: null, trended: true },
        { name: "A", state_text: ["Off", "On"], description: "x", trended: true }
      )
    ).toEqual({ description: null });
  });
});

describe("formStateToPatch with the saved form (0.24.16)", () => {
  const msv = {
    ...baseObject,
    object_type: "multi-state-value",
    state_text: ["Off", "Low", "High"],
    relinquish_default: "1",
  };

  it("sends only the fields that changed", () => {
    const saved = toFormState(msv);
    const patch = formStateToPatch({ ...saved, description: "new" }, msv.object_type, saved);
    expect(patch).toEqual({ description: "new" });
  });

  it("sends state_text when it changed", () => {
    const saved = toFormState(msv);
    const patch = formStateToPatch({ ...saved, stateText: ["Off", "Low", "High", "Max"] }, msv.object_type, saved);
    expect(patch).toEqual({ state_text: ["Off", "Low", "High", "Max"] });
  });

  it("sends nothing when nothing changed", () => {
    const saved = toFormState(msv);
    expect(formStateToPatch(saved, msv.object_type, saved)).toEqual({});
  });

  it("turns reporting off along with trended", () => {
    const trendedObj = { ...baseObject, trended: true, trend_reporting_enabled: true };
    const saved = toFormState(trendedObj);
    const patch = formStateToPatch({ ...saved, trended: false }, trendedObj.object_type, saved);
    expect(patch).toEqual({ trended: false, trend_reporting_enabled: false });
  });
});

describe("binary text, start value and Trend Log identity", () => {
  it("sends active_text only when changed, a cleared one as null", () => {
    const bv: BacnetObjectDTO = { ...baseObject, object_type: "binary-value", active_text: "Open", inactive_text: null };
    const saved = toFormState(bv);
    expect(formStateToPatch({ ...saved, activeText: "Opened" }, bv.object_type, saved)).toEqual({ active_text: "Opened" });
    expect(formStateToPatch({ ...saved, activeText: "" }, bv.object_type, saved)).toEqual({ active_text: null });
    expect(formStateToPatch(saved, bv.object_type, saved)).toEqual({});
  });

  it("start_value is sent for an input and not for an output", () => {
    const ai: BacnetObjectDTO = { ...baseObject, object_type: "analog-input" };
    const saved = toFormState(ai);
    expect(formStateToPatch({ ...saved, startValue: "21.5" }, ai.object_type, saved)).toEqual({ start_value: "21.5" });
    expect(formStateToPatch({ ...saved, startValue: "21.5" }, "analog-output", saved)).not.toHaveProperty("start_value");
  });

  it("binary text is not sent for a non-binary object", () => {
    const saved = toFormState(baseObject);
    expect(formStateToPatch({ ...saved, activeText: "On" }, baseObject.object_type, saved)).not.toHaveProperty("active_text");
  });

  it("a cleared Trend Log name sends null, an instance sends a number", () => {
    const trended: BacnetObjectDTO = { ...baseObject, trended: true, trend_log_name: "Zone Temp Log", trend_log_instance: null };
    const saved = toFormState(trended);
    expect(formStateToPatch({ ...saved, trendLogName: "" }, trended.object_type, saved)).toEqual({ trend_log_name: null });
    expect(formStateToPatch({ ...saved, trendLogInstance: "77" }, trended.object_type, saved)).toEqual({ trend_log_instance: 77 });
  });
});
