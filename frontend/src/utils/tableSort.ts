import { useState } from "react";

export type SortDir = "ascending" | "descending";
export type SortState<K extends string> = { key: K; dir: SortDir } | null;

export function useSort<K extends string>(initial: SortState<K> = null) {
  const [sort, setSort] = useState<SortState<K>>(initial);
  const toggle = (key: K) =>
    setSort((s) => (s?.key === key ? { key, dir: s.dir === "ascending" ? "descending" : "ascending" } : { key, dir: "ascending" }));
  return { sort, toggle };
}

export function sortRows<T, K extends string>(
  rows: readonly T[],
  sort: SortState<K>,
  keyOf: Record<K, (row: T) => string | number>
): T[] {
  if (!sort) return [...rows];
  const key = keyOf[sort.key];
  const sign = sort.dir === "ascending" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = key(a);
    const y = key(b);
    const order =
      typeof x === "number" && typeof y === "number"
        ? x - y
        : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" });
    return order * sign;
  });
}

export function matchesSearch(query: string, ...fields: Array<string | number | null | undefined>): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return fields.some((f) => f !== null && f !== undefined && String(f).toLowerCase().includes(q));
}
