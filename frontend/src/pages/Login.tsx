import { useQueryClient } from "@tanstack/react-query";
import LoginForm from "../components/LoginForm";
import { AUTH_STATE_KEY } from "../utils/useAuthState";
import { usePageTitle } from "../utils/usePageTitle";

export default function Login() {
  usePageTitle("Sign in");
  const queryClient = useQueryClient();
  return (
    <main className="bp-page">
      <div className="bp-auth">
        <h1 className="bp-page-header__title">Sign in to BACpack</h1>
        <LoginForm onSignedIn={() => queryClient.invalidateQueries({ queryKey: AUTH_STATE_KEY })} />
      </div>
    </main>
  );
}
