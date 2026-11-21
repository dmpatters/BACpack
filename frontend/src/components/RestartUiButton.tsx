import { useMutation, useQuery } from "@tanstack/react-query";
import { engineApi, versionApi } from "../api/endpoints";
import { errorMessage, type Notice } from "./Banner";
import Button from "./Button";

// Only a service manager starts the web process again, so the button exists
// only when one runs BACpack.
export default function RestartUiButton({
  onNotice,
  label = "Restart UI only",
}: {
  onNotice: (notice: Notice) => void;
  label?: string;
}) {
  const { data: version } = useQuery({ queryKey: ["version"], queryFn: versionApi.get });
  const restartUi = useMutation({
    mutationFn: engineApi.restartUi,
    onSuccess: () =>
      onNotice({
        type: "ok",
        message: "the web UI is restarting. Reload in a few seconds.",
      }),
    onError: (err) => onNotice({ type: "error", message: errorMessage(err) }),
  });

  return (
    <span data-testid="restart-ui-slot">
      {version?.service && (
        <Button
          type="button"
          disabled={restartUi.isPending}
          onClick={() => restartUi.mutate()}
          title="Restart only the web UI."
        >
          {label}
        </Button>
      )}
    </span>
  );
}
