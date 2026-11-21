import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { engineApi, objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO, CalendarEntry } from "../../api/types";
import {
  ENTRY_KIND_LABELS,
  describeEntry,
  deviceDate,
  entryMatches,
  entryProblem,
  isoDate,
  nextMatches,
  rawEntry,
} from "../../utils/calendarDates";
import { changedFields } from "../../utils/objectForm";
import { bacnetWritableHelp } from "../../utils/objectTypes";
import { newCalendarEntry } from "../../utils/scheduleForm";
import Button from "../Button";
import CalendarEntryFields from "../CalendarEntryFields";
import { RowActions, TableFrame } from "../DataTable";
import { CheckField, Fieldset, Form, FormActions } from "../Form";
import { ActionStatus, ErrorBlock } from "../AsyncState";
import SidePanel from "../SidePanel";
import StatusPill from "../StatusPill";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import { useObjectLive } from "../../utils/useLiveState";

const blankEntry = () => newCalendarEntry("date");

// docs/design-system/components/CalendarEditor. The entry form changes the
// list here; Save sends the list, so a client's own Date_List write isn't
// overwritten unless this list changed.
export default function DatesTab({ deviceId, obj }: { deviceId: number; obj: BacnetObjectDTO }) {
  const queryClient = useQueryClient();
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const deviceTime = statuses.find((s) => s.device_id === deviceId)?.device_time;
  const today = deviceTime ? deviceDate(deviceTime) : null;

  const { form, setForm, baseline, setBaseline } = useDraft(obj, (o) => ({
    dateList: o.date_list ?? [],
    bacnetWritable: o.bacnet_writable,
  }));
  const [editing, setEditing] = useState<number | null>(null);
  const [entry, setEntry] = useState<CalendarEntry>(blankEntry);
  const [entryError, setEntryError] = useState<string | null>(null);
  // Closing the panel hides the entry form and keeps what was typed.
  const [panelOpen, setPanelOpen] = useState(false);

  const entryPending = editing !== null || differs(entry, blankEntry());
  const dirty = differs(form, baseline) || entryPending;
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  const save = useMutation({
    mutationFn: (sent: typeof form) =>
      objectsApi.update(
        deviceId,
        obj.id,
        changedFields(
          { date_list: sent.dateList, bacnet_writable: sent.bacnetWritable },
          { date_list: obj.date_list ?? [], bacnet_writable: obj.bacnet_writable }
        )
      ),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });

  const resetEntry = () => {
    setEditing(null);
    setEntry(blankEntry());
    setEntryError(null);
    setPanelOpen(false);
  };

  // A running device's calendar answers for itself; the saved list, on the
  // device's date, is the fallback.
  const live = useObjectLive(deviceId, obj.id);
  const livePresent = live.status === "live" ? live.data.present_value : undefined;
  const inToday =
    typeof livePresent === "boolean"
      ? livePresent
      : today !== null && baseline.dateList.some((e) => entryMatches(e, today));
  const entryTitle = editing === null ? "Add entry" : `Edit entry ${editing + 1}`;

  return (
    <div className="bp-stack">
      {today && (
        <div className="bp-cluster">
          <span className="bp-label">Today, {isoDate(today)} (device time)</span>
          <StatusPill tone={inToday ? "ok" : undefined} plain={!inToday}>
            {inToday ? "today is in calendar" : "not in calendar"}
          </StatusPill>
        </div>
      )}

      <div>
        <Button
          type="button"
          aria-haspopup="dialog"
          // Reopens whatever is half-typed, an edit included.
          onClick={() => setPanelOpen(true)}
        >
          {editing === null ? "Add entry" : `Continue editing entry ${editing + 1}`}
        </Button>
      </div>

      <TableFrame label="Dates">
        <thead>
          <tr>
            <th>Kind</th>
            <th>Matches</th>
            <th>BACnet entry</th>
            <th>
              <span className="bp-visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {form.dateList.map((e, i) => (
            <tr key={i}>
              <td>
                <StatusPill plain>{ENTRY_KIND_LABELS[e.kind]}</StatusPill>
              </td>
              <td>
                {describeEntry(e)}
                {today && nextMatches(e, today, 1).length === 0 && (
                  <>
                    {" "}
                    <StatusPill tone="warn">matches no date</StatusPill>
                  </>
                )}
              </td>
              <td className="bp-cell-id">{rawEntry(e)}</td>
              <RowActions
                onEdit={() => {
                  setEditing(i);
                  setEntry(e);
                  setEntryError(null);
                  setPanelOpen(true);
                }}
                onDelete={() => {
                  setForm((f) => ({ ...f, dateList: f.dateList.filter((_, j) => j !== i) }));
                  if (editing === i) resetEntry();
                  // The edited entry moves up when an earlier one goes.
                  else if (editing !== null && i < editing) setEditing(editing - 1);
                }}
              />
            </tr>
          ))}
          {form.dateList.length === 0 && (
            <tr>
              {/* No dates is valid: the calendar is never in effect (Clause 12.9). */}
              <td colSpan={4} className="bp-muted">
                No dates.
              </td>
            </tr>
          )}
        </tbody>
      </TableFrame>

      <SidePanel open={panelOpen} title={entryTitle} onClose={() => setPanelOpen(false)}>
        <Form
          aria-label={entryTitle}
          onSubmit={(e) => {
            e.preventDefault();
            const problem = entryProblem(entry);
            setEntryError(problem);
            if (problem) return;
            setForm((f) => ({
              ...f,
              dateList: editing === null ? [...f.dateList, entry] : f.dateList.map((old, j) => (j === editing ? entry : old)),
            }));
            resetEntry();
          }}
        >
          <CalendarEntryFields idPrefix="date" entry={entry} onChange={setEntry} today={today} />
          <FormActions>
            <Button variant="primary" type="submit">
              {editing === null ? "Add entry" : "Update entry"}
            </Button>
            {editing !== null && (
              <Button variant="ghost" type="button" onClick={resetEntry}>
                Cancel
              </Button>
            )}
          </FormActions>
          {entryError && (
            <ErrorBlock lead={editing === null ? "Can't add:" : "Can't update:"}>{entryError}</ErrorBlock>
          )}
        </Form>
      </SidePanel>

      <Form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(form);
        }}
      >
        <Fieldset legend="BACnet access">
          <CheckField
            label="BACnet writable"
            help={bacnetWritableHelp("calendar")}
            checked={form.bacnetWritable}
            onChange={(e) => setForm((f) => ({ ...f, bacnetWritable: e.target.checked }))}
          />
        </Fieldset>
        <FormActions>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            Save
          </Button>
        </FormActions>
        <ActionStatus
          action={save}
          failed="Couldn't save"
          done="Saved."
          hideDone={differs(form, baseline)}
        />
      </Form>
    </div>
  );
}
