// Typed client for the ChainWatch API. All data shown in the dashboard comes from here.

export type Policy = "expanding" | "static" | "phase1";
export type LabelStatus = "illicit" | "licit" | "unknown";

export interface Info {
  service_version: string;
  git_commit: string | null;
  data_mode: string;
  label_latency_steps: number;
  replay_steps: [number, number];
  policies: Record<string, unknown>;
  scoring_assumption: string;
  disclaimer: string;
  n_models: number;
}

export interface StepSummary {
  step: number;
  n_transactions: number;
  n_unknown: number;
  policies: Policy[];
}

export interface RankingRow {
  tx_id: number;
  risk_score: number;
  rank: number;
  percentile: number;
  label_status: LabelStatus;
}

export interface Ranking {
  step: number;
  policy: Policy;
  model_version: string;
  model_trained_at_end_of_step: number;
  total: number;
  offset: number;
  labels_revealed_at_step: number;
  rows: RankingRow[];
}

export interface Budget {
  illicit_found_labelled_only: number;
  illicit_found_all_transactions: number;
  max_possible: number;
}

export interface StepMetrics {
  step: number;
  policy: Policy;
  model_version: string;
  n_transactions: number;
  n_labelled: number;
  n_unknown: number;
  n_illicit: number;
  labels_revealed_at_step: number;
  average_precision: number | null;
  roc_auc: number | null;
  budgets: Record<string, Budget>;
}

export interface Contribution {
  feature: string;
  contribution: number;
  value: number | null;
  transformed_value: number;
}

export interface TxDetail {
  tx_id: number;
  time_step: number;
  policy: Policy;
  model_version: string;
  model_trained_at_end_of_step: number;
  risk_score: number;
  rank_in_step: number;
  n_in_step: number;
  percentile_in_step: number;
  label_status: LabelStatus;
  label_available_at_step: number | null;
  augmented_features_missing_in_source: boolean;
  augmented_features: Record<string, number | null>;
  explanation: {
    method: string;
    base_value: number;
    logit: number;
    top_contributions: Contribution[];
    caveat: string;
  };
}

export interface NbNode {
  tx_id: number;
  hop: number;
  risk_score: number;
  rank: number;
  label_status: LabelStatus;
}

export interface Neighbourhood {
  center: number;
  step: number;
  nodes: NbNode[];
  edges: { source: number; target: number }[];
  truncated: boolean;
  note: string;
}

export interface ModelCard {
  version: string;
  kind: string;
  description: string;
  trained_at_end_of_step: number | null;
  label_latency: number | null;
  train_steps: [number, number] | null;
  n_train: number | null;
  n_train_illicit: number | null;
  model_sha256: string;
  created_utc: string;
}

export type Row = Record<string, string | number | null>;

export interface Experiments {
  source: string;
  phase1_baselines?: Row[];
  phase2_per_step?: Record<"dev" | "confirm", (Row & { strategy: string; step: number })[]>;
  phase2_bootstrap_confirm?: Row[];
  phase2_bootstrap_dev?: Row[];
  phase2_costs?: Row[];
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
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

export const api = {
  info: () => get<Info>("/v1/info"),
  steps: () => get<StepSummary[]>("/v1/replay/steps"),
  ranking: (step: number, policy: Policy, labels: string, offset: number, limit = 25) =>
    get<Ranking>(
      `/v1/replay/steps/${step}/ranking?policy=${policy}&labels=${labels}&offset=${offset}&limit=${limit}`,
    ),
  metrics: (step: number, policy: Policy) =>
    get<StepMetrics>(`/v1/replay/steps/${step}/metrics?policy=${policy}`),
  timeline: (policy: Policy) => get<StepMetrics[]>(`/v1/replay/timeline?policy=${policy}`),
  tx: (id: number, policy: Policy) => get<TxDetail>(`/v1/transactions/${id}?policy=${policy}`),
  neighbourhood: (id: number, policy: Policy, hops: number) =>
    get<Neighbourhood>(`/v1/transactions/${id}/neighbourhood?policy=${policy}&hops=${hops}&max_nodes=80`),
  models: () => get<ModelCard[]>("/v1/models"),
  experiments: () => get<Experiments>("/v1/experiments"),
};
