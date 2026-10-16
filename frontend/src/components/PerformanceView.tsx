import { useEffect, useMemo, useState } from "react";
import { api, type Experiments, type Info, type Policy, type StepMetrics } from "../lib/api";
import { fmtHits, fmtScore } from "../lib/format";
import { StepLineChart, type Series } from "./charts";
import { POLICY_COLOR, POLICY_LABEL } from "./common";

const POLICIES: Policy[] = ["expanding", "static", "phase1"];

export default function PerformanceView({ info }: { info: Info }) {
  const [timelines, setTimelines] = useState<Partial<Record<Policy, StepMetrics[]>>>({});
  const [exp, setExp] = useState<Experiments | null>(null);
  const [budgetPolicy, setBudgetPolicy] = useState<Policy>("expanding");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const available = POLICIES.filter((p) => p in info.policies);
    Promise.all(available.map((p) => api.timeline(p).then((t) => [p, t] as const)))
      .then((pairs) => setTimelines(Object.fromEntries(pairs)))
      .catch((e) => setError(e.message));
    api.experiments().then(setExp).catch((e) => setError(e.message));
  }, [info]);

  const apData = useMemo(() => {
    const byStep = new Map<number, Record<string, number | null>>();
    for (const [p, tl] of Object.entries(timelines)) {
      for (const m of tl ?? []) {
        const row = byStep.get(m.step) ?? { step: m.step };
        row[p] = m.average_precision;
        byStep.set(m.step, row);
      }
    }
    return [...byStep.values()].sort((a, b) => (a.step as number) - (b.step as number));
  }, [timelines]);

  // A step without a top-20 budget (e.g. too few transactions) becomes a gap, not a crash.
  const budgetData = (timelines[budgetPolicy] ?? []).map((m) => {
    const b = m.budgets?.["20"] as (typeof m.budgets)[string] | undefined;
    return {
      step: m.step,
      labelled: b?.illicit_found_labelled_only ?? null,
      all: b?.illicit_found_all_transactions ?? null,
      max: b?.max_possible ?? null,
    };
  });

  const research = useMemo(() => {
    const rows = exp?.phase2_per_step?.confirm ?? [];
    const keep = ["static", "expanding", "recency_half_life10"];
    const byStep = new Map<number, Record<string, number | null>>();
    for (const r of rows) {
      if (!keep.includes(r.strategy)) continue;
      const row = byStep.get(r.step) ?? { step: r.step };
      row[r.strategy] = r.ap as number | null;
      byStep.set(r.step, row);
    }
    return [...byStep.values()].sort((a, b) => (a.step as number) - (b.step as number));
  }, [exp]);

  const latePooled = (strategy: string): number | null => {
    const rows = (exp as (Experiments & { phase2_bootstrap_confirm_late?: Record<string, unknown>[] }) | null)
      ?.phase2_bootstrap_confirm_late ?? [];
    const r = rows.find((x) => x.strategy === strategy && x.metric === "pooled_ap");
    return typeof r?.estimate === "number" ? r.estimate : null;
  };

  const apSeries: Series[] = POLICIES.filter((p) => p in timelines).map((p) => ({
    key: p, label: POLICY_LABEL[p], color: POLICY_COLOR[p], dashed: p === "static" }));

  return (
    <section>
      {error && <p className="err">{error}</p>}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Replay: average precision per step (labelled transactions only)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          Computed live by this service from the frozen snapshots on the {info.data_mode === "synthetic" ? "synthetic demo" : "loaded Elliptic++"} data.
          Each step uses labels that became known only later (retrospective evaluation).
        </p>
        <StepLineChart data={apData} series={apSeries} yLabel="AP" yDomain={[0, 1]} />
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Investigation budget: illicit transactions in the top 20 reviews per step</h2>
        <div className="filters" style={{ marginBottom: 4 }}>
          <label>Policy
            <select value={budgetPolicy} onChange={(e) => setBudgetPolicy(e.target.value as Policy)}>
              {POLICIES.filter((p) => p in timelines).map((p) => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
            </select>
          </label>
        </div>
        <StepLineChart data={budgetData} fmt={fmtHits} yLabel="illicit found" series={[
          { key: "max", label: "maximum possible", color: "var(--axis)", dashed: true },
          { key: "labelled", label: "ranking labelled transactions only", color: "var(--s1)" },
          { key: "all", label: "ranking all transactions (unknown = miss)", color: "var(--s2)" },
        ]} footer={<p className="muted" style={{ fontSize: 12 }}>
          The gap between the two lines is the operational cost hidden by labelled-only evaluation:
          in practice unknown (unlabelled) transactions compete for the same review slots, and whether
          they are illicit cannot be determined from this dataset.</p>} />
      </div>

      <div className="card">
        <h2>Research result (Phase 2, real Elliptic++, exploratory confirm period 35–49)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          Seed-mean AP per step from the committed Phase 2 backtest (5 seeds, label latency 1).
          Steps 35–49 were inspected in Phase 1, so this is exploratory, not an untouched holdout.
          Pooled 43–49 AP: static {fmtScore(latePooled("static"))} vs retrain-every-step{" "}
          {fmtScore(latePooled("expanding"))} (see docs/TEMPORAL_ROBUSTNESS.md).
        </p>
        {research.length ? (
          <StepLineChart data={research} yLabel="AP" yDomain={[0, 1]} series={[
            { key: "static", label: "static (frozen at step 34)", color: "var(--s2)", dashed: true },
            { key: "expanding", label: "retrain every step", color: "var(--s1)" },
            { key: "recency_half_life10", label: "recency-weighted (h=10)", color: "var(--s3)" },
          ]} />
        ) : <p className="muted">Phase 2 report files not found.</p>}
      </div>
    </section>
  );
}
