import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { clientToolApi, cloneApi } from "../api/endpoints";
import type { DiscoveredDevice, RemoteIdentity, RemoteObject, RemotePointRequest } from "../api/types";
import { errorMessage, type Notice } from "./Banner";
import Button from "./Button";
import { Field, FormActions, FormGrid } from "./Form";
import SidePanel from "./SidePanel";
import ClonePanel, { cloneJobKey, progressText, useCloneJob } from "./ClonePanel";
import { ErrorBlock } from "./AsyncState";
import { EmptyValue, TableFrame } from "./DataTable";
import { objectId, plural, segmentationText } from "../utils/text";

type Identity = RemoteIdentity | "loading" | "failed";
const IDENTIFY_AT_ONCE = 4;
const deviceKey = (d: DiscoveredDevice) => `${d.instance}@${d.address}`;

// The device's BACnet client tool: Who-Is, browse a remote device's objects,
// read or write any property. Every request goes out from this device's own
// address, answered by its own process, so the device has to be running.

// A typed value for a write: JSON where it parses (72.5, 1, true), otherwise
// the text as-is (active, inactive). The device converts it to the remote
// property's datatype.
export function parseValue(text: string): unknown {
  const trimmed = text.trim();
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

export default function DeviceClientTab({ deviceId, isLive }: { deviceId: number; isLive: boolean }) {
  const [busy, setBusy] = useState(false);
  const [low, setLow] = useState("");
  const [high, setHigh] = useState("");
  const [found, setFound] = useState<DiscoveredDevice[] | null>(null);
  const [whoIsError, setWhoIsError] = useState<string | null>(null);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [remote, setRemote] = useState<DiscoveredDevice | null>(null);
  const [objects, setObjects] = useState<RemoteObject[] | null>(null);
  const [picked, setPicked] = useState<RemoteObject | null>(null);
  const [property, setProperty] = useState("present-value");
  const [arrayIndex, setArrayIndex] = useState("");
  const [value, setValue] = useState("");
  const [priority, setPriority] = useState("");
  const [readValue, setReadValue] = useState<string | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [writeNotice, setWriteNotice] = useState<Notice>(null);
  const [identities, setIdentities] = useState<Record<string, Identity>>({});
  const whoIsRun = useRef(0);
  const objectsHeading = useRef<HTMLHeadingElement>(null);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const cloneJob = useCloneJob(deviceId).data ?? null;

  // Browse results land below the fold: focusing their heading scrolls to them.
  useEffect(() => {
    if (remote && objects) objectsHeading.current?.focus();
  }, [remote, objects]);

  // Leaving the device ends a running identify queue (it checks the run id).
  useEffect(
    () => () => {
      whoIsRun.current++;
    },
    []
  );

  if (!isLive) {
    return (
      <p className="bp-muted">
        Start the device to use its BACnet client (<Link to={`/devices/${deviceId}/process`}>Process tab</Link>).
      </p>
    );
  }

  // One tool request at a time; a failure goes to `report`, shown under the
  // button that sent the request.
  const run = async (action: () => Promise<void>, report: (message: string | null) => void) => {
    setBusy(true);
    report(null);
    try {
      await action();
    } catch (err) {
      report(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  // The names are a second read of each found device, so they arrive after
  // the table; a device that never answers it keeps its raw vendor id. Four
  // at a time, so a big network doesn't queue hundreds of requests at once.
  // A newer Who-Is drops the older run's answers.
  const identifyAll = (devices: DiscoveredDevice[]) => {
    const runId = ++whoIsRun.current;
    setIdentities(Object.fromEntries(devices.map((d) => [deviceKey(d), "loading" as Identity])));
    const queue = [...devices];
    const next = async (): Promise<void> => {
      const d = queue.shift();
      if (!d || runId !== whoIsRun.current) return;
      let result: Identity;
      try {
        result = await clientToolApi.identify(deviceId, { instance: d.instance, address: d.address, timeout_seconds: 3 });
      } catch {
        result = "failed";
      }
      if (runId !== whoIsRun.current) return;
      setIdentities((m) => ({ ...m, [deviceKey(d)]: result }));
      return next();
    };
    for (let i = 0; i < IDENTIFY_AT_ONCE; i++) void next();
  };

  const whoIs = () =>
    run(async () => {
      const body: { low?: number; high?: number } = {};
      if (low !== "") body.low = Number(low);
      if (high !== "") body.high = Number(high);
      const answered = await clientToolApi.whoIs(deviceId, body);
      setFound(answered);
      identifyAll(answered);
    }, setWhoIsError);

  const browse = (device: DiscoveredDevice) =>
    run(async () => {
      setRemote(device);
      setObjects(null);
      setPicked(null);
      setObjects(await clientToolApi.objectList(deviceId, { instance: device.instance, address: device.address }));
    }, setBrowseError);

  // One side panel at a time: the clone panel and the read/write panel.
  const startClone = (device: DiscoveredDevice) =>
    run(async () => {
      await cloneApi.start(deviceId, { instance: device.instance, address: device.address, timeout_seconds: 5 });
      await queryClient.invalidateQueries({ queryKey: cloneJobKey(deviceId) });
      setPicked(null);
      setCloneOpen(true);
    }, setCloneError);

  const point = (): RemotePointRequest => ({
    instance: remote!.instance,
    address: remote!.address,
    object_type: picked!.object_type,
    object_instance: picked!.instance,
    property: property.trim() || "present-value",
    array_index: arrayIndex === "" ? null : Number(arrayIndex),
  });

  const read = () => {
    setReadValue(null);
    return run(async () => {
      const result = await clientToolApi.read(deviceId, point());
      setReadValue(JSON.stringify(result.value));
    }, setReadError);
  };

  const write = (relinquish: boolean) => {
    const sent = relinquish ? null : parseValue(value);
    // A NULL write is a relinquish: only the Relinquish button sends one.
    if (!relinquish && sent === null) {
      setWriteNotice({ type: "error", message: "Use Relinquish to send a NULL." });
      return;
    }
    return run(
      async () => {
        await clientToolApi.write(deviceId, {
          ...point(),
          value: sent,
          priority: priority === "" ? null : Number(priority),
        });
        setWriteNotice({ type: "ok", message: relinquish ? `relinquished priority ${priority}` : "written" });
      },
      (message) => setWriteNotice(message === null ? null : { type: "error", message })
    );
  };

  return (
    <>
      <form
        className="bp-toolbar bp-toolbar--who-is"
        aria-label="Who-Is"
        onSubmit={(e) => {
          e.preventDefault();
          whoIs();
        }}
      >
        <Field label="Low instance">
          <input
            id="client_low"
            type="number"
            min={0}
            max={4194303}
            placeholder="all devices"
            value={low}
            onChange={(e) => setLow(e.target.value)}
          />
        </Field>
        <Field label="High instance">
          <input
            id="client_high"
            type="number"
            min={0}
            max={4194303}
            value={high}
            onChange={(e) => setHigh(e.target.value)}
          />
        </Field>
        <Button variant="primary" type="submit" disabled={busy}>
          Who-Is
        </Button>
      </form>
      {whoIsError !== null && <ErrorBlock lead="Who-Is failed:">{whoIsError}</ErrorBlock>}
      {cloneJob && !cloneOpen && (
        <div className="bp-toolbar">
          <span>
            {`Clone of device ${cloneJob.instance}: `}
            {cloneJob.status === "done"
              ? "read, ready to create."
              : cloneJob.status === "error"
                ? "the read failed."
                : `${progressText(cloneJob).replace(/^Reading/, "reading")}.`}
          </span>
          <Button
            small
            type="button"
            onClick={() => {
              setPicked(null);
              setCloneOpen(true);
            }}
          >
            Open clone
          </Button>
        </div>
      )}
      {cloneError !== null && <ErrorBlock lead="Couldn't start the clone:">{cloneError}</ErrorBlock>}

      {found && found.length === 0 && <p className="bp-muted">No devices answered.</p>}
      {found && found.length > 0 && (
        <TableFrame label="Discovered devices">
          <thead>
            <tr>
              <th>Device</th>
              <th>Name</th>
              <th>Address</th>
              <th>Vendor</th>
              <th className="bp-cell-num">Max APDU</th>
              <th>Segmentation</th>
              <th className="bp-cell-actions">
                <span className="bp-muted">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {found.map((d) => {
              const identity = identities[deviceKey(d)];
              const known = identity !== undefined && identity !== "loading" && identity !== "failed" ? identity : null;
              return (
                <tr key={deviceKey(d)}>
                  <td className="bp-cell-id">{d.instance}</td>
                  <td>
                    {identity === "loading" ? <span className="bp-muted">…</span> : (known?.name ?? <EmptyValue />)}
                  </td>
                  <td className="bp-cell-mono">{d.address}</td>
                  <td>
                    {known?.vendor_name ? (
                      <>
                        <span>{known.vendor_name}</span> <span className="bp-mono bp-muted">{d.vendor_id}</span>
                      </>
                    ) : (
                      <span className="bp-mono">{d.vendor_id}</span>
                    )}
                  </td>
                  <td className="bp-cell-num">{`${d.max_apdu} bytes`}</td>
                  <td>{segmentationText(d.segmentation)}</td>
                  <td className="bp-cell-actions">
                    <Button small type="button" disabled={busy} onClick={() => browse(d)}>
                      Browse
                    </Button>{" "}
                    <Button
                      small
                      type="button"
                      disabled={busy || cloneJob?.status === "pending" || cloneJob?.status === "running"}
                      onClick={() => startClone(d)}
                    >
                      Clone
                    </Button>{" "}
                    <Link to={`/connectors?type=bacnet&device=${deviceId}&instance=${d.instance}`}>Create connector</Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}
      {browseError !== null && <ErrorBlock lead="Browse failed:">{browseError}</ErrorBlock>}

      {remote && objects && (
        <>
          <h2 ref={objectsHeading} tabIndex={-1}>
            Device {remote.instance} · {plural(objects.length, "object")}
          </h2>
          <TableFrame label="Remote objects" scroll>
            <thead>
              <tr>
                <th>Object</th>
                <th>Name</th>
                <th className="bp-cell-actions">
                  <span className="bp-muted">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {objects.map((o) => (
                <tr key={objectId(o.object_type, o.instance)}>
                  <td className="bp-cell-mono">{objectId(o.object_type, o.instance)}</td>
                  <td>{o.name ?? <EmptyValue />}</td>
                  <td className="bp-cell-actions">
                    <Button
                      small
                      type="button"
                      onClick={() => {
                        setCloneOpen(false);
                        setPicked(o);
                        setReadValue(null);
                        setReadError(null);
                        setWriteNotice(null);
                      }}
                    >
                      Select
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableFrame>
        </>
      )}

      <SidePanel
        open={remote !== null && picked !== null}
        title={picked ? `${objectId(picked.object_type, picked.instance)}${picked.name ? ` (${picked.name})` : ""}` : ""}
        focusKey={picked ? objectId(picked.object_type, picked.instance) : undefined}
        onClose={() => setPicked(null)}
      >
        {remote && picked && (
          <div className="bp-stack">
            <FormGrid>
              <Field label="Property">
                <input id="client_property" type="text" value={property} onChange={(e) => setProperty(e.target.value)} />
              </Field>
              <Field label="Array index">
                <input
                  id="client_array_index"
                  type="number"
                  min={0}
                  placeholder="whole value"
                  value={arrayIndex}
                  onChange={(e) => setArrayIndex(e.target.value)}
                />
              </Field>
            </FormGrid>
            <FormActions>
              <Button variant="primary" type="button" disabled={busy} onClick={read}>
                Read
              </Button>
            </FormActions>
            {readValue !== null && (
              <p>
                <code>{readValue}</code>
              </p>
            )}
            {readError !== null && <ErrorBlock lead="Read failed:">{readError}</ErrorBlock>}
            <FormGrid>
              <Field label="Value">
                <input
                  id="client_value"
                  type="text"
                  placeholder="72.5, active, 3 ..."
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                />
              </Field>
              <Field label="Priority">
                <input
                  id="client_priority"
                  type="number"
                  min={1}
                  max={16}
                  placeholder="none"
                  value={priority}
                  onChange={(e) => setPriority(e.target.value)}
                />
              </Field>
            </FormGrid>
            <FormActions>
              <Button variant="primary" type="button" disabled={busy || value.trim() === ""} onClick={() => write(false)}>
                Write
              </Button>
              <Button type="button" disabled={busy || priority === ""} onClick={() => write(true)}>
                Relinquish
              </Button>
            </FormActions>
            {writeNotice?.type === "error" && <ErrorBlock lead="Write failed:">{writeNotice.message}</ErrorBlock>}
            {writeNotice?.type === "ok" && (
              <p className="bp-done" role="status">
                {writeNotice.message}
              </p>
            )}
          </div>
        )}
      </SidePanel>
      <ClonePanel deviceId={deviceId} open={cloneOpen} onClose={() => setCloneOpen(false)} />
    </>
  );
}
