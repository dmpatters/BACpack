import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { capabilitiesApi, diagnosticsApi } from "../api/endpoints";
import type { CapabilityBibb } from "../api/types";
import { buttonClass } from "../components/Button";
import { CheckField } from "../components/Form";
import PageHeader from "../components/PageHeader";
import StatusPill from "../components/StatusPill";
import { TableFrame } from "../components/DataTable";
import { segmentationText, words } from "../utils/text";
import { usePageTitle } from "../utils/usePageTitle";
import { REPO_URL } from "../utils/links";

function groupByArea(bibbs: CapabilityBibb[]): [string, CapabilityBibb[]][] {
  const groups = new Map<string, CapabilityBibb[]>();
  for (const bibb of bibbs) {
    groups.set(bibb.area, [...(groups.get(bibb.area) ?? []), bibb]);
  }
  return [...groups.entries()];
}

// Each distinct note once, with the BIBBs that carry it: today only
// DM-TS-B/DM-UTC-B's "either one" rule, which explains the summary count.
function notesWithBibbs(bibbs: CapabilityBibb[]): [string, string[]][] {
  const notes = new Map<string, string[]>();
  for (const bibb of bibbs) {
    if (bibb.note) notes.set(bibb.note, [...(notes.get(bibb.note) ?? []), bibb.name]);
  }
  return [...notes.entries()];
}

// Shown before the capabilities load: it's what someone filing a bug comes for.
function DiagnosticsSection() {
  const [realValues, setRealValues] = useState(false);
  return (
    <section id="diagnostics">
      <h2>Diagnostics</h2>
      <p className="bp-muted">
        A zip to attach to a bug report.
      </p>
      <CheckField
        label="Use real values"
        checked={realValues}
        onChange={(e) => setRealValues(e.target.checked)}
        help={
          realValues
            ? "Real names, addresses and URLs. Passwords, tokens and header values stay out."
            : "Names, addresses and URLs are replaced with stand-ins."
        }
      />
      <p>
        <a className={buttonClass()} href={diagnosticsApi.downloadUrl(realValues)} download>
          Download diagnostics
        </a>
      </p>
    </section>
  );
}

export default function About() {
  usePageTitle("About");
  const { hash } = useLocation();
  const { data: caps } = useQuery({ queryKey: ["capabilities"], queryFn: capabilitiesApi.get });

  // The capabilities section only exists once the fetch resolves, so the
  // browser's own anchor scroll (e.g. from the Process tab's link) misses it.
  useEffect(() => {
    if (caps && hash === "#capabilities") {
      document.getElementById("capabilities")?.scrollIntoView();
    }
  }, [caps, hash]);

  // The Missing column only says why a BIBB isn't met, so it's shown only
  // while one isn't: with every BIBB met it would be an empty column.
  const showMissing = caps?.bibbs.some((b) => b.missing.length > 0) ?? false;
  const notes = caps ? notesWithBibbs(caps.bibbs) : [];

  return (
    <>
      <PageHeader title="About" />
      <p>
        BACpack is an open-source BACnet server, a device simulator and gateway.
      </p>
      <p className="bp-muted">
        No redundancy or high availability. Set the web UI login on the Security page.
      </p>

      <dl className="bp-facts" data-testid="about-facts">
        <dt>Version</dt>
        <dd>{caps ? caps.bacpack_version : "…"}</dd>
        <dt>License</dt>
        <dd>MIT</dd>
        <dt>Conformance</dt>
        <dd>
          <a href="/api/pics" target="_blank" rel="noreferrer">
            PICS
          </a>
        </dd>
        <dt>Source</dt>
        <dd>
          <a href={REPO_URL}>github.com/dmpatters/BACpack</a>
        </dd>
      </dl>

      <DiagnosticsSection />

      {caps && (
        <section id="capabilities">
          <h2>BACnet capabilities</h2>
          <dl className="bp-facts" data-testid="protocol-facts">
            <dt>Protocol revision</dt>
            <dd>{caps.protocol_revision}</dd>
            <dt>Segmentation</dt>
            <dd>{segmentationText(caps.segmentation)}</dd>
            <dt>Max APDU length</dt>
            <dd>{`${caps.max_apdu_length} bytes`}</dd>
            <dt>Data links</dt>
            <dd>{caps.data_links.join(", ")}</dd>
          </dl>

          <h3>
            {caps.bibb_summary.met} of {caps.bibb_summary.total} B-BC requirements met
          </h3>
          <p className="bp-help" data-testid="bibb-profile-source">
            Listed in {caps.bibb_profile_source} of the standard. BACpack devices advertise protocol revision{" "}
            {caps.protocol_revision}.
          </p>
          <TableFrame label="B-BC requirements">
              <thead>
                <tr>
                  <th>BIBB</th>
                  <th>Title</th>
                  <th>Status</th>
                  {showMissing && <th>Missing</th>}
                </tr>
              </thead>
              {groupByArea(caps.bibbs).map(([area, bibbs]) => (
                <tbody key={area}>
                  <tr>
                    <th scope="rowgroup" colSpan={showMissing ? 4 : 3}>
                      {area}
                    </th>
                  </tr>
                  {bibbs.map((b) => (
                    <tr key={b.name}>
                      <td className="bp-cell-mono">{b.name}</td>
                      <td>{b.title}</td>
                      <td>
                        <StatusPill tone={b.supported ? "ok" : "warn"}>{b.supported ? "Supported" : "Not supported"}</StatusPill>
                      </td>
                      {showMissing && <td>{b.missing.join("; ")}</td>}
                    </tr>
                  ))}
                </tbody>
              ))}
          </TableFrame>
          {notes.length > 0 && (
            <div className="bp-muted" data-testid="bibb-notes">
              {notes.map(([note, names]) => (
                <p key={note}>
                  {names.join(", ")}: {note}
                </p>
              ))}
            </div>
          )}

          <h3>Services</h3>
          <TableFrame label="Services">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Kind</th>
                  <th>Direction</th>
                </tr>
              </thead>
              <tbody>
                {caps.services.map((s) => (
                  <tr key={s.name}>
                    <td>
                      <span>{words(s.name)}</span> <span className="bp-mono bp-muted">{s.name}</span>
                    </td>
                    <td>{words(s.kind)}</td>
                    <td>
                      {[s.executes && "Executes", s.initiates && "Initiates"]
                        .filter(Boolean)
                        .join(" · ")}
                    </td>
                  </tr>
                ))}
              </tbody>
          </TableFrame>

          <h3>Object types</h3>
          <TableFrame label="Object types">
              <thead>
                <tr>
                  <th>Object type</th>
                  <th>Identifier</th>
                </tr>
              </thead>
              <tbody>
                {caps.object_types.map((t) => (
                  <tr key={t}>
                    <td>{words(t)}</td>
                    <td className="bp-mono bp-muted">{t}</td>
                  </tr>
                ))}
              </tbody>
          </TableFrame>
        </section>
      )}
    </>
  );
}
