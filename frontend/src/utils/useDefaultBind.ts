import { useQuery } from "@tanstack/react-query";
import { settingsApi } from "../api/endpoints";

// The server's default bind address, which a device without one of its own
// serves on; null while loading, unset or unreadable.
export function useDefaultBind(): string | null {
  const { data } = useQuery({ queryKey: ["networkSettings"], queryFn: () => settingsApi.get() });
  return data?.bind_address ?? null;
}
