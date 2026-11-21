// A route id the API can look up. Anything else ("new", "abc", "1e3") would
// be sent as NaN or a different number, so the page shows not-found instead.
export function parseId(param: string | undefined): number | null {
  if (param === undefined || !/^\d+$/.test(param)) return null;
  const id = Number(param);
  return Number.isSafeInteger(id) ? id : null;
}
