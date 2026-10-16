// Live Bitcoin monitor: REAL network observations (mempool.space), separate from the
// historical / synthetic replay. Types mirror /v1/live/* (src/chainwatch/serving/live_api.py).
import { ApiError } from "./api";
import type { Lifecycle, PriorityLevel, RiskRecord } from "./risk";

/** Collector connection as reported by the API ("failed": the collector gave up and needs attention). */
export type Connection = "connected" | "reconnecting" | "disconnected" | "failed";

export interface LiveRow {
  id: number;
  txid: string;
  first_seen: number;
  fetched_at: number;
  outcome: "fetched" | "failed";
  error: string | null;
  fee_rate: number | null;
  fee: number | null;
  vsize: number | null;
  n_inputs: number | null;
  n_outputs: number | null;
  output_value_total: number | null;
  rbf_signal: boolean | null;
  confirmed_at_fetch: boolean | null;
  flags: string[];
  /** Review priority of the assessment (1..4); null: no finding, or not assessed (yet). Absent before v0.7.0. */
  risk_priority?: PriorityLevel | null;
  /** True when a risk record exists (a null priority then means "no finding"). Absent on older servers. */
  risk_assessed?: boolean;
}

export interface LiveFeatures {
  version: number;
  locktime: number;
  locktime_kind: string;
  /** False when every input sequence is final, so the locktime has no effect. Absent from older collectors. */
  locktime_enforced?: boolean | null;
  size: number;
  weight: number;
  vsize: number;
  fee: number;
  fee_rate: number;
  n_inputs: number;
  n_outputs: number;
  n_parent_txs: number;
  segwit: boolean;
  rbf_signal: boolean;
  input_value_total: number | null;
  output_value_total: number;
  max_output_value: number;
  output_values: number[];
  output_types: Record<string, number>;
  input_types: Record<string, number>;
  input_refs: string[];
  n_op_return: number;
  n_dust_outputs: number;
  /** null when the source did not say whether the transaction was confirmed. */
  confirmed_at_fetch: boolean | null;
  // feature schema v2 (risk intelligence); absent in rows stored by older collectors
  schema_version?: string;
  coinbase?: boolean;
  max_equal_value_outputs?: number;
  n_output_types?: number;
}

export interface LiveDetail extends LiveRow {
  features: LiveFeatures | null;
  explorer_url: string;
  flag_descriptions: Record<string, string>;
  /** Risk assessment; null when not assessed (older database, failed fetch, risk disabled). */
  risk?: RiskRecord | null;
  lifecycle?: Lifecycle | null;
}

export interface LiveStatus {
  mode: "live";
  source_url: string;
  source_is_public_mempool: boolean;
  data_origin: string;
  connection: Connection;
  collector: {
    state: string;
    updated_at: number;
    started_at: number | null;
    connected_since: number | null;
    last_message_at: number | null;
    reconnects: number;
    counters: Record<string, number | string | null>;
    settings: Record<string, string | number>;
  } | null;
  coverage: { sample_rate: number; fetch_per_minute: number; fetched_share_of_announced: number | null };
  via: string;
  last_observation_at: number | null;
  availability_24h: { window_s: number; observed_share: number | null; down_s: number; sequence_gaps: number };
  explorer_tx_url: string;
  indicators: Record<string, string>;
  notice: string;
  server_time: number;
}

export type OutageKind = "collector_down" | "disconnected" | "sequence_gap";

export interface Outage {
  id: number;
  kind: OutageKind;
  started_at: number;
  ended_at: number | null;
  detail: string | null;
}

export interface MinuteStat {
  t: number;
  received: number;
  sampled: number;
  fetched: number;
  failed: number;
  dropped: number;
  rate_limited: number;
}

export interface LiveStats {
  window_minutes: number;
  totals: Omit<MinuteStat, "t">;
  per_minute: MinuteStat[];
  n_fetched: number;
  fee_rate_quantiles: { p10: number; p50: number; p90: number; max: number } | null;
  vsize_quantiles: { p10: number; p50: number; p90: number; max: number } | null;
  fee_rate_histogram: { from: number; to: number | null; count: number }[];
  flag_counts: Record<string, number>;
  failures: Record<string, number>;
}

export async function liveGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? res.statusText);
  }
  // A 200 with an empty or truncated body must not be treated as "no data" (it would erase state).
  if (body === null || typeof body !== "object") {
    throw new ApiError(res.status, "invalid_response", "unreadable response from the live API");
  }
  return body as T;
}

export const liveApi = {
  status: () => liveGet<LiveStatus>("/v1/live/status"),
  recent: (limit = 200) =>
    liveGet<{ total: number; rows: LiveRow[] }>(`/v1/live/transactions?limit=${limit}&sort=id&outcome=all`),
  tx: (txid: string) => liveGet<LiveDetail>(`/v1/live/transactions/${txid}`),
  stats: (windowMinutes = 60) => liveGet<LiveStats>(`/v1/live/stats?window_minutes=${windowMinutes}`),
  outages: (hours = 24) => liveGet<{ outages: Outage[] }>(`/v1/live/outages?hours=${hours}&limit=50`),
};

export const OUTAGE_LABEL: Record<OutageKind, string> = {
  collector_down: "Collector not running",
  disconnected: "WebSocket disconnected",
  sequence_gap: "Missed mempool updates",
};

/** Human duration: 45s, 12m 5s, 3h 4m. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

/** Duration of an outage; open outages run until `now`. Gaps are instantaneous events. */
export function outageDuration(o: Outage, now: number): number | null {
  if (o.kind === "sequence_gap") return null;
  return (o.ended_at ?? now) - o.started_at;
}

export const MAX_FEED = 500;

/**
 * Add rows (newest first), keep at most `max`. Rows are unique by id AND by txid: an incoming row
 * replaces a held row with the same id or txid (it is the newer copy, e.g. now assessed).
 */
export function mergeFeed(feed: LiveRow[], incoming: LiveRow[], max = MAX_FEED): LiveRow[] {
  if (incoming.length === 0) return feed;
  const byTxid = new Map<string, LiveRow>();
  for (const r of incoming) {
    const prev = byTxid.get(r.txid);
    if (!prev || r.id >= prev.id) byTxid.set(r.txid, r);
  }
  const ids = new Set([...byTxid.values()].map((r) => r.id));
  const kept = feed.filter((r) => !byTxid.has(r.txid) && !ids.has(r.id));
  return [...byTxid.values(), ...kept].sort((a, b) => b.id - a.id).slice(0, max);
}

/**
 * Apply a page of the newest rows (`/v1/live/transactions?sort=id`) to the feed. `lastId` is the
 * client's newest id when the request was SENT (every row up to it was stored by then, and ids
 * never decrease), `lastIdNow` its newest id now (stream rows may have arrived meanwhile). When the
 * page's newest id is below `lastId`, the database was restored or reset: the held rows belong to
 * another database, so they are replaced and the id restarts from the page.
 */
export function applyRecentPage(
  feed: LiveRow[],
  lastId: number | null,
  page: LiveRow[],
  lastIdNow: number | null = lastId,
  max = MAX_FEED,
): { feed: LiveRow[]; lastId: number | null; reset: boolean } {
  const serverMax = maxRowId(page);
  if (lastId !== null && (serverMax === null || serverMax < lastId)) {
    return { feed: mergeFeed([], page, max), lastId: serverMax, reset: true };
  }
  const ids = [lastIdNow, serverMax].filter((v): v is number => v !== null);
  return { feed: mergeFeed(feed, page, max), lastId: ids.length ? Math.max(...ids) : null, reset: false };
}

/** Feed rows this far behind the collector's last observation (server seconds) count as not updating. */
export const FEED_LAG_S = 120;

/**
 * Seconds the browser's feed lags behind the collector, when the collector says it is connected
 * and observing but this page has received nothing newer for over `FEED_LAG_S`; otherwise null.
 * Both timestamps are server clock, so client clock skew does not matter.
 */
export function feedLag(
  status: Pick<LiveStatus, "connection" | "last_observation_at"> | null,
  feed: Pick<LiveRow, "fetched_at">[],
): number | null {
  if (!status || status.connection !== "connected" || status.last_observation_at === null) return null;
  if (feed.length === 0) return null; // initial load still pending, or the empty-feed message covers it
  const newest = Math.max(...feed.map((r) => r.fetched_at));
  const lag = status.last_observation_at - newest;
  return lag > FEED_LAG_S ? lag : null;
}

/**
 * Explorer link for a transaction, or null: never for mock / unconfirmed sources (the txid does not
 * exist on the real network) and never for a URL that is not https.
 */
export function explorerHref(url: string | null | undefined, kind: SourceKind): string | null {
  if (kind !== "real" || typeof url !== "string" || !url.startsWith("https:")) return null;
  return url;
}

/** Explorer link for another transaction (e.g. a parent), derived from this transaction's link. */
export function relatedExplorerHref(url: string | null | undefined, kind: SourceKind, txid: string, other: string): string | null {
  const base = explorerHref(url, kind);
  if (base === null || !base.includes(txid) || !/^[0-9a-f]{64}$/i.test(other)) return null;
  return base.replace(txid, other);
}

export type SortKey = "newest" | "fee_rate" | "vsize" | "n_outputs" | "fee";

export interface FeedFilter {
  minFeeRate: number | null;
  flag: string | null;
  showFailed: boolean;
  sort: SortKey;
}

export function filterSort(rows: LiveRow[], f: FeedFilter): LiveRow[] {
  const out = rows.filter(
    (r) =>
      (f.showFailed || r.outcome === "fetched") &&
      (f.minFeeRate === null || (r.fee_rate ?? -1) >= f.minFeeRate) &&
      (f.flag === null || r.flags.includes(f.flag)),
  );
  const key = f.sort;
  if (key === "newest") return out.sort((a, b) => b.id - a.id);
  return out.sort((a, b) => ((b[key] ?? -Infinity) as number) - ((a[key] ?? -Infinity) as number));
}

/** Badge text: the collector's connection to the network, as reported by the API. */
export function connectionLabel(c: Connection | null): string {
  if (c === "connected") return "Connected";
  if (c === "reconnecting") return "Reconnecting";
  if (c === "failed") return "Collector failed";
  return "Disconnected";
}

export function formatSat(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 100_000_000
    ? `${(v / 100_000_000).toFixed(8).replace(/0+$/, "").replace(/\.$/, "")} BTC`
    : `${v.toLocaleString("en-US")} sat`;
}

export function shortTxid(txid: string): string {
  return `${txid.slice(0, 10)}…${txid.slice(-6)}`;
}

export function ago(ts: number, now: number): string {
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function confirmedLabel(v: boolean | null | undefined): string {
  if (v === true) return "yes";
  if (v === false) return "no (in mempool)";
  return "unknown";
}

/** UTC "YYYY-MM-DD HH:MM:SS". */
export function utcStamp(ts: number): string {
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19);
}

// ------------------------------------------------------------------ status interpretation
/** A status response older than this (client clock) is treated as unknown, e.g. API unreachable. */
export const STATUS_STALE_S = 20;
/** Connected but no successful transaction fetch for this long: data is stale. */
export const OBSERVATION_STALE_S = 120;

export type SourceKind = "real" | "mock" | "unknown";

/** Data-origin badge. Only an explicit `source_is_public_mempool === true` is labelled real. */
export function sourceBadge(status: Pick<LiveStatus, "source_is_public_mempool"> | null): {
  kind: SourceKind;
  text: string;
} {
  if (!status || typeof status.source_is_public_mempool !== "boolean") {
    return { kind: "unknown", text: "DATA SOURCE UNKNOWN: live status not loaded" };
  }
  return status.source_is_public_mempool
    ? { kind: "real", text: "REAL BITCOIN NETWORK DATA" }
    : { kind: "mock", text: "LOCAL MOCK / CUSTOM SOURCE: not real network data" };
}

export interface ConnectionView {
  tone: "ok" | "warn" | "off";
  label: string;
  detail: string | null;
}

/**
 * What the connection badge may claim, given the last status, when it arrived (client seconds),
 * whether the latest poll failed, and the client clock. Never "Connected" on stale information.
 */
export function connectionView(
  status: LiveStatus | null,
  opts: {
    now: number;
    statusAt: number | null;
    apiError: boolean;
    /** The API answered 503 live_no_data: reachable, but no collector has written yet. */
    noData?: boolean;
    /** The tab just became visible again and a fresh status is on its way. */
    refreshing?: boolean;
  },
): ConnectionView {
  const { now, statusAt, apiError, noData = false, refreshing = false } = opts;
  if (noData) {
    return { tone: "off", label: "No live data", detail: "no live data yet (collector not started)" };
  }
  const age = statusAt === null ? Infinity : now - statusAt;
  if (refreshing && !apiError && age > STATUS_STALE_S) {
    return { tone: "off", label: "Refreshing…", detail: "refreshing…" };
  }
  if (!status || statusAt === null) {
    return { tone: "off", label: "Status unknown", detail: apiError ? "live API unreachable" : "waiting for status" };
  }
  if (apiError || age > STATUS_STALE_S) {
    return { tone: "off", label: "Status unknown", detail: `live API unreachable; last status ${ago(statusAt, now)}` };
  }
  if (status.connection === "failed" || status.collector?.state === "failed") {
    return { tone: "off", label: "Collector failed", detail: "the collector exited after an internal error; systemd restarts it (see its journal)" };
  }
  if (status.connection === "connected") {
    // Server clock "now", so collector timestamps are compared without client clock skew.
    const serverNow = (Number.isFinite(status.server_time) ? status.server_time : statusAt) + age;
    if (status.last_observation_at === null) {
      return { tone: "ok", label: "Connected", detail: "no transaction fetched yet" };
    }
    const quiet = serverNow - status.last_observation_at;
    if (quiet > OBSERVATION_STALE_S) {
      return { tone: "warn", label: "Connected, no new data", detail: `no successful observation for ${formatDuration(quiet)}` };
    }
    return { tone: "ok", label: "Connected", detail: null };
  }
  if (status.connection === "reconnecting") return { tone: "warn", label: "Reconnecting", detail: null };
  return { tone: "off", label: connectionLabel(status.connection), detail: null };
}

// ------------------------------------------------------------------ charts
export interface ThroughputPoint {
  t: number;
  label: string;
  announced: number | null;
}

/**
 * Per-minute throughput for the chart: complete minutes only (the minute containing `serverNow`
 * is still filling and would look like a drop), with minutes that have no row filled with null so
 * collection gaps show as breaks instead of being bridged by the line.
 */
export function throughputSeries(
  perMinute: Pick<MinuteStat, "t" | "received">[],
  serverNow: number,
): { points: ThroughputPoint[]; lastComplete: ThroughputPoint | null } {
  const lastMinute = Math.floor(serverNow / 60) * 60 - 60; // start of the last complete minute
  const byT = new Map<number, number>();
  for (const m of perMinute) if (m.t <= lastMinute) byT.set(m.t, m.received);
  if (byT.size === 0) return { points: [], lastComplete: null };
  const start = Math.min(...byT.keys());
  const points: ThroughputPoint[] = [];
  for (let t = start; t <= lastMinute; t += 60) {
    points.push({ t, label: new Date(t * 1000).toISOString().slice(11, 16), announced: byT.get(t) ?? null });
  }
  return { points, lastComplete: points[points.length - 1] ?? null };
}

// ------------------------------------------------------------------ event stream
export function eventsUrl(since: number | null): string {
  return since === null ? "/v1/live/events" : `/v1/live/events?since=${since}`;
}

export function maxRowId(rows: Pick<LiveRow, "id">[]): number | null {
  return rows.length ? Math.max(...rows.map((r) => r.id)) : null;
}

/**
 * Delay before re-opening an event stream the browser gave up on (HTTP error such as 503 "too many
 * streams"): exponential from `base`, capped at `max`, with "equal jitter" so clients spread out.
 */
export function reconnectDelay(attempt: number, rand: () => number = Math.random, base = 3000, max = 60_000): number {
  const ceiling = Math.min(max, base * 2 ** Math.max(0, attempt));
  return Math.round(ceiling / 2 + (rand() * ceiling) / 2);
}
