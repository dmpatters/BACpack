import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { devicesApi, engineApi } from "../api/endpoints";
import { GUIDE_URL } from "../utils/bind";
import Button from "./Button";
import { ActionStatus } from "./AsyncState";

// "UDP port 192.168.1.200:47808 is already in use by another program or device"
const IN_USE = /UDP port (\d{1,3}(?:\.\d{1,3}){3}):(\d+)/;

function GuideLink() {
  return (
    <a href={GUIDE_URL} target="_blank" rel="noreferrer">
      Other ways round this
    </a>
  );
}

// Under a device's "port in use" error: a free port from the server, a button
// that moves the device there and starts it, and the guide. With no free port
// found, only the guide.
export default function PortInUseHelp({ deviceId, error }: { deviceId: number; error: string }) {
  const queryClient = useQueryClient();
  const { data: suggestion } = useQuery({
    queryKey: ["portSuggestion", deviceId, error],
    queryFn: () => devicesApi.portSuggestion(deviceId),
  });

  const moveAndStart = useMutation({
    mutationFn: async (bindAddress: string) => {
      await devicesApi.update(deviceId, { bind_address: bindAddress });
      await engineApi.start(deviceId);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["device", deviceId] });
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      queryClient.invalidateQueries({ queryKey: ["engineStatus"] });
    },
  });

  if (!suggestion) return null;
  if (suggestion.port === null || suggestion.bind_address === null) {
    return (
      <p data-testid="port-in-use-help">
        <GuideLink />
      </p>
    );
  }

  const taken = IN_USE.exec(error);
  const bindAddress = suggestion.bind_address;
  const port = suggestion.port;
  return (
    <div data-testid="port-in-use-help">
      <p>
        {taken ? `Port ${taken[2]} on ${taken[1]} is in use, probably by Yabe or Workbench. ` : "That port is in use. "}
        Port {port} is free.
      </p>
      <p className="bp-muted">
        Allow UDP {port} in the firewall for other computers.
      </p>
      <div className="bp-cluster">
        <Button variant="primary" disabled={moveAndStart.isPending} onClick={() => moveAndStart.mutate(bindAddress)}>
          Use port {port}
        </Button>
        <GuideLink />
      </div>
      <ActionStatus action={moveAndStart} failed={`Couldn't move to port ${port}`} />
    </div>
  );
}
