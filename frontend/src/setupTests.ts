import "@testing-library/jest-dom/vitest";

// A data router (createMemoryRouter) builds a Request with an AbortSignal for
// every navigation; jsdom replaces AbortSignal, and Node's Request rejects the
// replacement ("Expected signal to be an instance of AbortSignal"). The SPA
// has no loaders to abort, so tests drop the signal.
if (typeof Request !== "undefined") {
  const NodeRequest = Request;
  globalThis.Request = class extends NodeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(input, init && { ...init, signal: undefined });
    }
  } as typeof Request;
}

// jsdom doesn't implement matchMedia; the theme code reads
// prefers-color-scheme through it.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as MediaQueryList;
}
