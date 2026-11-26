import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectorsApi, devicesApi } from "../api/endpoints";
import type { AuthType, ConnectorDTO, ConnectorType, DeviceSummary, ResponseFormat } from "../api/types";
import Banner, { LoadError, errorMessage, type Notice } from "../components/Banner";
import ConnectorHealthPill from "../components/ConnectorHealth";
import VerifyTlsField from "../components/VerifyTlsField";
import HeadersEditor, { headersBody, type HeaderRow } from "../components/HeadersEditor";
import Button from "../components/Button";
import PageHeader from "../components/PageHeader";
import Callout from "../components/Callout";
import SidePanel from "../components/SidePanel";
import { Field, Fieldset, Form, FormActions, FormGrid } from "../components/Form";
import { ActionStatus, Loading } from "../components/AsyncState";
import { useConfirm } from "../components/ConfirmDialog";
import { ClipCell, RowActions, SortHeader, TableFrame } from "../components/DataTable";
import TableToolbar, { countText } from "../components/TableToolbar";
import { matchesSearch, sortRows, useSort } from "../utils/tableSort";
import { usePageTitle } from "../utils/usePageTitle";

export interface ConnectorForm {
  type: ConnectorType;
  name: string;
  url: string;
  pollIntervalSeconds: string;
  timeoutSeconds: string;
  responseFormat: ResponseFormat;
  authType: AuthType;
  authUsername: string;
  authPassword: string;
  authToken: string;
  verifyTls: boolean;
  deviceId: string;
  remoteInstance: string;
  remoteAddress: string;
  rewriteIntervalSeconds: string;
  headers: HeaderRow[];
}

// POST /api/connectors body: only the fields that belong to the chosen type.
export function connectorCreateBody(form: ConnectorForm): Record<string, unknown> {
  const common = {
    name: form.name,
    type: form.type,
    poll_interval_seconds: Number(form.pollIntervalSeconds),
    timeout_seconds: Number(form.timeoutSeconds),
  };
  if (form.type === "bacnet") {
    return {
      ...common,
      device_id: Number(form.deviceId),
      remote_device_instance: Number(form.remoteInstance),
      remote_address: form.remoteAddress.trim() || null,
      rewrite_interval_seconds: Number(form.rewriteIntervalSeconds),
    };
  }
  const headers = headersBody(form.headers);
  return {
    ...common,
    url: form.url,
    response_format: form.responseFormat,
    auth_type: form.authType,
    ...(form.authType === "basic"
      ? { auth_username: form.authUsername || undefined, auth_password: form.authPassword || undefined }
      : {}),
    ...(form.authType === "bearer" ? { auth_token: form.authToken || undefined } : {}),
    ...(Object.keys(headers).length > 0 ? { headers } : {}),
    verify_tls: form.verifyTls,
  };
}

export function describeTarget(connector: ConnectorDTO, devices: DeviceSummary[]): string {
  if (connector.type !== "bacnet") return connector.url;
  const owner = devices.find((d) => d.id === connector.device_id);
  const via = owner ? owner.name : `device ${connector.device_id}`;
  const at = connector.remote_address ? ` at ${connector.remote_address}` : "";
  return `device ${connector.remote_device_instance}${at} (via ${via})`;
}

function initialForm(params: URLSearchParams): ConnectorForm {
  return {
    type: params.get("type") === "bacnet" ? "bacnet" : "http-poll",
    name: "",
    url: "",
    pollIntervalSeconds: "30",
    timeoutSeconds: "10",
    responseFormat: "json",
    authType: "none",
    authUsername: "",
    authPassword: "",
    authToken: "",
    verifyTls: true,
    deviceId: params.get("device") ?? "",
    remoteInstance: params.get("instance") ?? "",
    remoteAddress: "",
    rewriteIntervalSeconds: "60",
    headers: [],
  };
}

export default function ConnectorsList() {
  usePageTitle("Connectors");
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [notice, setNotice] = useState<Notice>(null);
  const {
    data: connectors = [],
    error: connectorsError,
    isPending: connectorsPending,
    refetch: refetchConnectors,
  } = useQuery({ queryKey: ["connectors"], queryFn: connectorsApi.list });
  const { data: devices = [] } = useQuery({ queryKey: ["devices"], queryFn: devicesApi.list });
  const { data: status = {} } = useQuery({
    queryKey: ["connectorStatus"],
    queryFn: connectorsApi.status,
    refetchInterval: 5000,
  });
  // Open at once when the URL names a type (the device page's links do).
  const [addOpen, setAddOpen] = useState(() => searchParams.has("type"));
  const [form, setForm] = useState<ConnectorForm>(() => initialForm(searchParams));
  const set = (field: keyof ConnectorForm) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  // The device select shows the first device until one is picked.
  const deviceId = form.deviceId || (devices[0] ? String(devices[0].id) : "");

  // The "added" line goes once the next connector is being typed.
  const [editedSinceAdd, setEditedSinceAdd] = useState(false);
  const create = useMutation({
    mutationFn: () => connectorsApi.create(connectorCreateBody({ ...form, deviceId })),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      // Every credential clears, headers too: their values are password
      // fields, so a kept one wouldn't show and would go to the next URL.
      setForm((f) => ({ ...f, name: "", url: "", authUsername: "", authPassword: "", authToken: "", headers: [] }));
      setEditedSinceAdd(false);
    },
  });

  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const { sort, toggle } = useSort<"name">();
  const { ask, dialog } = useConfirm();
  const remove = useMutation({
    mutationFn: (id: number) => connectorsApi.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      setNotice({ type: "ok", message: "connector deleted" });
    },
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
  });
  const shown = sortRows(
    connectors.filter(
      (c) => (typeFilter === "" || c.type === typeFilter) && matchesSearch(search, c.name, describeTarget(c, devices))
    ),
    sort,
    { name: (c) => c.name }
  );
  const mappingsText = (c: ConnectorDTO) =>
    c.type === "bacnet" ? `${c.mapping_count} read · ${c.write_mapping_count} write` : String(c.mapping_count);
  const mappingTotal = (c: ConnectorDTO) => c.mapping_count + (c.type === "bacnet" ? c.write_mapping_count : 0);

  const toolbarShown = !connectorsPending && !connectorsError && connectors.length > 0;
  const addButton = (
    <Button small={toolbarShown} type="button" aria-haspopup="dialog" onClick={() => setAddOpen(true)}>
      Add connector
    </Button>
  );

  return (
    <>
      <PageHeader title="Connectors" />
      <Callout tone="info">HTTP connectors feed values into local objects on one device.</Callout>
      <Banner notice={notice} />

      {dialog}
      {!toolbarShown && <div className="bp-toolbar">{addButton}</div>}
      {toolbarShown && (
        <TableToolbar
          actions={addButton}
          search={search}
          onSearch={setSearch}
          searchLabel="Search connectors"
          placeholder="Search name or source"
          filter={{
            label: "Connector type",
            value: typeFilter,
            onChange: setTypeFilter,
            options: [
              { value: "", label: "All types" },
              { value: "http-poll", label: "HTTP" },
              { value: "bacnet", label: "BACnet" },
            ],
          }}
          count={countText(shown.length, connectors.length, "connector")}
        />
      )}
      {connectorsError ? (
        <LoadError what="connectors" error={connectorsError} onRetry={() => refetchConnectors()} />
      ) : connectorsPending ? (
        <Loading what="connectors" />
      ) : connectors.length === 0 ? (
        <p className="bp-muted">No connectors yet.</p>
      ) : shown.length === 0 ? (
        <p className="bp-muted">No connectors match.</p>
      ) : (
        <TableFrame label="Connectors">
          <thead>
            <tr>
              <SortHeader label="Name" sortKey="name" sort={sort} onSort={toggle} />
              <th>Type</th>
              <th>Source</th>
              <th className="bp-cell-num">Interval (s)</th>
              <th>Status</th>
              <th>Last error</th>
              <th>Mappings</th>
              <th className="bp-cell-actions">
                <span className="bp-muted">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => {
              const s = status[String(c.id)];
              const total = mappingTotal(c);
              return (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{c.type === "bacnet" ? "BACnet" : "HTTP"}</td>
                  <ClipCell value={describeTarget(c, devices)} />
                  <td className="bp-cell-num">{c.poll_interval_seconds}</td>
                  <td>
                    <ConnectorHealthPill status={s} enabled={c.enabled} />
                  </td>
                  <ClipCell value={c.enabled ? s?.last_error : null} />
                  <td>{mappingsText(c)}</td>
                  <RowActions
                    editTo={`/connectors/${c.id}`}
                    onDelete={() =>
                      ask({
                        title: `Delete ${c.name}?`,
                        body: `This removes the connector and its ${total} ${total === 1 ? "mapping" : "mappings"}.`,
                        confirmLabel: "Delete connector",
                        onConfirm: () => remove.mutateAsync(c.id),
                      })
                    }
                  />
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}

      <SidePanel open={addOpen} title="Add connector" onClose={() => setAddOpen(false)}>
        <Form
            aria-label="Add connector"
            onChange={() => setEditedSinceAdd(true)}
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            <Fieldset legend="Connector">
              <FormGrid>
                <Field label="Type">
                  <select id="type" value={form.type} onChange={set("type")}>
                    <option value="http-poll">HTTP poll</option>
                    <option value="bacnet">BACnet</option>
                  </select>
                </Field>
                <Field label="Name">
                  <input id="name" type="text" required value={form.name} onChange={set("name")} />
                </Field>
              </FormGrid>
              {form.type === "http-poll" && (
                <Field label="URL">
                  <input
                    id="url"
                    type="text"
                    className="bp-input--mono"
                    required
                    placeholder="http://host/status"
                    value={form.url}
                    onChange={set("url")}
                  />
                </Field>
              )}
            </Fieldset>

            {form.type === "bacnet" && (
              <Fieldset legend="Remote device">
                <FormGrid>
                  <Field label="Local device">
                    <select id="device_id" value={deviceId} onChange={set("deviceId")}>
                      {devices.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name} ({d.id})
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Remote device instance">
                    <input
                      id="remote_instance"
                      type="number"
                      min={0}
                      max={4194302}
                      required
                      value={form.remoteInstance}
                      onChange={set("remoteInstance")}
                    />
                  </Field>
                  <Field label="Static address" help="Blank finds it by Who-Is.">
                    <input
                      id="remote_address"
                      type="text"
                      className="bp-input--mono"
                      value={form.remoteAddress}
                      onChange={set("remoteAddress")}
                    />
                  </Field>
                  <Field label="Re-write every (s)" help="0 writes only on change.">
                    <input
                      id="rewrite_interval_seconds"
                      type="number"
                      min={0}
                      value={form.rewriteIntervalSeconds}
                      onChange={set("rewriteIntervalSeconds")}
                    />
                  </Field>
                </FormGrid>
              </Fieldset>
            )}

            <Fieldset legend="Timing">
              <FormGrid>
                <Field label="Poll interval (s)">
                  <input
                    id="poll_interval_seconds"
                    type="number"
                    min={1}
                    value={form.pollIntervalSeconds}
                    onChange={set("pollIntervalSeconds")}
                  />
                </Field>
                <Field label="Timeout (s)">
                  <input
                    id="timeout_seconds"
                    type="number"
                    min={1}
                    value={form.timeoutSeconds}
                    onChange={set("timeoutSeconds")}
                  />
                </Field>
                {form.type === "http-poll" && (
                  <Field label="Response format">
                    <select id="response_format" value={form.responseFormat} onChange={set("responseFormat")}>
                      <option value="json">JSON</option>
                      <option value="plain-text">Plain text</option>
                    </select>
                  </Field>
                )}
              </FormGrid>
            </Fieldset>

            {form.type === "http-poll" && (
              <Fieldset legend="Authentication">
                <FormGrid>
                  <Field label="Auth">
                    <select id="auth_type" value={form.authType} onChange={set("authType")}>
                      <option value="none">None</option>
                      <option value="basic">Basic</option>
                      <option value="bearer">Bearer token</option>
                    </select>
                  </Field>
                  {form.authType === "basic" && (
                    <>
                      <Field label="Username">
                        <input id="auth_username" type="text" value={form.authUsername} onChange={set("authUsername")} />
                      </Field>
                      <Field label="Password">
                        <input id="auth_password" type="password" value={form.authPassword} onChange={set("authPassword")} />
                      </Field>
                    </>
                  )}
                  {form.authType === "bearer" && (
                    <Field label="Token">
                      <input id="auth_token" type="password" value={form.authToken} onChange={set("authToken")} />
                    </Field>
                  )}
                </FormGrid>
                <VerifyTlsField
                  id="verify_tls"
                  checked={form.verifyTls}
                  onChange={(verifyTls) => setForm((f) => ({ ...f, verifyTls }))}
                />
              </Fieldset>
            )}

            {form.type === "http-poll" && (
              <Fieldset legend="Headers">
                <HeadersEditor rows={form.headers} onChange={(headers) => setForm((f) => ({ ...f, headers }))} />
              </Fieldset>
            )}

            <FormActions>
              <Button variant="primary" type="submit" disabled={create.isPending}>
                Add connector
              </Button>
              <Button variant="ghost" type="button" onClick={() => setAddOpen(false)}>
                Cancel
              </Button>
            </FormActions>
            <ActionStatus
              action={create}
              failed="Couldn't add the connector"
              done="Connector added."
              hideDone={editedSinceAdd}
            />
        </Form>
      </SidePanel>
    </>
  );
}
