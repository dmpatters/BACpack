import { useEffect, useRef, useState } from "react";
import PageHeader from "../components/PageHeader";
import { ErrorBlock, Loading } from "../components/AsyncState";
import { usePageTitle } from "../utils/usePageTitle";

// Swagger UI is bundled into its own chunk, loaded only on this page.
export default function ApiDocs() {
  usePageTitle("API Docs");
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");

  useEffect(() => {
    let cancelled = false;
    import("./apiDocsSwagger")
      .then(({ mountSwagger }) => {
        if (cancelled || !host.current) return;
        mountSwagger(host.current, "/openapi.json");
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("failed");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <PageHeader
        title="API Docs"
        sub={
          <>
            <a href="/openapi.json">Download the OpenAPI description (JSON)</a>
          </>
        }
      />
      {state === "loading" && <Loading what="the API docs" />}
      {state === "failed" && <ErrorBlock lead="Couldn't load the API docs.">Reload the page to try again.</ErrorBlock>}
      <div className="bp-apidocs" ref={host} />
    </>
  );
}
