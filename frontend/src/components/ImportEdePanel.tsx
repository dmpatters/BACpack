import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PlanChangedError, edeApi } from "../api/endpoints";
import type { EdeDeviceChoice, EdeDevicePlan, EdeReadResult } from "../api/types";
import { errorMessage } from "./Banner";
import Button from "./Button";
import Callout from "./Callout";
import { Field, Fieldset, FormActions, FormGrid } from "./Form";
import SidePanel from "./SidePanel";
import { ErrorBlock, Loading } from "./AsyncState";
import { TableFrame } from "./DataTable";
import PlanLists from "./PlanLists";

// Edits to an id or name re-plan once typing pauses.
const REPLAN_DELAY_MS = 300;
const MAX_INSTANCE = 4194302;

type Row = { instance: number; ticked: boolean; id: string; name: string };

function choicesOf(rows: Row[]): EdeDeviceChoice[] {
  return rows.filter((r) => r.ticked).map((r) => ({ instance: r.instance, id: Number(r.id), name: r.name.trim() }));
}

function validChoice(c: EdeDeviceChoice): boolean {
  return Number.isInteger(c.id) && c.id >= 0 && c.id <= MAX_INSTANCE && c.name !== "";
}

function useSettled<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

// An EDE file as new, stopped devices: read the file's devices, tick the ones
// to create, preview each, create them all in one go. The panel holds the
// files; the server keeps nothing between calls.
export default function ImportEdePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <SidePanel open={open} title="Import EDE" onClose={onClose}>
      <ImportEdeBody />
    </SidePanel>
  );
}

function ImportEdeBody() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [stateTexts, setStateTexts] = useState<File | null>(null);
  // Bumped whenever either file changes, so no plan of an older pair is used.
  const [filesVersion, setFilesVersion] = useState(0);
  const [rows, setRows] = useState<Row[]>([]);
  const [replanned, setReplanned] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const textsRef = useRef<HTMLInputElement>(null);

  const read = useMutation({
    mutationFn: ({ f, texts }: { f: File; texts: File | null }) => edeApi.read(f, texts),
    onSuccess: (result: EdeReadResult) =>
      setRows(result.devices.map((d) => ({ instance: d.instance, ticked: true, id: String(d.instance), name: d.name }))),
    onError: () => setRows([]),
  });
  const readResult = read.data ?? null;

  const choices = choicesOf(rows);
  const choicesKey = JSON.stringify(choices);
  const settledKey = useSettled(choicesKey, REPLAN_DELAY_MS);
  const settled: EdeDeviceChoice[] = JSON.parse(settledKey);
  const plannable = file !== null && readResult !== null && settled.length > 0 && settled.every(validChoice);

  const plan = useQuery({
    queryKey: ["edePlan", filesVersion, settledKey],
    queryFn: () => edeApi.plan(file as File, stateTexts, settled),
    enabled: plannable,
    retry: false,
  });
  // The plan on screen is for what the rows say now, not an edit ago.
  const current = plannable && settledKey === choicesKey && plan.isSuccess && !plan.isFetching;

  const create = useMutation({
    mutationFn: () => edeApi.create(file as File, stateTexts, choices, plan.data?.plan_digest),
    onMutate: () => setReplanned(false),
    onSuccess: ({ device_ids }) => {
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      reset();
      navigate(device_ids.length === 1 ? `/devices/${device_ids[0]}` : "/devices");
    },
    onError: (err) => {
      // The file or the devices changed since the preview: show the fresh plan.
      if (err instanceof PlanChangedError) plan.refetch().then(() => setReplanned(true));
    },
  });

  function reset() {
    setFile(null);
    setStateTexts(null);
    setRows([]);
    setFilesVersion((v) => v + 1);
    read.reset();
    if (fileRef.current) fileRef.current.value = "";
    if (textsRef.current) textsRef.current.value = "";
  }

  function chooseFiles(f: File | null, texts: File | null) {
    setFile(f);
    setStateTexts(texts);
    setFilesVersion((v) => v + 1);
    setReplanned(false);
    create.reset();
    setRows([]);
    read.reset();
    if (f) read.mutate({ f, texts });
  }

  const setRow = (instance: number, change: Partial<Row>) =>
    setRows((prev) => prev.map((r) => (r.instance === instance ? { ...r, ...change } : r)));

  const plans = current ? (plan.data?.plans ?? []) : [];
  const problem = plans.some((p) => p.problem !== null);
  const ticked = rows.filter((r) => r.ticked).length;

  return (
    <form
      className="bp-stack"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <p className="bp-muted">
        Creates a stopped device for each device in an EDE points list.
      </p>
      <FormGrid>
        <Field label="EDE file">
          <input
            id="ede_file"
            ref={fileRef}
            type="file"
            accept=".csv,.txt"
            onChange={(e) => chooseFiles(e.target.files?.[0] ?? null, stateTexts)}
          />
        </Field>
        <Field label="State texts file" help="Optional EDE state texts">
          <input
            id="ede_state_texts"
            ref={textsRef}
            type="file"
            accept=".csv,.txt"
            onChange={(e) => chooseFiles(file, e.target.files?.[0] ?? null)}
          />
        </Field>
      </FormGrid>

      {read.isPending && <Loading what="the file" />}
      {read.isError && <ErrorBlock lead="Couldn't read the file:">{errorMessage(read.error)}</ErrorBlock>}

      {readResult && (
        <>
          {(readResult.project_name || readResult.version_of_layout) && (
            <p className="bp-muted">
              {readResult.project_name ? `Project ${readResult.project_name}` : "EDE file"}
              {readResult.version_of_layout ? `, layout version ${readResult.version_of_layout}` : ""}.
            </p>
          )}
          {readResult.file_notes.length > 0 && (
            <Callout tone="info" lead="About the file:">
              {readResult.file_notes.join(" ")}
            </Callout>
          )}
          {readResult.columns_not_used.length > 0 && (
            <Callout tone="info" lead="Columns not used:">
              {readResult.columns_not_used.join(", ")}.
            </Callout>
          )}

          <TableFrame label="Devices in the file">
            <thead>
              <tr>
                <th>Create</th>
                <th>In the file</th>
                <th className="bp-cell-num">Objects</th>
                <th>Device instance</th>
                <th>Device name</th>
              </tr>
            </thead>
            <tbody>
              {readResult.devices.map((d) => {
                const row = rows.find((r) => r.instance === d.instance);
                if (!row) return null;
                return (
                  <tr key={d.instance}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Create device ${d.instance}`}
                        checked={row.ticked}
                        onChange={(e) => setRow(d.instance, { ticked: e.target.checked })}
                      />
                    </td>
                    <td>
                      {d.instance} {d.name}
                      {d.existing_device && (
                        <span className="bp-help">
                          {" "}
                          BACpack already has device {d.existing_device.id} ({d.existing_device.name}).
                        </span>
                      )}
                    </td>
                    <td className="bp-cell-num">{d.object_count}</td>
                    <td>
                      <input
                        className="bp-input"
                        type="number"
                        aria-label={`Instance for device ${d.instance}`}
                        required={row.ticked}
                        min={0}
                        max={MAX_INSTANCE}
                        value={row.id}
                        disabled={!row.ticked}
                        onChange={(e) => setRow(d.instance, { id: e.target.value })}
                      />
                    </td>
                    <td>
                      <input
                        className="bp-input"
                        aria-label={`Name for device ${d.instance}`}
                        required={row.ticked}
                        value={row.name}
                        disabled={!row.ticked}
                        onChange={(e) => setRow(d.instance, { name: e.target.value })}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </TableFrame>
          {ticked > 1 && (
            <Callout tone="info">
              Give each its own port in Process.
            </Callout>
          )}

          {plannable && !current && !plan.isError && <Loading what="the preview" />}
          {plan.isError && <ErrorBlock lead="Couldn't preview:">{errorMessage(plan.error)}</ErrorBlock>}
          {plans.map((p) => (
            <DevicePlan key={p.instance} plan={p} />
          ))}

          <FormActions>
            <Button variant="primary" type="submit" disabled={!current || problem || create.isPending}>
              {ticked > 1 ? "Create devices" : "Create device"}
            </Button>
          </FormActions>
          {replanned && create.isError ? (
            <ErrorBlock lead="Nothing was created:">
              the file or the devices changed since the preview. Check the new plan.
            </ErrorBlock>
          ) : (
            create.isError && <ErrorBlock lead="Couldn't create:">{errorMessage(create.error)}</ErrorBlock>
          )}
        </>
      )}
    </form>
  );
}

function DevicePlan({ plan }: { plan: EdeDevicePlan }) {
  const counts = Object.entries(plan.counts).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Fieldset legend={`Device ${plan.device.id} ${plan.device.name}`}>
      {plan.problem && <ErrorBlock lead="Can't create as it is:">{plan.problem}. Change the instance or name.</ErrorBlock>}
      {counts.length > 0 && (
        <TableFrame label={`Objects to create for device ${plan.device.id}`}>
          <thead>
            <tr>
              <th>Type</th>
              <th className="bp-cell-num">Objects</th>
            </tr>
          </thead>
          <tbody>
            {counts.map(([type, count]) => (
              <tr key={type}>
                <td>{type}</td>
                <td className="bp-cell-num">{count}</td>
              </tr>
            ))}
          </tbody>
        </TableFrame>
      )}
      <PlanLists
        skipped={plan.skipped}
        notes={plan.notes}
        skippedTypes={plan.skipped_types}
        vendorName={null}
        source="ede"
        skippedTitle="Not imported"
      />
    </Fieldset>
  );
}
