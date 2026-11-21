import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "../api/endpoints";
import { setAuthHandlers } from "../api/http";
import Login from "../pages/Login";
import Setup from "../pages/Setup";
import { AUTH_STATE_KEY, useAuthState } from "../utils/useAuthState";
import { errorMessage } from "./Banner";
import { ErrorBlock, Loading } from "./AsyncState";
import SessionExpiredDialog from "./SessionExpiredDialog";

const AuthContext = createContext<{ signOut: () => Promise<void> }>({ signOut: async () => {} });

export function useSignOut(): () => Promise<void> {
  return useContext(AuthContext).signOut;
}

// Decides what the visitor sees first: the setup screen on a fresh install,
// the login page when a login is required and nobody is signed in, else the
// app. Once the app has been shown it stays mounted: a session that ends
// later (a 401 from any call) puts SessionExpiredDialog over it, so an open
// form keeps its edits. An explicit sign-out is the one way back to the
// login page.
export default function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { data: state, error, refetch } = useAuthState();
  const [expired, setExpired] = useState(false);
  const [appShown, setAppShown] = useState(false);

  // The login page is already up while the app is hidden: a 401 that answers a
  // call made before a sign-out must not leave a stale "session ended" behind.
  const appShownRef = useRef(false);
  appShownRef.current = appShown;

  const needsLogin = state?.mode === "login" && !state.signed_in;
  useEffect(() => {
    if (state && state.mode !== null && !needsLogin) {
      if (!appShownRef.current) setExpired(false);
      setAppShown(true);
    }
  }, [state, needsLogin]);

  useEffect(() => {
    setAuthHandlers({
      onUnauthorized: () => {
        if (appShownRef.current) setExpired(true);
      },
      onSetupRequired: () => queryClient.invalidateQueries({ queryKey: AUTH_STATE_KEY }),
    });
    return () => setAuthHandlers({});
  }, [queryClient]);

  const signOut = useCallback(async () => {
    // Whatever the server says, this browser is done: a failed logout must
    // not leave the user stuck on a page that no longer works.
    try {
      await authApi.logout();
    } catch {
      // the login page is where the user lands either way
    }
    setExpired(false);
    setAppShown(false);
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== AUTH_STATE_KEY[0] });
    queryClient.setQueryData(AUTH_STATE_KEY, { mode: "login", signed_in: false, username: null });
  }, [queryClient]);
  const context = useMemo(() => ({ signOut }), [signOut]);

  if (!state) {
    return error ? (
      <ErrorBlock lead="Couldn't reach BACpack:" onRetry={() => refetch()}>
        {errorMessage(error)}
      </ErrorBlock>
    ) : (
      <Loading what="BACpack" />
    );
  }
  if (state.mode === null) return <Setup />;
  if (needsLogin && !appShown) return <Login />;

  const reSignIn = () => {
    setExpired(false);
    queryClient.invalidateQueries();
  };
  return (
    <AuthContext.Provider value={context}>
      {children}
      {(expired || needsLogin) && <SessionExpiredDialog onSignedIn={reSignIn} />}
    </AuthContext.Provider>
  );
}
