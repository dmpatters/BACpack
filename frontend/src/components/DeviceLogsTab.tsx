import LogView from "./LogView";

export default function DeviceLogsTab({ deviceId }: { deviceId: number }) {
  return (
    <>
      <p className="bp-muted">
        Application log messages for this device, plus server-wide events. Refreshes every few
        seconds unless paused.
      </p>
      <LogView deviceId={deviceId} />
    </>
  );
}
