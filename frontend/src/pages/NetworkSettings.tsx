import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { engineApi, settingsApi } from "../api/endpoints";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER } from "../utils/bind";
import type { NetworkSettingsDTO } from "../api/types";
import Banner, { LoadError, type Notice } from "../components/Banner";
import DeviceBindingsTable from "../components/DeviceBindingsTable";
import RestartAllButton from "../components/RestartAllButton";
import RestartUiButton from "../components/RestartUiButton";
import Button from "../components/Button";
import PageHeader from "../components/PageHeader";
import { Field, Fieldset, Form, FormActions, SettingsColumn } from "../components/Form";
import { ActionStatus, Loading } from "../components/AsyncState";
import { differs, useUnsavedChanges } from "../components/UnsavedChanges";
import { usePageTitle } from "../utils/usePageTitle";

type NetworkFields = {
  bindAddress: string;
};

function savedNetwork(settings: NetworkSettingsDTO): NetworkFields {
  return { bindAddress: settings.bind_address ?? "" };
}

const EMPTY_NETWORK: NetworkFields = { bindAddress: "" };

export default function NetworkSettings() {
  usePageTitle("Network Settings");
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  // Only a running device has anything to restart to pick a saved setting up.
  // Until the status is known (loading, or it failed) a device may be running:
  // restart stays available and the note keeps the hint.
  const { data: statuses } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const anyRunning = statuses === undefined || statuses.some((s) => s.process_status === "running");
  const { data: settings, error: loadError, refetch } = useQuery({
    queryKey: ["networkSettings"],
    queryFn: settingsApi.get,
  });

  // The form keeps what the server last confirmed (the loaded settings, or
  // what its own save just sent).
  const [network, setNetwork] = useState<NetworkFields>(EMPTY_NETWORK);
  const [networkBaseline, setNetworkBaseline] = useState<NetworkFields>(EMPTY_NETWORK);
  const setNet = <K extends keyof NetworkFields>(key: K, value: NetworkFields[K]) =>
    setNetwork((f) => ({ ...f, [key]: value }));

  // Every save refetches the settings: a form with unsaved edits keeps them.
  const latest = useRef({ network, networkBaseline });
  latest.current = { network, networkBaseline };
  useEffect(() => {
    if (!settings) return;
    const now = latest.current;
    if (!differs(now.network, now.networkBaseline)) setNetwork(savedNetwork(settings));
    setNetworkBaseline(savedNetwork(settings));
  }, [settings]);

  const networkDirty = settings !== undefined && differs(network, networkBaseline);
  useUnsavedChanges(networkDirty, "/settings/network");

  const saveNetwork = useMutation({
    mutationFn: (sent: NetworkFields) => settingsApi.update({ bind_address: sent.bindAddress || null }),
    onSuccess: (_result, sent) => {
      queryClient.invalidateQueries({ queryKey: ["networkSettings"] });
      setNetworkBaseline(sent);
    },
  });

  if (loadError && !settings) {
    return (
      <>
        <PageHeader title="Network Settings" />
        <LoadError what="network settings" error={loadError} onRetry={() => refetch()} />
      </>
    );
  }
  if (!settings) {
    return (
      <>
        <PageHeader title="Network Settings" />
        <Loading what="network settings" />
      </>
    );
  }

  return (
    <>
      <PageHeader title="Network Settings" />

      <h2>Device bindings</h2>
      <div className="bp-stack">
        <DeviceBindingsTable defaultBind={settings.bind_address} />
        <div className="bp-cluster">
          <RestartAllButton onNotice={setNotice} disabled={!anyRunning} />
          <RestartUiButton onNotice={setNotice} />
        </div>
      </div>
      <Banner notice={notice} />

      <SettingsColumn>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          saveNetwork.mutate(network);
        }}
      >
        <Fieldset legend="Network">
          <Field label="Default bind address" help={BIND_ADDRESS_HELP}>
            <input
              id="bind_address"
              type="text"
              className="bp-input--mono"
              required
              placeholder={BIND_ADDRESS_PLACEHOLDER}
              value={network.bindAddress}
              onChange={(e) => setNet("bindAddress", e.target.value)}
            />
          </Field>
          <FormActions>
            <Button variant="primary" type="submit" disabled={saveNetwork.isPending}>
              Save network settings
            </Button>
          </FormActions>
          <ActionStatus
            action={saveNetwork}
            failed="Couldn't save"
            done={anyRunning ? 'Saved. Use "Restart all running devices" above to apply.' : "Saved."}
            hideDone={networkDirty}
          />
        </Fieldset>
      </Form>
      </SettingsColumn>
    </>
  );
}
