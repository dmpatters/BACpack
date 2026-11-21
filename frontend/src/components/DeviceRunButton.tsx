import { useMutation, useQueryClient } from "@tanstack/react-query";
import { engineApi } from "../api/endpoints";
import type { ProcessStatus } from "../api/types";
import Button from "./Button";
import { errorMessage } from "./Banner";

// The device header's run actions: Restart serving and Stop while running or
// mid-transition, Start otherwise. A failure goes to `onError` for the page to
// show below the header: an error inside the button group would stretch it.
export default function DeviceRunButton({
  deviceId,
  status,
  onError,
}: {
  deviceId: number;
  status: ProcessStatus | undefined;
  onError: (message: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const onSuccess = () => {
    onError(null);
    queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
  };
  const failed = (e: unknown) => onError(errorMessage(e));
  // A new attempt drops the last attempt's error at once.
  const begin = () => onError(null);
  const start = useMutation({ mutationFn: () => engineApi.start(deviceId), onMutate: begin, onSuccess, onError: failed });
  const stop = useMutation({ mutationFn: () => engineApi.stop(deviceId), onMutate: begin, onSuccess, onError: failed });
  const restart = useMutation({ mutationFn: () => engineApi.restart(deviceId), onMutate: begin, onSuccess, onError: failed });

  const transitioning = status === "starting" || status === "stopping" || status === "reinitializing";
  const runningOrTransitioning = status === "running" || transitioning;
  return runningOrTransitioning ? (
    <span className="bp-cluster">
      <Button
        disabled={restart.isPending || transitioning}
        onClick={() => restart.mutate()}
        title="Stop and start this device's process."
      >
        Restart serving
      </Button>
      <Button disabled={stop.isPending || transitioning} onClick={() => stop.mutate()}>
        Stop
      </Button>
    </span>
  ) : (
    <Button variant="primary" disabled={start.isPending || status === undefined} onClick={() => start.mutate()}>
      Start
    </Button>
  );
}
