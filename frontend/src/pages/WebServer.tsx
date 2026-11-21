import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { WEB_CERTIFICATE_URL, webServerApi } from "../api/endpoints";
import type { WebCertificate, WebServerSettings } from "../api/types";
import { ActionStatus, Loading } from "../components/AsyncState";
import Banner, { LoadError, errorMessage, type Notice } from "../components/Banner";
import Button from "../components/Button";
import Callout from "../components/Callout";
import { useConfirm } from "../components/ConfirmDialog";
import { Field, Fieldset, Form, FormActions, SettingsColumn } from "../components/Form";
import PageHeader from "../components/PageHeader";
import RestartUiButton from "../components/RestartUiButton";
import { useUnsavedChanges } from "../components/UnsavedChanges";
import { formatUtc } from "../utils/time";
import { usePageTitle } from "../utils/usePageTitle";

const PAGE = "/settings/web";
const KEY = ["webServer"];

function useRefreshWeb() {
  const queryClient = useQueryClient();
  return (fresh: WebServerSettings) => {
    queryClient.setQueryData(KEY, fresh);
    queryClient.invalidateQueries({ queryKey: ["version"] });
  };
}

export default function WebServer() {
  usePageTitle("Web Server");
  const { data, error, refetch } = useQuery({ queryKey: KEY, queryFn: webServerApi.get });
  const [notice, setNotice] = useState<Notice>(null);

  return (
    <>
      <PageHeader title="Web Server" />
      {error && !data ? (
        <LoadError what="the web server settings" error={error} onRetry={() => refetch()} />
      ) : !data ? (
        <Loading what="the web server settings" />
      ) : (
        <>
          {data.restart_pending && (
            <Callout tone="warn" lead="Restart the web server to apply.">
              <LosesThePage settings={data} />
              <RestartUiButton label="Restart web server" onNotice={setNotice} />
            </Callout>
          )}
          <Banner notice={notice} />
          <SettingsColumn>
            <ListenAddressForm settings={data} />
            <CertificateSection settings={data} />
          </SettingsColumn>
        </>
      )}
    </>
  );
}

// A restart that changes the listen address to one specific address drops a
// browser that reached the page through another name or address.
function LosesThePage({ settings }: { settings: WebServerSettings }) {
  const address = settings.listen_address;
  if (address === settings.running?.address || address === "0.0.0.0" || address === window.location.hostname) {
    return null;
  }
  const port = settings.running?.port ?? window.location.port;
  const url = `https://${address}${port ? `:${port}` : ""}`;
  return (
    <p>
      This browser will lose the page unless {window.location.hostname} points at {address}. Otherwise open{" "}
      <code>{url}</code>.
    </p>
  );
}

function ListenAddressForm({ settings }: { settings: WebServerSettings }) {
  const refresh = useRefreshWeb();
  const saved = settings.listen_address;
  const [address, setAddress] = useState(saved);
  // A refetch (another tab's save) moves an untouched field; an edited one stays.
  const lastSaved = useRef(saved);
  useEffect(() => {
    if (lastSaved.current === saved) return;
    if (address === lastSaved.current) setAddress(saved);
    lastSaved.current = saved;
  }, [saved, address]);
  const dirty = address !== saved;
  useUnsavedChanges(dirty, PAGE);
  const save = useMutation({
    mutationFn: () => webServerApi.setListenAddress(address.trim()),
    onSuccess: (fresh) => {
      setAddress(fresh.listen_address);
      refresh(fresh);
    },
  });
  return (
    <>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Fieldset legend="Listen address">
          <Field
            label="Address"
            help="0.0.0.0 listens on every address."
            error={save.isError ? errorMessage(save.error) : null}
          >
            <input type="text" value={address} onChange={(e) => setAddress(e.target.value)} />
          </Field>
          <FormActions>
            <Button variant="primary" type="submit" disabled={!dirty || !address.trim() || save.isPending}>
              Save address
            </Button>
          </FormActions>
          <ActionStatus action={{ isError: false, isSuccess: save.isSuccess, error: null }} failed="" done="Saved." hideDone={dirty} />
        </Fieldset>
      </Form>
    </>
  );
}

function CertificateSection({ settings }: { settings: WebServerSettings }) {
  const { certificate, certificate_problem: problem } = settings;
  return (
    <>
      <Fieldset legend="Certificate">
        {problem && (
          <Callout tone="danger" lead="The certificate files can't be used.">
            {problem} Run <code>bacpack-admin web --self-signed</code> on the computer.
          </Callout>
        )}
        {certificate ? <CertificateDetails certificate={certificate} /> : !problem && <p className="bp-muted">No certificate yet.</p>}
        <SelfSignedAction certificate={certificate} />
      </Fieldset>
      <UploadForm />
    </>
  );
}

function CertificateDetails({ certificate }: { certificate: WebCertificate }) {
  return (
    <>
      {certificate.expired && (
        <Callout tone="danger" lead="Expired.">
          Regenerate it or install a new one.
        </Callout>
      )}
      <dl className="bp-facts">
        <dt>Source</dt>
        <dd>{certificate.source === "self-signed" ? "BACpack self-signed" : "Your own certificate"}</dd>
        <dt>SHA-256 fingerprint</dt>
        <dd>
          <code>{certificate.fingerprint_sha256}</code>
        </dd>
        <dt>Valid from</dt>
        <dd>{formatUtc(certificate.not_before)}</dd>
        <dt>Valid until</dt>
        <dd>{formatUtc(certificate.not_after)}</dd>
        <dt>Addresses</dt>
        <dd>{certificate.sans.join(", ")}</dd>
      </dl>
      <p>
        <a href={WEB_CERTIFICATE_URL} download>
          Download certificate
        </a>
      </p>
    </>
  );
}

function SelfSignedAction({ certificate }: { certificate: WebCertificate | null }) {
  const refresh = useRefreshWeb();
  const { ask, dialog } = useConfirm();
  const own = certificate?.source === "own";
  const label = own ? "Go back to self-signed" : "Regenerate";
  const make = useMutation({ mutationFn: webServerApi.useSelfSigned, onSuccess: refresh });
  return (
    <>
      <div className="bp-cluster">
        <Button
          type="button"
          disabled={make.isPending}
          onClick={() =>
            ask({
              title: `${label}?`,
              body: own
                ? "BACpack removes your own certificate from this computer. Browsers will warn again."
                : "Browsers will warn again.",
              confirmLabel: label,
              onConfirm: () => make.mutateAsync().then(
                () => undefined,
                () => undefined
              ),
            })
          }
        >
          {label}
        </Button>
      </div>
      <ActionStatus action={make} failed="Couldn't make the certificate" done="New certificate made." />
      {dialog}
    </>
  );
}

// FileReader rather than Blob.text(): the test environment's File has no text().
function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error(`Couldn't read ${file.name}.`));
    reader.readAsText(file);
  });
}

function UploadForm() {
  const refresh = useRefreshWeb();
  const [certFile, setCertFile] = useState<File | null>(null);
  const [keyFile, setKeyFile] = useState<File | null>(null);
  const [round, setRound] = useState(0);
  const install = useMutation({
    mutationFn: async () => {
      if (!certFile || !keyFile) throw new Error("Choose both files.");
      return webServerApi.uploadCertificate({
        certificate_pem: await readText(certFile),
        key_pem: await readText(keyFile),
      });
    },
    onSuccess: (fresh) => {
      setCertFile(null);
      setKeyFile(null);
      setRound((n) => n + 1);
      refresh(fresh);
    },
  });
  return (
    <Form
      onSubmit={(e) => {
        e.preventDefault();
        install.mutate();
      }}
    >
      <Fieldset legend="Use your own certificate">
        <p className="bp-help">
          The key can't have a passphrase.
        </p>
        <Field label="Certificate (PEM, chain allowed)">
          <input key={round} type="file" accept=".pem,.crt,.cer" onChange={(e) => setCertFile(e.target.files?.[0] ?? null)} />
        </Field>
        <Field label="Private key (PEM)">
          <input key={round} type="file" accept=".pem,.key" onChange={(e) => setKeyFile(e.target.files?.[0] ?? null)} />
        </Field>
        <FormActions>
          <Button variant="primary" type="submit" disabled={!certFile || !keyFile || install.isPending}>
            Install certificate
          </Button>
        </FormActions>
        <ActionStatus action={install} failed="Couldn't install the certificate" done="Certificate installed." hideDone={certFile !== null || keyFile !== null} />
      </Fieldset>
    </Form>
  );
}
