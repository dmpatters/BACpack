import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { devicesApi } from "../api/endpoints";
import type { DeviceClockState } from "../api/types";
import { describeOffset, toDatetimeLocal, wallTime } from "../utils/deviceClock";
import Button from "./Button";
import { Field, Form, FormActions } from "./Form";
import { ActionStatus } from "./AsyncState";
import IsoEcho from "./IsoEcho";

type ClockBody = { device_time: string } | { offset_seconds: number };

// Time sync. Shows and sets one device's clock; refreshes through the Process
// tab's own status polling.
export default function DeviceClock({ deviceId, clock }: { deviceId: number; clock: DeviceClockState }) {
  const queryClient = useQueryClient();
  // null = follow the polled device time; a string = the user is editing.
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? toDatetimeLocal(clock.device_time);
  const inSync = Math.abs(clock.clock_offset_seconds) < 1;

  const setClock = useMutation({
    mutationFn: (body: ClockBody) => devicesApi.setClock(deviceId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
      setDraft(null);
    },
  });
  const reset = setClock.variables !== undefined && "offset_seconds" in setClock.variables;

  return (
    <section data-testid="device-clock">
      <h2>Device clock</h2>
      <p data-testid="device-clock-reading">
        {wallTime(clock.device_time)} device time · {describeOffset(clock.clock_offset_seconds)}
        {clock.clock_change_pending && " · applying…"}
      </p>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          // datetime-local drops ":SS" when seconds are zero
          setClock.mutate({ device_time: value.length === 16 ? `${value}:00` : value });
        }}
      >
        <Field label="New device time" help={<IsoEcho value={value} kind="datetime" zone="device time" />}>
          <input
            id="device_clock_input"
            type="datetime-local"
            step={1}
            value={value}
            onChange={(e) => setDraft(e.target.value)}
          />
        </Field>
        <FormActions>
          <Button variant="primary" type="submit" disabled={setClock.isPending || !value}>
            Set device clock
          </Button>
          <Button type="button" disabled={setClock.isPending || inSync} onClick={() => setClock.mutate({ offset_seconds: 0 })}>
            Reset to host time
          </Button>
        </FormActions>
        <ActionStatus
          action={setClock}
          failed="Couldn't set the device clock"
          done={reset ? "Device clock reset to host time." : "Device clock set."}
        />
      </Form>
    </section>
  );
}
