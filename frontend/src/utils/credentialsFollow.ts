const DEFAULT_PORTS: Record<string, string> = { "http:": "80", "https:": "443" };

function origin(url: string): { protocol: string; host: string; port: string } | null {
  try {
    const u = new URL(url.trim());
    if (!DEFAULT_PORTS[u.protocol] || !u.hostname) return null;
    return { protocol: u.protocol, host: u.hostname, port: u.port || DEFAULT_PORTS[u.protocol] };
  } catch {
    return null;
  }
}

// The browser's URL parser is more forgiving than the server's on malformed input, so the warning
// is a guide and the server decides.
// The server's rule for a connector's saved credentials (connector_config.credentials_follow):
// they follow a URL on the same origin, or a plain http -> https upgrade on the same host.
export function credentialsFollow(oldUrl: string, newUrl: string): boolean {
  const a = origin(oldUrl);
  const b = origin(newUrl);
  if (!a || !b || a.host !== b.host) return false;
  if (a.protocol === b.protocol) return a.port === b.port;
  return a.protocol === "http:" && a.port === "80" && b.protocol === "https:" && b.port === "443";
}

// True when saving `edited` over `saved` makes the server clear the saved credentials. A URL still
// being typed (not http(s) with a host) says no: the server refuses it anyway.
export function urlClearsCredentials(saved: string, edited: string): boolean {
  if (origin(edited) === null) return false;
  return !credentialsFollow(saved, edited);
}
