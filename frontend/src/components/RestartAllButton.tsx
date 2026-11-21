import { useMutation, useQueryClient } from "@tanstack/react-query";
import { engineApi } from "../api/endpoints";
import type { RestartAllResult } from "../api/types";
import { errorMessage, type Notice } from "./Banner";
import Button from "./Button";

function devices(n: number) {
  return `${n} running device${n === 1 ? "" : "s"}`;
}

function resultNotice({ restarted, failed }: RestartAllResult): Notice {
  if (failed.length === 0) {
    return restarted.length === 0
      ? { type: "ok", message: "No devices are running." }
      : { type: "ok", message: `restarted ${devices(restarted.length)}` };
  }
  const total = restarted.length + failed.length;
  return {
    type: "error",
    message: `restarted ${restarted.length} of ${devices(total)}: ${failed.map((f) => f.error).join("; ")}`,
  };
}

// Settings only read when a device starts (network role, BBMD peers) need
// every running device restarted; stopped and crashed ones are left alone.
export default function RestartAllButton({
  onNotice,
  disabled = false,
}: {
  onNotice: (notice: Notice) => void;
  disabled?: boolean;
}) {
  const queryClient = useQueryClient();
  const restartAll = useMutation({
    mutationFn: engineApi.restartAll,
    onSuccess: (result) => onNotice(resultNotice(result)),
    onError: (err) => onNotice({ type: "error", message: errorMessage(err) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["engineStatus"] }),
  });

  return (
    <Button
      type="button"
      disabled={restartAll.isPending || disabled}
      onClick={() => restartAll.mutate()}
      title={
        disabled
          ? "No devices are running."
          : "Restart every running device."
      }
    >
      {restartAll.isPending ? "Restarting…" : "Restart all running devices"}
    </Button>
  );
}
