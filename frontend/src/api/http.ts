// Thin fetch wrapper: same-origin JSON calls against BACpack's /api/* JSON
// API. The session cookie rides along on its own; an expired session or an
// install still waiting for its setup screen is reported through
// setAuthHandlers.

interface ValidationEntry {
  loc: (string | number)[];
  msg: string;
}

function isValidationEntry(entry: unknown): entry is ValidationEntry {
  if (typeof entry !== "object" || entry === null) return false;
  const e = entry as { loc?: unknown; msg?: unknown };
  return Array.isArray(e.loc) && typeof e.msg === "string";
}

// An error body's `detail` as one readable line. FastAPI's 422 detail is a
// list of {loc, msg}: shown as "field: message", without Pydantic's
// "Value error, " prefix. Anything else not a string is shown as
// JSON rather than "[object Object]".
export function formatErrorDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0 && detail.every(isValidationEntry)) {
    return detail
      .map((entry) => {
        const field = [...entry.loc].reverse().find((part) => typeof part === "string" && part !== "body");
        const msg = entry.msg.replace(/^Value error, /, "");
        return field === undefined ? msg : `${field}: ${msg}`;
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  readonly status: number;
  // The response's `detail` when it is a plain string, e.g. "setup_required".
  readonly code?: string;
  // From a 429's Retry-After header.
  readonly retryAfterSeconds?: number;
  constructor(message: string, status: number, code?: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export interface AuthHandlers {
  onUnauthorized?: () => void;
  onSetupRequired?: () => void;
}

let authHandlers: AuthHandlers = {};

export function setAuthHandlers(handlers: AuthHandlers): void {
  authHandlers = handlers;
}

// The server's 401 for a call that needs a session; other 401s (a wrong
// password on a settings form, a wrong login) are the form's own errors.
const LOGIN_REQUIRED = "login required";
const SETUP_REQUIRED = "setup_required";

export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const hasBody = init?.body !== undefined && init.body !== null;
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(hasBody ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });

  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    let code: string | undefined;
    try {
      const body = await res.json();
      if (body && typeof body === "object" && "detail" in body) {
        detail = formatErrorDetail(body.detail);
        if (typeof body.detail === "string") code = body.detail;
      }
    } catch {
      // non-JSON error body -- fall back to statusText
    }
    if (res.status === 401 && code === LOGIN_REQUIRED) authHandlers.onUnauthorized?.();
    if (res.status === 403 && code === SETUP_REQUIRED) authHandlers.onSetupRequired?.();
    const retryAfter = Number(res.headers.get("retry-after"));
    throw new ApiError(detail, res.status, code, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined);
  }

  if (res.status === 204) {
    return undefined as T;
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

function jsonBody(body: unknown): string | undefined {
  return body === undefined ? undefined : JSON.stringify(body);
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: jsonBody(body) }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: jsonBody(body) }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body: jsonBody(body) }),
  del: <T>(path: string, body?: unknown) => request<T>(path, { method: "DELETE", body: jsonBody(body) }),
};
