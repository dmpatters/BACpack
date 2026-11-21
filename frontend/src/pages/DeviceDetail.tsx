import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { devicesApi, engineApi, objectsApi } from "../api/endpoints";
import ObjectDetail from "./ObjectDetail";
import EditOverlay from "../components/EditOverlay";
import { openedFromList } from "../utils/overlayState";
import Tabs, { TabPanels, type TabDef } from "../components/Tabs";
import EmptyState from "../components/EmptyState";
import { isNotFound } from "../api/http";
import Banner, { errorMessage, type Notice } from "../components/Banner";
import DeviceObjectsTab from "../components/DeviceObjectsTab";
import DeviceProcessTab from "../components/DeviceProcessTab";
import DeviceTrafficTab from "../components/DeviceTrafficTab";
import DeviceLogsTab from "../components/DeviceLogsTab";
import DeviceImportExportTab from "../components/DeviceImportExportTab";
import DeviceSettingsTab from "../components/DeviceSettingsTab";
import DeviceClientTab from "../components/DeviceClientTab";
import Button from "../components/Button";
import Callout from "../components/Callout";
import PageHeader from "../components/PageHeader";
import ConfirmDialog from "../components/ConfirmDialog";
import DeviceRunButton from "../components/DeviceRunButton";
import { ProcessStatusPill } from "../components/StatusPill";
import { Loading } from "../components/AsyncState";
import { LEAVE_UNSAVED } from "../components/UnsavedChanges";
import { parseId } from "../utils/ids";
import { usePageTitleWhen } from "../utils/usePageTitle";

const DEVICE_TABS: TabDef[] = [
  { slug: "objects", label: "Objects" },
  { slug: "process", label: "Process" },
  { slug: "traffic", label: "Traffic" },
  { slug: "logs", label: "Logs" },
  { slug: "import-export", label: "Import/Export" },
  { slug: "settings", label: "Settings" },
  { slug: "client", label: "BACnet client" },
];

// An object's overlay: the title is the object's name once the list has it.
function ObjectOverlay({
  deviceId,
  deviceName,
  objectId,
  tab,
  onClose,
}: {
  deviceId: number;
  deviceName: string;
  objectId: number | null;
  tab: string;
  onClose: () => void;
}) {
  const { data: objects } = useQuery({
    queryKey: ["objects", deviceId],
    queryFn: () => objectsApi.list(deviceId),
  });
  const name =
    objects?.find((o) => o.id === objectId)?.name ?? (objectId === null ? "Object" : `Object ${objectId}`);
  return (
    <EditOverlay
      title={name}
      crumb={
        <>
          <span className="bp-overlay__crumb-device">{deviceName}</span>
          <span className="bp-overlay__crumb-sep"> › </span>
          {name}
        </>
      }
      onClose={onClose}
    >
      <ObjectDetail deviceId={deviceId} objectId={objectId} tab={tab} />
    </EditOverlay>
  );
}

// Also serves an object's URL: the device's Objects tab stays mounted
// underneath with the object's overlay on top, so closing it returns to the
// list as it was.
export default function DeviceDetail() {
  const {
    deviceId: deviceIdParam,
    objectId: objectIdParam,
    tab: tabParam,
  } = useParams<{ deviceId: string; objectId?: string; tab?: string }>();
  const overlayOpen = objectIdParam !== undefined;
  const tab = overlayOpen ? "objects" : (tabParam ?? "objects");
  const deviceId = parseId(deviceIdParam);
  const navigate = useNavigate();
  const { state: locationState } = useLocation();
  const closeObject = () => {
    if (openedFromList(locationState)) navigate(-1);
    else navigate(`/devices/${deviceIdParam}`, { replace: true });
  };
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const {
    data: device,
    error,
    isFetching: deviceFetching,
    refetch: refetchDevice,
  } = useQuery({
    queryKey: ["device", deviceId],
    queryFn: () => devicesApi.get(deviceId!),
    enabled: deviceId !== null,
  });
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const status = statuses.find((s) => s.device_id === deviceId)?.process_status;
  const isLive = status === "running";
  // What one device's action reported doesn't belong on the next device's page.
  useEffect(() => {
    setRunError(null);
    setNotice(null);
  }, [deviceId]);
  // A failed start no longer applies once the device is serving.
  useEffect(() => {
    if (isLive) setRunError(null);
  }, [isLive]);
  const problem =
    deviceId === null || (error && !device && isNotFound(error))
      ? `Device ${deviceIdParam} not found`
      : error && !device
        ? `Couldn't load device ${deviceIdParam}`
        : null;
  const tabLabel = DEVICE_TABS.find((t) => t.slug === tab)?.label;
  usePageTitleWhen(!overlayOpen, ...(problem ? [problem] : [device?.name ?? `Device ${deviceIdParam}`, tabLabel]));

  const deleteDevice = useMutation({
    mutationFn: () => devicesApi.remove(deviceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      // The delete dialog was the question; the guard doesn't ask again.
      navigate("/", { state: LEAVE_UNSAVED });
    },
    onError: (err) => {
      setConfirmingDelete(false);
      setNotice({ type: "error", message: errorMessage(err) });
    },
  });

  if (problem) {
    return (
      <EmptyState
        title={problem}
        text={
          error && !isNotFound(error) ? errorMessage(error) : undefined
        }
        backTo="/"
        backLabel="Back to devices"
      />
    );
  }
  if (!device) return <Loading what="device" />;

  return (
    <>
      <PageHeader
        title={device.name}
        meta={`device ${device.id}`}
        sub={device.description || undefined}
        actions={
          <>
            {status && <ProcessStatusPill status={status} />}
            <DeviceRunButton deviceId={device.id} status={status} onError={setRunError} />
            <Button
              variant="danger"
              onClick={() => {
                // Object edits don't refresh this query, so the count shown
                // in the dialog is fetched fresh.
                void refetchDevice();
                setConfirmingDelete(true);
              }}
            >
              Delete device
            </Button>
          </>
        }
      />
      <Banner notice={notice} />
      {runError && <Callout tone="danger">{runError}</Callout>}
      {confirmingDelete && (
        <ConfirmDialog
          title={`Delete device ${device.id}?`}
          confirmLabel="Delete device"
          busy={deleteDevice.isPending}
          onConfirm={() => deleteDevice.mutate()}
          onCancel={() => setConfirmingDelete(false)}
        >
          This removes <strong>{device.name}</strong>,{" "}
          {deviceFetching
            ? "all its objects"
            : `its ${device.object_count} ${device.object_count === 1 ? "object" : "objects"}`}{" "}
          and their trend history.
        </ConfirmDialog>
      )}

      <Tabs tabs={DEVICE_TABS} active={tab} basePath={`/devices/${device.id}`} label="Device sections" />

      {/* Always mounted, so unsaved edits in an opened tab survive a mistyped tab URL. */}
      <TabPanels
        key={device.id}
        active={tab}
        panels={[
          { key: "objects", render: () => <DeviceObjectsTab deviceId={device.id} /> },
          { key: "process", render: () => <DeviceProcessTab deviceId={device.id} /> },
          { key: "traffic", render: () => <DeviceTrafficTab deviceId={device.id} /> },
          { key: "logs", render: () => <DeviceLogsTab deviceId={device.id} /> },
          { key: "import-export", render: () => <DeviceImportExportTab deviceId={device.id} /> },
          { key: "settings", render: () => <DeviceSettingsTab deviceId={device.id} /> },
          { key: "client", render: () => <DeviceClientTab deviceId={device.id} isLive={isLive} /> },
        ]}
      />
      {!DEVICE_TABS.some((t) => t.slug === tab) && (
        <EmptyState
          level={2}
          title={`No "${tab}" tab`}

          backTo={`/devices/${device.id}`}
          backLabel={`Back to ${device.name}`}
        />
      )}
      {overlayOpen && (
        <ObjectOverlay
          deviceId={device.id}
          deviceName={device.name}
          objectId={parseId(objectIdParam)}
          tab={tabParam ?? "general"}
          onClose={closeObject}
        />
      )}
    </>
  );
}
