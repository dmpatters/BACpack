import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiKeysApi, loginSettingsApi } from "../api/endpoints";
import type { LoginMode, LoginSettings, NewApiKey } from "../api/types";
import { ActionStatus, Loading } from "../components/AsyncState";
import { LoadError } from "../components/Banner";
import Button from "../components/Button";
import Callout from "../components/Callout";
import { useConfirm } from "../components/ConfirmDialog";
import { TableFrame } from "../components/DataTable";
import { CheckField, Field, Fieldset, Form, FormActions, SettingsColumn, useFieldProblem } from "../components/Form";
import PageHeader from "../components/PageHeader";
import { useUnsavedChanges } from "../components/UnsavedChanges";
import { MIN_PASSWORD_LENGTH } from "../utils/password";
import { formatUtc } from "../utils/time";
import { AUTH_STATE_KEY } from "../utils/useAuthState";
import { usePageTitle } from "../utils/usePageTitle";

const PAGE = "/settings/security";

export default function Security() {
  usePageTitle("Security");
  const { data: login, error, refetch } = useQuery({ queryKey: ["loginSettings"], queryFn: loginSettingsApi.get });

  return (
    <>
      <PageHeader title="Security" />
      <SettingsColumn>
        {error && !login ? (
          <LoadError what="the login settings" error={error} onRetry={() => refetch()} />
        ) : !login ? (
          <Loading what="the login settings" />
        ) : (
          <LoginSection login={login} />
        )}
      </SettingsColumn>
      <ApiKeysSection loginMode={login?.mode} />
    </>
  );
}

function useRefreshLogin() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: ["loginSettings"] });
    queryClient.invalidateQueries({ queryKey: AUTH_STATE_KEY });
  };
}

function LoginSection({ login }: { login: LoginSettings }) {
  if (login.mode === "login") {
    return (
      <>
        <h2>Login</h2>
        <UsernameForm current={login.username ?? ""} />
        <PasswordForm />
        <DisableForm />
      </>
    );
  }
  return (
    <>
      <h2>Login</h2>
      <Callout tone="warn" lead="BACpack is running without a login." />
      <EnableForm />
    </>
  );
}

function UsernameForm({ current }: { current: string }) {
  const refresh = useRefreshLogin();
  const [username, setUsername] = useState(current);
  const dirty = username !== current;
  useUnsavedChanges(dirty, PAGE);
  const save = useMutation({
    mutationFn: () => loginSettingsApi.update({ username: username.trim() }),
    onSuccess: refresh,
  });
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <Fieldset legend="Username">
        <Field label="Username">
          <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <FormActions>
          <Button variant="primary" type="submit" disabled={!dirty || !username.trim() || save.isPending}>
            Save username
          </Button>
        </FormActions>
        <ActionStatus action={save} failed="Couldn't save" done="Saved." hideDone={dirty} />
      </Fieldset>
    </Form>
  );
}

function PasswordForm() {
  const queryClient = useQueryClient();
  const refresh = useRefreshLogin();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [revokeKeys, setRevokeKeys] = useState(false);
  const dirty = current !== "" || next !== "" || again !== "" || revokeKeys;
  useUnsavedChanges(dirty, PAGE);
  const submit = useFieldProblem([
    ["next", next.length < MIN_PASSWORD_LENGTH && `The new password needs at least ${MIN_PASSWORD_LENGTH} characters.`],
    ["again", next !== again && "The two new passwords differ."],
  ]);
  const save = useMutation({
    mutationFn: (revoke: boolean) =>
      loginSettingsApi.update({ new_password: next, current_password: current, revoke_api_keys: revoke }),
    onSuccess: (_data, revoke) => {
      if (revoke) queryClient.invalidateQueries({ queryKey: ["apiKeys"] });
      setCurrent("");
      setNext("");
      setAgain("");
      setRevokeKeys(false);
      refresh();
    },
  });
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        if (submit.canSubmit()) save.mutate(revokeKeys);
      }}
    >
      <Fieldset legend="Password">
        <Field label="Current password">
          <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <Field
          label="New password"
          help={`At least ${MIN_PASSWORD_LENGTH} characters.`}
          error={submit.errorFor("next")}
        >
          <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        <Field label="New password again" error={submit.errorFor("again")}>
          <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
        </Field>
        <CheckField
          label="Also revoke every API key"
          help="API keys stay valid after a password change unless you tick this."
          checked={revokeKeys}
          onChange={(e) => setRevokeKeys(e.target.checked)}
        />
        <FormActions>
          <Button variant="primary" type="submit" disabled={!current || save.isPending}>
            Change password
          </Button>
        </FormActions>
        <ActionStatus action={save} failed="Couldn't change the password" done="Password changed." hideDone={dirty} />
      </Fieldset>
    </Form>
  );
}

function DisableForm() {
  const refresh = useRefreshLogin();
  const [current, setCurrent] = useState("");
  useUnsavedChanges(current !== "", PAGE);
  const disable = useMutation({
    mutationFn: () => loginSettingsApi.disable({ current_password: current }),
    onSuccess: () => {
      setCurrent("");
      refresh();
    },
  });
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        disable.mutate();
      }}
    >
      <Fieldset legend="Run without login">
        <Callout tone="warn">Switching the login off allows for unauthenticated control.</Callout>
        <Field label="Current password">
          <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <FormActions>
          <Button variant="danger" type="submit" disabled={!current || disable.isPending}>
            Run without login
          </Button>
        </FormActions>
        <ActionStatus action={disable} failed="Couldn't switch the login off" done="The login is off." hideDone={current !== ""} />
      </Fieldset>
    </Form>
  );
}

function EnableForm() {
  const queryClient = useQueryClient();
  const refresh = useRefreshLogin();
  const { data: keys } = useQuery({ queryKey: ["apiKeys"], queryFn: apiKeysApi.list });
  const keyCount = keys?.length ?? 0;
  // Ticked unless the user unticks it: keys made while the login was off
  // start working the moment it is on.
  const [keepKeys, setKeepKeys] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const dirty = username !== "" || password !== "" || again !== "" || keepKeys;
  useUnsavedChanges(dirty, PAGE);
  const submit = useFieldProblem([
    ["username", !username.trim() && "Choose a username."],
    ["password", password.length < MIN_PASSWORD_LENGTH && `The password needs at least ${MIN_PASSWORD_LENGTH} characters.`],
    ["again", password !== again && "The two passwords differ."],
  ]);
  const enable = useMutation({
    mutationFn: (revoke: boolean) =>
      loginSettingsApi.enable({ username: username.trim(), password, revoke_api_keys: revoke }),
    onSuccess: (_data, revoke) => {
      if (revoke) queryClient.invalidateQueries({ queryKey: ["apiKeys"] });
      setKeepKeys(false);
      setUsername("");
      setPassword("");
      setAgain("");
      refresh();
    },
  });
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        // Sent even before the key list loads, or if it fails to: revoking
        // no keys is harmless, keeping keys the user never saw is not.
        if (submit.canSubmit()) enable.mutate(!keepKeys);
      }}
    >
      <Fieldset legend="Create a login">
        <Field label="Username" error={submit.errorFor("username")}>
          <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Password" help={`At least ${MIN_PASSWORD_LENGTH} characters.`} error={submit.errorFor("password")}>
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Password again" error={submit.errorFor("again")}>
          <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
        </Field>
        {keyCount > 0 && (
          <CheckField
            label={keyCount === 1 ? "Also revoke the 1 existing API key" : `Also revoke the ${keyCount} existing API keys`}
            help="They were made while the login was off. Untick to keep keys you set up on purpose."
            checked={!keepKeys}
            onChange={(e) => setKeepKeys(!e.target.checked)}
          />
        )}
        <FormActions>
          <Button variant="primary" type="submit" disabled={enable.isPending}>
            Create login
          </Button>
        </FormActions>
        <ActionStatus action={enable} failed="Couldn't create the login" done="Login created." hideDone={dirty} />
      </Fieldset>
    </Form>
  );
}

function ApiKeysSection({ loginMode }: { loginMode: LoginMode | undefined }) {
  const queryClient = useQueryClient();
  // Polled so Last used catches up while the page stays open; the server
  // records a key's use at most once a minute.
  const { data: keys, error, refetch } = useQuery({
    queryKey: ["apiKeys"],
    queryFn: apiKeysApi.list,
    refetchInterval: 30_000,
  });
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const needsPassword = loginMode === "login";
  useEffect(() => {
    if (!needsPassword) setPassword("");
  }, [needsPassword]);
  const [shown, setShown] = useState<NewApiKey | null>(null);
  const [copied, setCopied] = useState(false);
  useUnsavedChanges(name !== "" || (needsPassword && password !== ""), PAGE);
  const { ask, dialog } = useConfirm();

  const create = useMutation({
    mutationFn: () => apiKeysApi.create(name.trim(), needsPassword ? password : undefined),
    onSuccess: (key) => {
      setName("");
      setPassword("");
      setCopied(false);
      setShown(key);
      queryClient.invalidateQueries({ queryKey: ["apiKeys"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: number) => apiKeysApi.revoke(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["apiKeys"] }),
  });

  return (
    <>
      <SettingsColumn>
      <h2>API keys</h2>
      <p>
        Send a key as <code>Authorization: Bearer &lt;key&gt;</code>.
      </p>
      {shown && (
        <Callout tone="ok" lead="Copy the key now.">
          <div className="bp-cluster">
            <code data-testid="new-api-key">{shown.key}</code>
            <Button
              type="button"
              small
              onClick={async () => {
                await navigator.clipboard?.writeText(shown.key);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button type="button" small variant="ghost" onClick={() => setShown(null)}>
              Done
            </Button>
          </div>
        </Callout>
      )}
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Fieldset legend="New key">
          <Field label="Name" help="e.g. grafana">
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          {needsPassword && (
            <Field label="Current password" help="Creating a key asks for the password.">
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          )}
          <FormActions>
            <Button
              variant="primary"
              type="submit"
              disabled={!name.trim() || loginMode === undefined || (needsPassword && !password) || create.isPending}
            >
              Create key
            </Button>
          </FormActions>
          <ActionStatus action={create} failed="Couldn't create the key" done="" hideDone />
        </Fieldset>
      </Form>
      </SettingsColumn>
      {error && !keys ? (
        <SettingsColumn>
          <LoadError what="the API keys" error={error} onRetry={() => refetch()} />
        </SettingsColumn>
      ) : !keys ? (
        <SettingsColumn>
          <Loading what="the API keys" />
        </SettingsColumn>
      ) : keys.length === 0 ? (
        <SettingsColumn>
          <p className="bp-muted">No API keys yet.</p>
        </SettingsColumn>
      ) : (
        <TableFrame label="API keys">
          <thead>
            <tr>
              <th>Name</th>
              <th>Key starts with</th>
              <th>Created</th>
              <th>Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name}</td>
                <td className="bp-cell-mono">{key.prefix}</td>
                <td>{formatUtc(key.created)}</td>
                <td>{key.last_used ? formatUtc(key.last_used) : <span className="bp-muted">never</span>}</td>
                <td>
                  <Button
                    small
                    variant="danger"
                    type="button"
                    aria-label={`Revoke ${key.name}`}
                    onClick={() =>
                      ask({
                        title: `Revoke ${key.name}?`,
                        body: "Anything using this key stops working.",
                        confirmLabel: "Revoke",
                        onConfirm: () => revoke.mutateAsync(key.id).catch(() => undefined),
                      })
                    }
                  >
                    Revoke
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </TableFrame>
      )}
      {revoke.isError && <Callout tone="danger">Couldn't revoke the key.</Callout>}
      {dialog}
    </>
  );
}
