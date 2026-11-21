import { useQuery } from "@tanstack/react-query";
import { versionApi } from "../api/endpoints";
import type { PlanNote, PlanSkipped } from "../api/types";
import { featureRequestUrl, type SkippedTypesSource } from "../utils/links";
import { objectId } from "../utils/text";

// What a clone or EDE plan leaves out and what it changed, each with the
// object it is about, and a link asking for the object types BACpack skipped.
export default function PlanLists({
  skipped,
  notes,
  skippedTypes,
  vendorName,
  source,
  skippedTitle,
}: {
  skipped: PlanSkipped[];
  notes: PlanNote[];
  skippedTypes: Record<string, number>;
  vendorName: string | null;
  source: SkippedTypesSource;
  skippedTitle: string;
}) {
  const hasTypes = Object.keys(skippedTypes).length > 0;
  // Only the request link needs it.
  const { data: version } = useQuery({ queryKey: ["version"], queryFn: () => versionApi.get(), enabled: hasTypes });
  return (
    <>
      {skipped.length > 0 && (
        <>
          <h3>
            {skippedTitle} ({skipped.length})
          </h3>
          <ul>
            {/* An EDE file can list one type and instance twice: key by place. */}
            {skipped.map((s, i) => (
              <li key={i}>
                <span className="bp-mono">{objectId(s.object_type, s.instance)}</span> {s.name}: {s.reason}
              </li>
            ))}
          </ul>
          {hasTypes && (
            <p>
              <a
                href={featureRequestUrl(skippedTypes, vendorName, version?.version ?? "unknown", source)}
                target="_blank"
                rel="noreferrer"
              >
                Request support for these types
              </a>
            </p>
          )}
        </>
      )}
      {notes.length > 0 && (
        <>
          <h3>Notes ({notes.length})</h3>
          <ul>
            {notes.map((n, i) => (
              <li key={i}>
                <span className="bp-mono">{objectId(n.object_type, n.instance)}</span> {n.name}: {n.note}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
