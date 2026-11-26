import { Fragment, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { engineApi, fetchAllObjectsWithDevice, objectsApi } from "../../api/endpoints";
import type {
  BacnetObjectDTO,
  ObjectWithDevice,
  ScheduleDaySchedule,
  ScheduleException,
  ScheduleTimeValue,
  ScheduleValueType,
  ScheduleWriteStatusEntry,
  ScheduleWriteTarget,
} from "../../api/types";
import RemotePointPicker from "../RemotePointPicker";
import CalendarEntryFields from "../CalendarEntryFields";
import { deviceDate, entryProblem } from "../../utils/calendarDates";
import { changedFields } from "../../utils/objectForm";
import ScheduleGrid from "../ScheduleGrid";
import StateEditor from "../StateEditor";
import ValueControl from "../ValueControl";
import { duplicateTimes, firstOtherValue, nextEntryTime, timeProblem, type ValueNaming } from "../../utils/scheduleGrid";
import { renumberValue, scheduleStates, stateProblems } from "../../utils/stateNames";
import { unitShort, unitSuffix } from "../../utils/units";
import { bacnetWritableHelp, canBeScheduleWriteTarget, valueProblem } from "../../utils/objectTypes";
import { WEEKDAY_NAMES } from "../../utils/calendarDates";
import {
  BLANK_DEVICE,
  describeWriteStatus,
  emptyWeeklySchedule,
  isRemoteWriteTarget,
  newScheduleException,
  newRemoteScheduleWriteTarget,
  newScheduleWriteTarget,
  normalizeExceptionSchedule,
  normalizeWeeklySchedule,
  writeTargetStatusKey,
} from "../../utils/scheduleForm";
import Button from "../Button";
import { CheckField, Columns, Field, Fieldset, Form, FormActions, FormGrid, useSubmitProblem } from "../Form";
import { ActionStatus, ErrorBlock, type ActionState } from "../AsyncState";
import { differs, rebase, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import UnitCombobox from "../UnitCombobox";
import Callout from "../Callout";
import IsoEcho from "../IsoEcho";

// The weekly schedule, exceptions and write targets are sub-fields of the
// object's own JSON columns, not separate rows: the editor keeps them as
// client-side arrays and saves them in one PUT.

const SAVED = "Saved.";

// onSaved runs once the server confirmed the save, so the editor counts the
// sent values as saved before the refetch lands.
type SaveFn = (patch: Record<string, unknown>, onSaved: () => void) => void;

export default function ScheduleTab({ deviceId, obj }: { deviceId: number; obj: BacnetObjectDTO }) {
  const queryClient = useQueryClient();

  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  // The device's own clock: schedules and calendars run on it.
  const deviceTime = statuses.find((s) => s.device_id === deviceId)?.device_time ?? null;
  const { data: allObjects = [] } = useQuery({
    queryKey: ["allObjectsWithDevice"],
    queryFn: fetchAllObjectsWithDevice,
  });
  // Remote targets' status, polled on its own so the editor's form state is
  // never reset by a refresh.
  const hasRemote = (obj.schedule_write_targets ?? []).some(isRemoteWriteTarget);
  const { data: writeStatus } = useQuery({
    queryKey: ["scheduleWriteStatus", deviceId, obj.id],
    queryFn: () => objectsApi.get(deviceId, obj.id).then((o) => o.schedule_write_status ?? null),
    enabled: hasRemote,
    refetchInterval: 5000,
  });

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => objectsApi.update(deviceId, obj.id, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });
  const onSave: SaveFn = (patch, onSaved) => save.mutate(patch, { onSuccess: onSaved });

  if (obj.object_type !== "schedule") {
    return <Callout tone="danger">{obj.name} is not a schedule object</Callout>;
  }

  return (
    <ScheduleEditor
      obj={obj}
      deviceId={deviceId}
      allObjects={allObjects}
      // The poll's own null (status cleared) wins over the older status
      // the object was loaded with; that's only the fallback until the
      // first poll answers.
      writeStatus={writeStatus === undefined ? obj.schedule_write_status ?? null : writeStatus}
      deviceTime={deviceTime}
      action={save}
      saving={save.isPending}
      onSave={onSave}
    />
  );
}

// --- Exception time-values editor ---------------------------------------

function TimeValueListEditor({
  label,
  entries,
  naming,
  onChange,
}: {
  label: string;
  entries: ScheduleTimeValue[];
  naming: ValueNaming;
  onChange: (entries: ScheduleTimeValue[]) => void;
}) {
  const dupes = duplicateTimes(entries);
  return (
    <div className="bp-form">
      {entries.map((tv, i) => (
        <Fragment key={i}>
          <div className="bp-list-row">
            <input
              className="bp-input"
              type="time"
              aria-label={`${label} time ${i + 1}`}
              aria-invalid={dupes.has(i) ? true : undefined}
              value={tv.time}
              onChange={(e) => onChange(entries.map((x, j) => (j === i ? { ...x, time: e.target.value } : x)))}
            />
            <IsoEcho value={tv.time} kind="time" zone="device time" />
            <ValueControl
              aria-label={`${label} value ${i + 1}`}
              family={naming.family}
              states={naming.states}
              emptyLabel="End exception"
              value={tv.value ?? ""}
              onChange={(value) => onChange(entries.map((x, j) => (j === i ? { ...x, value } : x)))}
            />
            <Button
              variant="delete"
              small
              type="button"
              aria-label={`Remove ${label} time ${i + 1}`}
              onClick={() => onChange(entries.filter((_, j) => j !== i))}
            >
              Remove
            </Button>
          </div>
          {dupes.has(i) && (
            <p className="bp-field-error" role="alert">
              Entry {i + 1} repeats {tv.time}.
            </p>
          )}
        </Fragment>
      ))}
      <div>
        <Button
          small
          type="button"
          onClick={() => onChange([...entries, { time: nextEntryTime(entries), value: firstOtherValue(naming) }])}
        >
          + Add time
        </Button>
      </div>
    </div>
  );
}

// --- Schedule editor ---------------------------------------------------

interface ScheduleFields {
  valueType: ScheduleValueType;
  scheduleDefault: string;
  unitsValue: string;
  stateText: string[];
  effectiveStart: string;
  effectiveEnd: string;
  priority: string;
  weeklySchedule: ScheduleDaySchedule[];
  exceptionSchedule: ScheduleException[];
  writeTargets: ScheduleWriteTarget[];
  bacnetWritable: boolean;
}

// The editor's fields as the object was loaded -- the base a save is
// compared against, so only what was edited here is sent.
function loadedScheduleFields(obj: BacnetObjectDTO): ScheduleFields {
  return {
    valueType: obj.schedule_value_type ?? "analog",
    scheduleDefault: obj.schedule_default ?? "",
    unitsValue: obj.units ?? "",
    stateText: obj.state_text ?? [],
    effectiveStart: obj.schedule_effective_start ?? "",
    effectiveEnd: obj.schedule_effective_end ?? "",
    priority: String(obj.schedule_priority),
    weeklySchedule: obj.weekly_schedule ?? emptyWeeklySchedule(),
    exceptionSchedule: obj.exception_schedule ?? [],
    writeTargets: obj.schedule_write_targets ?? [],
    bacnetWritable: obj.bacnet_writable,
  };
}

function schedulePatch(f: ScheduleFields): Record<string, unknown> {
  return {
    schedule_value_type: f.valueType,
    schedule_default: f.scheduleDefault || null,
    units: f.valueType === "analog" ? f.unitsValue || null : null,
    state_text:
      f.valueType === "multi-state" ? f.stateText : null,
    schedule_effective_start: f.effectiveStart || null,
    schedule_effective_end: f.effectiveEnd || null,
    schedule_priority: Number(f.priority),
    weekly_schedule: normalizeWeeklySchedule(f.weeklySchedule),
    exception_schedule: normalizeExceptionSchedule(f.exceptionSchedule),
    schedule_write_targets: f.writeTargets,
    bacnet_writable: f.bacnetWritable,
  };
}

function ScheduleEditor({
  obj,
  deviceId,
  allObjects,
  writeStatus,
  deviceTime,
  action,
  saving,
  onSave,
}: {
  obj: BacnetObjectDTO;
  deviceId: number;
  allObjects: ObjectWithDevice[];
  deviceTime: string | null;
  writeStatus: Record<string, ScheduleWriteStatusEntry> | null;
  action: ActionState;
  saving: boolean;
  onSave: SaveFn;
}) {
  const [valueType, setValueType] = useState<ScheduleValueType>(obj.schedule_value_type ?? "analog");
  const [scheduleDefault, setScheduleDefault] = useState(obj.schedule_default ?? "");
  const [unitsValue, setUnitsValue] = useState(obj.units ?? "");
  const [stateText, setStateText] = useState<string[]>(obj.state_text ?? []);
  const [effectiveStart, setEffectiveStart] = useState(obj.schedule_effective_start ?? "");
  const [effectiveEnd, setEffectiveEnd] = useState(obj.schedule_effective_end ?? "");
  const [priority, setPriority] = useState(String(obj.schedule_priority));
  const [weeklySchedule, setWeeklySchedule] = useState<ScheduleDaySchedule[]>(
    obj.weekly_schedule ?? emptyWeeklySchedule()
  );
  const [exceptionSchedule, setExceptionSchedule] = useState<ScheduleException[]>(
    obj.exception_schedule ?? []
  );
  const [writeTargets, setWriteTargets] = useState<ScheduleWriteTarget[]>(obj.schedule_write_targets ?? []);
  // Row keys for writeTargets, so removing a row doesn't hand its picker state to the next one.
  const nextRowId = useRef(0);
  const [targetRowIds, setTargetRowIds] = useState<number[]>(() =>
    (obj.schedule_write_targets ?? []).map(() => nextRowId.current++)
  );
  const addWriteTarget = (target: ScheduleWriteTarget) => {
    const id = nextRowId.current++;
    setWriteTargets((targets) => [...targets, target]);
    setTargetRowIds((ids) => [...ids, id]);
  };
  const removeWriteTarget = (index: number) => {
    setWriteTargets((targets) => targets.filter((_, j) => j !== index));
    setTargetRowIds((ids) => ids.filter((_, j) => j !== index));
  };
  const [bacnetWritable, setBacnetWritable] = useState(obj.bacnet_writable);
  // What the server last confirmed: the loaded object, or the fields a save just sent.
  const [baseline, setBaseline] = useState<ScheduleFields>(() => loadedScheduleFields(obj));

  // A refetch keeps what was edited here (see useDraft).
  const loadedFrom = useRef(obj);
  useEffect(() => {
    if (loadedFrom.current === obj) return;
    loadedFrom.current = obj;
    const fresh = loadedScheduleFields(obj);
    const edited = latest.current;
    const f = rebase(edited.current, edited.baseline, fresh);
    setValueType(f.valueType);
    setScheduleDefault(f.scheduleDefault);
    setUnitsValue(f.unitsValue);
    setStateText(f.stateText);
    setEffectiveStart(f.effectiveStart);
    setEffectiveEnd(f.effectiveEnd);
    setPriority(f.priority);
    setWeeklySchedule(f.weeklySchedule);
    setExceptionSchedule(f.exceptionSchedule);
    if (f.writeTargets !== edited.current.writeTargets) {
      setWriteTargets(f.writeTargets);
      setTargetRowIds(f.writeTargets.map(() => nextRowId.current++));
    }
    setBacnetWritable(f.bacnetWritable);
    setBaseline(fresh);
  }, [obj]);

  const current: ScheduleFields = {
    valueType,
    scheduleDefault,
    unitsValue,
    stateText,
    effectiveStart,
    effectiveEnd,
    priority,
    weeklySchedule,
    exceptionSchedule,
    writeTargets,
    bacnetWritable,
  };
  const naming: ValueNaming = {
    family: valueType,
    states: stateText,
    unit: valueType === "analog" ? unitShort(unitsValue) : "",
    defaultValue: scheduleDefault,
  };
  // Keeps this schedule's own values on their states when the states change;
  // a deleted state's entries become NULL (the default from then on).
  const renumberAll = (renumber: (number | null)[]) => {
    const moved = (value: string | null) => (value ? renumberValue(value, renumber) || null : value);
    setScheduleDefault((value) => renumberValue(value, renumber));
    setWeeklySchedule((days) => days.map((day) => day.map((tv) => ({ ...tv, value: moved(tv.value) }))));
    setExceptionSchedule((events) =>
      events.map((e) => ({ ...e, time_values: e.time_values.map((tv) => ({ ...tv, value: moved(tv.value) })) }))
    );
  };
  const latest = useRef({ current, baseline });
  latest.current = { current, baseline };
  // What a save would send, so normalisation doesn't read as an edit.
  const dirty = differs(schedulePatch(current), schedulePatch(baseline));
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  const calendarObjects = allObjects.filter((o) => o.device_id === deviceId && o.object_type === "calendar");
  const writeTargetCandidates = allObjects.filter(
    (o) => o.device_id === deviceId && o.id !== obj.id && canBeScheduleWriteTarget(o.object_type)
  );

  const highestUsedState = Math.max(0, ...scheduleStates(obj));

  // A new remote row starts on the last remote row's device.
  const lastRemoteDevice = () =>
    [...writeTargets].reverse().find((t) => isRemoteWriteTarget(t) && t.device_instance !== BLANK_DEVICE)
      ?.device_instance ?? BLANK_DEVICE;

  function scheduleProblem(): string | null {
    if (valueType === "multi-state") {
      const states = stateProblems(stateText).message;
      if (states) return states;
    }
    const fits = (value: string | null) => valueProblem(value ?? "", valueType, stateText.length);
    const defaultFit = fits(scheduleDefault);
    if (defaultFit) return `Default value ${defaultFit}.`;
    const entryFit = (entries: ScheduleTimeValue[], where: string) => {
      const bad = entries.find((tv) => fits(tv.value));
      return bad ? `${where} ${bad.time}: ${fits(bad.value)}.` : null;
    };
    for (let d = 0; d < 7; d++) {
      const problem = timeProblem(weeklySchedule[d] ?? [], WEEKDAY_NAMES[d]) ?? entryFit(weeklySchedule[d] ?? [], WEEKDAY_NAMES[d]);
      if (problem) return problem;
    }
    for (let i = 0; i < exceptionSchedule.length; i++) {
      const event = exceptionSchedule[i];
      const period = entryProblem(event.period);
      if (period) return `Exception ${i + 1}: ${period}`;
      const times =
        timeProblem(event.time_values, `exception ${i + 1}`) ?? entryFit(event.time_values, `Exception ${i + 1}`);
      if (times) return times;
    }
    if (writeTargets.some((t) => t.device_instance === BLANK_DEVICE)) return "Every remote write target needs a device instance.";
    return null;
  }

  const problem = useSubmitProblem(scheduleProblem());

  function submit() {
    if (!problem.canSubmit()) return;
    const sent = current;
    onSave(changedFields(schedulePatch(sent), schedulePatch(baseline)), () => setBaseline(sent));
  }

  return (
    <Form
      className="bp-form--wide"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="bp-schedule-top">
        <Fieldset legend="Value">
          <FormGrid>
            <Field label="Value type">
              <select
                id="value_type"
                value={valueType}
                onChange={(e) => setValueType(e.target.value as ScheduleValueType)}
              >
                <option value="analog">Analog</option>
                <option value="binary">Binary</option>
                <option value="multi-state">Multi-state</option>
              </select>
            </Field>
            <Field
              label={`Default value${valueType === "analog" ? unitSuffix(unitsValue) : ""}`}
              help="None releases the targets."
            >
              <ValueControl
                id="schedule_default"
                family={valueType}
                states={stateText}
                emptyLabel="None (release targets)"
                value={scheduleDefault}
                onChange={setScheduleDefault}
              />
            </Field>
            <Field label="Priority for writing (1-16)">
              <input
                id="priority"
                type="number"
                required
                min={1}
                max={16}
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </Field>
          </FormGrid>
          {valueType === "analog" && (
            <Field label="Units">
              <UnitCombobox id="units" value={unitsValue} onChange={setUnitsValue} />
            </Field>
          )}
          {valueType === "multi-state" && obj.schedule_value_type === "multi-state" && !obj.state_text && stateText.length === 0 && (
            // The backend takes a multi-state schedule without names; this tab doesn't save one.
            <Callout tone="info">
              This schedule has no state names.
              {highestUsedState > 0 && ` Its values use states 1 to ${highestUsedState}.`} Name them below to save it.
            </Callout>
          )}
          {valueType === "multi-state" && (
            <StateEditor
              names={stateText}
              savedNames={baseline.stateText}
              onChange={(edit) => {
                setStateText(edit.names);
                renumberAll(edit.renumber);
              }}
            />
          )}
        </Fieldset>

        <Fieldset legend="BACnet access">
          <CheckField
            label="BACnet writable"
            help={bacnetWritableHelp("schedule")}
            checked={bacnetWritable}
            onChange={(e) => setBacnetWritable(e.target.checked)}
          />
        </Fieldset>

        <Fieldset legend="Effective period">
          <p className="bp-help">Blank means always in effect.</p>
          <FormGrid>
            <Field label="Start date" help={<IsoEcho value={effectiveStart} kind="date" />}>
              <input
                id="effective_start"
                type="date"
                value={effectiveStart}
                onChange={(e) => setEffectiveStart(e.target.value)}
              />
            </Field>
            <Field label="End date" help={<IsoEcho value={effectiveEnd} kind="date" />}>
              <input
                id="effective_end"
                type="date"
                value={effectiveEnd}
                onChange={(e) => setEffectiveEnd(e.target.value)}
              />
            </Field>
          </FormGrid>
        </Fieldset>
      </div>

      <Fieldset legend="Weekly schedule">
        <ScheduleGrid days={weeklySchedule} onChange={setWeeklySchedule} naming={naming} deviceTime={deviceTime} />
      </Fieldset>

      <Columns>
        <Fieldset legend="Exception schedule">
          {exceptionSchedule.length === 0 && <p className="bp-muted">No exceptions.</p>}
          {exceptionSchedule.map((event, i) => (
            <Fieldset key={i} legend={`Exception ${i + 1}`}>
              <CalendarEntryFields
                idPrefix={`exception-${i}-period`}
                entry={event.period}
                today={deviceTime ? deviceDate(deviceTime) : null}
                calendars={calendarObjects.map((c) => ({ instance_number: c.instance_number, name: c.name }))}
                onChange={(period) =>
                  setExceptionSchedule((events) => events.map((e, j) => (j === i ? { ...e, period } : e)))
                }
              />
              <Field label="Priority (1 highest, 16 lowest)">
                <input
                  id={`exception_priority_${i}`}
                  type="number"
                  min={1}
                  max={16}
                  value={event.priority}
                  onChange={(e) =>
                    setExceptionSchedule((events) =>
                      events.map((ev, j) => (j === i ? { ...ev, priority: Number(e.target.value) } : ev))
                    )
                  }
                />
              </Field>
              <TimeValueListEditor
                label={`Exception ${i + 1}`}
                entries={event.time_values}
                naming={naming}
                onChange={(time_values) =>
                  setExceptionSchedule((events) => events.map((e, j) => (j === i ? { ...e, time_values } : e)))
                }
              />
              <div>
                <Button
                  variant="delete"
                  small
                  type="button"
                  onClick={() => setExceptionSchedule((events) => events.filter((_, j) => j !== i))}
                >
                  Remove exception
                </Button>
              </div>
            </Fieldset>
          ))}
          <div>
            <Button
              small
              type="button"
              onClick={() => setExceptionSchedule((events) => [...events, newScheduleException()])}
            >
              + Add exception
            </Button>
          </div>
        </Fieldset>

        <Fieldset legend="Write targets">
          {writeTargets.length === 0 && <p className="bp-muted">No write targets.</p>}
          {writeTargets.map((target, i) => {
            const update = (next: ScheduleWriteTarget) =>
              setWriteTargets((targets) => targets.map((t, j) => (j === i ? next : t)));
            const remote = isRemoteWriteTarget(target);
            return (
              <Fieldset key={targetRowIds[i] ?? `new-${i}`} legend={`Write target ${i + 1}`}>
                <FormGrid>
                  <Field label="Where">
                    <select
                      id={`write-target-${i}-where`}
                      value={remote ? "remote" : "local"}
                      onChange={(e) =>
                        update(
                          e.target.value === "remote"
                            ? newRemoteScheduleWriteTarget(lastRemoteDevice())
                            : newScheduleWriteTarget(
                                writeTargetCandidates[0]?.object_type ?? "",
                                writeTargetCandidates[0]?.instance_number ?? 0
                              )
                        )
                      }
                    >
                      <option value="local" disabled={writeTargetCandidates.length === 0}>
                        This device
                      </option>
                      <option value="remote">Another device</option>
                    </select>
                  </Field>
                  {remote ? (
                    <>
                      <Field label="Device instance">
                        <input
                          id={`write-target-${i}-device`}
                          type="number"
                          min={0}
                          max={4194302}
                          value={target.device_instance === BLANK_DEVICE ? "" : target.device_instance ?? ""}
                          onChange={(e) =>
                            update({
                              ...target,
                              device_instance: e.target.value === "" ? BLANK_DEVICE : Number(e.target.value),
                            })
                          }
                        />
                      </Field>
                      <Field label="Remote object type">
                        <input
                          id={`write-target-${i}-type`}
                          type="text"
                          placeholder="e.g. analog-value"
                          value={target.object_type}
                          onChange={(e) => update({ ...target, object_type: e.target.value })}
                        />
                      </Field>
                      <Field label="Remote object instance">
                        <input
                          id={`write-target-${i}-instance`}
                          type="number"
                          min={0}
                          value={target.instance_number}
                          onChange={(e) => update({ ...target, instance_number: Number(e.target.value) })}
                        />
                      </Field>
                      <Field label="Remote property">
                        <input
                          id={`write-target-${i}-property`}
                          type="text"
                          value={target.property_identifier}
                          onChange={(e) => update({ ...target, property_identifier: e.target.value })}
                        />
                      </Field>
                    </>
                  ) : (
                    <Field label="Target object">
                      <select
                        id={`write-target-${i}-object`}
                        value={`${target.object_type}:${target.instance_number}`}
                        onChange={(e) => {
                          const [objectType, instanceNumber] = e.target.value.split(":");
                          update(newScheduleWriteTarget(objectType, Number(instanceNumber)));
                        }}
                      >
                        <option value=":">Choose</option>
                        {writeTargetCandidates.map((o) => (
                          <option key={o.id} value={`${o.object_type}:${o.instance_number}`}>
                            {o.name} ({o.object_type}:{o.instance_number})
                          </option>
                        ))}
                      </select>
                    </Field>
                  )}
                </FormGrid>
                {remote && (
                  <div>
                    <RemotePointPicker
                      deviceId={deviceId}
                      target={
                        target.device_instance === BLANK_DEVICE || target.device_instance == null
                          ? null
                          : { instance: target.device_instance }
                      }
                      onPick={(o) => update({ ...target, object_type: o.object_type, instance_number: o.instance })}
                      offer={(o) => canBeScheduleWriteTarget(o.object_type)}
                    />
                    <span className="bp-muted">
                      {describeWriteStatus(writeStatus?.[writeTargetStatusKey(target)], target.object_type)}
                    </span>
                  </div>
                )}
                <div>
                  <Button variant="delete" small type="button" onClick={() => removeWriteTarget(i)}>
                    Remove
                  </Button>
                </div>
              </Fieldset>
            );
          })}
          <div className="bp-cluster">
            {writeTargetCandidates.length > 0 && (
              <Button
                small
                type="button"
                onClick={() =>
                  addWriteTarget(
                    newScheduleWriteTarget(writeTargetCandidates[0].object_type, writeTargetCandidates[0].instance_number)
                  )
                }
              >
                + Add write target
              </Button>
            )}
            <Button small type="button" onClick={() => addWriteTarget(newRemoteScheduleWriteTarget(lastRemoteDevice()))}>
              + Add remote target
            </Button>
          </div>
        </Fieldset>
      </Columns>

      <FormActions>
        <Button variant="primary" type="submit" disabled={saving}>
          Save
        </Button>
      </FormActions>
      {problem.shown && <ErrorBlock lead="Can't save:">{problem.shown}</ErrorBlock>}
      <ActionStatus action={action} failed="Couldn't save" done={SAVED} hideDone={dirty} />
    </Form>
  );
}
