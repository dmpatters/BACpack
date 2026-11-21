import { useMutation, useQueryClient } from "@tanstack/react-query";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO } from "../../api/types";
import { formStateToPatch, isMultiState, toFormState, type ObjectFormState } from "../../utils/objectForm";
import StateEditor from "../StateEditor";
import ValueControl from "../ValueControl";
import { renumberValue, stateProblems, stateReferences } from "../../utils/stateNames";
import { bacnetWritableHelp, hasRelinquishDefault, hasUnits, isInput, valueFamily } from "../../utils/objectTypes";
import { unitSuffix } from "../../utils/units";
import Button from "../Button";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid, useSubmitProblem } from "../Form";
import { ActionStatus, ErrorBlock } from "../AsyncState";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import UnitCombobox from "../UnitCombobox";

export default function GeneralTab({
  deviceId,
  obj,
  objects = [],
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
  objects?: BacnetObjectDTO[];
}) {
  const queryClient = useQueryClient();
  const { form, setForm, baseline, setBaseline } = useDraft(obj, toFormState);

  const dirty = differs(form, baseline);
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  const save = useMutation({
    mutationFn: (sent: ObjectFormState) =>
      objectsApi.update(deviceId, obj.id, formStateToPatch(sent, obj.object_type, toFormState(obj))),
    onSuccess: (_result, sent) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });

  const multiState = isMultiState(obj.object_type);
  const family = valueFamily(obj.object_type);
  const problem = useSubmitProblem(multiState ? stateProblems(form.stateText).message : null);

  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        if (problem.canSubmit()) save.mutate(form);
      }}
    >
        <Fieldset legend="Object">
          <FormGrid>
            <Field label="Name">
              <input
                id="obj_name"
                required
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              />
            </Field>
            <Field label="Description">
              <input
                id="obj_description"
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              />
            </Field>
          </FormGrid>

          {hasUnits(obj.object_type, obj.schedule_value_type) && (
            // The picker keeps an already-stored non-standard unit as the value
            // (see validate_units's docstring for the incident behind this).
            <Field label="Units">
              <UnitCombobox
                id="obj_units"
                value={form.units}
                onChange={(value) => setForm((f) => ({ ...f, units: value }))}
              />
            </Field>
          )}

          {multiState && (
            <StateEditor
              names={form.stateText}
              savedNames={baseline.stateText}
              references={stateReferences(obj, objects)}
              onChange={(edit) =>
                setForm((f) => ({
                  ...f,
                  stateText: edit.names,
                  relinquishDefault: renumberValue(f.relinquishDefault, edit.renumber),
                  startValue: renumberValue(f.startValue, edit.renumber),
                }))
              }
            />
          )}

          {family === "binary" && (
            <FormGrid>
              <Field label="Active text">
                <input
                  id="obj_active_text"
                  placeholder="On"
                  maxLength={64}
                  value={form.activeText}
                  onChange={(e) => setForm((f) => ({ ...f, activeText: e.target.value }))}
                />
              </Field>
              <Field label="Inactive text">
                <input
                  id="obj_inactive_text"
                  placeholder="Off"
                  maxLength={64}
                  value={form.inactiveText}
                  onChange={(e) => setForm((f) => ({ ...f, inactiveText: e.target.value }))}
                />
              </Field>
            </FormGrid>
          )}

          {isInput(obj.object_type) && family && (
            <Field label={`Start value${family === "analog" ? unitSuffix(form.units) : ""}`}>
              <ValueControl
                id="obj_start_value"
                family={family}
                states={form.stateText}
                emptyLabel={family === "analog" ? "0" : family === "binary" ? "Inactive" : "State 1"}
                value={form.startValue}
                onChange={(value) => setForm((f) => ({ ...f, startValue: value }))}
              />
            </Field>
          )}

          {hasRelinquishDefault(obj.object_type) && family && (
            <Field
              label={`Relinquish default${family === "analog" ? unitSuffix(form.units) : ""}`}

            >
              <ValueControl
                id="obj_relinquish"
                family={family}
                states={form.stateText}
                emptyLabel="None"
                value={form.relinquishDefault}
                onChange={(value) => setForm((f) => ({ ...f, relinquishDefault: value }))}
              />
            </Field>
          )}
        </Fieldset>

        <Fieldset legend="Behavior">
          <CheckField
            label="BACnet writable"
            help={bacnetWritableHelp(obj.object_type)}
            checked={form.bacnetWritable}
            onChange={(e) => setForm((f) => ({ ...f, bacnetWritable: e.target.checked }))}
          />
          <CheckField
            label="Out of service by default"
            checked={form.outOfServiceDefault}
            onChange={(e) => setForm((f) => ({ ...f, outOfServiceDefault: e.target.checked }))}
          />
        </Fieldset>

      <FormActions>
        <Button variant="primary" type="submit" disabled={save.isPending}>
          Save
        </Button>
      </FormActions>
      {problem.shown && <ErrorBlock lead="Can't save:">{problem.shown}</ErrorBlock>}
      <ActionStatus
        action={save}
        failed="Couldn't save"
        done="Saved."
        hideDone={dirty}
      />
    </Form>
  );
}
