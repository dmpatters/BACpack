import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { devicesApi, objectsApi } from "../api/endpoints";
import Tabs, { TabPanels, type TabDef } from "../components/Tabs";
import EmptyState from "../components/EmptyState";
import { isNotFound } from "../api/http";
import { errorMessage } from "../components/Banner";
import GeneralTab from "../components/object-tabs/GeneralTab";
import TrendingTab from "../components/object-tabs/TrendingTab";
import AlarmingTab from "../components/object-tabs/AlarmingTab";
import NotificationClassTab from "../components/object-tabs/NotificationClassTab";
import ScheduleTab from "../components/object-tabs/ScheduleTab";
import DatesTab from "../components/object-tabs/DatesTab";
import SimulationTab from "../components/object-tabs/SimulationTab";
import { simFamily } from "../utils/simulation";
import { isAlarmingCapable, isCommandable } from "../utils/objectTypes";
import PageHeader from "../components/PageHeader";
import PriorityColumn from "../components/PriorityColumn";
import LiveReadout from "../components/LiveReadout";
import { unitShort } from "../utils/units";
import { Loading } from "../components/AsyncState";
import { usePageTitle } from "../utils/usePageTitle";

// The object's own page, shown in DeviceDetail's overlay; objectId is null
// for a URL that isn't a number.
export default function ObjectDetail({
  deviceId,
  objectId,
  tab,
}: {
  deviceId: number;
  objectId: number | null;
  tab: string;
}) {
  const { state } = useLocation();
  const idLabel = objectId === null ? "" : ` ${objectId}`;

  const { data: device, error: deviceError } = useQuery({
    queryKey: ["device", deviceId],
    queryFn: () => devicesApi.get(deviceId),
  });
  // The device's object list, not the per-object GET: every object save
  // invalidates ["objects", deviceId], so this page refreshes with it.
  const { data: objects, error: objectsError } = useQuery({
    queryKey: ["objects", deviceId],
    queryFn: () => objectsApi.list(deviceId),
  });

  const obj = objectId === null ? undefined : objects?.find((o) => o.id === objectId);
  const tabs = obj ? objectTabs(obj.object_type) : [];
  // Only when nothing loaded: a failed refetch (uvicorn restarting) keeps
  // the cached object on screen, with any unsaved edits.
  const loadError = deviceError ?? objectsError;
  const failed = loadError && !(device && objects);
  const objectMissing = Boolean(device && objects && !obj);
  // The objects list 404s only when the device itself is gone.
  const problem =
    failed && isNotFound(loadError)
      ? `Device ${deviceId} not found`
      : failed
        ? `Couldn't load object${idLabel}`
        : objectMissing
          ? `Object${idLabel} not found`
          : null;
  usePageTitle(...(problem ? [problem] : [obj?.name, tabs.find((t) => t.slug === tab)?.label, device?.name]));
  if (problem && objectMissing && device) {
    return (
      <EmptyState
        title={problem}

        backTo={`/devices/${device.id}`}
        backLabel={`Back to ${device.name}`}
        replace
      />
    );
  }
  if (problem) {
    return (
      <EmptyState
        title={problem}
        text={
          failed && !isNotFound(loadError) ? errorMessage(loadError) : undefined
        }
        backTo="/"
        backLabel="Back to devices"
        replace
      />
    );
  }
  if (!obj || !device) return <Loading what="object" />;

  const basePath = `/devices/${device.id}/objects/${obj.id}`;
  const panels: Record<string, () => JSX.Element> = {
    general: () => <GeneralTab deviceId={device.id} obj={obj} objects={objects} />,
    trending: () => <TrendingTab deviceId={device.id} obj={obj} />,
    alarming: () => <AlarmingTab deviceId={device.id} obj={obj} />,
    recipients: () => <NotificationClassTab deviceId={device.id} obj={obj} />,
    schedule: () => <ScheduleTab deviceId={device.id} obj={obj} />,
    dates: () => <DatesTab deviceId={device.id} obj={obj} />,
    simulation: () => <SimulationTab deviceId={device.id} obj={obj} objects={objects} />,
  };

  return (
    <div className={isCommandable(obj.object_type) ? "bp-object bp-object--prio" : "bp-object"}>
      <div className="bp-object__head">
        <PageHeader
          title={obj.name}
          meta={`${obj.object_type}:${obj.instance_number}`}
          sub={`Object on ${device.name}`}
        />
        {obj.object_type !== "notification-class" && (
          <LiveReadout deviceId={device.id} obj={obj} unitLabel={unitShort(obj.units)} />
        )}
      </div>
      {isCommandable(obj.object_type) && <PriorityColumn deviceId={device.id} obj={obj} />}
      <div className="bp-object__tabs">
        {/* Replace, keeping the state: Back from the overlay must close it, not step through tabs. */}
        <Tabs tabs={tabs} active={tab} basePath={basePath} label="Object sections" replace state={state} />
        {/* Always mounted, so unsaved edits in an opened tab survive a mistyped tab URL. */}
        <TabPanels key={obj.id} active={tab} panels={tabs.map((t) => ({ key: t.slug, render: panels[t.slug] }))} />
        {!tabs.some((t) => t.slug === tab) && (
          <EmptyState
            level={2}
            title={`No "${tab}" tab`}

            backTo={basePath}
            backLabel={`Back to ${obj.name}`}
            replace
          />
        )}
      </div>
    </div>
  );
}

// Trending is offered for every type but a Notification Class, which has no
// Present_Value to trend.
function objectTabs(objectType: string): TabDef[] {
  const tabs: TabDef[] = [{ slug: "general", label: "General" }];
  if (objectType !== "notification-class") tabs.push({ slug: "trending", label: "Trending" });
  if (isAlarmingCapable(objectType)) tabs.push({ slug: "alarming", label: "Alarming" });
  if (objectType === "notification-class") tabs.push({ slug: "recipients", label: "Recipients" });
  if (objectType === "schedule") tabs.push({ slug: "schedule", label: "Schedule" });
  if (objectType === "calendar") tabs.push({ slug: "dates", label: "Dates" });
  if (simFamily(objectType)) tabs.push({ slug: "simulation", label: "Simulation" });
  return tabs;
}
