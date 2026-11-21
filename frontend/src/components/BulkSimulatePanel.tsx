import { useState } from "react";
import type { Simulation } from "../api/types";
import SimulationFields from "./SimulationFields";
import { CheckField, Field, Form, FormActions, FormGrid } from "./Form";
import {
  KINDS,
  KIND_LABELS,
  PERIODIC_KINDS,
  commonFamily,
  defaultSimulation,
  switchKind,
  toSimulation,
  toValues,
  type SimValues,
} from "../utils/simulation";
import Button from "./Button";
import { plural } from "../utils/text";

interface Props {
  objectTypes: string[];
  selectedCount: number;
  applying: boolean;
  onApply: (simulation: Simulation, randomPhase: boolean) => void;
  onCancel: () => void;
}

/** Bulk "Simulate selected" -- one simulator applied to every
 * selected object, optionally each with its own random phase. Only kinds
 * every selected object's type accepts are offered. */
export default function BulkSimulatePanel({ objectTypes, selectedCount, applying, onApply, onCancel }: Props) {
  const family = commonFamily(objectTypes);
  const [kind, setKind] = useState(family ? KINDS[family][0] : "");
  const [values, setValues] = useState<SimValues>(() =>
    family ? toValues(defaultSimulation(family, KINDS[family][0])) : {}
  );
  const [randomPhase, setRandomPhase] = useState(true);

  if (!family) {
    return (
      <div className="bp-form">
        <p className="bp-muted">
          {selectedCount === 0
            ? "Select objects to simulate."
            : "The selected objects have no simulator in common."}
        </p>
        <div className="bp-cluster">
          <Button type="button" onClick={onCancel}>
            Close
          </Button>
        </div>
      </div>
    );
  }

  const periodic = PERIODIC_KINDS.includes(kind);
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        onApply(toSimulation(family, kind, values), periodic && randomPhase);
      }}
    >
      <>
        <FormGrid>
          <Field label="Kind">
            <select
              id="bulk_sim_kind"
              value={kind}
              onChange={(e) => {
                setValues((old) => switchKind(family, kind, e.target.value, old));
                setKind(e.target.value);
              }}
            >
              {KINDS[family].map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </Field>
        </FormGrid>
        <SimulationFields family={family} kind={kind} values={values} onChange={setValues} idPrefix="bulk_sim" />
        {periodic && (
          <CheckField
            label="Random phase per object"

            checked={randomPhase}
            onChange={(e) => setRandomPhase(e.target.checked)}
          />
        )}
        <FormActions>
          <Button variant="primary" type="submit" disabled={applying || selectedCount === 0}>
            Simulate {plural(selectedCount, "object")}
          </Button>
          <Button variant="ghost" type="button" onClick={onCancel}>
            Cancel
          </Button>
        </FormActions>
      </>
    </Form>
  );
}
