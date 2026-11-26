import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { devicesApi, engineApi } from "../api/endpoints";
import LiveApplyBanners from "./LiveApplyBanners";
import { changedFields } from "../utils/objectForm";
import Button from "./Button";
import { Field, Fieldset, Form, FormActions, FormGrid, SettingsColumn } from "./Form";
import { ActionStatus, Loading } from "./AsyncState";
import { differs, useDraft, useUnsavedChanges } from "./UnsavedChanges";

export default function DeviceSettingsTab({ deviceId }: { deviceId: number }) {
  const queryClient = useQueryClient();
  const { data: device } = useQuery({
    queryKey: ["device", deviceId],
    queryFn: () => devicesApi.get(deviceId),
  });
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const status = statuses.find((s) => s.device_id === deviceId);
  const isRunning = status?.process_status === "running";

  const { form, setForm, baseline, setBaseline } = useDraft(device, (d) => ({
    name: d?.name ?? "",
    description: d?.description ?? "",
    location: d?.location ?? "",
    vendorIdentifier: String(d?.vendor_identifier ?? 999),
    vendorName: d?.vendor_name ?? "",
    backupFailureTimeout: String(d?.backup_failure_timeout ?? 60),
  }));
  const { name, description, location, vendorIdentifier, vendorName, backupFailureTimeout } = form;
  const set = (field: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));
  // The BACnet password stays out of the draft: it is write-only (the server
  // never returns it), so it is sent only when typed or cleared.
  const [newPassword, setNewPassword] = useState("");
  const [clearPassword, setClearPassword] = useState(false);
  const passwordSet = device?.bacnet_password_set === true;
  const dirty = device !== undefined && (differs(form, baseline) || newPassword !== "" || clearPassword);
  useUnsavedChanges(dirty, `/devices/${deviceId}`);

  const save = useMutation({
    // Only what changed: a BACnet client may have written the name or
    // description since this tab loaded, and resending it would undo that.
    mutationFn: (sent: typeof form) =>
      devicesApi.update(deviceId, {
        ...changedFields(
          {
            name: sent.name,
            description: sent.description || null,
            location: sent.location || null,
            vendor_identifier: Number(sent.vendorIdentifier),
            // Empty means "unset", not "empty string" -- an empty vendorName
            // would be served to real clients verbatim. null lets the server
            // fall back to "BACpack".
            vendor_name: sent.vendorName || null,
            backup_failure_timeout: Number(sent.backupFailureTimeout),
          },
          {
            name: device?.name,
            description: device?.description || null,
            location: device?.location || null,
            vendor_identifier: device?.vendor_identifier,
            vendor_name: device?.vendor_name || null,
            backup_failure_timeout: device?.backup_failure_timeout,
          }
        ),
        ...(newPassword !== "" ? { bacnet_password: newPassword } : clearPassword ? { bacnet_password: null } : {}),
      }),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      setNewPassword("");
      setClearPassword(false);
      queryClient.invalidateQueries({ queryKey: ["device", deviceId] });
      queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  // Same action as the Process tab's button: identity is only read when the
  // device starts, so the saved settings need one right here.
  const restart = useMutation({
    mutationFn: () => engineApi.restart(deviceId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
    },
  });

  if (!device) return <Loading what="device settings" />;

  return (
    <>
      <LiveApplyBanners status={status} />

      <SettingsColumn>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(form);
        }}
      >
          <Fieldset legend="Identity">
            <Field label="Name">
              <input id="dev_name" required value={name} onChange={set("name")} />
            </Field>
            <Field label="Description">
              <input id="dev_description" value={description} onChange={set("description")} />
            </Field>
            <Field label="Location">
              <input id="dev_location" value={location} onChange={set("location")} />
            </Field>
            <Field label="Backup failure timeout (seconds)">
              <input
                id="dev_backup_failure_timeout"
                type="number"
                required
                min={1}
                max={65535}
                value={backupFailureTimeout}
                onChange={set("backupFailureTimeout")}
              />
            </Field>
          </Fieldset>

          <Fieldset legend="Vendor">
            <FormGrid>
              <Field label="Vendor identifier">
                <input
                  id="dev_vendor_identifier"
                  type="number"
                  required
                  min={0}
                  max={65535}
                  value={vendorIdentifier}
                  onChange={set("vendorIdentifier")}
                />
              </Field>
              <Field label="Vendor name" help="Blank serves BACpack.">
                <input
                  id="dev_vendor_name"
                  value={vendorName}
                  placeholder="BACpack"
                  onChange={set("vendorName")}
                />
              </Field>
            </FormGrid>
          </Fieldset>
          <Fieldset legend="BACnet password">
            <Field
              label="Password for DeviceCommunicationControl and ReinitializeDevice"
              help={
                clearPassword
                  ? newPassword === ""
                    ? "Will be cleared when you save."
                    : "Will be replaced when you save."
                  : `${passwordSet ? "Set." : "Not set."} Up to 20 characters.`
              }
            >
              <input
                id="dev_bacnet_password"
                type="password"
                maxLength={20}
                autoComplete="new-password"
                value={newPassword}
                // clearPassword stays once chosen: a typed value wins on Save, and
                // deleting it again goes back to clearing; only Keep password undoes it.
                onChange={(e) => setNewPassword(e.target.value)}
              />
            </Field>
            {passwordSet && (
              <Button
                type="button"
                small
                onClick={() => {
                  setClearPassword(!clearPassword);
                  setNewPassword("");
                }}
              >
                {clearPassword ? "Keep password" : "Clear password"}
              </Button>
            )}
          </Fieldset>

        <FormActions>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            Save
          </Button>
          {isRunning && (
            <Button type="button" disabled={restart.isPending} onClick={() => restart.mutate()}>
              Restart serving
            </Button>
          )}
        </FormActions>
        <ActionStatus
          action={save}
          failed="Couldn't save"
          done="Saved. Name and vendor apply after a restart."
          hideDone={dirty}
        />
        <ActionStatus action={restart} failed="Couldn't restart" done="Restarted." />
      </Form>
      </SettingsColumn>
    </>
  );
}
