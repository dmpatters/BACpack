import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cloneApi } from "../api/endpoints";
import { isNotFound } from "../api/http";
import type { CloneJob } from "../api/types";
import { errorMessage } from "./Banner";
import Button from "./Button";
import { Field, FormActions, FormGrid } from "./Form";
import SidePanel from "./SidePanel";
import { ErrorBlock, Loading } from "./AsyncState";
import { TableFrame } from "./DataTable";
import PlanLists from "./PlanLists";
import { plural } from "../utils/text";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER } from "../utils/bind";

const POLL_MS = 1000;

export const cloneJobKey = (deviceId: number) => ["cloneJob", deviceId];

// The device's newest clone job, or null when it has none.
export function useCloneJob(deviceId: number) {
  return useQuery({
    queryKey: cloneJobKey(deviceId),
    queryFn: async (): Promise<CloneJob | null> => {
      try {
        return await cloneApi.get(deviceId);
      } catch (err) {
        if (isNotFound(err)) return null;
        throw err;
      }
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? POLL_MS : false;
    },
  });
}

export function progressText(job: CloneJob): string {
  const { phase, done, total } = job.progress;
  if (phase === "object-list") {
    return total > 0 ? `Reading the object list: ${done.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}` : "Reading the object list";
  }
  return `Reading ${done.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} objects`;
}

// Cloning a controller: the read's progress, then the plan Create would save,
// with what it leaves out.
export default function ClonePanel({
  deviceId,
  open,
  onClose,
}: {
  deviceId: number;
  open: boolean;
  onClose: () => void;
}) {
  const job = useCloneJob(deviceId).data ?? null;
  const title = job?.instance != null ? `Clone device ${job.instance}` : "Clone a controller";
  return (
    <SidePanel open={open} title={title} onClose={onClose} focusKey={job?.job_id}>
      {open && job && <CloneJobBody deviceId={deviceId} job={job} onClose={onClose} />}
      {open && !job && <p className="bp-muted">No clone in progress.</p>}
    </SidePanel>
  );
}

function CloneJobBody({ deviceId, job, onClose }: { deviceId: number; job: CloneJob; onClose: () => void }) {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: cloneJobKey(deviceId) });
  const cancel = useMutation({ mutationFn: () => cloneApi.cancel(deviceId, job.job_id), onSuccess: refresh });
  const discard = useMutation({
    mutationFn: () => cloneApi.discard(deviceId, job.job_id),
    onSuccess: () => {
      queryClient.setQueryData(cloneJobKey(deviceId), null);
      onClose();
    },
  });

  if (job.status === "pending" || job.status === "running") {
    return (
      <div className="bp-stack">
        <p role="status">{progressText(job)}</p>
        <FormActions>
          <Button type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            Cancel
          </Button>
        </FormActions>
        {cancel.isError && <ErrorBlock lead="Couldn't cancel:">{errorMessage(cancel.error)}</ErrorBlock>}
      </div>
    );
  }
  if (job.status === "error") {
    return (
      <div className="bp-stack">
        <ErrorBlock lead="The read failed:">{job.error ?? "unknown error"}</ErrorBlock>
        <FormActions>
          <Button type="button" disabled={discard.isPending} onClick={() => discard.mutate()}>
            Discard
          </Button>
        </FormActions>
      </div>
    );
  }
  return <ClonePlanView deviceId={deviceId} job={job} discard={() => discard.mutate()} discarding={discard.isPending} />;
}

function ClonePlanView({
  deviceId,
  job,
  discard,
  discarding,
}: {
  deviceId: number;
  job: CloneJob;
  discard: () => void;
  discarding: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Blank until the first plan fills them with the controller's own.
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [bind, setBind] = useState("");

  const plan = useQuery({
    queryKey: ["clonePlan", deviceId, job.job_id],
    queryFn: () => cloneApi.plan(deviceId, job.job_id, {}),
  });
  const result = plan.data;

  // The first plan fills the fields; edits after that are the user's.
  const filled = useRef(false);
  useEffect(() => {
    if (!result || filled.current) return;
    filled.current = true;
    setId(String(result.plan.device.id));
    setName(result.plan.device.name);
    setBind(result.suggested_bind.bind_address ?? "");
  }, [result]);

  const create = useMutation({
    mutationFn: () =>
      cloneApi.create(deviceId, job.job_id, { id: Number(id), name: name.trim(), bind_address: bind.trim() || null }),
    onSuccess: ({ device_id }) => {
      queryClient.setQueryData(cloneJobKey(deviceId), null);
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      navigate(`/devices/${device_id}`);
    },
  });


  if (plan.isPending) return <Loading what="the clone plan" />;
  if (plan.isError || !result) return <ErrorBlock lead="Couldn't plan the clone:">{errorMessage(plan.error)}</ErrorBlock>;
  const p = result.plan;
  const counts = Object.entries(p.counts).sort(([a], [b]) => a.localeCompare(b));
  // The plan was made for the controller's own id and name: a problem with
  // those holds Create back until one is changed (Create checks the new ones).
  const edited = id !== String(p.device.id) || name.trim() !== p.device.name;

  return (
    <form
      className="bp-stack"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <p className="bp-muted">
        Read {plural(p.object_count + p.skipped.length, "object")} from device {job.instance}
        {p.vendor_name ? ` (${p.vendor_name})` : ""}. The new device is saved stopped. It may share the controller's
        id, and its notification classes keep the site's recipients.
      </p>
      <FormGrid>
        <Field label="Device instance">
          <input
            id="clone_device_id"
            type="number"
            required
            min={0}
            max={4194302}
            value={id}
            onChange={(e) => setId(e.target.value)}
          />
        </Field>
        <Field label="Device name">
          <input id="clone_device_name" required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Bind address" help={`${BIND_ADDRESS_HELP} Blank uses the default.`}>
          <input
            id="clone_bind"
            className="bp-input--mono"
            placeholder={BIND_ADDRESS_PLACEHOLDER}
            value={bind}
            onChange={(e) => setBind(e.target.value)}
          />
        </Field>
      </FormGrid>
      {p.problem && !edited && <ErrorBlock lead="Can't create as it is:">{p.problem}. Change the instance or name.</ErrorBlock>}

      <TableFrame label="Objects to create">
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

      <PlanLists
        skipped={p.skipped}
        notes={p.notes}
        skippedTypes={p.skipped_types}
        vendorName={p.vendor_name}
        source="clone"
        skippedTitle="Not cloned"
      />

      <FormActions>
        <Button variant="primary" type="submit" disabled={create.isPending || (p.problem !== null && !edited)}>
          Create device
        </Button>
        <Button type="button" disabled={discarding} onClick={discard}>
          Discard
        </Button>
      </FormActions>
      {create.isError && <ErrorBlock lead="Couldn't create:">{errorMessage(create.error)}</ErrorBlock>}
    </form>
  );
}
