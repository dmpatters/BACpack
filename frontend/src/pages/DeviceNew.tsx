import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { devicesApi } from "../api/endpoints";
import type { DeviceSummary } from "../api/types";
import Button from "../components/Button";
import Callout from "../components/Callout";
import { Field, Fieldset, Form, FormActions, FormGrid } from "../components/Form";
import { ActionStatus } from "../components/AsyncState";

// The New device panel's form on the Devices page.
export default function NewDeviceForm({ onCreated }: { onCreated: (device: DeviceSummary) => void }) {
  const queryClient = useQueryClient();
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [location, setLocation] = useState("");
  const [vendorIdentifier, setVendorIdentifier] = useState("999");
  const [vendorName, setVendorName] = useState("");

  const createDevice = useMutation({
    mutationFn: () =>
      devicesApi.create({
        id: Number(id),
        name,
        description: description || undefined,
        location: location || undefined,
        vendor_identifier: vendorIdentifier ? Number(vendorIdentifier) : undefined,
        vendor_name: vendorName || undefined,
      }),
    onSuccess: (device) => {
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      onCreated(device);
    },
  });

  return (
    <>
      <Callout tone="info">
        Uses the default bind address from <Link to="/settings/network">Network Settings</Link>.
      </Callout>

      <Form
        onSubmit={(e) => {
          e.preventDefault();
          createDevice.mutate();
        }}
      >
        <Fieldset legend="Device">
          <FormGrid>
            <Field label="Device instance number">
              <input
                id="id"
                type="number"
                min={0}
                max={4194302}
                required
                value={id}
                onChange={(e) => setId(e.target.value)}
              />
            </Field>
            <Field label="Name">
              <input id="name" type="text" required value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
          </FormGrid>
          <Field label="Description">
            <input id="description" type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <Field label="Location">
            <input id="location" type="text" value={location} onChange={(e) => setLocation(e.target.value)} />
          </Field>
          <FormGrid>
            <Field label="Vendor identifier">
              <input
                id="vendor_identifier"
                type="number"
                value={vendorIdentifier}
                onChange={(e) => setVendorIdentifier(e.target.value)}
              />
            </Field>
            <Field label="Vendor name" help="Blank serves BACpack.">
              <input
                id="vendor_name"
                type="text"
                placeholder="BACpack"
                value={vendorName}
                onChange={(e) => setVendorName(e.target.value)}
              />
            </Field>
          </FormGrid>
        </Fieldset>

        <FormActions>
          <Button variant="primary" type="submit" disabled={createDevice.isPending}>
            Create device
          </Button>
        </FormActions>
        <ActionStatus action={createDevice} failed="Couldn't create the device" />
      </Form>
    </>
  );
}
