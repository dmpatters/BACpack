import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PlanChangedError, configApi } from "../api/endpoints";
import type { ConfigSyncPlan } from "../api/types";
import Button, { buttonClass } from "../components/Button";
import PageHeader from "../components/PageHeader";
import Callout from "../components/Callout";
import { useConfirm } from "../components/ConfirmDialog";
import { Field, Fieldset, Form, FormActions, SettingsColumn } from "../components/Form";
import { ActionStatus, ErrorBlock } from "../components/AsyncState";
import ImportPlan, { importPlanSummary, type PlanGroup } from "../components/ImportPlan";
import { plural } from "../utils/text";
import { usePageTitle } from "../utils/usePageTitle";

// What the dialog names before an apply: the deletions are what can't be undone.
export function applySummary(plan: ConfigSyncPlan): string {
  const devices = plan.devices_deleted.length;
  const objects = Object.values(plan.objects).reduce((n, s) => n + s.deleted.length, 0);
  if (devices === 0 && objects === 0) return "It deletes nothing.";
  const parts = [
    devices > 0 ? `${devices} ${devices === 1 ? "device" : "devices"}` : null,
    objects > 0 ? `${objects} ${objects === 1 ? "object" : "objects"}` : null,
  ].filter(Boolean);
  const one = devices + objects === 1;
  return `It deletes ${parts.join(" and ")} and ${one ? "its" : "their"} trend history.`;
}

// The plan's headline: device changes, then the object changes across devices.
export function planHeadline(plan: ConfigSyncPlan): string {
  const objects = Object.values(plan.objects);
  const count = (key: "created" | "updated" | "deleted") => objects.reduce((n, s) => n + s[key].length, 0);
  const parts = [
    importPlanSummary(plan.devices_created.length, plan.devices_updated.length, plan.devices_deleted.length, "device"),
    importPlanSummary(count("created"), count("updated"), count("deleted"), "object"),
    importPlanSummary(plan.connectors_created.length, plan.connectors_updated.length, 0, "connector"),
    plan.settings_changed ? "Updates retention settings." : NO_CHANGES,
  ].filter((s) => s !== NO_CHANGES);
  return parts.length ? parts.join(" ") : NO_CHANGES;
}

const NO_CHANGES = "No changes.";

// Connector mapping changes, one group per connector that has any.
function connectorGroups(plan: ConfigSyncPlan) {
  const groups: PlanGroup[] = [];
  if (plan.connectors_created.length || plan.connectors_updated.length) {
    groups.push({
      title: "Connectors",
      created: plan.connectors_created,
      updated: plan.connectors_updated,
      deleted: [],
      noDelete: true,
    });
  }
  for (const [name, s] of Object.entries(plan.connector_mappings)) {
    if (s.created.length || s.updated.length || s.deleted.length) groups.push({ title: `Connector ${name}`, ...s });
  }
  return groups;
}

// Whole-server JSON config export/import. Devices and objects are a full
// sync (anything not in the file gets deleted); connectors are created or
// updated by name and never deleted. So this page always previews the plan
// (dry run) before a second, explicit click commits it, as the backend
// defaults to.
export default function Config() {
  usePageTitle("Config");
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<ConfigSyncPlan | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [applied, setApplied] = useState("");
  // Set once a plan-changed apply has re-run the preview.
  const [replanned, setReplanned] = useState(false);
  const { ask, dialog } = useConfirm();

  // A preview is for the file it was made from: one that lands after another
  // file was chosen is dropped.
  const chosen = useRef(file);
  chosen.current = file;
  const preview = useMutation({
    mutationFn: (f: File) => configApi.import(f, true),
    onSuccess: (result, f) => {
      if (f === chosen.current) setPlan(result);
    },
    onError: (_err, f) => {
      if (f === chosen.current) setPlan(null);
    },
  });

  const apply = useMutation({
    mutationFn: () => configApi.import(file as File, false, plan?.plan_digest),
    onMutate: () => setReplanned(false),
    onSuccess: (result) => {
      setPlan(null);
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      const connectors =
        result.connectors_created.length || result.connectors_updated.length
          ? ` Connectors: created ${result.connectors_created.length}, updated ${result.connectors_updated.length}.`
          : "";
      setApplied(
        `Applied: created ${plural(result.devices_created.length, "device")}, updated ${plural(result.devices_updated.length, "device")} and deleted ${plural(result.devices_deleted.length, "device")}.${connectors}`
      );
    },
    onError: (err) => {
      // The server's plan no longer matches the preview: show the fresh one.
      if (err instanceof PlanChangedError && file) {
        preview.mutate(file, { onSuccess: () => setReplanned(true) });
      }
    },
  });

  return (
    <>
      <PageHeader title="Config" />
      {dialog}
      <SettingsColumn>
        <Fieldset legend="Export">
          <p>
            <a href={configApi.exportUrl()}>Download bacpack-config.json</a>
          </p>
          <p className="bp-help">
            Passwords, tokens and header values are left out.
          </p>
        </Fieldset>

        <Fieldset legend="Import">
          <p className="bp-help">Connectors are matched by name.</p>
          <Form
            onSubmit={(e) => {
              e.preventDefault();
              if (file) preview.mutate(file);
            }}
          >
            <Field label="Configuration file">
              <input
                id="config_file"
                ref={fileInputRef}
                type="file"
                accept=".json,application/json"
                required
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setPlan(null);
                  apply.reset();
                }}
              />
            </Field>
            <FormActions>
              <Button variant="primary" type="submit" disabled={!file || preview.isPending}>
                Preview
              </Button>
            </FormActions>
            <ActionStatus action={preview} failed="Couldn't preview the file" />
          </Form>
        </Fieldset>

      <Fieldset legend="Sample building">
        <p>An air handler and three VAV boxes.</p>
        <p>
          <Link className={buttonClass()} to="/devices/sample" aria-haspopup="dialog">
            Load sample building
          </Link>
        </p>
      </Fieldset>

      {plan && (
        <Fieldset legend="Plan">
          <p>{planHeadline(plan)}</p>
          <ImportPlan
            groups={[
              {
                title: "Devices",
                created: plan.devices_created.map(String),
                updated: plan.devices_updated.map(String),
                deleted: plan.devices_deleted.map(String),
              },
              ...Object.entries(plan.objects).map(([id, s]) => ({ title: `Device ${id}`, ...s })),
              ...connectorGroups(plan),
            ]}
          />
          {Object.keys(plan.needs_credentials).length > 0 && (
            <Callout tone="info" lead="Needs credentials.">
              Set them on the Connectors page after applying.
              <ul>
                {Object.entries(plan.needs_credentials).map(([name, missing]) => (
                  <li key={name}>{`${name}: ${missing.join(", ")}`}</li>
                ))}
              </ul>
            </Callout>
          )}
          <Callout tone="warn" lead="Import is a full sync.">
            Devices and objects not in the file are deleted.
          </Callout>
          <FormActions>
            <Button
              variant="danger"
              type="button"
              disabled={apply.isPending}
              onClick={() =>
                ask({
                  title: "Apply this configuration?",
                  body: applySummary(plan),
                  confirmLabel: "Apply configuration",
                  onConfirm: () => apply.mutateAsync(),
                })
              }
            >
              Apply
            </Button>
          </FormActions>
        </Fieldset>
      )}
      {replanned && apply.isError ? (
        <ErrorBlock lead="Nothing was applied:">
          the configuration changed since the preview. Check the new plan.
        </ErrorBlock>
      ) : (
        <ActionStatus action={apply} failed="Couldn't apply the configuration" done={applied} />
      )}
      </SettingsColumn>
    </>
  );
}
