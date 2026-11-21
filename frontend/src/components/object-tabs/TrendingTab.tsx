import { useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { objectsApi, trendApi, trendStorageApi } from "../../api/endpoints";
import type { BacnetObjectDTO, TrendMode } from "../../api/types";
import TrendChart, { MIN_CHART_WIDTH, useElementWidth } from "../TrendChart";
import { formStateToPatch, toFormState, type ObjectFormState } from "../../utils/objectForm";
import {
  BYTES_PER_TREND_SAMPLE,
  DEFAULT_TREND_INTERVAL_SECONDS,
  LIVE_POLL_MS,
  MAX_TREND_SAMPLE_CAP,
} from "../../constants";
import Button from "../Button";
import Callout from "../Callout";
import { errorMessage } from "../Banner";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid, Segmented } from "../Form";
import { ActionStatus, ErrorBlock, Loading } from "../AsyncState";
import { PRESETS, maxPointsFor, seconds, type PresetKey } from "../../utils/trendChart";
import { valueFamily } from "../../utils/objectTypes";
import { unitShort } from "../../utils/units";
import { plural } from "../../utils/text";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import IsoEcho from "../IsoEcho";
import { TransitionChecks, transitionsOrDefault } from "../TransitionChecks";

function trendLogInstanceProblem(value: string): string | null {
  if (value === "") return null;
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 && n <= 4194302 ? null : "An instance number is 0 to 4194302.";
}

function formatMb(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(2);
}

export default function TrendingTab({
  deviceId,
  obj,
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
}) {
  const queryClient = useQueryClient();
  const objectId = obj.id;
  const { form, setForm, baseline, setBaseline } = useDraft(obj, toFormState);

  const instanceProblem = trendLogInstanceProblem(form.trendLogInstance);
  const dirty = differs(form, baseline);
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  const save = useMutation({
    mutationFn: (sent: ObjectFormState) =>
      objectsApi.update(deviceId, obj.id, formStateToPatch(sent, obj.object_type, toFormState(obj))),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });

  const chartRef = useRef<HTMLDivElement>(null);
  const maxPoints = maxPointsFor(Math.max(useElementWidth(chartRef), MIN_CHART_WIDTH));
  const [preset, setPreset] = useState<PresetKey | "custom">("1h");
  const [custom, setCustom] = useState({ start: "", end: "" });
  const customError = preset === "custom" ? customRangeError(custom) : null;
  const rangeReady = preset !== "custom" || (custom.start !== "" && custom.end !== "" && customError === null);
  const trend = useQuery({
    queryKey: ["trendRange", deviceId, objectId, preset, preset === "custom" ? custom : null, maxPoints],
    queryFn: async () => {
      // A preset asks for its length and the server picks "now", so a wrong
      // browser clock can't hide the newest samples; the chart draws against
      // the window the server says it used.
      const asked =
        preset === "custom" ? customRangeIso(custom) : { last: PRESETS.find((p) => p.key === preset)!.seconds };
      const range = await trendApi.range(deviceId, objectId, { ...asked, maxPoints });
      return { range, start: seconds(range.start), end: seconds(range.end) };
    },
    enabled: obj.trended && rangeReady,
    placeholderData: keepPreviousData,
    // A past custom range doesn't change.
    refetchInterval: preset === "custom" ? false : LIVE_POLL_MS,
  });
  // A failed load keeps the last chart on screen, marked "previous result":
  // TanStack drops the placeholder once the new range's query errors.
  const lastGood = useRef(trend.data);
  if (trend.data) lastGood.current = trend.data;
  const shown = trend.data ?? (trend.isError ? lastGood.current : undefined);
  const unit = unitShort(obj.units);

  const { data: storage } = useQuery({
    queryKey: ["trendStorage"],
    queryFn: trendStorageApi.get,
    enabled: obj.trended,
  });
  const usage = storage?.trends.find((t) => t.object_id === objectId);

  return (
    // Settings on the left, the chart on the right (stacked below 1100px).
    <div className="bp-tab-split">
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          if (form.trended && instanceProblem) return;
          save.mutate(form);
        }}
      >
        <CheckField
          label="Trended"
          checked={form.trended}
          onChange={(e) => setForm((f) => ({ ...f, trended: e.target.checked }))}
        />

        <FormGrid>
          <Field label="Trend mode">
            <select
              id="obj_trend_mode"
              value={form.trendMode}
              onChange={(e) => setForm((f) => ({ ...f, trendMode: e.target.value as TrendMode }))}
            >
              <option value="interval">Interval</option>
              <option value="cov">COV</option>
            </select>
          </Field>
          <Field label="Interval (s)" help="Interval mode only.">
            <input
              id="obj_trend_interval"
              type="number"
              min={1}
              value={form.trendIntervalSeconds}
              onChange={(e) => setForm((f) => ({ ...f, trendIntervalSeconds: e.target.value }))}
            />
          </Field>
          <Field label="Sample cap" help={`Up to ${MAX_TREND_SAMPLE_CAP}`}>
            <input
              id="obj_trend_sample_cap"
              type="number"
              min={1}
              max={MAX_TREND_SAMPLE_CAP}
              placeholder={String(MAX_TREND_SAMPLE_CAP)}
              value={form.trendSampleCap}
              onChange={(e) => setForm((f) => ({ ...f, trendSampleCap: e.target.value }))}
            />
          </Field>
        </FormGrid>

        {form.trended && (
          <FormGrid>
            <Field
              label="Trend Log instance"
              error={instanceProblem}
              help="Blank uses this object's number."
            >
              <input
                id="obj_trend_log_instance"
                type="number"
                min={0}
                placeholder={String(obj.id)}
                value={form.trendLogInstance}
                onChange={(e) => setForm((f) => ({ ...f, trendLogInstance: e.target.value }))}
              />
            </Field>
            <Field label="Trend Log name" help="Blank follows this object's name.">
              <input
                id="obj_trend_log_name"
                placeholder={`${form.name}-Trend`}
                value={form.trendLogName}
                onChange={(e) => setForm((f) => ({ ...f, trendLogName: e.target.value }))}
              />
            </Field>
          </FormGrid>
        )}

        {form.trended && (
          <Fieldset legend="BUFFER_READY reporting">
            <CheckField
              label="BUFFER_READY reporting enabled"
              checked={form.trendReportingEnabled}
              onChange={(e) => setForm((f) => ({ ...f, trendReportingEnabled: e.target.checked }))}
            />
            <FormGrid>
              <Field label="Trend log notification class instance number">
                <input
                  id="obj_trend_nc"
                  type="number"
                  min={0}
                  value={form.trendNotificationClassInstance}
                  onChange={(e) => setForm((f) => ({ ...f, trendNotificationClassInstance: e.target.value }))}
                />
              </Field>
              <Field label="Notification threshold (records)" help="0 never notifies.">
                <input
                  id="obj_trend_threshold"
                  type="number"
                  min={0}
                  // An emptied field would otherwise save as 0 (never notify);
                  // 0 has to be typed.
                  required={form.trendReportingEnabled}
                  value={form.trendNotificationThreshold}
                  onChange={(e) => setForm((f) => ({ ...f, trendNotificationThreshold: e.target.value }))}
                />
              </Field>
            </FormGrid>
            <CheckField
              label="Event detection enabled"
              checked={form.trendEventDetectionEnable}
              onChange={(e) => setForm((f) => ({ ...f, trendEventDetectionEnable: e.target.checked }))}
            />
            <Field label="Notify type">
              <select
                id="obj_trend_notify_type"
                value={form.trendNotifyType}
                onChange={(e) => setForm((f) => ({ ...f, trendNotifyType: e.target.value as "alarm" | "event" }))}
              >
                <option value="alarm">Alarm</option>
                <option value="event">Event</option>
              </select>
            </Field>
            <TransitionChecks
              legend="Event enable"
              help="BUFFER_READY uses To normal only."
              idPrefix="obj_trend_event_enable"
              value={transitionsOrDefault(form.trendEventEnable)}
              onChange={(next) => setForm((f) => ({ ...f, trendEventEnable: next }))}
            />
            <p className="bp-help">
              Last notified at record {obj.trend_last_notify_record ?? 0}.
            </p>
          </Fieldset>
        )}

        <FormActions>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            Save
          </Button>
        </FormActions>
        <ActionStatus
          action={save}
          failed="Couldn't save"
          done="Saved."
          hideDone={dirty}
        />
      </Form>

      {!obj.trended ? (
        <div>
          <Callout tone="danger">{obj.name} is not trended</Callout>
        </div>
      ) : (
        <div className="bp-stack">
          <div className="bp-chart-range">
            <Segmented
              label="Range"
              value={preset}
              options={[...PRESETS.map((p) => ({ value: p.key, label: p.label })), { value: "custom" as const, label: "Custom" }]}
              onChange={setPreset}
            />
            <a href={trendApi.exportCsvUrl(deviceId, objectId)}>Export CSV</a>
            {preset === "custom" && (
              <>
                <Field label="Start (UTC)" help={<IsoEcho value={custom.start} kind="datetime" zone="UTC" />}>
                  <input
                    type="datetime-local"
                    value={custom.start}
                    onChange={(e) => setCustom((c) => ({ ...c, start: e.target.value }))}
                  />
                </Field>
                <Field label="End (UTC)" error={customError} help={<IsoEcho value={custom.end} kind="datetime" zone="UTC" />}>
                  <input
                    type="datetime-local"
                    value={custom.end}
                    onChange={(e) => setCustom((c) => ({ ...c, end: e.target.value }))}
                  />
                </Field>
              </>
            )}
            <span className="bp-help">Times in UTC</span>
          </div>
          {trend.isError && <ErrorBlock lead="Couldn't load the trend">{errorMessage(trend.error)}</ErrorBlock>}
          <div className="bp-chart-stack" ref={chartRef}>
            {shown ? (
              <TrendChart
                range={shown.range}
                start={shown.start}
                end={shown.end}
                title={obj.name}
                sub={
                  trend.isError
                    ? "previous result"
                    : `${obj.object_type}:${obj.instance_number}${unit ? ` · ${unit}` : ""}`
                }
                unit={unit}
                levels={chartLevels(obj)}
                stale={trend.isPlaceholderData || trend.isError}
                format={(v) => String(Number(v.toFixed(2)))}
              />
            ) : (
              trend.isPending && rangeReady && <Loading what="trend" />
            )}
          </div>
          <div>
            <p className="bp-muted">
              {obj.trend_mode === "cov"
                ? "COV-driven"
                : obj.trend_interval_seconds
                  ? `Polled every ${obj.trend_interval_seconds}s`
                  : `Polled every ${DEFAULT_TREND_INTERVAL_SECONDS}s (default)`}{" "}
              history.
            </p>
            {usage && (
              <p className="bp-muted">
                Currently using ~{formatMb(usage.bytes_estimate)} MB ({plural(usage.sample_count, "sample")}).{" "}
                {projectedMaxLabel(obj)}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function projectedMaxLabel(obj: { trend_sample_cap: number | null }): string {
  const cap = obj.trend_sample_cap || MAX_TREND_SAMPLE_CAP;
  const which = obj.trend_sample_cap ? "sample cap" : "default cap";
  return `Projected maximum: ~${formatMb(cap * BYTES_PER_TREND_SAMPLE)} MB (${which} ${cap}).`;
}

// A datetime-local value ("2027-01-15T08:00", maybe with seconds) read as UTC.
function utcSeconds(value: string): number {
  return Date.parse(`${value.length === 16 ? `${value}:00` : value}Z`) / 1000;
}

function customRange(custom: { start: string; end: string }): { start: number; end: number } {
  return { start: utcSeconds(custom.start), end: utcSeconds(custom.end) };
}

function customRangeIso(custom: { start: string; end: string }): { start: string; end: string } {
  const { start, end } = customRange(custom);
  return { start: new Date(start * 1000).toISOString(), end: new Date(end * 1000).toISOString() };
}

function customRangeError(custom: { start: string; end: string }): string | null {
  if (!custom.start || !custom.end) return null;
  const { start, end } = customRange(custom);
  return end <= start ? "End is before start." : null;
}

// Step charts name their values; every state is listed, so the axis doesn't
// shift with the range.
function chartLevels(obj: BacnetObjectDTO): { value: number; label: string }[] | undefined {
  if (obj.object_type === "calendar") {
    return [
      { value: 0, label: "Not in calendar" },
      { value: 1, label: "In calendar" },
    ];
  }
  const family = obj.object_type === "schedule" ? obj.schedule_value_type : valueFamily(obj.object_type);
  if (family === "binary") {
    return [
      { value: 0, label: "Inactive" },
      { value: 1, label: "Active" },
    ];
  }
  // A multi-state schedule may have no state names: a numeric step axis then.
  if (family === "multi-state" && obj.state_text && obj.state_text.length > 0) {
    return obj.state_text.map((name, i) => ({ value: i + 1, label: name.trim() || `State ${i + 1}` }));
  }
  return undefined;
}
