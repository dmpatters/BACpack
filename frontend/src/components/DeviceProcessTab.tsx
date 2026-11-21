import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { capabilitiesApi, devicesApi, engineApi } from "../api/endpoints";
import DeviceClock from "./DeviceClock";
import LiveApplyBanners from "./LiveApplyBanners";
import { wallTime } from "../utils/deviceClock";
import { formatUtc } from "../utils/time";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER, formatBind } from "../utils/bind";
import { useDefaultBind } from "../utils/useDefaultBind";
import { dccText } from "../utils/text";
import Button from "./Button";
import Callout from "./Callout";
import DeviceNetworkRole from "./DeviceNetworkRole";
import PortInUseHelp from "./PortInUseHelp";
import { Columns, Field, Form, FormActions } from "./Form";
import { ActionStatus, Loading } from "./AsyncState";
import { differs, useDraft, useUnsavedChanges } from "./UnsavedChanges";

export default function DeviceProcessTab({ deviceId }: { deviceId: number }) {
  const queryClient = useQueryClient();
  const [bindSavedNote, setBindSavedNote] = useState("");

  const { data: device } = useQuery({
    queryKey: ["device", deviceId],
    queryFn: () => devicesApi.get(deviceId),
  });
  const { form, setForm, baseline, setBaseline } = useDraft(device, (d) => ({ bindAddress: d?.bind_address ?? "" }));
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const status = statuses.find((s) => s.device_id === deviceId);
  const { data: caps } = useQuery({ queryKey: ["capabilities"], queryFn: capabilitiesApi.get });
  const defaultBind = useDefaultBind();

  const dirty = differs(form, baseline);
  useUnsavedChanges(dirty, `/devices/${deviceId}`);

  const saveBindAddress = useMutation({
    mutationFn: (sent: { bindAddress: string }) => devicesApi.update(deviceId, { bind_address: sent.bindAddress || null }),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["device", deviceId] });
      // On a running device the restart-pending banner says what's left to do.
      setBindSavedNote(
        status?.process_status === "running"
          ? "Bind address saved."
          : "Bind address saved. Applies on start."
      );
    },
  });
  // The "applies when the device starts" note is stale once it has started.
  const running = status?.process_status === "running";
  useEffect(() => {
    if (running) setBindSavedNote((note) => (note.includes("applies when the device starts") ? "" : note));
  }, [running]);

  if (!status) return <Loading what="process status" />;

  const isRunning = status.process_status === "running";
  // "reinitializing" is mid-flight too: a respawn is in progress (see
  // do_ReinitializeDeviceRequest in application.py).
  const isTransitioning =
    status.process_status === "starting" ||
    status.process_status === "stopping" ||
    status.process_status === "reinitializing";

  return (
    <>
      {/* Start, Stop, Restart serving and the run state are in the page header. */}
      {!isRunning && !isTransitioning && (
        <p className="bp-muted">This device is not running.</p>
      )}

      <Columns>
        <div>
          <dl className="bp-facts" data-testid="process-info" aria-label="Process">
            <dt>BACnet</dt>
            <dd>{dccText(status.dcc_state)}</dd>
            {status.process_pid != null && (
              <>
                <dt>Process id</dt>
                <dd className="bp-mono">{status.process_pid}</dd>
              </>
            )}
            {status.process_started_at && (
              <>
                <dt>Started</dt>
                <dd>{formatUtc(status.process_started_at)}</dd>
              </>
            )}
            <dt>Bind address</dt>
            <dd className="bp-mono">{formatBind(status.bind_address, defaultBind)}</dd>
            <dt>Database revision</dt>
            <dd>{status.database_revision}</dd>
            <dt>Last restore</dt>
            <dd>{status.last_restore_time ? `${wallTime(status.last_restore_time)} device time` : "Never"}</dd>
          </dl>

          {status.process_status === "crashed" && status.process_error && (
            <Callout tone="danger">Crashed: {status.process_error}</Callout>
          )}
          {status.process_status === "crashed" && status.port_in_use && status.process_error && (
            <PortInUseHelp deviceId={deviceId} error={status.process_error} />
          )}
          <LiveApplyBanners status={status} />

          <DeviceClock deviceId={deviceId} clock={status} />
        </div>

        <div>
          <h2>Network binding</h2>
          <Form
            onSubmit={(e) => {
              e.preventDefault();
              saveBindAddress.mutate(form);
            }}
          >
            <Field label="Bind address" help={`${BIND_ADDRESS_HELP} Blank uses the default.`}>
              <input
                id="device_bind_address"
                type="text"
                className="bp-input--mono"
                placeholder={BIND_ADDRESS_PLACEHOLDER}
                value={form.bindAddress}
                onChange={(e) => setForm({ bindAddress: e.target.value })}
              />
            </Field>
            <FormActions>
              <Button type="submit" disabled={saveBindAddress.isPending}>
                Save bind address
              </Button>
            </FormActions>
            <ActionStatus action={saveBindAddress} failed="Couldn't save the bind address" done={bindSavedNote} hideDone={dirty} />
          </Form>

          <DeviceNetworkRole deviceId={deviceId} device={device} />

          {caps && (
            <>
              <h2>BACnet capabilities</h2>
              <p data-testid="capabilities-summary">
                Advertises {caps.services.filter((s) => s.executes).length} services ·{" "}
                {caps.object_types.length} object types · {caps.bibb_summary.met}/
                {caps.bibb_summary.total} B-BC requirements met{" "}
                <Link to="/about#capabilities">View details →</Link>
              </p>
            </>
          )}
        </div>
      </Columns>
    </>
  );
}
