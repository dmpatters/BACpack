import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { devicesApi, engineApi } from "../api/endpoints";
import type { DeviceSummary, NetworkRole } from "../api/types";
import { formatBind, GUIDE_URL, sameNetwork } from "../utils/bind";
import { useDefaultBind } from "../utils/useDefaultBind";
import Button from "./Button";
import { ActionStatus } from "./AsyncState";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid } from "./Form";
import { differs, useDraft, useUnsavedChanges } from "./UnsavedChanges";

// Mid-start or mid-restart counts as running: it answers within a moment.
const RUNNING = new Set(["running", "starting", "reinitializing"]);

type RoleFields = {
  role: NetworkRole;
  foreignBbmd: string;
  foreignTtl: string;
  peers: string;
  links: number[];
};

function loadRole(device: DeviceSummary | undefined): RoleFields {
  return {
    role: device?.network_role ?? "normal",
    foreignBbmd: device?.foreign_bbmd ?? "",
    foreignTtl: String(device?.foreign_ttl ?? 60),
    peers: device?.bbmd_peers ? device.bbmd_peers.join(", ") : "",
    links: [...(device?.bbmd_links ?? [])].sort((a, b) => a - b),
  };
}

// The device's network role: normal, a foreign device registering with a BBMD,
// or a BBMD that forwards broadcasts to the devices ticked here. Links are
// one-way: the ticked device is untouched.
export default function DeviceNetworkRole({ deviceId, device }: { deviceId: number; device: DeviceSummary | undefined }) {
  const queryClient = useQueryClient();
  const { data: devices = [] } = useQuery({ queryKey: ["devices"], queryFn: devicesApi.list });
  const { data: statuses = [] } = useQuery({ queryKey: ["engineStatus"], queryFn: engineApi.status, refetchInterval: 5000 });
  // A device without its own address serves on this one, as the server's link checks have it.
  const defaultBind = useDefaultBind();
  const { form, setForm, baseline, setBaseline } = useDraft(device, loadRole);
  const dirty = differs(form, baseline);
  useUnsavedChanges(dirty, `/devices/${deviceId}`);

  const save = useMutation({
    mutationFn: (sent: RoleFields) =>
      // All five are sent whatever the role, so a role's saved fields survive a
      // save made in another (a BBMD keeps its links while it is Normal).
      devicesApi.update(deviceId, {
        network_role: sent.role,
        foreign_bbmd: sent.foreignBbmd.trim() || null,
        foreign_ttl: Number(sent.foreignTtl),
        bbmd_peers: sent.peers
          .split(",")
          .map((peer) => peer.trim())
          .filter(Boolean),
        bbmd_links: sent.links,
      }),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["device", deviceId] });
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
    },
  });

  const others = devices.filter((d) => d.id !== deviceId);
  const toggle = (id: number, on: boolean) =>
    setForm((f) => ({
      ...f,
      links: (on ? [...f.links, id] : f.links.filter((linked) => linked !== id)).sort((a, b) => a - b),
    }));

  return (
    <>
      <h2>Network role</h2>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(form);
        }}
      >
        <Field
          label="Network role"
          help={
            <a href={GUIDE_URL} target="_blank" rel="noreferrer">
              Sharing the BACnet port
            </a>
          }
        >
          <select
            id="device_network_role"
            value={form.role}
            onChange={(e) => setForm((f) => ({ ...f, role: e.target.value as NetworkRole }))}
          >
            <option value="normal">Normal (local broadcast only)</option>
            <option value="foreign">Foreign device (register with a remote BBMD)</option>
            <option value="bbmd">BBMD (accept registrations, forward to other devices)</option>
          </select>
        </Field>

        {form.role === "foreign" && (
          <FormGrid>
            <Field label="BBMD address">
              <input
                id="device_foreign_bbmd"
                type="text"
                className="bp-input--mono"
                placeholder="192.168.1.5:47808"
                value={form.foreignBbmd}
                onChange={(e) => setForm((f) => ({ ...f, foreignBbmd: e.target.value }))}
              />
            </Field>
            <Field label="Registration lifetime (s)">
              <input
                id="device_foreign_ttl"
                type="number"
                required
                min={1}
                max={65535}
                value={form.foreignTtl}
                onChange={(e) => setForm((f) => ({ ...f, foreignTtl: e.target.value }))}
              />
            </Field>
          </FormGrid>
        )}

        {form.role === "bbmd" && (
          <>
            <Fieldset legend="Forward broadcasts to">
              {others.length === 0 && <p className="bp-muted">No other devices.</p>}
              {others.map((other) => {
                const label = `${other.name} (${other.id}) · ${formatBind(other.bind_address, defaultBind)}`;
                const onSameNetwork = sameNetwork(
                  device?.bind_address || defaultBind,
                  other.bind_address || defaultBind
                );
                const foreign = other.network_role === "foreign";
                const why = onSameNetwork
                  ? "already on this device's network"
                  : foreign
                    ? "a foreign device can't be linked"
                    : undefined;
                const linked = form.links.includes(other.id);
                // A linked device that isn't running can't answer what the BBMD forwards.
                const stopped =
                  linked && statuses.length > 0 && !RUNNING.has(statuses.find((s) => s.device_id === other.id)?.process_status ?? "");
                return (
                  <CheckField
                    key={other.id}
                    label={label}
                    help={why ?? (stopped ? "stopped" : undefined)}
                    // A link that is already saved can still be unticked.
                    disabled={why !== undefined && !linked}
                    checked={linked}
                    onChange={(e) => toggle(other.id, e.target.checked)}
                  />
                );
              })}
            </Fieldset>
            <Field
              label="Other BBMDs (typed)"
              help="Comma-separated, e.g. 192.168.2.5:47808."
            >
              <input
                id="device_bbmd_peers"
                type="text"
                className="bp-input--mono"
                value={form.peers}
                onChange={(e) => setForm((f) => ({ ...f, peers: e.target.value }))}
              />
            </Field>
          </>
        )}

        <FormActions>
          <Button type="submit" disabled={save.isPending}>
            Save network role
          </Button>
        </FormActions>
        <ActionStatus
          action={save}
          failed="Couldn't save the network role"
          done="Network role saved. Applies on start."
          hideDone={dirty}
        />
      </Form>
    </>
  );
}
