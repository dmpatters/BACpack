import { ErrorBlock } from "./AsyncState";
import Callout from "./Callout";

export type Notice = { type: "ok" | "error"; message: string } | null;

/** A page's last action result, kept in local state and set from a
 * mutation's onSuccess/onError. */
export default function Banner({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return <Callout tone={notice.type === "ok" ? "ok" : "danger"}>{notice.message}</Callout>;
}

/** In place of a list or form whose query failed, so a failure never reads as
 * empty data. */
export function LoadError({ what, error, onRetry }: { what: string; error: unknown; onRetry?: () => void }) {
  return (
    <ErrorBlock lead={`Couldn't load ${what}:`} onRetry={onRetry}>
      {errorMessage(error)}
    </ErrorBlock>
  );
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
