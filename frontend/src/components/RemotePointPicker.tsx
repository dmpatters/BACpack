import { useState } from "react";
import { clientToolApi } from "../api/endpoints";
import type { RemoteObject, RemoteTarget } from "../api/types";
import { errorMessage } from "./Banner";
import Button from "./Button";
import { Field } from "./Form";
import { ErrorBlock, Loading } from "./AsyncState";
import { objectId } from "../utils/text";

// Browses a remote device's objects through a local device's BACnet client so
// a mapping's remote object can be picked instead of typed. The local device
// has to be running. `offer` narrows the list (a schedule's write target only
// wants objects it can write). A null `target` (no device instance typed yet)
// disables browsing.
export default function RemotePointPicker({
  deviceId,
  target,
  onPick,
  offer,
}: {
  deviceId: number;
  target: RemoteTarget | null;
  onPick: (object: RemoteObject) => void;
  offer?: (object: RemoteObject) => boolean;
}) {
  // A browse's list or error belongs to the device it was browsed on: once the
  // target changes it's another device's, so it's no longer shown.
  const targetKey = target ? `${target.instance}@${target.address ?? ""}` : null;
  const [browsed, setBrowsed] = useState<{ key: string; objects?: RemoteObject[]; error?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const current = browsed && browsed.key === targetKey ? browsed : null;
  const objects = current?.objects ?? null;
  const error = current?.error ?? null;

  const browse = async () => {
    if (!target || targetKey === null) return;
    const key = targetKey;
    setLoading(true);
    setBrowsed(null);
    try {
      const found = await clientToolApi.objectList(deviceId, target);
      setBrowsed({ key, objects: offer ? found.filter(offer) : found });
    } catch (err) {
      setBrowsed({ key, error: errorMessage(err) });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <Button small type="button" onClick={browse} disabled={loading || !target}>
        Browse remote device
      </Button>
      {!target && <span className="bp-muted"> enter a device instance first</span>}
      {/* Behind a click already, so no delay before it shows. */}
      {loading && <Loading what="remote objects" delayMs={0} />}
      {error && <ErrorBlock lead="Browse failed:">{error}</ErrorBlock>}
      {objects && (
        <Field label="Remote object">
          <select
            value=""
            onChange={(e) => {
              if (e.target.value === "") return;
              const picked = objects[Number(e.target.value)];
              if (picked) onPick(picked);
            }}
          >
            <option value="">pick one of {objects.length} objects...</option>
            {objects.map((o, i) => (
              <option key={objectId(o.object_type, o.instance)} value={i}>
                {objectId(o.object_type, o.instance)}
                {o.name ? ` (${o.name})` : ""}
              </option>
            ))}
          </select>
        </Field>
      )}
    </div>
  );
}
