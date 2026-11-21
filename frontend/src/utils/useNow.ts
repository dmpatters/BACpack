import { useEffect, useState } from "react";

// The clock in ms, re-read every `everyMs` while `ticking`, and at once when
// ticking starts again.
export function useNow(ticking: boolean, everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [ticking, everyMs]);
  return now;
}
