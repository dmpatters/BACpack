import { useState, type ReactElement } from "react";
import type { TrendMode } from "../api/types";
import { buildBulkPatch, emptyBulkPatchForm, type BulkPatchForm } from "../utils/objectForm";
import Button from "./Button";
import { ErrorBlock } from "./AsyncState";
import { errorMessage } from "./Banner";
import { CheckField, Field, FormActions } from "./Form";
import UnitCombobox from "./UnitCombobox";

interface Props {
  selectedCount: number;
  // The object types in the selection, to say what can't apply to some of them.
  selectedTypes?: string[];
  onApply: (patch: Record<string, unknown>) => void;
  onCancel: () => void;
  applying: boolean;
  // The last apply's failure, shown under Apply.
  applyError?: unknown;
}

// "Set BACnet writable", "Set units": an acronym keeps its capitals.
function setLabel(field: string): string {
  return /^[A-Z]{2}/.test(field) ? `Set ${field}` : `Set ${field[0].toLowerCase()}${field.slice(1)}`;
}

// One field: its enable box beside its value control, each named on its own.
function BulkRow({
  field,
  enabled,
  onEnable,
  children,
}: {
  field: string;
  enabled: boolean;
  onEnable: (enabled: boolean) => void;
  children: ReactElement;
}) {
  return (
    <div className="bp-bulk-row">
      <CheckField label={setLabel(field)} checked={enabled} onChange={(e) => onEnable(e.target.checked)} />
      <Field label={field}>{children}</Field>
    </div>
  );
}

// A yes/no field: the value is a checkbox of its own beside the enable box.
function BulkCheckRow({
  field,
  help,
  setting,
  onChange,
}: {
  field: string;
  help?: string;
  setting: { enabled: boolean; value: boolean };
  onChange: (patch: Partial<{ enabled: boolean; value: boolean }>) => void;
}) {
  return (
    <div className="bp-bulk-row">
      <CheckField label={setLabel(field)} checked={setting.enabled} onChange={(e) => onChange({ enabled: e.target.checked })} />
      <CheckField
        label={field}
        help={help}
        disabled={!setting.enabled}
        checked={setting.value}
        onChange={(e) => onChange({ value: e.target.checked })}
      />
    </div>
  );
}

/** Bulk-edit for the object grid -- see "Bulk edit" in the
 * plan doc's SPA design section. Each field has its own enable checkbox;
 * only checked fields are sent, so unchecked fields are left untouched on
 * every selected object rather than being reset to a default. Deliberately
 * excludes name/object_type/instance_number (per-object identity) and
 * state_text (per-object semantics) -- same fields the design doc rules
 * out. */
export default function BulkEditPanel({ selectedCount, selectedTypes = [], onApply, onCancel, applying, applyError }: Props) {
  const [form, setForm] = useState<BulkPatchForm>(emptyBulkPatchForm());

  function setField<K extends keyof BulkPatchForm>(key: K, patch: Partial<BulkPatchForm[K]>) {
    setForm((f) => ({ ...f, [key]: { ...f[key], ...patch } }));
  }

  const objects = selectedCount === 1 ? "object" : "objects";
  // The server refuses the whole batch for it, so say so before Apply.
  const trendsNotificationClass =
    form.trended.enabled && form.trended.value && selectedTypes.includes("notification-class");
  return (
    <div className="bp-form">
      <BulkRow field="Description" enabled={form.description.enabled} onEnable={(enabled) => setField("description", { enabled })}>
        <input
          type="text"
          disabled={!form.description.enabled}
          value={form.description.value}
          onChange={(e) => setField("description", { value: e.target.value })}
        />
      </BulkRow>

      <BulkRow field="Units" enabled={form.units.enabled} onEnable={(enabled) => setField("units", { enabled })}>
        <UnitCombobox
          disabled={!form.units.enabled}
          value={form.units.value}
          onChange={(value) => setField("units", { value })}
        />
      </BulkRow>

      <BulkCheckRow
        field="BACnet writable"
        help="Points: Present_Value only. Schedules and calendars: all configuration."
        setting={form.bacnetWritable}
        onChange={(p) => setField("bacnetWritable", p)}
      />
      <BulkCheckRow
        field="Out of service by default"
        setting={form.outOfServiceDefault}
        onChange={(p) => setField("outOfServiceDefault", p)}
      />
      <BulkCheckRow field="Alarming enabled" setting={form.alarmingEnabled} onChange={(p) => setField("alarmingEnabled", p)} />
      <BulkCheckRow
        field="Trended"
        help={trendsNotificationClass ? "A Notification Class in the selection can't be trended. Deselect it." : undefined}
        setting={form.trended}
        onChange={(p) => setField("trended", p)}
      />

      <BulkRow field="Trend mode" enabled={form.trendMode.enabled} onEnable={(enabled) => setField("trendMode", { enabled })}>
        <select
          disabled={!form.trendMode.enabled}
          value={form.trendMode.value}
          onChange={(e) => setField("trendMode", { value: e.target.value as TrendMode })}
        >
          <option value="interval">Interval</option>
          <option value="cov">COV</option>
        </select>
      </BulkRow>

      <BulkRow
        field="Interval (s)"
        enabled={form.trendIntervalSeconds.enabled}
        onEnable={(enabled) => setField("trendIntervalSeconds", { enabled })}
      >
        <input
          type="number"
          min={1}
          disabled={!form.trendIntervalSeconds.enabled}
          value={form.trendIntervalSeconds.value}
          onChange={(e) => setField("trendIntervalSeconds", { value: e.target.value })}
        />
      </BulkRow>

      <BulkRow field="Sample cap" enabled={form.trendSampleCap.enabled} onEnable={(enabled) => setField("trendSampleCap", { enabled })}>
        <input
          type="number"
          min={0}
          disabled={!form.trendSampleCap.enabled}
          value={form.trendSampleCap.value}
          onChange={(e) => setField("trendSampleCap", { value: e.target.value })}
        />
      </BulkRow>

      <FormActions>
        <Button
          variant="primary"
          type="button"
          disabled={applying || selectedCount === 0 || trendsNotificationClass}
          onClick={() => onApply(buildBulkPatch(form))}
        >
          Apply to {selectedCount} {objects}
        </Button>
        <Button variant="ghost" type="button" onClick={onCancel}>
          Cancel
        </Button>
      </FormActions>
      {applyError != null && <ErrorBlock lead="Couldn't apply the edit:">{errorMessage(applyError)}</ErrorBlock>}
    </div>
  );
}
