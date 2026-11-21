import { Fragment } from "react";
import { plural } from "../utils/text";

const SHOWN = 20;

// Each id stays whole (`analog-value:1` would otherwise break at its hyphen);
// a long list wraps between ids.
function line(verb: string, ids: string[], danger = false) {
  const shown = ids.slice(0, SHOWN);
  const rest = ids.length > SHOWN ? ` and ${ids.length - SHOWN} more` : "";
  return (
    <li style={danger && ids.length > 0 ? { color: "var(--danger)" } : undefined}>
      {verb} {ids.length}:{" "}
      {ids.length === 0
        ? "none"
        : shown.map((id, i) => (
            <Fragment key={i}>
              {i > 0 && ", "}
              <span className="bp-nowrap">{id}</span>
            </Fragment>
          ))}
      {rest}
    </li>
  );
}

function summary(verbs: [string, string, string], counts: [number, number, number], noun: string): string {
  const parts = counts
    .map((n, i) => (n > 0 ? `${verbs[i]} ${plural(n, noun)}` : null))
    .filter((p): p is string => p !== null);
  if (parts.length === 0) return "No changes.";
  const text = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

export function importPlanSummary(created: number, updated: number, deleted: number, noun: string): string {
  return summary(["creates", "updates", "deletes"], [created, updated, deleted], noun);
}

export function pastSummary(created: number, updated: number, deleted: number, noun: string): string {
  return summary(["created", "updated", "deleted"], [created, updated, deleted], noun);
}

// `noDelete`: the import never removes what this group lists, so it has no Delete line.
export type PlanGroup = { title: string; created: string[]; updated: string[]; deleted: string[]; noDelete?: boolean };

export default function ImportPlan({ groups }: { groups: PlanGroup[] }) {
  return (
    <div className="bp-stack">
      {groups.map((g) => (
        <section key={g.title}>
          {groups.length > 1 && <h3 className="bp-section-title">{g.title}</h3>}
          <ul className="bp-mono">
            {line("Create", g.created)}
            {line("Update", g.updated)}
            {!g.noDelete && line("Delete", g.deleted, true)}
          </ul>
        </section>
      ))}
    </div>
  );
}
