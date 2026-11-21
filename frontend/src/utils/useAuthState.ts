import { useQuery } from "@tanstack/react-query";
import { authApi } from "../api/endpoints";

export const AUTH_STATE_KEY = ["authState"] as const;

// Who is signed in, and whether the install runs with a login at all. Shared
// by the auth gate, the nav's Sign out and the Dashboard warning.
export function useAuthState() {
  return useQuery({ queryKey: AUTH_STATE_KEY, queryFn: authApi.state });
}
