import { useEffect, useState } from "react";
import { api, type Experiments, type ModelCard, type Row } from "../lib/api";
import { describeVersion, fmtInt, fmtScore } from "../lib/format";

const num = (v: string | number | null | undefined, d = 3) => (typeof v === "number" ? fmtScore(v, d) : "—");

export default function ModelsView() {
  const [models, setModels] = useState<ModelCard[]>([]);
  const [exp, setExp] = useState<Experiments | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.models().then(setModels).catch((e) => setError(e.message));
    api.experiments().then(setExp).catch((e) => setError(e.message));
  }, []);

  const p1 = (exp?.phase1_baselines ?? []) as Row[];
  const boot = ((exp?.phase2_bootstrap_confirm ?? []) as Row[]).filter((r) => r.metric === "pooled_ap");
  const costs = new Map(((exp?.phase2_costs ?? []) as Row[]).filter((r) => r.stage === "confirm")
    .map((r) => [r.strategy as string, r]));

  return (
    <section>
      {error && <p className="err">{error}</p>}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Model registry (loaded by this service)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          Each bundle's file hash is verified before loading. Snapshots reproduce the Phase 2
          expanding-window models (seed 0); the replay engine only uses a snapshot for steps after its training time.
        </p>
        <div className="scroll">
          <table>
            <thead><tr><th>Version</th><th>Description</th><th className="num">Training rows</th>
              <th className="num">Illicit</th><th>SHA-256</th></tr></thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.version}>
                  <td title={m.version}>{describeVersion(m.version)}</td>
                  <td className="muted">{m.description}</td>
                  <td className="num">{fmtInt(m.n_train)}</td>
                  <td className="num">{fmtInt(m.n_train_illicit)}</td>
                  <td><code>{m.model_sha256.slice(0, 12)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Phase 1: classical baselines (test steps 35–49, real Elliptic++)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>{exp?.source}</p>
        <div className="scroll">
          <table>
            <thead><tr><th>Model</th><th>Features</th><th className="num">AP</th><th className="num">ROC-AUC</th>
              <th className="num">F1</th><th className="num">P@100</th></tr></thead>
            <tbody>
              {p1.map((r) => (
                <tr key={`${r.model}-${r.features}`}>
                  <td>{r.model}</td><td>{r.features}</td>
                  <td className="num">{num(r["AP (PR-AUC)"])}</td><td className="num">{num(r["ROC-AUC"])}</td>
                  <td className="num">{num(r.F1)}</td><td className="num">{num(r["P@100"])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h2>Phase 2: adaptive retraining vs static (confirm 35–49, exploratory)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          Pooled AP (5-seed mean) with 95% circular block-bootstrap intervals over time steps; Δ is
          paired against the static model. Label latency 1 step.
        </p>
        <div className="scroll">
          <table>
            <thead><tr><th>Strategy</th><th className="num">Pooled AP</th><th className="num">95% CI</th>
              <th className="num">Δ vs static</th><th className="num">95% CI of Δ</th><th className="num">Fits</th>
              <th className="num">Fit seconds</th></tr></thead>
            <tbody>
              {boot.map((r) => {
                const c = costs.get(r.strategy as string);
                return (
                  <tr key={r.strategy as string}>
                    <td>{r.strategy}</td>
                    <td className="num">{num(r.estimate)}</td>
                    <td className="num">[{num(r.ci_low)}, {num(r.ci_high)}]</td>
                    <td className="num">{num(r.diff_vs_baseline)}</td>
                    <td className="num">[{num(r.diff_ci_low)}, {num(r.diff_ci_high)}]</td>
                    <td className="num">{c ? fmtInt(c.n_fits as number) : "—"}</td>
                    <td className="num">{c ? fmtScore(c.fit_seconds as number, 0) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
