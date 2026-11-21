import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authApi } from "../api/endpoints";
import Button from "../components/Button";
import Callout from "../components/Callout";
import { CheckField, Field, Form, FormActions, Segmented, SettingsColumn, useFieldProblem } from "../components/Form";
import { errorMessage } from "../components/Banner";
import { MIN_PASSWORD_LENGTH } from "../utils/password";
import { AUTH_STATE_KEY } from "../utils/useAuthState";
import { usePageTitle } from "../utils/usePageTitle";

type Choice = "login" | "none";

// First use: a fresh install keeps every other page behind this one until the
// choice is made. Whoever gets here first claims the install.
export default function Setup() {
  usePageTitle("Set up BACpack");
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState<Choice>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const done = () => queryClient.invalidateQueries({ queryKey: AUTH_STATE_KEY });
  const createLogin = useMutation({
    mutationFn: () => authApi.setup({ mode: "login", username: username.trim(), password }),
    onSuccess: done,
  });
  const runWithout = useMutation({
    mutationFn: () => authApi.setup({ mode: "none", confirm: true }),
    onSuccess: done,
  });

  const submit = useFieldProblem([
    ["username", !username.trim() && "Choose a username."],
    ["password", password.length < MIN_PASSWORD_LENGTH && `The password needs at least ${MIN_PASSWORD_LENGTH} characters.`],
    ["password", password === username.trim() && "The password can't be the same as the username."],
    ["again", password !== again && "The two passwords differ."],
  ]);
  const failed = createLogin.isError ? createLogin.error : runWithout.isError ? runWithout.error : null;

  return (
    <main className="bp-page">
      <SettingsColumn>
      <h1 className="bp-page-header__title">Set up BACpack</h1>
      <p>Choose how the web UI is protected.</p>
      <Segmented
        label="Web UI access"
        value={choice}
        options={[
          { value: "login", label: "Create login" },
          { value: "none", label: "Run without login" },
        ]}
        onChange={setChoice}
      />
      {choice === "login" ? (
        <Form
          onSubmit={(e) => {
            e.preventDefault();
            if (submit.canSubmit()) createLogin.mutate();
          }}
        >
          <Field label="Username" error={submit.errorFor("username")}>
            <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
          </Field>
          <Field
            label="Password"
            help={`At least ${MIN_PASSWORD_LENGTH} characters.`}
            error={submit.errorFor("password")}
          >
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Field label="Password again" error={submit.errorFor("again")}>
            <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
          </Field>
          <FormActions>
            <Button variant="primary" type="submit" disabled={createLogin.isPending}>
              Create login
            </Button>
          </FormActions>
        </Form>
      ) : (
        <Form
          onSubmit={(e) => {
            e.preventDefault();
            runWithout.mutate();
          }}
        >
          <CheckField
            label="I understand that anyone who can reach this address can control BACpack."
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <FormActions>
            <Button variant="danger-solid" type="submit" disabled={!confirmed || runWithout.isPending}>
              Run without login
            </Button>
          </FormActions>
        </Form>
      )}
      {failed !== null && <Callout tone="danger">{errorMessage(failed)}</Callout>}
      </SettingsColumn>
    </main>
  );
}
