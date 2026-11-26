import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { sampleApi } from "../api/endpoints";
import { ActionStatus, ErrorBlock, Loading } from "./AsyncState";
import type { SampleLoadResult } from "../api/types";
import { errorMessage, type Notice } from "./Banner";
import Button from "./Button";
import Callout from "./Callout";
import { CheckField } from "./Form";
import SidePanel from "./SidePanel";
import { formatBind } from "../utils/bind";
import { guideUrl } from "../utils/links";
import { plural } from "../utils/text";

// The panel closes on success, so the per-device outcome goes to the page as a notice.
export function sampleNotice({ devices }: SampleLoadResult): Notice {
  const total = plural(devices.length, "device");
  const failed = devices.filter((d) => d.started === false);
  if (failed.length > 0) {
    const started = devices.filter((d) => d.started === true).length;
    const why = failed.map((d) => (d.error ? `${d.name}: ${d.error}` : d.name)).join("; ");
    return { type: "error", message: `Loaded the sample building; started ${started} of ${total}. ${why}` };
  }
  if (devices.every((d) => d.started === null)) {
    return { type: "ok", message: `Loaded the sample building: ${total}, not started.` };
  }
  return { type: "ok", message: `Loaded the sample building: ${total} started.` };
}

type PanelProps = { open: boolean; onClose: () => void; onLoaded: (result: SampleLoadResult) => void };

// The packaged sample building: the plan for this server, then an add-only
// load. The server hands out the ports.
export default function SampleBuildingPanel({ open, onClose, onLoaded }: PanelProps) {
  return (
    <SidePanel open={open} title="Sample building" onClose={onClose}>
      <SampleBody open={open} onClose={onClose} onLoaded={onLoaded} />
    </SidePanel>
  );
}

function SampleBody({ open, onClose, onLoaded }: PanelProps) {
  const queryClient = useQueryClient();
  const [start, setStart] = useState(true);
  const plan = useQuery({ queryKey: ["samplePlan"], queryFn: () => sampleApi.plan(), enabled: open, staleTime: 0 });

  const load = useMutation({
    mutationFn: () => sampleApi.load(start),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
      queryClient.invalidateQueries({ queryKey: ["samplePlan"] });
      onLoaded(result);
      onClose();
    },
    // The server's answer may have changed (a clash, a port taken): show the fresh plan.
    onError: () => {
      plan.refetch();
    },
  });

  // An old error from a closed panel must not come back with it.
  const resetLoad = load.reset;
  useEffect(() => {
    if (open) resetLoad();
  }, [open, resetLoad]);

  const data = plan.data;
  return (
    <form
      className="bp-stack"
      onSubmit={(e) => {
        e.preventDefault();
        load.mutate();
      }}
    >
      <p className="bp-muted">
        An air handler and three VAV boxes.
      </p>
      {plan.isPending && <Loading what="the sample building" />}
      {plan.isError && <ErrorBlock lead="Couldn't plan the sample building:">{errorMessage(plan.error)}</ErrorBlock>}
      {data && (
        <>
          <ul className="bp-stack">
            {data.devices.map((d) => (
              <li key={d.id}>
                <strong>{d.name}</strong> ({d.id}), {d.role === "bbmd" ? "BBMD" : "normal"}
                {d.bind_address ? `, ${formatBind(d.bind_address)}` : ""}, {plural(d.object_count, "object")}
              </li>
            ))}
          </ul>
          {data.notes.map((note) => (
            <p key={note.text} className="bp-muted">
              {note.text}
              {note.guide && (
                <>
                  {" "}
                  <a href={guideUrl(note.guide)} target="_blank" rel="noreferrer">
                    More
                  </a>
                </>
              )}
            </p>
          ))}
          {data.refusal ? (
            <Callout tone="warn" lead="Can't load it here.">
              {data.refusal}
              {data.refusal.startsWith("Set the server's default bind") && (
                <>
                  {" "}
                  <Link to="/settings/network">Network Settings</Link>
                </>
              )}
            </Callout>
          ) : (
            <>
              <CheckField label="Start the devices" checked={start} onChange={(e) => setStart(e.target.checked)} />
              <div>
                <Button variant="primary" type="submit" disabled={load.isPending}>
                  {load.isPending && start ? "Loading and starting…" : "Load"}
                </Button>
              </div>
            </>
          )}
        </>
      )}
      <ActionStatus action={load} failed="Couldn't load the sample building" />
    </form>
  );
}
