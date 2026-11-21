import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { authApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import Button from "./Button";
import { Field, Form, FormActions } from "./Form";
import Callout from "./Callout";
import { errorMessage } from "./Banner";

function failureText(err: unknown): string {
  if (err instanceof ApiError && err.status === 429) {
    const minutes = err.retryAfterSeconds ? Math.max(1, Math.ceil(err.retryAfterSeconds / 60)) : null;
    return minutes
      ? `Too many failed logins. Try again in about ${minutes} minute${minutes === 1 ? "" : "s"}.`
      : "Too many failed logins. Try again later.";
  }
  if (err instanceof ApiError && err.status === 401) return "Wrong username or password.";
  return errorMessage(err);
}

// The username and password form of the Login page and of the dialog that
// replaces it when a session ends under an open page.
export default function LoginForm({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => authApi.login({ username, password }),
    onSuccess: () => {
      setPassword("");
      onSignedIn();
    },
  });

  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        login.mutate();
      }}
    >
      <Field label="Username">
        <input type="text" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
      </Field>
      <Field label="Password">
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <FormActions>
        <Button variant="primary" type="submit" disabled={login.isPending || !username || !password}>
          Sign in
        </Button>
      </FormActions>
      {login.isError && <Callout tone="danger">{failureText(login.error)}</Callout>}
    </Form>
  );
}
