import { useState } from "react";
import { Link, useLocation, useMatch, useNavigate } from "react-router-dom";
import SidePanel from "../components/SidePanel";
import NewDeviceForm from "./DeviceNew";
import ImportEdePanel from "../components/ImportEdePanel";
import SampleBuildingPanel, { sampleNotice } from "../components/SampleBuildingPanel";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { devicesApi, engineApi } from "../api/endpoints";
import DeviceCard from "../components/DeviceCard";
import Banner, { LoadError, errorMessage, type Notice } from "../components/Banner";
import { Loading } from "../components/AsyncState";
import RestartAllButton from "../components/RestartAllButton";
import RestartUiButton from "../components/RestartUiButton";
import PageHeader from "../components/PageHeader";
import { buttonClass } from "../components/Button";
import Callout from "../components/Callout";
import { usePageTitle } from "../utils/usePageTitle";
import { useDefaultBind } from "../utils/useDefaultBind";
import { useAuthState } from "../utils/useAuthState";

export default function Dashboard() {
  usePageTitle("Devices");
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  // One entry per device with a start/stop in flight: a single id would let
  // a second click clear the first device's busy state.
  const [pendingIds, setPendingIds] = useState<Set<number>>(new Set());
  const markPending = (deviceId: number, pending: boolean) =>
    setPendingIds((prev) => {
      const next = new Set(prev);
      if (pending) next.add(deviceId);
      else next.delete(deviceId);
      return next;
    });

  const {
    data: devices = [],
    error: devicesError,
    isPending: devicesPending,
    refetch: refetchDevices,
  } = useQuery({ queryKey: ["devices"], queryFn: devicesApi.list });
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const defaultBind = useDefaultBind();
  const { data: auth } = useAuthState();

  // /devices/new, /devices/import-ede and /devices/sample are this page with
  // that panel open; each panel has an address so links and Back work. Opened
  // from the page, closing is Back. Switching from one panel to another
  // replaces the entry, so closing still lands on the plain page.
  const newDeviceOpen = useMatch("/devices/new") !== null;
  const importEdeOpen = useMatch("/devices/import-ede") !== null;
  const sampleOpen = useMatch("/devices/sample") !== null;
  const navigate = useNavigate();
  const { state: locationState } = useLocation();
  const panelFrom = (locationState as { panelFrom?: string } | null)?.panelFrom;
  const closePanel = () => {
    if (panelFrom === "dashboard") navigate(-1);
    else navigate("/devices", { replace: true });
  };
  // With a panel already open (this one or another) a link replaces the
  // entry and keeps where it was opened from, so one close lands on the page.
  // `replace` is always passed, which turns off React Router's own
  // same-location replace, so the second-click case is handled here.
  const anyPanelOpen = newDeviceOpen || importEdeOpen || sampleOpen;
  const panelLink = () => ({ replace: anyPanelOpen, state: { panelFrom: anyPanelOpen ? panelFrom : "dashboard" } });

  const onStartStopSuccess = () => {
    setNotice(null);
    queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
  };

  const start = useMutation({
    mutationFn: (deviceId: number) => engineApi.start(deviceId),
    onMutate: (deviceId) => markPending(deviceId, true),
    onSuccess: onStartStopSuccess,
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
    onSettled: (_data, _err, deviceId) => markPending(deviceId, false),
  });

  const stop = useMutation({
    mutationFn: (deviceId: number) => engineApi.stop(deviceId),
    onMutate: (deviceId) => markPending(deviceId, true),
    onSuccess: onStartStopSuccess,
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
    onSettled: (_data, _err, deviceId) => markPending(deviceId, false),
  });

  return (
    <>
      <PageHeader
        title="Devices"
        sub="Simulated BACnet devices"
        actions={
          <>
            {devices.length > 0 && <RestartAllButton onNotice={setNotice} />}
            <RestartUiButton onNotice={setNotice} />
            <Link className={buttonClass()} to="/devices/import-ede" {...panelLink()} aria-haspopup="dialog">
              Import EDE
            </Link>
            <Link
              className={buttonClass("primary")}
              to="/devices/new"
              {...panelLink()}
              aria-haspopup="dialog"
            >
              New device
            </Link>
          </>
        }
      />
      {auth?.mode === "none" && (
        <Callout tone="warn" lead="BACpack is running without a login.">
          <Link to="/settings/security">Turn one on under Security.</Link>
        </Callout>
      )}
      <Banner notice={notice} />

      {devicesError ? (
        <LoadError what="devices" error={devicesError} onRetry={() => refetchDevices()} />
      ) : devicesPending ? (
        <Loading what="devices" />
      ) : devices.length > 0 ? (
        <div className="bp-device-grid">
          {devices.map((d) => (
            <DeviceCard
              key={d.id}
              device={d}
              status={statuses.find((s) => s.device_id === d.id)}
              defaultBind={defaultBind}
              busy={pendingIds.has(d.id)}
              onStart={() => start.mutate(d.id)}
              onStop={() => stop.mutate(d.id)}
            />
          ))}
        </div>
      ) : (
        <>
          <p className="bp-muted">No devices yet.</p>
          <p>
            <Link className={buttonClass()} to="/devices/sample" {...panelLink()} aria-haspopup="dialog">
              Load sample building
            </Link>
          </p>
        </>
      )}

      <SidePanel open={newDeviceOpen} title="New device" onClose={closePanel}>
        <NewDeviceForm onCreated={(device) => navigate(`/devices/${device.id}`)} />
      </SidePanel>
      <ImportEdePanel open={importEdeOpen} onClose={closePanel} />
      <SampleBuildingPanel
        open={sampleOpen}
        onClose={closePanel}
        onLoaded={(result) => setNotice(sampleNotice(result))}
      />
    </>
  );
}
