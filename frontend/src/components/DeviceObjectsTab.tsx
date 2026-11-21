import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { objectsApi } from "../api/endpoints";
import type { BacnetObjectDTO, CalendarEntry, ScheduleValueType } from "../api/types";
import { MAX_TREND_SAMPLE_CAP, OBJECT_TYPES } from "../constants";
import Banner, { LoadError, errorMessage, type Notice } from "./Banner";
import ObjectRow from "./ObjectRow";
import BulkEditPanel from "./BulkEditPanel";
import BulkSimulatePanel from "./BulkSimulatePanel";
import { commonFamily } from "../utils/simulation";
import { isMultiState } from "../utils/objectForm";
import StateEditor from "./StateEditor";
import CalendarEntryFields from "./CalendarEntryFields";
import { entryProblem } from "../utils/calendarDates";
import ValueControl from "./ValueControl";
import { renumberValue, stateProblems } from "../utils/stateNames";
import { unitSuffix } from "../utils/units";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid, useSubmitProblem } from "./Form";
import { ActionStatus, ErrorBlock, Loading } from "./AsyncState";
import SidePanel from "./SidePanel";
import { OVERLAY_FROM_LIST } from "../utils/overlayState";
import UnitCombobox from "./UnitCombobox";
import { bacnetWritableHelp, hasRelinquishDefault, hasUnits, valueFamily, valueProblem } from "../utils/objectTypes";
import { emptyWeeklySchedule, newCalendarEntry } from "../utils/scheduleForm";
import Button from "./Button";
import { useConfirm } from "./ConfirmDialog";
import { SortHeader, TableFrame } from "./DataTable";
import TableToolbar, { countText } from "./TableToolbar";
import { matchesSearch, sortRows, useSort } from "../utils/tableSort";
import { plural } from "../utils/text";

export function visibleObjects(objects: BacnetObjectDTO[], search: string, type: string): BacnetObjectDTO[] {
  return objects.filter(
    (o) =>
      (type === "" || o.object_type === type) &&
      matchesSearch(search, o.name, `${o.object_type}:${o.instance_number}`, o.description)
  );
}

type ObjectSortKey = "id" | "name";
const SORT_KEYS: Record<ObjectSortKey, (o: BacnetObjectDTO) => string> = {
  // Type, then instance as a number: analog-value:2 before analog-value:10.
  id: (o) => `${o.object_type}:${String(o.instance_number).padStart(10, "0")}`,
  name: (o) => o.name,
};

export default function DeviceObjectsTab({ deviceId }: { deviceId: number }) {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // One panel at a time; each keeps its draft while hidden (SidePanel).
  const [panel, setPanel] = useState<"add" | "edit" | "simulate" | null>(null);
  // Simulate's draft belongs to a value family; it stays across a selection
  // that is momentarily empty.
  const simulateFamily = useRef("none");
  // Bumped by a successful apply, so the bulk panels start clean next time.
  const [applied, setApplied] = useState(0);
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const { sort, toggle } = useSort<ObjectSortKey>();
  const { ask, dialog } = useConfirm();

  const {
    data: objects = [],
    error: objectsError,
    isPending: objectsPending,
    refetch: refetchObjects,
  } = useQuery({
    queryKey: ["objects", deviceId],
    queryFn: () => objectsApi.list(deviceId),
  });

  const invalidateObjects = () => queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });

  const deleteObject = useMutation({
    mutationFn: (objectId: number) => objectsApi.remove(deviceId, objectId),
    onSuccess: (_result, objectId) => {
      invalidateObjects();
      // A deleted id left selected would make the next bulk action 404.
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(objectId);
        return next;
      });
      setNotice({ type: "ok", message: "object deleted" });
    },
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
  });

  const batchUpdate = useMutation({
    mutationFn: ({ patch, randomPhase }: { patch: Record<string, unknown>; randomPhase?: boolean; fromEditPanel?: boolean }) =>
      objectsApi.batchUpdate(deviceId, [...selected], patch, randomPhase ? { randomPhase } : {}),
    onSuccess: (updated) => {
      invalidateObjects();
      setSelected(new Set());
      setPanel(null);
      setApplied((n) => n + 1);
      setNotice({ type: "ok", message: `updated ${plural(updated.length, "object")}` });
    },
    // The edit panel shows its own failure under its Apply button.
    onError: (err, sent) => {
      if (!sent.fromEditPanel) setNotice({ type: "error", message: errorMessage(err) });
    },
  });

  const batchDelete = useMutation({
    mutationFn: () => objectsApi.batchDelete(deviceId, [...selected]),
    onSuccess: (result) => {
      invalidateObjects();
      setSelected(new Set());
      setNotice({ type: "ok", message: `deleted ${plural(result.deleted, "object")}` });
    },
    onError: (err) => setNotice({ type: "error", message: errorMessage(err) }),
  });

  const shown = sortRows(visibleObjects(objects, search, typeFilter), sort, SORT_KEYS);

  // A row the search or filter hides leaves the selection, so a bulk action
  // never touches a row the user can't see.
  function changeView(nextSearch: string, nextType: string) {
    setSearch(nextSearch);
    setTypeFilter(nextType);
    const keep = new Set(visibleObjects(objects, nextSearch, nextType).map((o) => o.id));
    setSelected((prev) => new Set([...prev].filter((id) => keep.has(id))));
  }

  const allShownSelected = shown.length > 0 && shown.every((o) => selected.has(o.id));
  const someShownSelected = !allShownSelected && shown.some((o) => selected.has(o.id));
  function toggleSelectAll() {
    setSelected(allShownSelected ? new Set() : new Set(shown.map((o) => o.id)));
  }
  function toggleSelect(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function askDelete(obj: BacnetObjectDTO) {
    ask({
      title: `Delete ${obj.name}?`,
      body: (
        <>
          This removes{" "}
          <strong>
            {obj.object_type}:{obj.instance_number}
          </strong>{" "}
          and its trend history.
        </>
      ),
      confirmLabel: "Delete object",
      onConfirm: () => deleteObject.mutateAsync(obj.id),
    });
  }

  function askBulkDelete() {
    const n = selected.size;
    ask({
      title: `Delete ${n} ${n === 1 ? "object" : "objects"}?`,
      body: "This removes them and their trend history.",
      confirmLabel: n === 1 ? "Delete object" : "Delete objects",
      onConfirm: () => batchDelete.mutateAsync(),
    });
  }

  const toolbarShown = !objectsPending && !objectsError && objects.length > 0;
  const addButton = (
    <Button small={toolbarShown} type="button" aria-haspopup="dialog" onClick={() => setPanel("add")}>
      Add object
    </Button>
  );
  const selectedTypes = objects.filter((o) => selected.has(o.id)).map((o) => o.object_type);
  const selectedFamily = commonFamily(selectedTypes);
  if (selectedFamily) simulateFamily.current = selectedFamily;

  return (
    <>
      <Banner notice={notice} />
      {dialog}
      {!toolbarShown && <div className="bp-toolbar">{addButton}</div>}
      {toolbarShown && (
        <TableToolbar
          actions={addButton}
          search={search}
          onSearch={(v) => changeView(v, typeFilter)}
          searchLabel="Search objects"
          placeholder="Search name, id or description"
          filter={{
            label: "Object type",
            value: typeFilter,
            onChange: (v) => changeView(search, v),
            options: [
              { value: "", label: "All types" },
              ...[...new Set(objects.map((o) => o.object_type))].sort().map((t) => ({ value: t, label: t })),
            ],
          }}
          count={countText(shown.length, objects.length, "object")}
          selectedCount={selected.size}
          onClearSelection={() => setSelected(new Set())}
          bulkActions={
            <>
              <Button
                small
                type="button"
                aria-haspopup="dialog"
                onClick={() => {
                  // One object ticked: its own page is the better editor.
                  if (selected.size === 1) {
                    navigate(`/devices/${deviceId}/objects/${[...selected][0]}`, { state: OVERLAY_FROM_LIST });
                  } else {
                    setPanel("edit");
                  }
                }}
              >
                Edit selected
              </Button>
              <Button small type="button" aria-haspopup="dialog" onClick={() => setPanel("simulate")}>
                Simulate selected
              </Button>
              <Button
                small
                type="button"
                disabled={batchUpdate.isPending}
                onClick={() => batchUpdate.mutate({ patch: { simulation: null } })}
              >
                Stop simulating
              </Button>
              <Button variant="delete" small type="button" onClick={askBulkDelete}>
                Delete selected
              </Button>
            </>
          }
        />
      )}

      <SidePanel open={panel === "edit"} title={bulkTitle("Edit", selected.size)} onClose={() => setPanel(null)}>
        <BulkEditPanel
          key={applied}
          selectedCount={selected.size}
          selectedTypes={selectedTypes}
          applying={batchUpdate.isPending}
          applyError={batchUpdate.isError && batchUpdate.variables?.fromEditPanel ? batchUpdate.error : null}
          onApply={(patch) => batchUpdate.mutate({ patch, fromEditPanel: true })}
          onCancel={() => setPanel(null)}
        />
      </SidePanel>

      <SidePanel open={panel === "simulate"} title={bulkTitle("Simulate", selected.size)} onClose={() => setPanel(null)}>
        <BulkSimulatePanel
          key={`${applied}:${selectedTypes.length === 0 ? simulateFamily.current : (selectedFamily ?? "none")}`}
          objectTypes={selectedTypes}
          selectedCount={selected.size}
          applying={batchUpdate.isPending}
          onApply={(simulation, randomPhase) => batchUpdate.mutate({ patch: { simulation }, randomPhase })}
          onCancel={() => setPanel(null)}
        />
      </SidePanel>

      {objectsError ? (
        <LoadError what="objects" error={objectsError} onRetry={() => refetchObjects()} />
      ) : objectsPending ? (
        <Loading what="objects" />
      ) : objects.length === 0 ? (
        <p className="bp-muted">No objects yet.</p>
      ) : shown.length === 0 ? (
        <p className="bp-muted">No objects match.</p>
      ) : (
        <TableFrame label="Objects" scroll>
          <thead>
            <tr>
              <th className="bp-cell-check">
                <input
                  type="checkbox"
                  checked={allShownSelected}
                  // Mixed has no attribute; a new callback each render keeps it current.
                  ref={(el) => {
                    if (el) el.indeterminate = someShownSelected;
                  }}
                  onChange={toggleSelectAll}
                  aria-label="Select all objects"
                />
              </th>
              <SortHeader label="Id" sortKey="id" sort={sort} onSort={toggle} />
              <SortHeader label="Name" sortKey="name" sort={sort} onSort={toggle} />
              <th>Description</th>
              <th>Units</th>
              <th>Flags</th>
              <th className="bp-cell-actions">
                <span className="bp-muted">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((obj) => (
              <ObjectRow
                key={obj.id}
                obj={obj}
                deviceId={deviceId}
                selected={selected.has(obj.id)}
                onToggleSelect={toggleSelect}
                onDelete={() => askDelete(obj)}
              />
            ))}
          </tbody>
        </TableFrame>
      )}

      <AddObjectForm deviceId={deviceId} objects={objects} open={panel === "add"} onClose={() => setPanel(null)} />
    </>
  );
}

// "Edit 3 objects at once", "Edit 1 object": the panel follows the selection,
// which can change while it is open.
function bulkTitle(verb: "Edit" | "Simulate", count: number): string {
  if (count === 0) return "No objects selected";
  if (count === 1) return `${verb} 1 object`;
  return verb === "Edit" ? `Edit ${count} objects at once` : `Simulate ${count} objects`;
}

// Next free instance number for `type` -- one past the highest instance
// already used by that object type on this device, or 1 if none yet.
// Instance numbers are only unique per (object_type, instance_number), so
// this is computed per type, not globally.
function nextInstanceFor(objects: BacnetObjectDTO[], type: string): number {
  const used = objects.filter((o) => o.object_type === type).map((o) => o.instance_number);
  return used.length ? Math.max(...used) + 1 : 1;
}

// The add form has no saved states for references to point at.
const NO_SAVED_STATES: string[] = [];

function AddObjectForm({
  deviceId,
  objects,
  open,
  onClose,
}: {
  deviceId: number;
  objects: BacnetObjectDTO[];
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [objectType, setObjectType] = useState<string>(OBJECT_TYPES[0]);
  const [instanceNumber, setInstanceNumber] = useState(() => String(nextInstanceFor(objects, OBJECT_TYPES[0])));
  // Tracks whether the user has hand-edited the suggested instance number.
  // While false, the field keeps following the real objects list as it loads
  // (it starts empty before the query resolves) and follows type switches;
  // once true, we leave the user's own value alone.
  const instanceTouchedRef = useRef(false);
  useEffect(() => {
    if (!instanceTouchedRef.current) {
      setInstanceNumber(String(nextInstanceFor(objects, objectType)));
    }
  }, [objects, objectType]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [units, setUnits] = useState("");
  const [stateText, setStateText] = useState<string[]>(["State 1", "State 2"]);
  const [bacnetWritable, setBacnetWritable] = useState(true);
  const [relinquishDefault, setRelinquishDefault] = useState("");
  const [outOfServiceDefault, setOutOfServiceDefault] = useState(false);
  const [trended, setTrended] = useState(false);
  const [trendMode, setTrendMode] = useState<"interval" | "cov">("interval");
  const [trendIntervalSeconds, setTrendIntervalSeconds] = useState("");
  const [trendSampleCap, setTrendSampleCap] = useState("");
  // Schedule/calendar -- creation stays minimal (just enough to pass
  // validate_schedule_config/validate_calendar_config's required-field
  // checks); the full weekly-schedule/exceptions/write-targets/date-list
  // editor is the new detail view (see "Configure" on the object row),
  // reached only after the object exists.
  const [scheduleValueType, setScheduleValueType] = useState<ScheduleValueType>("analog");
  const [scheduleDefault, setScheduleDefault] = useState("");
  const [firstDate, setFirstDate] = useState<CalendarEntry>(() => newCalendarEntry("date"));
  // A calendar may start with no dates: it is then never in effect.
  const [withFirstDate, setWithFirstDate] = useState(true);
  const [lastAdded, setLastAdded] = useState("");
  // The "Added" line goes once the next object is being typed.
  const [editedSinceAdd, setEditedSinceAdd] = useState(false);

  const isSchedule = objectType === "schedule";
  const isCalendar = objectType === "calendar";
  const isNotificationClass = objectType === "notification-class";

  const create = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        object_type: objectType,
        instance_number: Number(instanceNumber),
        name,
        description: description || undefined,
        trended: trended && !isNotificationClass,
      };
      // A hidden field keeps its value in the form but isn't sent.
      if (trended && !isNotificationClass) {
        body.trend_mode = trendMode;
        if (trendMode === "interval" && trendIntervalSeconds) body.trend_interval_seconds = Number(trendIntervalSeconds);
        if (trendSampleCap) body.trend_sample_cap = Number(trendSampleCap);
      }
      // BACnet writable applies to every type: presentValue for
      // commandable Output/Value types, full config (weekly schedule/
      // exceptions/date list/...) for schedule/calendar -- see
      // WritePropertyAllowlisted* in bacnet.py.
      body.bacnet_writable = bacnetWritable;
      if (isSchedule) {
        // An empty weekly_schedule (7 days, no time-values yet): the
        // schedule simply always reads as schedule_default until real
        // time-values are added via the Configure page.
        body.schedule_value_type = scheduleValueType;
        body.schedule_default = scheduleDefault;
        body.weekly_schedule = emptyWeeklySchedule();
        body.out_of_service_default = outOfServiceDefault;
        if (scheduleValueType === "analog") body.units = units || undefined;
        if (scheduleValueType === "multi-state") {
          body.state_text = stateText;
        }
      } else if (isCalendar) {
        body.date_list = withFirstDate ? [firstDate] : [];
      } else if (isNotificationClass) {
        // Minimal creation -- priority/ack_required/recipient_list all
        // default server-side ([100,100,100]/all-True/[]). The full
        // recipient-list editor lives on the Configure page, reached only
        // after the object exists, same pattern as Schedule/Calendar.
      } else {
        if (hasUnits(objectType)) body.units = units || undefined;
        if (hasRelinquishDefault(objectType)) body.relinquish_default = relinquishDefault || undefined;
        body.out_of_service_default = outOfServiceDefault;
        if (isMultiState(objectType)) {
          body.state_text = stateText;
        }
      }
      return objectsApi.create(deviceId, body);
    },
    onSuccess: (obj) => {
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
      // Bump past the number just used so the next add of the same type is
      // pre-filled too, without waiting on the objects list to refetch; going
      // back to auto-follow mode lets that refetch correct it if needed.
      instanceTouchedRef.current = false;
      setInstanceNumber(String(Number(instanceNumber) + 1));
      // Only the identity clears: the other settings stay for the next add,
      // which is usually a similar object.
      setName("");
      setDescription("");
      setLastAdded(obj.name);
      setEditedSinceAdd(false);
    },
  });

  const showUnits = hasUnits(objectType, isSchedule ? scheduleValueType : null);
  const showStates = isMultiState(objectType) || (isSchedule && scheduleValueType === "multi-state");
  const showRelinquish = hasRelinquishDefault(objectType);

  const valueFit = (): string | null => {
    const family = isSchedule ? scheduleValueType : valueFamily(objectType);
    const value = isSchedule ? scheduleDefault : showRelinquish ? relinquishDefault : "";
    const problem = family ? valueProblem(value, family, stateText.length) : null;
    if (!problem) return null;
    return `${isSchedule ? "Schedule default value" : "Relinquish default"} ${problem}.`;
  };
  const addProblem = useSubmitProblem(
    (showStates ? stateProblems(stateText).message : null) ?? (isCalendar && withFirstDate ? entryProblem(firstDate) : null) ?? valueFit()
  );

  return (
    <SidePanel open={open} title="Add object" onClose={onClose}>
      <Form
          aria-label="Add object"
          onChange={() => setEditedSinceAdd(true)}
          onSubmit={(e) => {
            e.preventDefault();
            if (addProblem.canSubmit()) create.mutate();
          }}
        >
          <Fieldset legend="Object">
            <FormGrid>
              <Field label="Type">
                <select
                  id="object_type"
                  value={objectType}
                  onChange={(e) => {
                    // Switching type switches instance-number namespaces too --
                    // go back to auto-suggesting for the newly selected type.
                    instanceTouchedRef.current = false;
                    setObjectType(e.target.value);
                  }}
                >
                  {OBJECT_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Instance number">
                <input
                  id="instance_number"
                  type="number"
                  min={0}
                  required
                  value={instanceNumber}
                  onChange={(e) => {
                    instanceTouchedRef.current = true;
                    setInstanceNumber(e.target.value);
                  }}
                />
              </Field>
              <Field label="Name">
                <input id="name" type="text" required value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
            </FormGrid>
            <Field label="Description">
              <input id="description" type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>

            {isSchedule && (
              <FormGrid>
                <Field label="Schedule value type">
                  <select
                    id="schedule_value_type"
                    value={scheduleValueType}
                    onChange={(e) => setScheduleValueType(e.target.value as ScheduleValueType)}
                  >
                    <option value="analog">Analog</option>
                    <option value="binary">Binary</option>
                    <option value="multi-state">Multi-state</option>
                  </select>
                </Field>
                <Field label={`Schedule default value${scheduleValueType === "analog" ? unitSuffix(units) : ""}`}>
                  <ValueControl
                    id="schedule_default"
                    family={scheduleValueType}
                    states={stateText}
                    required
                    value={scheduleDefault}
                    onChange={setScheduleDefault}
                  />
                </Field>
              </FormGrid>
            )}
            {isCalendar && (
              <Fieldset legend="First date">
                <CheckField
                  label="Start with a date"

                  checked={withFirstDate}
                  onChange={(e) => setWithFirstDate(e.target.checked)}
                />
                {withFirstDate && (
                  <CalendarEntryFields idPrefix="first_date" entry={firstDate} onChange={setFirstDate} today={null} />
                )}
              </Fieldset>
            )}
            {showUnits && (
              <Field label="Units">
                <UnitCombobox id="units" value={units} onChange={setUnits} />
              </Field>
            )}
            {showStates && (
              <StateEditor
                names={stateText}
                savedNames={NO_SAVED_STATES}
                onChange={(edit) => {
                  setStateText(edit.names);
                  setRelinquishDefault((value) => renumberValue(value, edit.renumber));
                  setScheduleDefault((value) => (isSchedule ? renumberValue(value, edit.renumber) : value));
                }}
              />
            )}
            {showRelinquish && (
              <Field
                label={`Relinquish default${valueFamily(objectType) === "analog" ? unitSuffix(units) : ""}`}

              >
                <ValueControl
                  id="relinquish_default"
                  family={valueFamily(objectType) ?? "analog"}
                  states={stateText}
                  emptyLabel="None"
                  value={relinquishDefault}
                  onChange={setRelinquishDefault}
                />
              </Field>
            )}
          </Fieldset>

          <Fieldset legend="Behavior">
            <CheckField
              label="BACnet writable"
              help={bacnetWritableHelp(objectType)}
              checked={bacnetWritable}
              onChange={(e) => setBacnetWritable(e.target.checked)}
            />
            {!isCalendar && !isNotificationClass && (
              <CheckField
                label="Out of service by default"
                checked={outOfServiceDefault}
                onChange={(e) => setOutOfServiceDefault(e.target.checked)}
              />
            )}
          </Fieldset>

          {!isNotificationClass && (
          <Fieldset legend="Trending">
            <CheckField label="Trended" checked={trended} onChange={(e) => setTrended(e.target.checked)} />
            {trended && (
              <FormGrid>
                <Field label="Trend mode">
                  <select id="trend_mode" value={trendMode} onChange={(e) => setTrendMode(e.target.value as "interval" | "cov")}>
                    <option value="interval">Interval</option>
                    <option value="cov">COV</option>
                  </select>
                </Field>
                {trendMode === "interval" && (
                  <Field label="Interval (s)">
                    <input
                      id="trend_interval_seconds"
                      type="number"
                      min={1}
                      placeholder="e.g. 60"
                      value={trendIntervalSeconds}
                      onChange={(e) => setTrendIntervalSeconds(e.target.value)}
                    />
                  </Field>
                )}
                <Field label="Sample cap" help={`Up to ${MAX_TREND_SAMPLE_CAP}`}>
                  <input
                    id="trend_sample_cap"
                    type="number"
                    min={1}
                    max={MAX_TREND_SAMPLE_CAP}
                    value={trendSampleCap}
                    onChange={(e) => setTrendSampleCap(e.target.value)}
                  />
                </Field>
              </FormGrid>
            )}
          </Fieldset>
          )}

          <FormActions>
            <Button variant="primary" type="submit" disabled={create.isPending}>
              Add object
            </Button>
            <Button variant="ghost" type="button" onClick={onClose}>
              Cancel
            </Button>
          </FormActions>
          {addProblem.shown && <ErrorBlock lead="Can't add:">{addProblem.shown}</ErrorBlock>}
          <ActionStatus
            action={create}
            failed="Couldn't add the object"
            done={lastAdded && `Added ${lastAdded}.`}
            hideDone={editedSinceAdd}
          />
      </Form>
    </SidePanel>
  );
}
