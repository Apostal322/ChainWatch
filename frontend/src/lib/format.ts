export const fmtScore = (x: number | null | undefined, digits = 3): string =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : x.toFixed(digits);

export const fmtPct = (x: number | null | undefined, digits = 1): string => {
  if (x === null || x === undefined) return "—";
  const p = x * 100;
  // keep small shares informative ("0.02%", not "0.0%")
  const d = p > 0 && p < 1 ? Math.max(digits, 2) : digits;
  return `${p.toFixed(d)}%`;
};

export const fmtInt = (x: number | null | undefined): string =>
  x === null || x === undefined ? "—" : Math.round(x).toLocaleString("en-US");

/** Expected-hit counts are tie-aware averages; show one decimal only when fractional. */
export const fmtHits = (x: number): string =>
  Math.abs(x - Math.round(x)) < 1e-9 ? String(Math.round(x)) : x.toFixed(1);

/** Short label for a model version, e.g. "snap-expanding-L1-t034" -> "snapshot @ end of step 34". */
export function describeVersion(version: string): string {
  const m = /^snap-expanding-L(\d+)-t(\d+)$/.exec(version);
  if (m) return `snapshot @ end of step ${Number(m[2])} (L=${m[1]})`;
  if (version === "phase1-xgboost-all") return "Phase 1 reference (steps 1–29)";
  return version;
}

/** Short git commit for the footer; keeps a "-dirty" suffix (uncommitted changes) when truncating. */
export function shortCommit(c: string | null | undefined, n = 10): string {
  if (!c) return "n/a";
  const dirty = /-dirty$/.test(c);
  const base = dirty ? c.slice(0, -"-dirty".length) : c;
  return `${base.slice(0, n)}${dirty ? "-dirty" : ""}`;
}
