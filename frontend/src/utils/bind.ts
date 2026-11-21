// Bind addresses as the UI shows them. The stored and API form stays
// "192.168.1.140/24" or "192.168.1.140/24:47809".

export const GUIDE_URL = "https://github.com/dmpatters/BACpack/blob/main/docs/guide/sharing-the-bacnet-port.md";

export const DEFAULT_BACNET_PORT = 47808;

// What every bind address field says about the format.
export const BIND_ADDRESS_HELP = "IPv4 address/prefix and optional port, e.g. 192.168.1.50/24:47810.";
export const BIND_ADDRESS_PLACEHOLDER = "192.168.1.50/24";

const BIND = /^(\d{1,3}(?:\.\d{1,3}){3})(?:\/(\d{1,2}))?(?::(\d{1,5}))?$/;

export interface ParsedBind {
  host: string;
  prefix: number | null;
  port: number;
}

export function parseBind(bind: string): ParsedBind | null {
  const match = BIND.exec(bind.trim());
  if (!match) return null;
  return {
    host: match[1],
    prefix: match[2] === undefined ? null : Number(match[2]),
    port: match[3] === undefined ? DEFAULT_BACNET_PORT : Number(match[3]),
  };
}

// "192.168.1.140/24 · UDP 47808". A device with no address of its own shows the
// server default, marked as such; with neither, "default".
export function formatBind(bind: string | null, fallback?: string | null): string {
  if (!bind) return fallback ? `${formatBind(fallback)} (default)` : "default";
  const parsed = parseBind(bind);
  if (!parsed) return bind;
  return `${addressOf(parsed)} · UDP ${parsed.port}`;
}

function addressOf(parsed: ParsedBind): string {
  return parsed.prefix === null ? parsed.host : `${parsed.host}/${parsed.prefix}`;
}

// The same as formatBind, as two values for a narrow layout (a device card's
// "bind" and "port" rows); "—" for a port it can't tell.
export function bindParts(bind: string | null, fallback?: string | null): { address: string; port: string } {
  if (!bind) {
    if (!fallback) return { address: "default", port: "—" };
    const parts = bindParts(fallback);
    return { address: `${parts.address} (default)`, port: parts.port };
  }
  const parsed = parseBind(bind);
  if (!parsed) return { address: bind, port: "—" };
  return { address: addressOf(parsed), port: String(parsed.port) };
}

function toInt(host: string): number {
  return host.split(".").reduce((total, part) => total * 256 + Number(part), 0);
}

function maskOf(prefix: number): number {
  return prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
}

// The same UDP port on overlapping subnets (an address without a prefix is one
// host): one BACnet network, whatever BACpack does. False when either side
// can't be read; a caller passes the server default for a device without its own.
export function sameNetwork(a: string | null, b: string | null): boolean {
  const first = a ? parseBind(a) : null;
  const second = b ? parseBind(b) : null;
  if (!first || !second || first.port !== second.port) return false;
  const mask = maskOf(Math.min(first.prefix ?? 32, second.prefix ?? 32));
  return ((toInt(first.host) & mask) >>> 0) === ((toInt(second.host) & mask) >>> 0);
}
