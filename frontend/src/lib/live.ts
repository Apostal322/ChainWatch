// Live Bitcoin monitor: REAL network observations (mempool.space), separate from the
// historical / synthetic replay. Types mirror /v1/live/* (src/chainwatch/serving/live_api.py).
import { ApiError } from "./api";

export type Connection = "connected" | "reconnecting" | "disconnected";

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
}

export interface LiveFeatures {
  version: number;
  locktime: number;
  locktime_kind: string;
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
  confirmed_at_fetch: boolean;
}

export interface LiveDetail extends LiveRow {
  features: LiveFeatures | null;
  explorer_url: string;
  flag_descriptions: Record<string, string>;
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
  explorer_tx_url: string;
  indicators: Record<string, string>;
  notice: string;
  server_time: number;
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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? res.statusText);
  }
  return body as T;
}

export const liveApi = {
  status: () => get<LiveStatus>("/v1/live/status"),
  recent: (limit = 200) =>
    get<{ total: number; rows: LiveRow[] }>(`/v1/live/transactions?limit=${limit}&sort=id&outcome=all`),
  tx: (txid: string) => get<LiveDetail>(`/v1/live/transactions/${txid}`),
  stats: (windowMinutes = 60) => get<LiveStats>(`/v1/live/stats?window_minutes=${windowMinutes}`),
};

export const MAX_FEED = 500;

/** Add new rows (newest first), drop duplicates by id, keep at most `max`. */
export function mergeFeed(feed: LiveRow[], incoming: LiveRow[], max = MAX_FEED): LiveRow[] {
  const seen = new Set(feed.map((r) => r.id));
  const fresh = incoming.filter((r) => !seen.has(r.id)).sort((a, b) => b.id - a.id);
  return [...fresh, ...feed].slice(0, max);
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
  return `${Math.floor(s / 3600)}h ago`;
}
