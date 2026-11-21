import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsApi, trendStorageApi } from "../api/endpoints";
import { LoadError } from "../components/Banner";
import Button from "../components/Button";
import PageHeader from "../components/PageHeader";
import Callout from "../components/Callout";
import { TableFrame } from "../components/DataTable";
import { TOOLBAR_MIN_ROWS } from "../components/TableToolbar";
import UsageMeter from "../components/UsageMeter";
import { Field, Fieldset, Form, FormActions, FormGrid, SettingsColumn } from "../components/Form";
import { ActionStatus, Loading } from "../components/AsyncState";
import { differs, useDraft, useUnsavedChanges } from "../components/UnsavedChanges";
import { usePageTitle } from "../utils/usePageTitle";

const MB = 1024 * 1024;

export default function TrendStorage() {
  usePageTitle("Trend Storage");
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({ queryKey: ["networkSettings"], queryFn: settingsApi.get });
  const storageQuery = useQuery({
    queryKey: ["trendStorage"],
    queryFn: trendStorageApi.get,
    // The server trims the history in the background after a lowered limit.
    refetchInterval: (query) => (query.state.data?.pruning ? 5000 : false),
  });
  const settings = settingsQuery.data;
  const storage = storageQuery.data;

  // A refetch (after a save, or another tab's) refreshes only the fields the
  // user hasn't touched since the baseline.
  const { form, setForm, baseline, setBaseline } = useDraft(settings, (s) => ({
    retentionDays: s?.trend_retention_days != null ? String(s.trend_retention_days) : "",
    softLimitMb: s?.trend_soft_limit_mb != null ? String(s.trend_soft_limit_mb) : "",
  }));
  const { retentionDays, softLimitMb } = form;
  const setRetentionDays = (value: string) => setForm((f) => ({ ...f, retentionDays: value }));
  const setSoftLimitMb = (value: string) => setForm((f) => ({ ...f, softLimitMb: value }));

  const dirty = settings !== undefined && differs({ retentionDays, softLimitMb }, baseline);
  useUnsavedChanges(dirty, "/trends");

  const save = useMutation({
    mutationFn: (sent: { retentionDays: string; softLimitMb: string }) =>
      settingsApi.update({
        trend_retention_days: sent.retentionDays ? Number(sent.retentionDays) : null,
        trend_soft_limit_mb: sent.softLimitMb ? Number(sent.softLimitMb) : null,
      }),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["networkSettings"] });
      queryClient.invalidateQueries({ queryKey: ["trendStorage"] });
    },
  });

  const loadError = (!settings && settingsQuery.error) || (!storage && storageQuery.error);
  if (loadError) {
    return (
      <>
        <PageHeader title="Trend Storage" />
        <LoadError
          what="trend storage"
          error={loadError}
          onRetry={() => {
            void settingsQuery.refetch();
            void storageQuery.refetch();
          }}
        />
      </>
    );
  }
  if (!settings || !storage) {
    return (
      <>
        <PageHeader title="Trend Storage" />
        <Loading what="trend storage" />
      </>
    );
  }

  const usedPct = storage.effective_limit_bytes
    ? Math.min(100, (storage.total_bytes_estimate / storage.effective_limit_bytes) * 100)
    : 0;
  const mb = (bytes: number) => `${(bytes / MB).toFixed(2)} MB`;
  const limitMb = (storage.effective_limit_bytes / MB).toFixed(0);
  const byLargest = [...storage.trends].sort((a, b) => b.bytes_estimate - a.bytes_estimate);

  return (
    <>
      <PageHeader title="Trend Storage" />
      <Callout tone="info">
        Sizes are estimated from row counts.
      </Callout>

      {storage.pruning && <Callout tone="info">Pruning database</Callout>}
      <UsageMeter
        label="Trend data"
        valueText={`${mb(storage.total_bytes_estimate)} of ${limitMb} MB`}
        percent={usedPct}
      />
      <p className="bp-muted">
        {mb(storage.total_bytes_estimate)} of {limitMb} MB · {storage.total_samples}{" "}
        {storage.total_samples === 1 ? "sample" : "samples"}
      </p>
      {byLargest.length === 0 ? (
        <p className="bp-muted">No trend data yet.</p>
      ) : (
        <TableFrame label="Trend storage by object" scroll={byLargest.length >= TOOLBAR_MIN_ROWS}>
          <thead>
            <tr>
              <th>Object</th>
              <th>Device</th>
              <th className="bp-cell-num">Samples</th>
              <th>Fill</th>
              <th className="bp-cell-num">Size</th>
            </tr>
          </thead>
          <tbody>
            {byLargest.map((t) => {
              // How full this trend is against its own sample cap, past which the oldest samples roll off.
              const fill = t.sample_cap ? Math.min(100, (t.sample_count / t.sample_cap) * 100) : 0;
              return (
                <tr key={t.object_id}>
                  <td>
                    <Link to={`/devices/${t.device_id}/objects/${t.object_id}/trending`}>{t.object_name}</Link>
                  </td>
                  <td className="bp-cell-id">{t.device_id}</td>
                  <td className="bp-cell-num">{t.sample_count}</td>
                  <td>
                    <UsageMeter
                      label={`${t.object_name} fill`}
                      valueText={`${t.sample_count} of ${t.sample_cap} samples (${fill.toFixed(1)}%)`}
                      percent={fill}
                      small
                      warns={false}
                    />
                  </td>
                  <td className="bp-cell-num bp-cell-mono">{mb(t.bytes_estimate)}</td>
                </tr>
              );
            })}
          </tbody>
        </TableFrame>
      )}

      <SettingsColumn>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({ retentionDays, softLimitMb });
        }}
      >
        <Fieldset legend="Retention">
        <FormGrid>
          <Field label="Retention (days)" help="Blank keeps samples forever.">
            <input
              id="trend_retention_days"
              type="number"
              min={1}
              value={retentionDays}
              onChange={(e) => setRetentionDays(e.target.value)}
            />
          </Field>
          <Field
            label="Soft limit (MB)"
            help="Blank or 0 turns it off."
          >
            <input
              id="trend_soft_limit_mb"
              type="number"
              min={0}
              max={1024}
              value={softLimitMb}
              onChange={(e) => setSoftLimitMb(e.target.value)}
            />
          </Field>
        </FormGrid>
        <FormActions>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            Save settings
          </Button>
        </FormActions>
        <ActionStatus action={save} failed="Couldn't save" done="Saved." hideDone={dirty} />
        </Fieldset>
      </Form>
      </SettingsColumn>
    </>
  );
}
