import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectorsApi, fetchAllObjectsWithDevice } from "../api/endpoints";
import type {
  AuthType,
  ConnectorDTO,
  ConnectorMappingDTO,
  ConnectorPreviewDTO,
  ConnectorPreviewField,
  ObjectWithDevice,
  ResponseFormat,
} from "../api/types";
import Banner, { LoadError, errorMessage, type Notice } from "../components/Banner";
import VerifyTlsField from "../components/VerifyTlsField";
import HeadersEditor, { headersBody, savedHeaderRows } from "../components/HeadersEditor";
import ConnectorHealthPill from "../components/ConnectorHealth";
import Button from "../components/Button";
import PageHeader from "../components/PageHeader";
import UrlText from "../components/UrlText";
import SidePanel from "../components/SidePanel";
import Callout from "../components/Callout";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid } from "../components/Form";
import { ActionStatus, Loading, type ActionState } from "../components/AsyncState";
import { LEAVE_UNSAVED, differs, rebase, useUnsavedChanges } from "../components/UnsavedChanges";
import { useConfirm } from "../components/ConfirmDialog";
import { ClipCell, RowActions, SortHeader, TableFrame } from "../components/DataTable";
import { ResultPill } from "../components/StatusPill";
import MappingValueCell from "../components/MappingValueCell";
import { useDeviceLives } from "../utils/useLiveState";
import { urlClearsCredentials } from "../utils/credentialsFollow";
import TableToolbar, { TOOLBAR_MIN_ROWS, countText, toolbarSearch } from "../components/TableToolbar";
import { parseId } from "../utils/ids";
import { matchesSearch, sortRows, useSort } from "../utils/tableSort";

// The JSON path picker's live preview is deliberately independent of the
// connector's own poll_interval_seconds -- a fixed cadence while this page is
// left open with the toggle on, not a per-connector setting.
const PREVIEW_INTERVAL_MS = 30_000;

// Same rule as the BACnet connector page and the API: these have no
// Present_Value a feed can set.
const NOT_MAPPABLE = ["schedule", "calendar", "notification-class"];

const ALL_FED = "Every object already has a mapping.";

export default function ConnectorDetail() {
  const { connectorId: connectorIdParam } = useParams<{ connectorId: string }>();
  // ConnectorPage renders this only once the id parsed.
  const connectorId = parseId(connectorIdParam)!;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const [previewOn, setPreviewOn] = useState(false);
  const [preview, setPreview] = useState<ConnectorPreviewDTO | null>(null);
  // Bumped by a settings save, so the picker re-fetches from the saved URL/format.
  const [previewNonce, setPreviewNonce] = useState(0);
  // One mapping panel at a time: Add and a row's Edit replace each other.
  const [panel, setPanel] = useState<{ kind: "add" } | { kind: "edit" } | null>(null);
  // The mapping whose edit form exists. Closing the panel only hides it, so a
  // half-typed edit survives; Cancel, a save, or another row's Edit end it.
  const [editId, setEditId] = useState<number | null>(null);
  const editingId = panel?.kind === "edit" ? editId : null;
  const endEdit = () => {
    setPanel(null);
    setEditId(null);
  };
  const [search, setSearch] = useState("");
  const { sort, toggle } = useSort<"object">();
  const { ask, dialog } = useConfirm();

  const { data: connector } = useQuery({
    queryKey: ["connector", connectorId],
    queryFn: () => connectorsApi.get(connectorId),
  });
  const { data: mappings = [], error: mappingsError, refetch: refetchMappings } = useQuery({
    queryKey: ["connectorMappings", connectorId],
    queryFn: () => connectorsApi.mappings.list(connectorId),
  });
  const { data: status = {} } = useQuery({
    queryKey: ["connectorStatus"],
    queryFn: connectorsApi.status,
    refetchInterval: 5000,
  });
  const {
    data: allObjects = [],
    error: objectsError,
    isPending: objectsPending,
    refetch: refetchObjects,
  } = useQuery({
    queryKey: ["allObjectsWithDevice"],
    queryFn: fetchAllObjectsWithDevice,
  });
  const objectsById = useMemo(() => new Map(allObjects.map((o) => [o.id, o])), [allObjects]);
  // A connector's mappings can feed objects on several devices: one live
  // read per device.
  const mappedDeviceIds = [
    ...new Set(
      mappings
        .map((m) => objectsById.get(m.object_id)?.device_id)
        .filter((id): id is number => id !== undefined)
    ),
  ];
  const lives = useDeviceLives(mappedDeviceIds);

  // Populates the JSON path picker. Deliberately NOT gated on previewOn, so
  // browsing paths needs no live polling. The cost is one real outbound request
  // per connector-detail page view (an on-demand button was the alternative).
  useEffect(() => {
    setPreview(null);
    let cancelled = false;
    connectorsApi
      .preview(connectorId)
      .then((result) => {
        if (!cancelled) setPreview(result);
      })
      .catch((err) => {
        if (!cancelled) setPreview({ ok: false, error: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [connectorId, previewNonce]);

  // Live preview only repeats the fetch for a live view; the picker's data
  // comes from the effect above. Kept out of that effect so un-ticking the box
  // doesn't fire a pointless request.
  useEffect(() => {
    if (!previewOn) {
      return;
    }
    let cancelled = false;
    const fetchPreview = () => {
      connectorsApi
        .preview(connectorId)
        .then((result) => {
          if (!cancelled) setPreview(result);
        })
        .catch((err) => {
          if (!cancelled) setPreview({ ok: false, error: errorMessage(err) });
        });
    };
    fetchPreview();
    const interval = setInterval(fetchPreview, PREVIEW_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [previewOn, connectorId]);

  const invalidateMappings = () =>
    queryClient.invalidateQueries({ queryKey: ["connectorMappings", connectorId] });

  const updateConnector = useMutation({
    mutationFn: (body: Record<string, unknown>) => connectorsApi.update(connectorId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connector", connectorId] });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      setPreviewNonce((n) => n + 1);
    },
  });

  const deleteConnector = useMutation({
    mutationFn: () => connectorsApi.remove(connectorId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      // The delete dialog was the question; the guard doesn't ask again.
      navigate("/connectors", { state: LEAVE_UNSAVED });
    },
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
  });

  const createMapping = useMutation({
    mutationFn: (body: Record<string, unknown>) => connectorsApi.mappings.create(connectorId, body),
    onSuccess: () => {
      invalidateMappings();
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      // The objects carry fed_by_connector_*, which the add form greys out by.
      queryClient.invalidateQueries({ queryKey: ["allObjectsWithDevice"] });
    },
  });

  const updateMapping = useMutation({
    mutationFn: ({ mappingId, body }: { mappingId: number; body: Record<string, unknown> }) =>
      connectorsApi.mappings.update(connectorId, mappingId, body),
    onSuccess: () => {
      invalidateMappings();
      endEdit();
    },
  });

  const deleteMapping = useMutation({
    mutationFn: (mappingId: number) => connectorsApi.mappings.remove(connectorId, mappingId),
    onSuccess: () => {
      invalidateMappings();
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      queryClient.invalidateQueries({ queryKey: ["allObjectsWithDevice"] });
      setNotice({ type: "ok", message: "mapping deleted" });
    },
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
  });

  if (!connector) return <Loading what="connector" />;

  const s = status[String(connector.id)];
  const readErrorCount = Object.values(s?.read_errors ?? {}).filter(Boolean).length;
  const mappedObjectIds = new Set(mappings.map((m) => m.object_id));
  const availableObjects = allObjects.filter(
    (o) => !mappedObjectIds.has(o.id) && !NOT_MAPPABLE.includes(o.object_type)
  );
  // An object another connector feeds is listed, greyed out, but can't be picked.
  const freeObjects = availableObjects.filter((o) => o.fed_by_connector_id == null);
  const previewFields: ConnectorPreviewField[] =
    preview?.ok && preview.response_format === "json" ? preview.fields : [];
  const label = (m: ConnectorMappingDTO) => describeObject(objectsById, m.object_id);
  const shown = sortRows(
    mappings.filter((m) => matchesSearch(toolbarSearch(search, mappings.length), label(m), m.json_path)),
    sort,
    { object: label }
  );
  const editing = mappings.find((m) => m.id === editId);
  const askDelete = (m: ConnectorMappingDTO) =>
    ask({
      title: `Delete the mapping for ${label(m)}?`,
      body: "The object stops being fed.",
      confirmLabel: "Delete mapping",
      onConfirm: () => deleteMapping.mutateAsync(m.id),
    });

  return (
    <>
      <PageHeader
        title={connector.name}
        sub={<UrlText url={connector.url} />}
        actions={
          <Button
            variant="danger"
            onClick={() =>
              ask({
                title: `Delete ${connector.name}?`,
                body: `This removes the connector and its ${mappings.length} ${mappings.length === 1 ? "mapping" : "mappings"}.`,
                confirmLabel: "Delete connector",
                onConfirm: () => deleteConnector.mutateAsync(),
              })
            }
          >
            Delete connector
          </Button>
        }
      />
      <Banner notice={notice} />
      {dialog}

      <p>
        <ConnectorHealthPill status={s} enabled={connector.enabled} />
        {s?.last_error && <> {s.last_error}</>}
        {readErrorCount > 0 && (
          <>
            {" "}
            · {readErrorCount} {readErrorCount === 1 ? "mapping has" : "mappings have"} a read error, shown below
          </>
        )}
      </p>

      <ConnectorSettingsForm
        connector={connector}
        action={updateConnector}
        onSave={(body, onSaved) => updateConnector.mutate(body, { onSuccess: onSaved })}
      />

      <h2>Mappings ({mappings.length})</h2>
      <CheckField label="Live preview" checked={previewOn} onChange={(e) => setPreviewOn(e.target.checked)} />
      {previewOn && (
        <Callout tone="warn">
          Polling <UrlText url={connector.url} /> every 30s while this page is open.
        </Callout>
      )}
      {previewOn && preview && !preview.ok && <p className="bp-muted">preview failed: {preview.error}</p>}
      {previewOn && preview?.ok && preview.response_format === "plain-text" && (
        <p className="bp-muted">
          Current response: <code>{preview.raw_text}</code>
        </p>
      )}

      {objectsError ? (
        <LoadError what="objects" error={objectsError} onRetry={() => refetchObjects()} />
      ) : objectsPending ? (
        <Loading what="objects" />
      ) : freeObjects.length > 0 ? (
        <p>
          <Button type="button" aria-haspopup="dialog" onClick={() => setPanel({ kind: "add" })}>
            Add mapping
          </Button>
        </p>
      ) : panel?.kind === "add" ? null : (
        // The open Add mapping panel says it instead.
        <p className="bp-muted">{ALL_FED}</p>
      )}

      {mappings.length >= TOOLBAR_MIN_ROWS && (
        <TableToolbar
          search={search}
          onSearch={setSearch}
          searchLabel="Search mappings"
          count={countText(shown.length, mappings.length, "mapping")}
        />
      )}
      {mappingsError ? (
        <LoadError what="mappings" error={mappingsError} onRetry={() => refetchMappings()} />
      ) : mappings.length === 0 ? (
        <p className="bp-muted">No mappings yet.</p>
      ) : shown.length === 0 ? (
        <p className="bp-muted">No mappings match.</p>
      ) : (
        <TableFrame label="Mappings">
          <thead>
            <tr>
              <SortHeader label="Object" sortKey="object" sort={sort} onSort={toggle} />
              <th>JSON path</th>
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
            {shown.map((m) => {
              const readError = s?.read_errors?.[String(m.id)];
              return (
                <tr key={m.id} data-selected={editingId === m.id || undefined}>
                  <td>{label(m)}</td>
                  <ClipCell value={m.json_path} />
                  <td className="bp-cell-num">{m.scale}</td>
                  <td className="bp-cell-num">{m.offset}</td>
                  <MappingValueCell
                    live={lives.get(m.object_id)}
                    direction="read"
                    obj={objectsById.get(m.object_id)}
                  />
                  <td>
                    <ResultPill error={readError} />
                  </td>
                  <ClipCell value={readError} />
                  <RowActions
                    onEdit={() => {
                      setEditId(m.id);
                      setPanel({ kind: "edit" });
                    }}
                    onDelete={() => askDelete(m)}
                  />
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}

      <SidePanel
        open={panel?.kind === "edit" && editing !== undefined}
        focusKey={editId ?? undefined}
        title={editing ? `Edit mapping ${label(editing)}` : "Edit mapping"}
        onClose={() => setPanel(null)}
      >
        {editing && (
          <EditMappingForm
            key={editing.id}
            mapping={editing}
            objectLabel={label(editing)}
            plainText={connector.response_format === "plain-text"}
            previewFields={previewFields}
            action={updateMapping}
            onSave={(body) => updateMapping.mutate({ mappingId: editing.id, body })}
            onCancel={endEdit}
          />
        )}
      </SidePanel>

      {/* Mounted once opened and kept while hidden or replaced by an Edit: a half-typed add keeps its fields. */}
      <SidePanel open={panel?.kind === "add"} title="Add mapping" onClose={() => setPanel(null)}>
        {freeObjects.length > 0 ? (
          <AddMappingForm
            objects={availableObjects}
            plainText={connector.response_format === "plain-text"}
            previewFields={previewFields}
            action={createMapping}
            onCreate={(body) => createMapping.mutateAsync(body)}
          />
        ) : (
          <p className="bp-muted">{ALL_FED}</p>
        )}
      </SidePanel>
    </>
  );
}

function describeObject(objectsById: Map<number, ObjectWithDevice>, objectId: number): string {
  const o = objectsById.get(objectId);
  if (!o) return `object ${objectId}`;
  return `${o.deviceName} / ${o.name} (${o.object_type}:${o.instance_number})`;
}

type SettingsFields = {
  name: string;
  url: string;
  enabled: boolean;
  verifyTls: boolean;
  pollIntervalSeconds: string;
  timeoutSeconds: string;
  responseFormat: ResponseFormat;
  authType: AuthType;
  authUsername: string;
};

function savedSettings(connector: ConnectorDTO): SettingsFields {
  return {
    name: connector.name,
    url: connector.url,
    enabled: connector.enabled,
    verifyTls: connector.verify_tls,
    pollIntervalSeconds: String(connector.poll_interval_seconds),
    timeoutSeconds: String(connector.timeout_seconds),
    responseFormat: connector.response_format,
    authType: connector.auth_type,
    authUsername: connector.auth_username ?? "",
  };
}

function ConnectorSettingsForm({
  connector,
  action,
  onSave,
}: {
  connector: ConnectorDTO;
  action: ActionState;
  onSave: (body: Record<string, unknown>, onSaved: () => void) => void;
}) {
  const [fields, setFields] = useState(() => savedSettings(connector));
  // What the server last confirmed: the loaded connector, or what a save just sent.
  const [baseline, setBaseline] = useState(() => savedSettings(connector));
  const [authPassword, setAuthPassword] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [headers, setHeaders] = useState(() => savedHeaderRows(connector.header_names));
  const [headersBaseline, setHeadersBaseline] = useState(() => savedHeaderRows(connector.header_names));
  const set = <K extends keyof SettingsFields>(key: K, value: SettingsFields[K]) =>
    setFields((f) => ({ ...f, [key]: value }));
  const missing = connector.missing_credentials;
  const unreadable = connector.unreadable_credentials ?? [];
  const hasSavedCredentials =
    (connector.auth_type === "basic" && !missing.includes("password")) ||
    (connector.auth_type === "bearer" && !missing.includes("token")) ||
    connector.header_names.some((name) => !missing.includes(`header ${name}`));
  const clearing = hasSavedCredentials && urlClearsCredentials(baseline.url, fields.url);
  const credentialHelp = (label: string, noun: string) => {
    if (unreadable.includes(label)) return `Saved ${noun} can't be read. Enter it again.`;
    if (missing.includes(label)) return "Not set.";
    return clearing ? `Blank leaves the ${noun} unset.` : `Blank keeps the saved ${noun}.`;
  };

  // A refetch (after a save, or another tab's) refreshes only what the user
  // hasn't touched since the baseline.
  const latest = useRef({ fields, baseline, headers, headersBaseline });
  latest.current = { fields, baseline, headers, headersBaseline };
  useEffect(() => {
    const now = latest.current;
    const fresh = savedSettings(connector);
    setFields(rebase(now.fields, now.baseline, fresh));
    setBaseline(fresh);
    const freshHeaders = savedHeaderRows(connector.header_names);
    if (!differs(now.headers, now.headersBaseline)) setHeaders(freshHeaders);
    setHeadersBaseline(freshHeaders);
  }, [connector]);

  const dirty = differs(fields, baseline) || differs(headers, headersBaseline) || authPassword !== "" || authToken !== "";
  useUnsavedChanges(dirty, `/connectors/${connector.id}`);

  return (
    <Fieldset legend="Settings">
      <Form
        aria-label="Settings"
        onSubmit={(e) => {
          e.preventDefault();
          const sent = fields;
          const body: Record<string, unknown> = {
            name: sent.name,
            url: sent.url,
            enabled: sent.enabled,
            poll_interval_seconds: Number(sent.pollIntervalSeconds),
            timeout_seconds: Number(sent.timeoutSeconds),
            response_format: sent.responseFormat,
            auth_type: sent.authType,
            verify_tls: sent.verifyTls,
          };
          // Leave blank to keep the current secret -- omit the key entirely
          // rather than sending "" (which the JSON API would treat as a
          // deliberate blank-out, see the API's exclude_unset contract).
          if (sent.authType === "basic") {
            body.auth_username = sent.authUsername || null;
            if (authPassword) body.auth_password = authPassword;
          }
          if (sent.authType === "bearer" && authToken) body.auth_token = authToken;
          body.headers = headersBody(headers);
          // What was typed while the save was in flight stays; only what it sent is cleared.
          const sentPassword = authPassword;
          const sentToken = authToken;
          const sentHeaders = headers;
          onSave(body, () => {
            setBaseline(sent);
            setAuthPassword((typed) => (typed === sentPassword ? "" : typed));
            setAuthToken((typed) => (typed === sentToken ? "" : typed));
            const saved = savedHeaderRows(Object.keys(body.headers as object));
            setHeaders((current) => (differs(current, sentHeaders) ? current : saved));
            setHeadersBaseline(saved);
          });
        }}
      >
        <FormGrid>
          <Field label="Name">
            <input id="c_name" type="text" required value={fields.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="URL">
            <input
              id="c_url"
              type="text"
              className="bp-input--mono"
              required
              value={fields.url}
              onChange={(e) => set("url", e.target.value)}
            />
          </Field>
        </FormGrid>
        {clearing && (
          <Callout tone="warn">
            Saving this URL clears the saved password, token and header values: they aren't sent to a different host,
            port or scheme. Enter them again here.
          </Callout>
        )}
        <CheckField label="Enabled" checked={fields.enabled} onChange={(e) => set("enabled", e.target.checked)} />
        <FormGrid>
          <Field label="Poll interval (s)">
            <input
              id="c_poll"
              type="number"
              required
              min={1}
              value={fields.pollIntervalSeconds}
              onChange={(e) => set("pollIntervalSeconds", e.target.value)}
            />
          </Field>
          <Field label="Timeout (s)">
            <input
              id="c_timeout"
              type="number"
              required
              min={1}
              value={fields.timeoutSeconds}
              onChange={(e) => set("timeoutSeconds", e.target.value)}
            />
          </Field>
          <Field label="Response format">
            <select
              id="c_format"
              value={fields.responseFormat}
              onChange={(e) => set("responseFormat", e.target.value as ResponseFormat)}
            >
              <option value="json">JSON</option>
              <option value="plain-text">Plain text</option>
            </select>
          </Field>
        </FormGrid>
        <FormGrid>
          <Field label="Auth">
            <select id="c_auth_type" value={fields.authType} onChange={(e) => set("authType", e.target.value as AuthType)}>
              <option value="none">None</option>
              <option value="basic">Basic</option>
              <option value="bearer">Bearer token</option>
            </select>
          </Field>
          {fields.authType === "basic" && (
            <>
              <Field label="Username">
                <input
                  id="c_auth_username"
                  type="text"
                  value={fields.authUsername}
                  onChange={(e) => set("authUsername", e.target.value)}
                />
              </Field>
              <Field label="Password" help={credentialHelp("password", "password")}>
                <input
                  id="c_auth_password"
                  type="password"
                  value={authPassword}
                  onChange={(e) => setAuthPassword(e.target.value)}
                />
              </Field>
            </>
          )}
          {fields.authType === "bearer" && (
            <Field label="Token" help={credentialHelp("token", "token")}>
              <input id="c_auth_token" type="password" value={authToken} onChange={(e) => setAuthToken(e.target.value)} />
            </Field>
          )}
        </FormGrid>
        <VerifyTlsField id="c_verify_tls" checked={fields.verifyTls} onChange={(verifyTls) => set("verifyTls", verifyTls)} />
        <Fieldset legend="Headers">
          <HeadersEditor rows={headers} onChange={setHeaders} missing={missing} unreadable={unreadable} clearing={clearing} />
        </Fieldset>
        <FormActions>
          <Button variant="primary" type="submit">
            Save
          </Button>
        </FormActions>
        <ActionStatus action={action} failed="Couldn't save" done="Saved." hideDone={dirty} />
      </Form>
    </Fieldset>
  );
}

export function JsonPathPicker({
  fields,
  onPick,
}: {
  fields: ConnectorPreviewField[];
  onPick: (path: string) => void;
}) {
  if (fields.length === 0) return null;
  return (
    <select
      className="bp-select"
      aria-label="Pick a JSON path from the preview"
      value=""
      onChange={(e) => {
        if (e.target.value) onPick(e.target.value);
      }}
    >
      <option value="">pick from preview...</option>
      {fields.map((f) => (
        <option key={f.path} value={f.path} disabled={f.is_array}>
          {f.is_array ? `${f.path} (an array, not selectable)` : `${f.path}: ${JSON.stringify(f.value)}`}
        </option>
      ))}
    </select>
  );
}

function EditMappingForm({
  mapping,
  objectLabel,
  plainText,
  previewFields,
  action,
  onSave,
  onCancel,
}: {
  mapping: ConnectorMappingDTO;
  objectLabel: string;
  plainText: boolean;
  previewFields: ConnectorPreviewField[];
  action: ActionState;
  onSave: (body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const [jsonPath, setJsonPath] = useState(mapping.json_path ?? "");
  const [scale, setScale] = useState(String(mapping.scale));
  const [offset, setOffset] = useState(String(mapping.offset));
  const [error, setError] = useState<string | null>(null);
  const dirty = differs(
    { jsonPath, scale, offset },
    { jsonPath: mapping.json_path ?? "", scale: String(mapping.scale), offset: String(mapping.offset) }
  );
  useUnsavedChanges(dirty, `/connectors/${mapping.connector_id}`);
  const title = `Edit mapping for ${objectLabel}`;
  const scaleEmpty = scale.trim() === "";
  return (
    <>
      <Form
        aria-label={title}
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          // Number("") is 0: a cleared scale would zero every value fed.
          if (scaleEmpty || offset.trim() === "") {
            setError("Scale and offset need a number");
            return;
          }
          setError(null);
          onSave({ json_path: jsonPath || null, scale: Number(scale), offset: Number(offset) });
        }}
      >
        <FormGrid>
          <Field label="JSON path">
            <input
              id="em_json_path"
              type="text"
              className="bp-input--mono"
              placeholder={plainText ? "n/a (plain text)" : "data.temperature"}
              value={jsonPath}
              onChange={(e) => setJsonPath(e.target.value)}
            />
          </Field>
          <Field label="Scale" error={error && scaleEmpty ? error : null}>
            <input id="em_scale" type="number" step="any" value={scale} onChange={(e) => setScale(e.target.value)} />
          </Field>
          <Field label="Offset" error={error && !scaleEmpty ? error : null}>
            <input id="em_offset" type="number" step="any" value={offset} onChange={(e) => setOffset(e.target.value)} />
          </Field>
        </FormGrid>
        {!plainText && <JsonPathPicker fields={previewFields} onPick={setJsonPath} />}
        <FormActions>
          <Button variant="primary" type="submit">
            Save mapping
          </Button>
          <Button variant="ghost" type="button" onClick={onCancel}>
            Cancel
          </Button>
        </FormActions>
        <ActionStatus action={action} failed="Couldn't save the mapping" />
      </Form>
    </>
  );
}

function AddMappingForm({
  objects,
  plainText,
  previewFields,
  action,
  onCreate,
}: {
  objects: ObjectWithDevice[];
  plainText: boolean;
  previewFields: ConnectorPreviewField[];
  action: ActionState;
  onCreate: (body: Record<string, unknown>) => Promise<unknown>;
}) {
  const firstFree = objects.find((o) => o.fed_by_connector_id == null)?.id ?? 0;
  const [objectId, setObjectId] = useState(firstFree);
  const [jsonPath, setJsonPath] = useState("");
  const [scale, setScale] = useState("1.0");
  const [offset, setOffset] = useState("0.0");

  useEffect(() => {
    const current = objects.find((o) => o.id === objectId);
    if (!current || current.fed_by_connector_id != null) setObjectId(firstFree);
  }, [objects, objectId, firstFree]);

  return (
    <Form
      aria-label="Add mapping"
      onSubmit={(e) => {
        e.preventDefault();
        onCreate({
          object_id: objectId,
          json_path: jsonPath || undefined,
          scale: Number(scale),
          offset: Number(offset),
        }).then(
          () => {
            setJsonPath("");
            setScale("1.0");
            setOffset("0.0");
          },
          // The error shows under the button; keep what was entered.
          () => {}
        );
      }}
    >
      <FormGrid>
        <Field label="Object">
          <select id="m_object" value={objectId} onChange={(e) => setObjectId(Number(e.target.value))}>
            {objects.map((o) => (
              <option key={o.id} value={o.id} disabled={o.fed_by_connector_id != null}>
                {o.deviceName} / {o.name} ({o.object_type}:{o.instance_number})
                {o.fed_by_connector_id != null
                  ? ` (fed by ${o.fed_by_connector_name ?? `connector ${o.fed_by_connector_id}`})`
                  : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="JSON path">
          <input
            id="m_json_path"
            type="text"
            className="bp-input--mono"
            placeholder={plainText ? "n/a (plain text)" : "data.temperature"}
            value={jsonPath}
            onChange={(e) => setJsonPath(e.target.value)}
          />
        </Field>
        <Field label="Scale">
          <input id="m_scale" type="number" step="any" required value={scale} onChange={(e) => setScale(e.target.value)} />
        </Field>
        <Field label="Offset">
          <input id="m_offset" type="number" step="any" required value={offset} onChange={(e) => setOffset(e.target.value)} />
        </Field>
      </FormGrid>
      {!plainText && <JsonPathPicker fields={previewFields} onPick={setJsonPath} />}
      <FormActions>
        <Button variant="primary" type="submit">
          Add mapping
        </Button>
      </FormActions>
      <ActionStatus action={action} failed="Couldn't add the mapping" done="Mapping added." />
    </Form>
  );
}
