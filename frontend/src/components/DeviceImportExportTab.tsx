import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PlanChangedError, objectsApi, type EdeImportOptions } from "../api/endpoints";
import type { CsvImportPlan } from "../api/types";
import Button from "./Button";
import Callout from "./Callout";
import { useConfirm } from "./ConfirmDialog";
import { Field, Fieldset, Form, FormActions, FormGrid, SettingsColumn } from "./Form";
import { ActionStatus, ErrorBlock } from "./AsyncState";
import ImportPlan, { importPlanSummary, pastSummary } from "./ImportPlan";
import PlanLists from "./PlanLists";
import { plural } from "../utils/text";

type Mode = "merge" | "sync";

// Only what was chosen, so a plain CSV import calls the API as it always has.
function edeArgs(stateTexts: File | null, edeDevice: number | undefined): [EdeImportOptions] | [] {
  if (stateTexts === null && edeDevice === undefined) return [];
  return [{ stateTexts, edeDevice }];
}

// An import previews its plan first (a dry run), and the apply sends the
// preview's digest back, so what is imported is what was shown. The server
// tells an EDE file from BACpack's CSV by its content; its plan adds what it
// skipped and changed.
export default function DeviceImportExportTab({ deviceId }: { deviceId: number }) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [stateTexts, setStateTexts] = useState<File | null>(null);
  // The EDE file's device to import, when the file has several.
  const [edeDevice, setEdeDevice] = useState<number | undefined>(undefined);
  const [mode, setMode] = useState<Mode>("merge");
  const [plan, setPlan] = useState<CsvImportPlan | null>(null);
  const [doneText, setDoneText] = useState("");
  // Set once a plan-changed import has re-run the preview.
  const [replanned, setReplanned] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textsInputRef = useRef<HTMLInputElement>(null);
  const { ask, dialog } = useConfirm();

  // A preview is for the inputs it was made with; one that lands after the
  // mode or a file changed is dropped, not shown beside the new inputs.
  type PreviewInputs = { f: File; device: number | undefined; mode: Mode; texts: File | null };
  const latest = useRef({ file, stateTexts, mode, edeDevice });
  latest.current = { file, stateTexts, mode, edeDevice };
  const current = (vars: PreviewInputs) =>
    vars.f === latest.current.file &&
    vars.texts === latest.current.stateTexts &&
    vars.mode === latest.current.mode &&
    vars.device === latest.current.edeDevice;
  const preview = useMutation({
    mutationFn: ({ f, device, mode: m, texts }: PreviewInputs) =>
      objectsApi.previewCsv(deviceId, f, m, ...edeArgs(texts, device)),
    onSuccess: (result, vars) => {
      if (current(vars)) setPlan(result);
    },
    onError: (_err, vars) => {
      if (current(vars)) setPlan(null);
    },
  });
  const startPreview = (f: File, device: number | undefined, options?: { onSuccess: () => void }) =>
    preview.mutate({ f, device, mode, texts: stateTexts }, options);

  const importCsv = useMutation({
    mutationFn: (sent: CsvImportPlan) =>
      objectsApi.importCsv(
        deviceId,
        file as File,
        sent.mode,
        sent.plan_digest,
        ...edeArgs(stateTexts, sent.ede?.device ?? undefined)
      ),
    onMutate: () => setReplanned(false),
    onSuccess: (result) => {
      setDoneText(
        "imported" in result
          ? `Imported ${plural(result.imported, "object")}.`
          : pastSummary(result.created.length, result.updated.length, result.deleted.length, "object")
      );
      setPlan(null);
      setFile(null);
      setStateTexts(null);
      setEdeDevice(undefined);
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (textsInputRef.current) textsInputRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
    onError: (err) => {
      // The device's objects changed since the preview: show the fresh plan.
      if (err instanceof PlanChangedError && file) {
        startPreview(file, edeDevice, { onSuccess: () => setReplanned(true) });
      }
    },
  });

  const dropPlan = () => {
    setPlan(null);
    setReplanned(false);
    preview.reset();
    importCsv.reset();
  };

  const ede = plan?.ede ?? null;
  const edeDetails = [
    ede?.project_name && `project ${ede.project_name}`,
    ede?.version_of_layout && `layout version ${ede.version_of_layout}`,
  ]
    .filter(Boolean)
    .join(", ");
  // A sync with nothing to keep or add empties the device: an EDE file whose
  // points BACpack can't use, or the wrong device of a file.
  const deletesEverything =
    plan !== null && plan.mode === "sync" && plan.deleted.length > 0 && plan.created.length + plan.updated.length === 0;

  const apply = (current: CsvImportPlan) => {
    const deletes = current.deleted.length;
    if (current.mode !== "sync" || deletes === 0) {
      importCsv.mutate(current);
      return;
    }
    ask({
      title: `Import ${file?.name ?? "this file"}?`,
      body: `It deletes ${plural(deletes, "object")} and ${deletes === 1 ? "its" : "their"} trend history.`,
      confirmLabel: "Import and delete",
      onConfirm: () => importCsv.mutateAsync(current),
    });
  };

  return (
    <>
      {dialog}
      <SettingsColumn>
        <Fieldset legend="Export">
          <p>
            <a href={objectsApi.exportCsvUrl(deviceId)}>Download objects as CSV</a>
          </p>
        </Fieldset>

        <Form
          onSubmit={(e) => {
            e.preventDefault();
            if (file) startPreview(file, edeDevice);
          }}
        >
          <Fieldset legend="Import">
            <FormGrid>
              <Field label="CSV file" help="BACpack CSV or EDE points list">
                <input
                  id="csv_file"
                  ref={fileInputRef}
                  type="file"
                  accept=".csv,.txt"
                  required
                  onChange={(e) => {
                    setFile(e.target.files?.[0] ?? null);
                    setEdeDevice(undefined);
                    dropPlan();
                  }}
                />
              </Field>
              <Field label="State texts file" help="Optional EDE state texts">
                <input
                  id="csv_state_texts"
                  ref={textsInputRef}
                  type="file"
                  accept=".csv,.txt"
                  onChange={(e) => {
                    setStateTexts(e.target.files?.[0] ?? null);
                    dropPlan();
                  }}
                />
              </Field>
              <Field label="Import mode">
                <select
                  id="csv_import_mode"
                  value={mode}
                  onChange={(e) => {
                    setMode(e.target.value as Mode);
                    dropPlan();
                  }}
                >
                  <option value="merge">Merge (add only)</option>
                  <option value="sync">Sync (replace the list)</option>
                </select>
              </Field>
            </FormGrid>
            <dl className="bp-facts">
              <dt>Merge</dt>
              <dd>
                Adds the file's objects. Refuses the file if an identifier or name already exists.
              </dd>
              <dt>Sync</dt>
              <dd>
                Objects not in the file are deleted.
              </dd>
            </dl>
            <FormActions>
              <Button variant="primary" type="submit" disabled={!file || preview.isPending}>
                Preview
              </Button>
            </FormActions>
            <ActionStatus action={preview} failed="Couldn't preview the file" />
            {/* A success only: a failure shows under the plan, beside the Import button. */}
            <ActionStatus
              action={{ isError: false, isSuccess: importCsv.isSuccess, error: null }}
              failed=""
              done={doneText}
            />
          </Fieldset>
        </Form>

      {plan && (
        <Fieldset legend="Plan">
          {ede && <p>Read as an EDE file{edeDetails ? ` (${edeDetails})` : ""}.</p>}
          {ede && ede.devices.length > 1 && (
            <FormGrid>
              <Field label="Device in the file">
                <select
                  id="csv_ede_device"
                  value={ede.device ?? ""}
                  disabled={preview.isPending}
                  onChange={(e) => {
                    const chosen = Number(e.target.value);
                    setEdeDevice(chosen);
                    setReplanned(false);
                    importCsv.reset();
                    if (file) startPreview(file, chosen);
                  }}
                >
                  {ede.device === null && <option value="">Choose a device</option>}
                  {ede.devices.map((d) => (
                    <option key={d.instance} value={d.instance}>
                      {d.instance} {d.name} ({plural(d.object_count, "object")})
                    </option>
                  ))}
                </select>
              </Field>
            </FormGrid>
          )}
          {!plan.needs_device && (
            <>
              <p>{importPlanSummary(plan.created.length, plan.updated.length, plan.deleted.length, "object")}</p>
              <ImportPlan
                groups={[{ title: "Objects", created: plan.created, updated: plan.updated, deleted: plan.deleted }]}
              />
              <PlanLists
                skipped={plan.skipped ?? []}
                notes={plan.notes ?? []}
                skippedTypes={plan.skipped_types ?? {}}
                vendorName={null}
                source="ede"
                skippedTitle="Not imported"
              />
            </>
          )}
          {ede && ede.file_notes.length > 0 && (
            <Callout tone="info" lead="About the file:">
              {ede.file_notes.join(" ")}
            </Callout>
          )}
          {plan.skipped_columns.length > 0 && (
            <Callout tone="info" lead="Columns not imported:">
              {plan.skipped_columns.join(", ")}
            </Callout>
          )}
          {!plan.needs_device && plan.mode === "sync" && (
            <Callout
              tone={deletesEverything ? "danger" : "warn"}
              lead={deletesEverything ? "This empties the device." : "Sync replaces this device's object list."}
            >
              {deletesEverything
                ? `Importing deletes all ${plural(plan.deleted.length, "object")}.`
                : plan.deleted.length > 0
                  ? `Importing deletes ${plural(plan.deleted.length, "object")}.`
                  : "Importing deletes no objects."}
            </Callout>
          )}
          {!plan.needs_device && (
            <FormActions>
              <Button
                variant={plan.mode === "sync" && plan.deleted.length > 0 ? "danger" : "primary"}
                type="button"
                disabled={importCsv.isPending || preview.isPending}
                onClick={() => apply(plan)}
              >
                Import
              </Button>
            </FormActions>
          )}
        </Fieldset>
      )}
      {replanned && importCsv.isError ? (
        <ErrorBlock lead="Nothing was imported:">
          the device's objects changed since the preview. Check the new plan.
        </ErrorBlock>
      ) : (
        <ActionStatus action={importCsv} failed="Couldn't import" />
      )}
      </SettingsColumn>
    </>
  );
}
