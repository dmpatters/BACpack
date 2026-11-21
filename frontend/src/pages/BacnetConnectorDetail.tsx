import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectorsApi, objectsApi } from "../api/endpoints";
import type {
  BacnetObjectDTO,
  ConnectorDTO,
  ConnectorMappingDTO,
  ConnectorWriteMappingDTO,
  RemoteTarget,
} from "../api/types";
import Banner, { LoadError, errorMessage, type Notice } from "../components/Banner";
import ConnectorHealthPill from "../components/ConnectorHealth";
import RemotePointPicker from "../components/RemotePointPicker";
import { isCommandable } from "../utils/objectTypes";
import { formatUtcBare } from "../utils/time";
import Button from "../components/Button";
import PageHeader from "../components/PageHeader";
import SidePanel from "../components/SidePanel";
import { useConfirm } from "../components/ConfirmDialog";
import { ClipCell, EmptyValue, RowActions, SortHeader, TableFrame } from "../components/DataTable";
import StatusPill, { ResultPill } from "../components/StatusPill";
import MappingValueCell from "../components/MappingValueCell";
import { useDeviceLives } from "../utils/useLiveState";
import TableToolbar, { TOOLBAR_MIN_ROWS, countText, toolbarSearch } from "../components/TableToolbar";
import { matchesSearch, sortRows, useSort } from "../utils/tableSort";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid } from "../components/Form";
import { ActionStatus, ErrorBlock, Loading, type ActionState } from "../components/AsyncState";
import { LEAVE_UNSAVED, differs, useDraft, useUnsavedChanges } from "../components/UnsavedChanges";
import { objectId } from "../utils/text";

// A BACnet connector: one remote device, run by the owning device's own
// process. Mappings are added, edited and deleted here.

export function formatPoint(
  objectType: string | null,
  instance: number | null,
  property: string | null,
  arrayIndex: number | null
): string {
  if (objectType === null || instance === null) return "—";
  return `${objectId(objectType, instance)} ${property ?? "present-value"}${arrayIndex === null ? "" : `[${arrayIndex}]`}`;
}

// A read mapping's value map, typed as "active=1, inactive=0": the remote
// value's name -> the number fed to the local object. A name holding a
// comma, an equals sign or a quote is written in double quotes, JSON-style
// ("a,b"=1), so formatValueMap's output always parses back. Blank is no
// map; null for anything that doesn't parse.
export function parseValueMap(text: string): Record<string, number> | null | undefined {
  if (!text.trim()) return undefined;
  const map: Record<string, number> = {};
  let i = 0;
  for (;;) {
    while (/\s/.test(text[i] ?? "")) i++;
    let name: string;
    if (text[i] === '"') {
      let end = i + 1;
      while (end < text.length && text[end] !== '"') end += text[end] === "\\" ? 2 : 1;
      if (end >= text.length) return null;
      try {
        name = JSON.parse(text.slice(i, end + 1));
      } catch {
        return null;
      }
      i = end + 1;
      while (/\s/.test(text[i] ?? "")) i++;
      if (text[i] !== "=") return null;
    } else {
      const equals = text.indexOf("=", i);
      const comma = text.indexOf(",", i);
      if (equals < 0 || (comma >= 0 && comma < equals)) return null;
      name = text.slice(i, equals).trim();
      i = equals;
    }
    const comma = text.indexOf(",", i + 1);
    const value = text.slice(i + 1, comma < 0 ? text.length : comma).trim();
    const number = Number(value);
    if (!name || value === "" || !Number.isFinite(number)) return null;
    map[name] = number;
    if (comma < 0) return map;
    i = comma + 1;
  }
}

function formatValueMapName(name: string): string {
  return /[,="]/.test(name) || name !== name.trim() ? JSON.stringify(name) : name;
}

export function formatValueMap(map: Record<string, number> | null): string {
  return map
    ? Object.entries(map)
        .map(([name, value]) => `${formatValueMapName(name)}=${value}`)
        .join(", ")
    : "";
}

function objectLabel(objects: BacnetObjectDTO[], id: number): string {
  const o = objects.find((x) => x.id === id);
  return o ? `${o.name} (${o.object_type}:${o.instance_number})` : `object ${id}`;
}

export default function BacnetConnectorDetail({ connector }: { connector: ConnectorDTO }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const deviceId = connector.device_id as number;
  const target: RemoteTarget = {
    instance: connector.remote_device_instance as number,
    address: connector.remote_address,
  };

  const { data: reads = [], error: readsError, refetch: refetchReads } = useQuery({
    queryKey: ["connectorMappings", connector.id],
    queryFn: () => connectorsApi.mappings.list(connector.id),
  });
  const { data: writes = [], error: writesError, refetch: refetchWrites } = useQuery({
    queryKey: ["connectorWriteMappings", connector.id],
    queryFn: () => connectorsApi.writeMappings.list(connector.id),
  });
  const { data: status = {} } = useQuery({
    queryKey: ["connectorStatus"],
    queryFn: connectorsApi.status,
    refetchInterval: 5000,
  });
  const {
    data: objects = [],
    error: objectsError,
    isPending: objectsPending,
    refetch: refetchObjects,
  } = useQuery({
    queryKey: ["objects", deviceId],
    queryFn: () => objectsApi.list(deviceId),
  });
  const lives = useDeviceLives(reads.length > 0 ? [deviceId] : []);
  // In place of an add form, which would otherwise say the device has no objects.
  const objectsNotLoaded = objectsError ? (
    <LoadError what="objects" error={objectsError} onRetry={() => refetchObjects()} />
  ) : objectsPending ? (
    <Loading what="objects" />
  ) : null;

  const onError = (err: unknown) => setNotice({ type: "error", message: errorMessage(err) });
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["connectorMappings", connector.id] });
    queryClient.invalidateQueries({ queryKey: ["connectorWriteMappings", connector.id] });
    queryClient.invalidateQueries({ queryKey: ["connectors"] });
    // The objects carry fed_by_connector_*, which the add form greys out by.
    queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
  };

  const updateConnector = useMutation({
    mutationFn: (body: Record<string, unknown>) => connectorsApi.update(connector.id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connector", connector.id] });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
  });
  const deleteConnector = useMutation({
    mutationFn: () => connectorsApi.remove(connector.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      // The delete dialog was the question; the guard doesn't ask again.
      navigate("/connectors", { state: LEAVE_UNSAVED });
    },
    onError,
  });
  const addRead = useMutation({
    mutationFn: (body: Record<string, unknown>) => connectorsApi.mappings.create(connector.id, body),
    onSuccess: refresh,
  });
  const deleteRead = useMutation({
    mutationFn: (mappingId: number) => connectorsApi.mappings.remove(connector.id, mappingId),
    onSuccess: refresh,
    onError,
  });
  const addWrite = useMutation({
    mutationFn: (body: Record<string, unknown>) => connectorsApi.writeMappings.create(connector.id, body),
    onSuccess: refresh,
  });
  const deleteWrite = useMutation({
    mutationFn: (mappingId: number) => connectorsApi.writeMappings.remove(connector.id, mappingId),
    onSuccess: refresh,
    onError,
  });
  // One mapping panel at a time: the adds and a row's Edit replace each other.
  const [panel, setPanel] = useState<{ kind: "add-read" } | { kind: "add-write" } | { kind: "edit" } | null>(null);
  // The mapping whose edit form exists. Closing the panel only hides it, so a
  // half-typed edit survives; Cancel, a save, or another row's Edit end it.
  const [kept, setKept] = useState<{ which: "read" | "write"; id: number } | null>(null);
  const endEdit = () => {
    setPanel(null);
    setKept(null);
  };
  const openEdit = (which: "read" | "write", id: number) => {
    setKept({ which, id });
    setPanel({ kind: "edit" });
  };
  const updateRead = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      connectorsApi.mappings.update(connector.id, id, body),
    onSuccess: () => {
      refresh();
      endEdit();
    },
  });
  const updateWrite = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      connectorsApi.writeMappings.update(connector.id, id, body),
    onSuccess: () => {
      refresh();
      endEdit();
    },
  });
  const editing = panel?.kind === "edit" ? kept : null;
  const editingRead = kept?.which === "read" ? reads.find((m) => m.id === kept.id) : undefined;
  const editingWrite = kept?.which === "write" ? writes.find((w) => w.id === kept.id) : undefined;

  const { ask, dialog } = useConfirm();
  const readSort = useSort<"object">();
  const writeSort = useSort<"object">();
  const [readSearch, setReadSearch] = useState("");
  const [writeSearch, setWriteSearch] = useState("");
  const mappedObject = { object: (m: { object_id: number }) => objectLabel(objects, m.object_id) };
  const shownReads = sortRows(
    reads.filter((m) =>
      matchesSearch(
        toolbarSearch(readSearch, reads.length),
        objectLabel(objects, m.object_id),
        formatPoint(m.remote_object_type, m.remote_object_instance, m.remote_property, m.remote_array_index)
      )
    ),
    readSort.sort,
    mappedObject
  );
  const shownWrites = sortRows(
    writes.filter((w) =>
      matchesSearch(
        toolbarSearch(writeSearch, writes.length),
        objectLabel(objects, w.object_id),
        formatPoint(w.remote_object_type, w.remote_object_instance, w.remote_property, w.remote_array_index)
      )
    ),
    writeSort.sort,
    mappedObject
  );
  const askDeleteRead = (m: ConnectorMappingDTO) =>
    ask({
      title: `Delete the read mapping for ${objectLabel(objects, m.object_id)}?`,
      body: "The local object stops being fed.",
      confirmLabel: "Delete mapping",
      onConfirm: () => deleteRead.mutateAsync(m.id),
    });
  const askDeleteWrite = (w: ConnectorWriteMappingDTO) =>
    ask({
      title: `Delete the write mapping for ${objectLabel(objects, w.object_id)}?`,
      body: `Priority ${w.priority} is relinquished on the remote device.`,
      confirmLabel: "Delete mapping",
      onConfirm: () => deleteWrite.mutateAsync(w.id),
    });

  const s = status[String(connector.id)];
  const readSources = objects.filter((o) => !["schedule", "calendar", "notification-class"].includes(o.object_type));
  const writeSources = objects.filter((o) => o.object_type !== "notification-class");

  return (
    <>
      <PageHeader
        title={connector.name}
        sub={
          <>
            BACnet connector: device {connector.remote_device_instance}
            {connector.remote_address ? ` at ${connector.remote_address}` : " (found by Who-Is)"}, run by device{" "}
            {deviceId}.
          </>
        }
        actions={
          <Button
            variant="danger"
            onClick={() => {
              const total = reads.length + writes.length;
              ask({
                title: `Delete ${connector.name}?`,
                body: `This removes the connector and its ${total} ${total === 1 ? "mapping" : "mappings"}.`,
                confirmLabel: "Delete connector",
                onConfirm: () => deleteConnector.mutateAsync(),
              });
            }}
          >
            Delete connector
          </Button>
        }
      />
      <Banner notice={notice} />
      {dialog}

      <p>
        <ConnectorHealthPill status={s} enabled={connector.enabled} />
        {s?.bound_address && (
          <>
            {" "}
            bound to <code>{s.bound_address}</code>
          </>
        )}
        {s?.no_rpm && " · no ReadPropertyMultiple, reading one property at a time"}
        {s?.last_error && ` · ${s.last_error}`}
      </p>

      <BacnetSettingsForm
        connector={connector}
        action={updateConnector}
        onSave={(body, onSaved) => updateConnector.mutate(body, { onSuccess: onSaved })}
      />

      <h2>Read mappings ({reads.length})</h2>
      <p className="bp-muted">
        Polled every {connector.poll_interval_seconds} s at priority 10.
      </p>
      {objectsError ? (
        objectsNotLoaded
      ) : (
        <p>
          <Button type="button" aria-haspopup="dialog" onClick={() => setPanel({ kind: "add-read" })}>
            Add read mapping
          </Button>
        </p>
      )}
      {reads.length >= TOOLBAR_MIN_ROWS && (
        <TableToolbar
          search={readSearch}
          onSearch={setReadSearch}
          searchLabel="Search read mappings"
          count={countText(shownReads.length, reads.length, "read mapping")}
        />
      )}
      {readsError ? (
        <LoadError what="read mappings" error={readsError} onRetry={() => refetchReads()} />
      ) : reads.length === 0 ? (
        <p className="bp-muted">No read mappings yet.</p>
      ) : shownReads.length === 0 ? (
        <p className="bp-muted">No read mappings match.</p>
      ) : (
        <TableFrame label="Read mappings">
          <thead>
            <tr>
              <th>Remote point</th>
              <SortHeader label="Local object" sortKey="object" sort={readSort.sort} onSort={readSort.toggle} />
              <th>Mode</th>
              <th className="bp-cell-num">Scale</th>
              <th className="bp-cell-num">Offset</th>
              <th>Last value</th>
              <th>Status</th>
              <th>Detail</th>
              <th className="bp-cell-actions">
                <span className="bp-muted">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {shownReads.map((m) => {
              const error = s?.read_errors?.[String(m.id)];
              const forward = m.direction === "proxy" ? s?.last_forward?.[String(m.id)] : undefined;
              const detail =
                error ??
                (forward
                  ? `last write ${forward.value === null ? "relinquish" : String(forward.value)} @ ${forward.priority}: ${forward.error ?? "ok"}`
                  : null);
              return (
                <tr key={m.id} data-selected={(editing?.which === "read" && editing.id === m.id) || undefined}>
                  <td className="bp-cell-mono">
                    {formatPoint(m.remote_object_type, m.remote_object_instance, m.remote_property, m.remote_array_index)}
                  </td>
                  <td>{objectLabel(objects, m.object_id)}</td>
                  <td>{m.direction === "proxy" ? <StatusPill tone="info">proxy</StatusPill> : "read"}</td>
                  <td className="bp-cell-num">{m.scale}</td>
                  <td className="bp-cell-num">{m.offset}</td>
                  <MappingValueCell
                    live={lives.get(m.object_id)}
                    direction={m.direction}
                    obj={objects.find((o) => o.id === m.object_id)}
                  />
                  <td>
                    {/* A failed forward is an error too; Detail names which. */}
                    <ResultPill error={!error && forward?.error ? forward.error : error} />
                  </td>
                  <ClipCell value={detail} />
                  <RowActions onEdit={() => openEdit("read", m.id)} onDelete={() => askDeleteRead(m)} />
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}
      {/* Mounted once opened and kept while hidden or replaced: a half-typed add keeps its fields. */}
      <SidePanel open={panel?.kind === "add-read"} title="Add read mapping" onClose={() => setPanel(null)}>
        {objectsNotLoaded ?? (
          <MappingForm
            kind="read"
            objects={readSources}
            deviceId={deviceId}
            target={target}
            action={addRead}
            onSubmit={(body) => addRead.mutate(body)}
          />
        )}
      </SidePanel>

      <h2>Write mappings ({writes.length})</h2>
      <p className="bp-muted">
        Sent when the local value changes
        {connector.rewrite_interval_seconds > 0 ? ` and every ${connector.rewrite_interval_seconds} s` : ""}.
      </p>
      {objectsError ? (
        objectsNotLoaded
      ) : (
        <p>
          <Button type="button" aria-haspopup="dialog" onClick={() => setPanel({ kind: "add-write" })}>
            Add write mapping
          </Button>
        </p>
      )}
      {writes.length >= TOOLBAR_MIN_ROWS && (
        <TableToolbar
          search={writeSearch}
          onSearch={setWriteSearch}
          searchLabel="Search write mappings"
          count={countText(shownWrites.length, writes.length, "write mapping")}
        />
      )}
      {writesError ? (
        <LoadError what="write mappings" error={writesError} onRetry={() => refetchWrites()} />
      ) : writes.length === 0 ? (
        <p className="bp-muted">No write mappings yet.</p>
      ) : shownWrites.length === 0 ? (
        <p className="bp-muted">No write mappings match.</p>
      ) : (
        <TableFrame label="Write mappings">
          <thead>
            <tr>
              <SortHeader label="Local object" sortKey="object" sort={writeSort.sort} onSort={writeSort.toggle} />
              <th>Remote point</th>
              <th className="bp-cell-num">Priority</th>
              <th>Last sent (UTC)</th>
              <th>Status</th>
              <th>Detail</th>
              <th className="bp-cell-actions">
                <span className="bp-muted">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {shownWrites.map((w) => {
              const entry = s?.write_status?.[String(w.id)];
              return (
                <tr key={w.id} data-selected={(editing?.which === "write" && editing.id === w.id) || undefined}>
                  <td>{objectLabel(objects, w.object_id)}</td>
                  <td className="bp-cell-mono">
                    {formatPoint(w.remote_object_type, w.remote_object_instance, w.remote_property, w.remote_array_index)}
                  </td>
                  <td className="bp-cell-num">{w.priority}</td>
                  <td className="bp-cell-mono">{entry?.last_sent_at ? formatUtcBare(entry.last_sent_at) : <EmptyValue />}</td>
                  <td>
                    <ResultPill error={entry ? entry.error : undefined} />
                  </td>
                  <ClipCell value={entry?.error} />
                  <RowActions onEdit={() => openEdit("write", w.id)} onDelete={() => askDeleteWrite(w)} />
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}
      <SidePanel open={panel?.kind === "add-write"} title="Add write mapping" onClose={() => setPanel(null)}>
        {objectsNotLoaded ?? (
          <MappingForm
            kind="write"
            objects={writeSources}
            deviceId={deviceId}
            target={target}
            action={addWrite}
            onSubmit={(body) => addWrite.mutate(body)}
          />
        )}
      </SidePanel>

      <SidePanel
        open={panel?.kind === "edit" && (editingRead !== undefined || editingWrite !== undefined)}
        focusKey={kept ? `${kept.which}-${kept.id}` : undefined}
        title={editingWrite ? "Edit write mapping" : "Edit read mapping"}
        onClose={() => setPanel(null)}
      >
        {editingRead && (
          <MappingForm
            key={`read-${editingRead.id}`}
            kind="read"
            objects={readSources}
            deviceId={deviceId}
            target={target}
            initial={editingRead}
            action={updateRead}
            onSubmit={(body) => updateRead.mutate({ id: editingRead.id, body })}
            onCancel={endEdit}
          />
        )}
        {editingWrite && (
          <MappingForm
            key={`write-${editingWrite.id}`}
            kind="write"
            objects={writeSources}
            deviceId={deviceId}
            target={target}
            initial={editingWrite}
            action={updateWrite}
            onSubmit={(body) => updateWrite.mutate({ id: editingWrite.id, body })}
            onCancel={endEdit}
          />
        )}
      </SidePanel>
    </>
  );
}

type BacnetSettingsFields = {
  name: string;
  enabled: boolean;
  remoteInstance: string;
  remoteAddress: string;
  pollIntervalSeconds: string;
  timeoutSeconds: string;
  rewriteIntervalSeconds: string;
};

function savedBacnetSettings(connector: ConnectorDTO): BacnetSettingsFields {
  return {
    name: connector.name,
    enabled: connector.enabled,
    remoteInstance: String(connector.remote_device_instance ?? ""),
    remoteAddress: connector.remote_address ?? "",
    pollIntervalSeconds: String(connector.poll_interval_seconds),
    timeoutSeconds: String(connector.timeout_seconds),
    rewriteIntervalSeconds: String(connector.rewrite_interval_seconds),
  };
}

function BacnetSettingsForm({
  connector,
  action,
  onSave,
}: {
  connector: ConnectorDTO;
  action: ActionState;
  onSave: (body: Record<string, unknown>, onSaved: () => void) => void;
}) {
  // A refetch (after a save, or another tab's) refreshes only the fields the
  // user hasn't touched since the baseline.
  const { form: fields, setForm: setFields, baseline, setBaseline } = useDraft(connector, savedBacnetSettings);
  const set = <K extends keyof BacnetSettingsFields>(key: K, value: BacnetSettingsFields[K]) =>
    setFields((f) => ({ ...f, [key]: value }));

  const dirty = differs(fields, baseline);
  useUnsavedChanges(dirty, `/connectors/${connector.id}`);

  return (
    <Fieldset legend="Settings">
      <Form
        aria-label="Settings"
        onSubmit={(e) => {
          e.preventDefault();
          const sent = fields;
          onSave(
            {
              name: sent.name,
              enabled: sent.enabled,
              remote_device_instance: Number(sent.remoteInstance),
              remote_address: sent.remoteAddress.trim() || null,
              poll_interval_seconds: Number(sent.pollIntervalSeconds),
              timeout_seconds: Number(sent.timeoutSeconds),
              rewrite_interval_seconds: Number(sent.rewriteIntervalSeconds),
            },
            () => setBaseline(sent)
          );
        }}
      >
        <FormGrid>
          <Field label="Name">
            <input id="b_name" type="text" required value={fields.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Remote device instance">
            <input
              id="b_remote_instance"
              type="number"
              required
              min={0}
              max={4194302}
              value={fields.remoteInstance}
              onChange={(e) => set("remoteInstance", e.target.value)}
            />
          </Field>
          <Field label="Static address" help="Blank finds it by Who-Is.">
            <input
              id="b_remote_address"
              type="text"
              className="bp-input--mono"
              value={fields.remoteAddress}
              onChange={(e) => set("remoteAddress", e.target.value)}
            />
          </Field>
        </FormGrid>
        <CheckField label="Enabled" checked={fields.enabled} onChange={(e) => set("enabled", e.target.checked)} />
        <FormGrid>
          <Field label="Poll interval (s)">
            <input
              id="b_poll"
              type="number"
              required
              min={1}
              value={fields.pollIntervalSeconds}
              onChange={(e) => set("pollIntervalSeconds", e.target.value)}
            />
          </Field>
          <Field label="Timeout (s)">
            <input
              id="b_timeout"
              type="number"
              required
              min={1}
              value={fields.timeoutSeconds}
              onChange={(e) => set("timeoutSeconds", e.target.value)}
            />
          </Field>
          <Field label="Re-write every (s)" help="0 writes only on change.">
            <input
              id="b_rewrite"
              type="number"
              required
              min={0}
              value={fields.rewriteIntervalSeconds}
              onChange={(e) => set("rewriteIntervalSeconds", e.target.value)}
            />
          </Field>
        </FormGrid>
        <FormActions>
          <Button variant="primary" type="submit">
            Save
          </Button>
        </FormActions>
        <ActionStatus
          action={action}
          failed="Couldn't save"
          done="Saved."
          hideDone={dirty}
        />
      </Form>
    </Fieldset>
  );
}

function MappingForm({
  kind,
  objects,
  deviceId,
  target,
  initial,
  action,
  onSubmit,
  onCancel,
}: {
  kind: "read" | "write";
  objects: BacnetObjectDTO[];
  deviceId: number;
  target: RemoteTarget;
  // Set when editing: the mapping's current fields. Its local object can't
  // change (the API's update takes no object_id) -- delete and re-add for that.
  initial?: ConnectorMappingDTO | ConnectorWriteMappingDTO;
  action: ActionState;
  onSubmit: (body: Record<string, unknown>) => void;
  onCancel?: () => void;
}) {
  const editing = initial !== undefined;
  const noun = kind === "read" ? "read mapping" : "write mapping";
  const title = editing ? `Edit ${noun}` : `Add ${noun}`;
  const readInitial = kind === "read" ? (initial as ConnectorMappingDTO | undefined) : undefined;
  const writeInitial = kind === "write" ? (initial as ConnectorWriteMappingDTO | undefined) : undefined;
  const [direction, setDirection] = useState<"read" | "proxy">(readInitial?.direction ?? "read");
  const proxy = kind === "read" && direction === "proxy";
  // Adding a read mapping: an object another mapping feeds can't take a second one.
  const taken = (o: BacnetObjectDTO) => kind === "read" && !editing && o.fed_by_connector_id != null;
  // A proxy mirrors a priority array: only commandable objects have one.
  const unfit = (o: BacnetObjectDTO) => proxy && !isCommandable(o.object_type);
  const usable = (o: BacnetObjectDTO) => !taken(o) && !unfit(o);
  const firstFree = objects.find(usable)?.id ?? 0;
  // Adding a proxy with every commandable object fed already, or none at all.
  const noneFree = !editing && !objects.some(usable);
  const [objectId, setObjectId] = useState(initial?.object_id ?? firstFree);
  // The fields as they start, so an edit form knows when it holds an unsaved change.
  const [start] = useState(() => ({
    direction: readInitial?.direction ?? "read",
    remoteType: initial?.remote_object_type ?? (kind === "read" ? "analog-input" : "analog-value"),
    remoteInstance: String(initial?.remote_object_instance ?? 1),
    remoteProperty: initial?.remote_property ?? "present-value",
    scale: String(readInitial?.scale ?? "1.0"),
    offset: String(readInitial?.offset ?? "0.0"),
    priority: String(writeInitial?.priority ?? 8),
    valueMap: formatValueMap(readInitial?.value_map ?? null),
  }));
  const [remoteType, setRemoteType] = useState(start.remoteType);
  const [remoteInstance, setRemoteInstance] = useState(start.remoteInstance);
  const [remoteProperty, setRemoteProperty] = useState(start.remoteProperty);
  const [scale, setScale] = useState(start.scale);
  const [offset, setOffset] = useState(start.offset);
  const [priority, setPriority] = useState(start.priority);
  const [valueMap, setValueMap] = useState(start.valueMap);
  const [formError, setFormError] = useState<string | null>(null);

  const dirty =
    editing &&
    differs({ direction, remoteType, remoteInstance, remoteProperty, scale, offset, priority, valueMap }, start);
  useUnsavedChanges(dirty, `/connectors/${initial?.connector_id}`);

  useEffect(() => {
    if (editing) return;
    const current = objects.find((o) => o.id === objectId);
    if (!current || !usable(current)) setObjectId(firstFree);
  }, [objects, objectId, editing, firstFree, direction]);

  if (objects.length === 0) return <p className="bp-muted">This device has no objects to map.</p>;
  if (kind === "read" && !editing && objects.every(taken)) {
    return <p className="bp-muted">Every object already has a read mapping.</p>;
  }

  const id = (field: string) => `${kind}_${editing ? "edit" : "add"}_${field}`;
  return (
    <>
      <Form
        aria-label={title}
        onSubmit={(e) => {
          e.preventDefault();
          setFormError(null);
          const body: Record<string, unknown> = {
            ...(editing ? {} : { object_id: objectId }),
            remote_object_type: remoteType,
            remote_object_instance: Number(remoteInstance),
            remote_property: remoteProperty,
          };
          if (kind === "read") {
            body.direction = direction;
            if (proxy) {
              const chosen = objects.find((o) => o.id === objectId);
              if (chosen && !isCommandable(chosen.object_type)) {
                setFormError("A proxy needs a commandable local object.");
                return;
              }
              body.remote_property = "present-value";
              body.scale = 1;
              body.offset = 0;
              body.value_map = null;
            } else {
              body.scale = Number(scale);
              body.offset = Number(offset);
              const map = parseValueMap(valueMap);
              if (map === null) {
                setFormError(
                  "Value map: use name=number pairs, e.g. active=1, inactive=0"
                );
                return;
              }
              if (map !== undefined) body.value_map = map;
              else if (editing) body.value_map = null;
            }
          } else {
            body.priority = Number(priority);
          }
          onSubmit(body);
        }}
      >
        <FormGrid>
          {kind === "read" && (
            <Field label="Mode">
              <select
                id={id("direction")}
                value={direction}
                onChange={(e) => setDirection(e.target.value as "read" | "proxy")}
              >
                <option value="read">Read only</option>
                <option value="proxy">Proxy (read + write)</option>
              </select>
            </Field>
          )}
          <Field label="Local object">
            <select
              id={id("object")}
              value={objectId}
              disabled={editing}
              onChange={(e) => setObjectId(Number(e.target.value))}
            >
              {objects.map((o) => (
                <option key={o.id} value={o.id} disabled={taken(o) || unfit(o)}>
                  {o.name} ({o.object_type}:{o.instance_number})
                  {taken(o) ? ` (fed by ${o.fed_by_connector_name ?? `connector ${o.fed_by_connector_id}`})` : ""}
                  {unfit(o) && !taken(o) ? " (not commandable)" : ""}
                </option>
              ))}
            </select>
          </Field>
        </FormGrid>
        <FormGrid>
          <Field label="Remote object type">
            <input id={id("type")} type="text" value={remoteType} onChange={(e) => setRemoteType(e.target.value)} />
          </Field>
          <Field label="Remote instance">
            <input
              id={id("instance")}
              type="number"
              required
              min={0}
              value={remoteInstance}
              onChange={(e) => setRemoteInstance(e.target.value)}
            />
          </Field>
          <Field label="Remote property">
            <input
              id={id("property")}
              type="text"
              value={proxy ? "present-value" : remoteProperty}
              disabled={proxy}
              onChange={(e) => setRemoteProperty(e.target.value)}
            />
          </Field>
        </FormGrid>
        {kind === "read" ? (
          <FormGrid>
            <Field label="Scale">
              <input id={id("scale")} type="number" step="any" required disabled={proxy} value={proxy ? "1" : scale} onChange={(e) => setScale(e.target.value)} />
            </Field>
            <Field label="Offset">
              <input id={id("offset")} type="number" step="any" required disabled={proxy} value={proxy ? "0" : offset} onChange={(e) => setOffset(e.target.value)} />
            </Field>
            <Field label="Value map" help="e.g. active=1, inactive=0">
              <input
                id={id("value_map")}
                type="text"
                disabled={proxy}
                value={proxy ? "" : valueMap}
                onChange={(e) => {
                  setValueMap(e.target.value);
                  setFormError(null);
                }}
              />
            </Field>
          </FormGrid>
        ) : (
          <Field label="Priority">
            <input
              id={id("priority")}
              type="number"
              required
              min={1}
              max={16}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
          </Field>
        )}

        <RemotePointPicker
          deviceId={deviceId}
          target={target}
          onPick={(o) => {
            setRemoteType(o.object_type);
            setRemoteInstance(String(o.instance));
          }}
        />
        {proxy && noneFree && (
          <p className="bp-muted">No commandable object is free for a proxy.</p>
        )}
        <FormActions>
          <Button variant="primary" type="submit" disabled={proxy && noneFree}>
            {editing ? `Save ${noun}` : title}
          </Button>
          {onCancel && (
            <Button variant="ghost" type="button" onClick={onCancel}>
              Cancel
            </Button>
          )}
        </FormActions>
        {formError && <ErrorBlock lead="Can't save:">{formError}</ErrorBlock>}
        <ActionStatus
          action={action}
          failed={editing ? "Couldn't save the mapping" : "Couldn't add the mapping"}
          done={editing ? undefined : "Mapping added."}
        />
      </Form>
    </>
  );
}
