// Risk intelligence for the live monitor: separate layers (screening, observed-graph relations,
// behavioural anomaly, supervised abstention) with an ordinal review priority. Types mirror
// /v1/live/risk/summary, /v1/live/findings, /v1/live/alerts and the `risk` record of
// /v1/live/transactions/{txid} (src/chainwatch/risk/*). There is never a single score.
import { liveGet } from "./live";

// ------------------------------------------------------------------ types
export type PriorityLevel = 1 | 2 | 3 | 4;
export type Layer = "screening" | "graph" | "anomaly";
export type ScreeningStatus =
  | "SCREENING_MATCH"
  | "NO_KNOWN_SCREENING_MATCH"
  | "SCREENING_DATA_UNAVAILABLE"
  | "INSUFFICIENT_INFORMATION"
  | "ERROR";
export type GraphStatus = "RELATION_FOUND" | "NO_KNOWN_RELATION" | "NOT_EVALUATED" | "ERROR";
export type AnomalyStatus = "ANOMALY" | "NO_ANOMALY" | "INSUFFICIENT_BASELINE" | "INSUFFICIENT_INFORMATION" | "ERROR";
export type AlertStatus = "new" | "under_review" | "dismissed" | "resolved" | "insufficient_evidence";
export type LifecycleState = "mined" | "removed" | "replaced";

export interface Priority {
  level: PriorityLevel | null;
  label: string;
  semantics: string;
}

export interface ScreeningMatch {
  role: "in" | "out";
  index: number;
  address: string;
  source_id: string;
  source_version: string;
  indicator_id: number;
  category: string;
  label: string;
  reference: string;
  /** The source's own per-address listing date (OFAC gives none: null). */
  listed_on: string | null;
  /** When the listed PARTY was designated (ISO date); the address may have been added later. */
  party_designated_on?: string | null;
  /** Point-in-time availability: when ChainWatch first had a snapshot containing the address. */
  available_at: number;
  programs: string[];
}

export interface GraphRelation {
  type: string;
  distance: number;
  path: string[];
  via_parent: string;
  source_id: string;
  label: string;
  reference: string;
}

export interface AnomalyIndicator {
  measurement: string;
  label: string;
  unit: string;
  value: number;
  direction: "high" | "low";
  reference_median: number;
  tail_probability: number;
  robust_z: number | null;
  outside_reference_range: boolean;
  explanation: string;
}

export interface Lifecycle {
  state: LifecycleState | null;
  at: number | null;
  replaced_by: string | null;
}

export interface Finding {
  id: number;
  txid: string;
  evaluated_at: number;
  observed_at: number;
  priority: Priority;
  screening: { status: ScreeningStatus; matches: ScreeningMatch[] };
  graph: { status: GraphStatus; relations: GraphRelation[] };
  anomaly: { status: AnomalyStatus; level: "strong" | "notable" | null; indicators: AnomalyIndicator[] };
  lifecycle: Lifecycle | null;
}

export interface Alert {
  id: number;
  group_key: string;
  layer: Layer;
  kind: string;
  priority: number;
  status: AlertStatus;
  title: string;
  created_at: number;
  updated_at: number;
  last_seen_at: number;
  occurrences: number;
  suppressed: number;
  retrospective: boolean;
  txids: string[];
  evidence: Record<string, unknown>;
  note: string | null;
}

export interface IndicatorSource {
  source_id: string;
  version_id: number;
  sha256: string;
  method: string;
  url: string;
  licence: string;
  retrieved_at: number;
  published_at: number | null;
  checked_at: number | null;
  n_records: number;
  n_rejected: number;
  active_indicators: number;
  first_retrieved_at: number | null;
  /** Server-side staleness (collector's `indicator_max_age_days`); absent on older servers. */
  stale?: boolean;
  age_s?: number | null;
}

export interface RiskSummaryCounts {
  since: number;
  fetched: number;
  assessed: number;
  not_assessed: number;
  by_priority: Partial<Record<"1" | "2" | "3" | "4" | "none", number>>;
  screening: Partial<Record<ScreeningStatus, number>>;
  graph: Partial<Record<GraphStatus, number>>;
  anomaly: Partial<Record<AnomalyStatus, number>>;
  supervised: Partial<Record<string, number>>;
  assessments_with_layer_errors: number;
  latency_ms: { p50: number | null; p95: number | null; max: number | null };
  lifecycle: Partial<Record<LifecycleState | "unknown", number>>;
  alerts_by_status: Partial<Record<AlertStatus, number>>;
  sources: IndicatorSource[];
}

export interface RiskSummary {
  mode: "live";
  enabled: boolean;
  window_hours: number;
  summary: RiskSummaryCounts | null;
  engine: {
    anomaly_reference_size: number | null;
    risk_latency_ms_p50: number | null;
    risk_latency_ms_p95: number | null;
    risk_errors: number | null;
    layer_errors: number | null;
    supervised: { state: string; model: string | null; reason: string | null } | null;
    /** Absent on servers before these settings were reported. */
    indicator_max_age_days?: number | null;
    anomaly_min_reference?: number | null;
  } | null;
  layers: Partial<Record<Layer | "supervised", string>> | null;
}

export interface RecordSource {
  source_id: string;
  version: string;
  method: string;
  retrieved_at: number;
  checked_at: number;
  age_s: number;
  stale: boolean;
  active_indicators: number | null;
}

export interface RiskRecord {
  txid: string;
  engine_version: string;
  feature_schema: string;
  observed_at: number;
  fetched_at: number;
  evaluated_at: number;
  screening: {
    status: ScreeningStatus;
    matches?: ScreeningMatch[];
    coverage?: {
      addresses_screened: number;
      inputs_without_address: number;
      inputs_missing_prevout: number;
      outputs_without_address: number;
      truncated: boolean;
    };
    sources?: RecordSource[];
    evaluated_at?: number;
  };
  graph: {
    status: GraphStatus;
    relations?: GraphRelation[];
    coverage?: { parents: number; parents_observed: number };
    limitation?: string;
  };
  anomaly: {
    status: AnomalyStatus;
    level?: "strong" | "notable" | null;
    indicators?: AnomalyIndicator[];
    detector?: { version: string; reference_size: number; window: number; alpha: number };
    detail?: string;
  };
  supervised: { status: string; detail?: string; gate?: string | null };
  priority: Priority;
  insufficient_evidence: boolean;
  errors: { layer: string; error: string }[];
  latency_ms: number;
}

export const riskApi = {
  summary: (hours = 24) => liveGet<RiskSummary>(`/v1/live/risk/summary?hours=${hours}`),
  findings: (maxPriority = 4, limit = 50) =>
    liveGet<{ findings: Finding[] }>(`/v1/live/findings?max_priority=${maxPriority}&limit=${limit}`),
  alerts: (status: AlertStatus | null = null, limit = 50) =>
    liveGet<{ alerts: Alert[] }>(`/v1/live/alerts?limit=${limit}${status ? `&status=${status}` : ""}`),
};

// ------------------------------------------------------------------ wording (hard rules)
/**
 * Words that must never describe a finding. A listing is a designation by a named source; an
 * anomaly is "unusual"; proximity is not culpability. Tests scan every text constant below.
 */
export const FORBIDDEN_WORDS = /\b(fraud|fraudulent|criminal|guilty|suspicious entity|illicit)\b/i;
/** The one sanctioned use of "fraud": stating what a listing is not. */
export const LISTING_CAVEAT = "a sanctions listing is a legal designation, not proof of fraud";

export const LAYER_TEXT = {
  screeningNoMatch: "No known screening match (only the enabled sources; not proof of legitimacy)",
  anomaly: "Behavioural anomaly (unusual, not evidence of wrongdoing)",
  graph: "Observed-graph relation (proximity is not ownership or culpability)",
  supervised: "No validated model: abstaining",
} as const;

// ------------------------------------------------------------------ priorities
export interface PriorityInfo {
  short: string; // "P1"
  name: string; // what the level means
  aria: string; // accessible label for the marker
}

const PRIORITY_NAME: Record<PriorityLevel, string> = {
  1: "Source match: an input or output address is listed by an enabled source",
  2: "Observed-graph relation to a listed address (distance 2, observed data only)",
  3: "Strong behavioural anomaly (unusual, not evidence of wrongdoing)",
  4: "Notable behavioural anomaly (unusual, not evidence of wrongdoing)",
};

export const PRIORITY_INFO: Record<PriorityLevel, PriorityInfo> = Object.fromEntries(
  ([1, 2, 3, 4] as PriorityLevel[]).map((k) => [
    k,
    { short: `P${k}`, name: PRIORITY_NAME[k], aria: `Review priority ${k}: ${PRIORITY_NAME[k]}` },
  ]),
) as Record<PriorityLevel, PriorityInfo>;

export const PRIORITY_SEMANTICS = "Ordinal review priority (1 = review first), not a probability or score of wrongdoing.";

export function isPriority(v: unknown): v is PriorityLevel {
  return v === 1 || v === 2 || v === 3 || v === 4;
}

/** Marker for a feed row. `null` is ambiguous on the wire: no finding, or not (yet) assessed. */
export function priorityMarker(level: number | null | undefined): { short: string; aria: string } {
  if (isPriority(level)) return { short: PRIORITY_INFO[level].short, aria: PRIORITY_INFO[level].aria };
  return { short: "—", aria: "No review priority: no finding, or not assessed yet" };
}

// ------------------------------------------------------------------ layer badges
export interface LayerBadge {
  text: string;
  cls: "layer-screening" | "layer-graph" | "layer-anomaly";
  caveat: string;
}

export const LAYER_BADGE: Record<Layer, LayerBadge> = {
  screening: { text: "Source match", cls: "layer-screening", caveat: LISTING_CAVEAT },
  graph: { text: "Observed-graph relation", cls: "layer-graph", caveat: "proximity is not ownership or culpability" },
  anomaly: { text: "Behavioural anomaly", cls: "layer-anomaly", caveat: "unusual, not evidence of wrongdoing" },
};

export function layerBadge(layer: string): LayerBadge {
  return (LAYER_BADGE as Record<string, LayerBadge>)[layer] ?? { text: layer, cls: "layer-anomaly", caveat: "" };
}

/** Layers that produced a finding for this transaction, in priority order. */
export function findingLayers(f: Pick<Finding, "screening" | "graph" | "anomaly">): Layer[] {
  const out: Layer[] = [];
  if (f.screening?.status === "SCREENING_MATCH") out.push("screening");
  if (f.graph?.status === "RELATION_FOUND") out.push("graph");
  if (f.anomaly?.status === "ANOMALY") out.push("anomaly");
  return out;
}

// ------------------------------------------------------------------ status text
export type Tone = "ok" | "info" | "warn" | "off";
export interface StatusText {
  text: string;
  tone: Tone;
}

export function screeningStatusText(status: string | null | undefined, sources: string[] = []): StatusText {
  const by = sources.length ? sources.join(", ") : "an enabled source";
  switch (status) {
    case "SCREENING_MATCH":
      return { text: `Listed by ${by} (${LISTING_CAVEAT})`, tone: "info" };
    case "NO_KNOWN_SCREENING_MATCH":
      return { text: LAYER_TEXT.screeningNoMatch, tone: "ok" };
    case "SCREENING_DATA_UNAVAILABLE":
      return { text: "Screening data unavailable: no indicator source was loaded, nothing was screened", tone: "warn" };
    case "INSUFFICIENT_INFORMATION":
      return { text: "Not screened: no input or output address could be determined", tone: "warn" };
    case "ERROR":
      return { text: "Screening failed for this transaction (layer error)", tone: "warn" };
    default:
      return { text: "Not assessed", tone: "off" };
  }
}

export function graphStatusText(status: string | null | undefined): StatusText {
  switch (status) {
    case "RELATION_FOUND":
      return { text: LAYER_TEXT.graph, tone: "info" };
    case "NO_KNOWN_RELATION":
      return { text: "No known relation within observed data (unobserved parents are not checked)", tone: "ok" };
    case "NOT_EVALUATED":
      return { text: "Not evaluated (no parent transactions to check)", tone: "off" };
    case "ERROR":
      return { text: "Relation check failed for this transaction (layer error)", tone: "warn" };
    default:
      return { text: "Not assessed", tone: "off" };
  }
}

export function anomalyStatusText(status: string | null | undefined, level?: string | null): StatusText {
  switch (status) {
    case "ANOMALY":
      return { text: `${level === "strong" ? "Strong b" : level === "notable" ? "Notable b" : "B"}ehavioural anomaly (unusual, not evidence of wrongdoing)`, tone: "info" };
    case "NO_ANOMALY":
      return { text: "No anomaly against recently sampled traffic", tone: "ok" };
    case "INSUFFICIENT_BASELINE":
      return { text: "Insufficient baseline: too few earlier transactions to compare with", tone: "warn" };
    case "INSUFFICIENT_INFORMATION":
      return { text: "Insufficient information: no measurement could be compared", tone: "warn" };
    case "ERROR":
      return { text: "Anomaly detection failed for this transaction (layer error)", tone: "warn" };
    default:
      return { text: "Not assessed", tone: "off" };
  }
}

export function supervisedStatusText(status: string | null | undefined): StatusText {
  if (status === "NO_VALIDATED_MODEL" || status === undefined || status === null) {
    return { text: LAYER_TEXT.supervised, tone: "off" };
  }
  return { text: `Supervised layer state: ${status}`, tone: "off" };
}

export const ALERT_STATUS_LABEL: Record<AlertStatus, string> = {
  new: "New",
  under_review: "Under review",
  dismissed: "Dismissed",
  resolved: "Resolved",
  insufficient_evidence: "Insufficient evidence",
};
export const ALERT_STATUSES = Object.keys(ALERT_STATUS_LABEL) as AlertStatus[];

export function alertStatusLabel(s: string): string {
  return (ALERT_STATUS_LABEL as Record<string, string>)[s] ?? s;
}

/** Open = still needs a reviewer (the backend sorts these first). */
export function isOpenAlert(s: string): boolean {
  return s === "new" || s === "under_review";
}

export const RETROSPECTIVE_TEXT = "retrospective: observed before the source listed it";
export const STATUS_CLI_NOTE = "Read-only here. Alert statuses are changed on the collector host with";
export const STATUS_CLI_COMMAND = "chainwatch-live alerts set-status --id <id> --status <status> [--note …]";

// ------------------------------------------------------------------ alerts
/** Same order as the API (open first, then priority, then most recently seen); stable. */
export function sortAlerts(alerts: Alert[]): Alert[] {
  return alerts
    .map((a, i) => ({ a, i }))
    .sort(
      (x, y) =>
        Number(isOpenAlert(y.a.status)) - Number(isOpenAlert(x.a.status)) ||
        x.a.priority - y.a.priority ||
        y.a.last_seen_at - x.a.last_seen_at ||
        x.i - y.i,
    )
    .map((x) => x.a);
}

export function filterAlerts(alerts: Alert[], status: AlertStatus | "open" | null): Alert[] {
  if (status === null) return alerts;
  if (status === "open") return alerts.filter((a) => isOpenAlert(a.status));
  return alerts.filter((a) => a.status === status);
}

export interface AlertEvidenceView {
  match: Partial<ScreeningMatch> | null;
  relation: GraphRelation | null;
  indicators: AnomalyIndicator[];
  limitation: string | null;
  txid: string | null;
}

const obj = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

/**
 * Evidence comes in several shapes: {match, txid} (screening), {address, role, …} (retrospective
 * screening), {relation, txid, limitation} (graph), {indicators, txid} (anomaly).
 */
export function alertEvidence(a: Pick<Alert, "evidence" | "layer">): AlertEvidenceView {
  const e = obj(a.evidence) ?? {};
  const match = obj(e.match) ?? (typeof e.address === "string" && a.layer === "screening" ? e : null);
  return {
    match: match as Partial<ScreeningMatch> | null,
    relation: (obj(e.relation) as GraphRelation | null) ?? null,
    indicators: Array.isArray(e.indicators) ? (e.indicators as AnomalyIndicator[]) : [],
    limitation: typeof e.limitation === "string" ? e.limitation : null,
    txid: typeof e.txid === "string" ? e.txid : null,
  };
}

// ------------------------------------------------------------------ sources / freshness
/** Fallback for servers that do not report `indicator_max_age_days` (collector default: 7). */
export const SOURCE_MAX_AGE_DAYS = 7;
export const SOURCE_MAX_AGE_S = SOURCE_MAX_AGE_DAYS * 86400;

export const SOURCE_NAME: Record<string, string> = {
  "ofac-sdn": "OFAC SDN (official list)",
  "ofac-sdn-mirror": "OFAC SDN (third-party extraction)",
};

export function sourceName(id: string): string {
  return SOURCE_NAME[id] ?? id;
}

export interface SourceFreshness {
  state: "fresh" | "stale" | "never";
  age_s: number | null;
  /** The source content was loaded from a local file, not fetched from its publisher. */
  localFile: boolean;
}

export function sourceFreshness(
  s: Pick<IndicatorSource, "checked_at" | "retrieved_at" | "url" | "stale"> | null | undefined,
  now: number,
  maxAge = SOURCE_MAX_AGE_S,
): SourceFreshness {
  if (!s) return { state: "never", age_s: null, localFile: false };
  const checked = s.checked_at ?? s.retrieved_at ?? null;
  const localFile = typeof s.url === "string" && s.url.startsWith("file:");
  if (checked === null || !Number.isFinite(checked)) return { state: "never", age_s: null, localFile };
  const age = Math.max(0, now - checked);
  const stale = typeof s.stale === "boolean" ? s.stale : age > maxAge; // the server's rule wins
  return { state: stale ? "stale" : "fresh", age_s: age, localFile };
}

export interface ScreeningAvailability {
  available: boolean;
  anyStale: boolean;
  headline: string;
}

/** Screening is unavailable when no source was ever loaded; stale sources still screen (with a warning). */
export function screeningAvailability(
  sources: IndicatorSource[] | null | undefined,
  now: number,
  maxAgeDays = SOURCE_MAX_AGE_DAYS,
): ScreeningAvailability {
  const list = sources ?? [];
  const states = list.map((s) => sourceFreshness(s, now, maxAgeDays * 86400).state);
  const loaded = states.filter((s) => s !== "never").length;
  if (loaded === 0) return { available: false, anyStale: false, headline: "Screening data unavailable" };
  const anyStale = states.includes("stale");
  return {
    available: true,
    anyStale,
    headline: anyStale ? `STALE: source not refreshed within ${maxAgeDays} days` : `${loaded} source${loaded === 1 ? "" : "s"} loaded`,
  };
}

export const shortSha = (sha: string | null | undefined): string => (sha ? sha.slice(0, 12) : "—");

// ------------------------------------------------------------------ anomaly baseline
/** Fallback for servers that do not report `anomaly_min_reference` (DetectorConfig default). */
export const ANOMALY_MIN_REFERENCE = 500;

export function baselineProgress(size: number | null | undefined, need = ANOMALY_MIN_REFERENCE): {
  ready: boolean;
  text: string;
  share: number | null;
} {
  if (size === null || size === undefined || !Number.isFinite(size)) {
    return { ready: false, text: "unknown (collector not reporting)", share: null };
  }
  return { ready: size >= need, text: `${size} of ${need} needed`, share: Math.min(1, size / need) };
}

// ------------------------------------------------------------------ evidence text
/** Several parties / references are joined by "; " in one field. */
export function splitList(v: string | null | undefined): string[] {
  return (v ?? "").split(";").map((x) => x.trim()).filter(Boolean);
}

export function partyDesignatedText(d: string | null | undefined): string | null {
  return d ? `party designated on ${d} (the address may have been added later)` : null;
}

export function roleText(role: string | null | undefined): string {
  if (role === "in") return "spends from";
  if (role === "out") return "pays to";
  return "involves";
}

export function lifecycleText(lc: Lifecycle | null | undefined): string {
  if (!lc || !lc.state) return "No later state seen (still unconfirmed when last checked, or not tracked)";
  if (lc.state === "mined") return "Mined";
  if (lc.state === "removed") return "Removed from the mempool (evicted or expired; not mined when last seen)";
  if (lc.state === "replaced") return lc.replaced_by ? `Replaced by ${lc.replaced_by}` : "Replaced (by an unknown transaction)";
  return String(lc.state);
}

function fmtNumber(v: number, unit: string): string {
  if (!Number.isFinite(v)) return String(v);
  return unit === "sat/vB" ? v.toFixed(2) : Math.round(v).toLocaleString("en-US");
}

/**
 * One anomaly indicator as a sentence. The backend's explanation is shown verbatim when present
 * (it names the reference size and median); the detail line adds the test statistics.
 */
export function formatIndicator(i: Partial<AnomalyIndicator>): { title: string; sentence: string; stats: string } {
  const unit = i.unit ?? "";
  const sentence =
    i.explanation && i.explanation.trim()
      ? i.explanation.trim()
      : i.value !== undefined && i.reference_median !== undefined
        ? `${i.measurement ?? "value"} = ${fmtNumber(i.value, unit)} ${unit} (median of recent sample ${fmtNumber(i.reference_median, unit)} ${unit}).`
        : "No explanation recorded.";
  const parts: string[] = [];
  if (typeof i.tail_probability === "number") parts.push(`tail probability ${formatProbability(i.tail_probability)}`);
  if (typeof i.robust_z === "number") parts.push(`robust z ${i.robust_z.toFixed(1)}`);
  else if (i.robust_z === null) parts.push("robust z unbounded (no spread in the reference)");
  if (i.outside_reference_range) parts.push("outside the whole reference range");
  return { title: i.label ?? i.measurement ?? "Anomaly", sentence, stats: parts.join(" · ") };
}

export function formatProbability(p: number): string {
  if (!Number.isFinite(p)) return "—";
  if (p === 0) return "0";
  return p < 0.001 ? p.toExponential(1) : p.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

/** Short summary lines for a finding, one per layer that found something. */
export function findingLines(f: Pick<Finding, "screening" | "graph" | "anomaly">): { layer: Layer; text: string }[] {
  const out: { layer: Layer; text: string }[] = [];
  const matches = f.screening?.matches ?? [];
  if (f.screening?.status === "SCREENING_MATCH") {
    const m = matches[0];
    const parties = splitList(m?.label);
    const party = parties.length
      ? ` (${parties[0]}${parties.length > 1 ? ` and ${parties.length - 1} other part${parties.length === 2 ? "y" : "ies"}` : ""})`
      : "";
    const more = matches.length > 1 ? ` (+${matches.length - 1} more)` : "";
    out.push({
      layer: "screening",
      text: m
        ? `${capitalise(roleText(m.role))} an address listed by ${sourceName(m.source_id)}${party}${more}`
        : "Listed by an enabled source",
    });
  }
  if (f.graph?.status === "RELATION_FOUND") {
    const r = (f.graph.relations ?? [])[0];
    out.push({
      layer: "graph",
      text: r
        ? `Spends an output of ${r.via_parent.slice(0, 12)}…, which spent from an address listed by ${sourceName(r.source_id)}`
        : "Relation within observed data",
    });
  }
  if (f.anomaly?.status === "ANOMALY") {
    for (const i of (f.anomaly.indicators ?? []).slice(0, 2)) {
      out.push({ layer: "anomaly", text: `${i.label}: ${formatIndicator(i).sentence}` });
    }
    if ((f.anomaly.indicators ?? []).length === 0) out.push({ layer: "anomaly", text: anomalyStatusText("ANOMALY", f.anomaly.level).text });
  }
  return out;
}

const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** Latency in ms, compact. */
export function formatMs(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v < 10 ? `${v.toFixed(2)} ms` : `${Math.round(v)} ms`;
}

/** Index findings by txid (newest wins) to fill feed priorities the event stream did not carry. */
export function priorityByTxid(findings: Pick<Finding, "txid" | "priority">[]): Map<string, PriorityLevel> {
  const m = new Map<string, PriorityLevel>();
  for (const f of findings) {
    const lvl = f.priority?.level;
    if (isPriority(lvl) && !m.has(f.txid)) m.set(f.txid, lvl);
  }
  return m;
}
