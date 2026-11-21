import { useId, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO, Simulation } from "../../api/types";
import {
  KINDS,
  KIND_LABELS,
  defaultSimulation,
  describeSimulation,
  simFamily,
  simulationErrors,
  switchKind,
  toSimulation,
  toValues,
  type SimFamily,
  type SimValues,
} from "../../utils/simulation";
import { simulatePreview, type PreviewSeries } from "../../utils/simPreview";
import { unitShort } from "../../utils/units";
import type { ComboOption } from "../Combobox";
import SimulationFields from "../SimulationFields";
import PreviewChart from "../PreviewChart";
import Button from "../Button";
import Callout from "../Callout";
import StatusPill from "../StatusPill";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid, Segmented, useSubmitProblem } from "../Form";
import { ActionStatus, ErrorBlock } from "../AsyncState";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import { ageText, useObjectLive, type ObjectLive } from "../../utils/useLiveState";
import { formatLiveValue } from "../../utils/liveValues";
import { formatUtcTime } from "../../utils/time";
import { useNow } from "../../utils/useNow";
import { useTabShown } from "../Tabs";

interface FormState {
  on: boolean;
  kind: string;
  values: SimValues;
}

function fromObject(obj: BacnetObjectDTO, family: SimFamily): FormState {
  const sim = obj.simulation ?? defaultSimulation(family, KINDS[family][0]);
  // An imported simulator can be stored without its optional settings: show
  // their defaults rather than blank required fields.
  return { on: obj.simulation != null, kind: sim.kind, values: toValues({ ...defaultSimulation(family, sim.kind), ...sim }) };
}

// Value simulator. Saving applies live: the device's simulation runner
// re-reads the settings every second, so there's no "Restart serving" step
// here.
export default function SimulationTab({
  deviceId,
  obj,
  objects = [],
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
  objects?: BacnetObjectDTO[];
}) {
  const family = simFamily(obj.object_type) as SimFamily;
  const queryClient = useQueryClient();
  const { form, setForm, baseline, setBaseline } = useDraft(obj, (o) => fromObject(o, family));
  const fed = obj.fed_by_connector_id != null;

  const dirty = differs(form, baseline);
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  const save = useMutation({
    mutationFn: (sent: FormState) =>
      objectsApi.update(deviceId, obj.id, {
        simulation: sent.on ? toSimulation(family, sent.kind, sent.values) : null,
      }),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });

  const stateNames = obj.state_text ?? [];
  const unit = family === "analog" ? unitShort(obj.units) : "";
  // The preview checks the settings even while simulation is off, so a
  // field left wrong never reaches the chart.
  const fieldErrors = simulationErrors(family, form.kind, form.values);
  const errors = form.on ? fieldErrors : {};
  const valid = Object.keys(fieldErrors).length === 0;
  const problem = useSubmitProblem(Object.values(errors)[0] ?? null);
  const sim = toSimulation(family, form.kind, form.values);
  // While a field is wrong the preview keeps the last shape that was right.
  const lastValid = useRef<Simulation>(valid ? sim : defaultSimulation(family, form.kind));
  if (valid) lastValid.current = sim;
  const shown = valid ? sim : lastValid.current;
  const shownKey = JSON.stringify(shown);
  const tabShown = useTabShown();
  // A kept-mounted tab would otherwise go on showing the window it opened with.
  const minute = Math.floor(useNow(tabShown, 60_000) / 60_000);
  const series = useMemo(
    () => simulatePreview(family, shown, Date.now() / 1000, stateNames.length),
    // shownKey stands for shown: a new object with the same settings needs no redraw.
    [family, shownKey, stateNames.length, minute]
  );

  const sources: ComboOption[] = objects
    .filter((o) => o.id !== obj.id && simFamily(o.object_type) === "analog")
    .map((o) => ({ value: `${o.object_type}:${o.instance_number}`, label: `${o.object_type}:${o.instance_number} · ${o.name}`, group: "" }));
  const storedSource = form.values.source ?? "";
  if (storedSource && !sources.some((s) => s.value === storedSource)) {
    sources.unshift({ value: storedSource, label: `${storedSource} (not on this device)`, group: "" });
  }
  const sourceLabel = sources.find((s) => s.value === storedSource)?.label ?? "";
  const follow = form.kind === "follow";
  const sourceObj = follow
    ? objects.find((o) => o.id !== obj.id && `${o.object_type}:${o.instance_number}` === storedSource)
    : undefined;
  const sourceLive = useObjectLive(deviceId, sourceObj?.id ?? null, follow && tabShown);
  const now = useNow(sourceLive.status === "stale" && tabShown);
  const followLine = sourceObj ? describeFollowed(sourceObj, sourceLive, now) : "";

  const changeKind = (kind: string) =>
    setForm((f) => ({ ...f, kind, values: switchKind(family, f.kind, kind, f.values) }));
  const off = !form.on || fed;
  const decimals = Number.isInteger(Number(form.values.decimals)) ? Number(form.values.decimals) : 2;

  return (
    <>
      {fed && (
        <Callout tone="danger">
          Fed by connector {obj.fed_by_connector_name}. Remove that mapping to simulate
          it.
        </Callout>
      )}
      <div className={form.on ? "bp-sim" : "bp-sim bp-sim--off"}>
        <Form
          className="bp-sim__form"
          onSubmit={(e) => {
            e.preventDefault();
            if (problem.canSubmit()) save.mutate(form);
          }}
        >
          <CheckField
            label="Simulate this object"
            help="At priority 10."
            checked={form.on}
            disabled={fed}
            onChange={(e) => setForm((f) => ({ ...f, on: e.target.checked }))}
          />
          <Fieldset legend="Simulation" disabled={off}>
            {KINDS[family].length > 3 ? (
              <FormGrid>
                <Field label="Kind">
                  <select id="sim_kind" value={form.kind} onChange={(e) => changeKind(e.target.value)}>
                    {KINDS[family].map((kind) => (
                      <option key={kind} value={kind}>
                        {KIND_LABELS[kind]}
                      </option>
                    ))}
                  </select>
                </Field>
              </FormGrid>
            ) : (
              <Segmented
                label="Kind"
                value={form.kind}
                options={KINDS[family].map((kind) => ({ value: kind, label: KIND_LABELS[kind] }))}
                onChange={changeKind}
                disabled={off}
              />
            )}
          </Fieldset>
          <SimulationFields
            family={family}
            kind={form.kind}
            values={form.values}
            onChange={(values: SimValues) => setForm((f) => ({ ...f, values }))}
            disabled={off}
            idPrefix="sim"
            unit={unit}
            errors={errors}
            sources={sources}
            stateNames={family === "multi-state" ? stateNames : undefined}
          />
          <FormActions>
            <Button variant="primary" type="submit" disabled={save.isPending || fed}>
              Save
            </Button>
            <Button
              variant="ghost"
              type="button"
              disabled={off}
              onClick={() => setForm((f) => ({ ...f, values: toValues(defaultSimulation(family, f.kind)) }))}
            >
              Reset to defaults
            </Button>
          </FormActions>
          {problem.shown && <ErrorBlock lead="Can't save:">{problem.shown}</ErrorBlock>}
          <ActionStatus
            action={save}
            failed="Couldn't save"
            done={baseline.on ? "Saved." : "Saved. Simulation off."}
            hideDone={dirty}
          />
        </Form>
        <div className="bp-sim__preview">
          <SimulationPreview
            family={family}
            sim={shown}
            series={series}
            stale={form.on && !valid}
            unit={unit}
            stateNames={stateNames}
            decimals={decimals}
            sourceLabel={sourceLabel}
            followLine={followLine}
          />
        </div>
      </div>
    </>
  );
}

// docs/design-system/components/SimulationEditor: never mistaken for logged
// data ("not live"); random kinds say they are one example run.
function SimulationPreview({
  family,
  sim,
  series,
  stale,
  unit,
  stateNames,
  decimals,
  sourceLabel,
  followLine,
}: {
  family: SimFamily;
  sim: Simulation;
  series: PreviewSeries | null;
  stale: boolean;
  unit: string;
  stateNames: string[];
  decimals: number;
  sourceLabel: string;
  followLine: string;
}) {
  const titleId = useId();
  const follow = sim.kind === "follow";
  const minutesShown = series ? Math.round((series.end - series.start) / 60) : 0;
  const described = describeSimulation(family, sim);
  const levels =
    family === "binary"
      ? [
          { value: 0, label: "Inactive" },
          { value: 1, label: "Active" },
        ]
      : family === "multi-state" && series
        ? [...new Set(series.points.map((p) => p.v))]
            .sort((a, b) => a - b)
            .map((v) => ({ value: v, label: stateNames[v - 1]?.trim() || `State ${v}` }))
        : undefined;

  return (
    <section className={stale ? "bp-chart bp-chart--stale" : "bp-chart"} aria-labelledby={titleId}>
      <div className="bp-chart__head">
        <h3 className="bp-chart__title" id={titleId}>
          Preview
          <span className="bp-chart__sub">{follow ? "right now" : `next ${minutesShown} min${unit ? ` · ${unit}` : ""}`}</span>
        </h3>
        <StatusPill plain>not live</StatusPill>
      </div>
      <div className="bp-chart__plot">
        {follow ? (
          <>
            <p className="bp-help">
              {sourceLabel
                ? `Follows ${sourceLabel}.`
                : "Choose a source."}
            </p>
            {followLine && <p className="bp-help">{followLine}</p>}
          </>
        ) : series ? (
          <div className="bp-chart__inner">
            <PreviewChart
              series={series}
              levels={levels}
              unit={unit || undefined}
              format={(v) => v.toFixed(decimals)}
              label={`Preview of ${KIND_LABELS[sim.kind]}${described ? `, ${described}` : ""}, for the next ${minutesShown} minutes`}
            />
          </div>
        ) : (
          <p className="bp-help">None of the checked states fits this object.</p>
        )}
      </div>
      <div className="bp-chart__foot">
        <span>
          {KIND_LABELS[sim.kind]}
          {described ? ` · ${described}` : ""}
        </span>
        <span>
          {follow ? "Writes nothing while the source is missing" : series?.random ? "One example run · times in UTC" : "Times in UTC"}
        </span>
      </div>
    </section>
  );
}

// The followed object's value as its device serves it now.
function describeFollowed(source: BacnetObjectDTO, live: ObjectLive, now: number): string {
  if (live.status === "loading") return "";
  if (live.status === "stopped") return `${source.name}: no live value: device not running`;
  if (live.status === "missing") return `${source.name} isn't live yet`;
  if (!live.data) return `${source.name}: no live value right now`;
  const unit = unitShort(source.units);
  const value = `${source.name} now reads ${formatLiveValue(live.data.present_value, source)}${unit ? ` ${unit}` : ""}`;
  return live.status === "stale"
    ? `${value} · ${ageText(live.data.read_at, now)} (stale)`
    : `${value} · updated ${formatUtcTime(live.data.read_at)}`;
}
