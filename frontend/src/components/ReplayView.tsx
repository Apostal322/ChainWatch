import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Info, type Policy, type Ranking, type StepMetrics, type StepSummary } from "../lib/api";
import { revealIfStacked } from "../lib/dom";
import { describeVersion, fmtHits, fmtInt, fmtPct, fmtScore } from "../lib/format";
import { LabelChip, PolicyPicker, Tile } from "./common";
import TransactionDetail from "./TransactionDetail";

const PAGE = 25;

export default function ReplayView({ info }: { info: Info }) {
  const [steps, setSteps] = useState<StepSummary[]>([]);
  const [step, setStep] = useState<number | null>(null);
  const [policy, setPolicy] = useState<Policy>("expanding");
  const [labels, setLabels] = useState("all");
  const [offset, setOffset] = useState(0);
  const [ranking, setRanking] = useState<Ranking | null>(null);
  const [metrics, setMetrics] = useState<StepMetrics | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);

  // Changing what is listed resets paging and selection in the same update, so no request is
  // ever made for the new step with the old page offset.
  const resetView = () => { setOffset(0); setSelected(null); };
  const changeStep = (s: number) => { setStep(s); resetView(); };
  const changePolicy = (p: Policy) => { setPolicy(p); resetView(); };
  const changeLabels = (l: string) => { setLabels(l); resetView(); };
  const selectRow = (id: number) => {
    setSelected(id);
    requestAnimationFrame(() => revealIfStacked(detailRef.current));
  };

  useEffect(() => {
    api.steps().then((s) => {
      setSteps(s);
      setStep(s.length ? s[s.length - 1].step : null);
    }).catch((e) => setError(e.message));
  }, []);

  const available = useMemo(() => steps.find((s) => s.step === step)?.policies ?? [], [steps, step]);
  useEffect(() => {
    if (available.length && !available.includes(policy)) {
      setPolicy(available[0]);
      setOffset(0);
      setSelected(null);
    }
  }, [available, policy]);

  // Each effect ignores responses that arrive after its inputs changed (out-of-order replies).
  useEffect(() => {
    if (step === null || !available.includes(policy)) return;
    let ignore = false;
    setError(null);
    api.ranking(step, policy, labels, offset, PAGE)
      .then((r) => { if (!ignore) setRanking(r); })
      .catch((e) => { if (!ignore) setError(e.message); });
    return () => { ignore = true; };
  }, [step, policy, labels, offset, available]);

  useEffect(() => {
    if (step === null || !available.includes(policy)) return;
    let ignore = false;
    api.metrics(step, policy)
      .then((m) => { if (!ignore) setMetrics(m); })
      .catch((e) => { if (!ignore) setError(e.message); });
    return () => { ignore = true; };
  }, [step, policy, available]);

  const b20 = metrics?.budgets["20"];
  return (
    <section>
      <div className="filters">
        <label>
          Time step (≈2 weeks each)
          <select value={step ?? ""} onChange={(e) => changeStep(Number(e.target.value))}>
            {steps.map((s) => (
              <option key={s.step} value={s.step}>
                Step {s.step} · {fmtInt(s.n_transactions)} transactions
              </option>
            ))}
          </select>
        </label>
        <label>
          Replay policy
          <PolicyPicker value={policy} available={available} onChange={changePolicy} />
        </label>
        <label>
          Show
          <select value={labels} onChange={(e) => changeLabels(e.target.value)}>
            <option value="all">All transactions</option>
            <option value="labelled">Labelled only</option>
            <option value="unknown">Unknown (unlabelled) only</option>
          </select>
        </label>
      </div>

      {metrics && (
        <div className="tiles">
          <Tile k="Model used" v={`step ${ranking?.model_trained_at_end_of_step ?? "…"}`}
                h={describeVersion(metrics.model_version)} />
          <Tile k="Transactions in step" v={fmtInt(metrics.n_transactions)}
                h={`${fmtPct(metrics.n_unknown / metrics.n_transactions, 0)} unknown labels`} />
          <Tile k="Labelled illicit (retrospective)" v={fmtInt(metrics.n_illicit)}
                h={`of ${fmtInt(metrics.n_labelled)} labelled · labels known at step ${metrics.labels_revealed_at_step}`} />
          <Tile k="Average precision (labelled only)" v={fmtScore(metrics.average_precision)}
                h={`ROC-AUC ${fmtScore(metrics.roc_auc)}`} />
          {b20 && (
            <Tile k="Illicit in top 20 reviews"
                  v={`${fmtHits(b20.illicit_found_labelled_only)} / ${b20.max_possible}`}
                  h={`ranking all transactions: ${fmtHits(b20.illicit_found_all_transactions)}`} />
          )}
        </div>
      )}

      {error && <p className="err">{error}</p>}
      <div className="split">
        <div className="card">
          <h2>Risk-ranked transactions</h2>
          <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
            Scored at the end of step {step} with a model trained only on labels available by then.
            Label column is shown for retrospective inspection; at prediction time it was unknown.
          </p>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col" className="num">Rank</th>
                  <th scope="col">Transaction</th>
                  <th scope="col">Risk score</th>
                  <th scope="col" className="num">Percentile</th>
                  <th scope="col">Label (retrospective)</th>
                </tr>
              </thead>
              <tbody>
                {ranking?.rows.map((r) => (
                  <tr key={r.tx_id} className={selected === r.tx_id ? "clickable selected" : "clickable"}
                      aria-current={selected === r.tx_id ? "true" : undefined}
                      tabIndex={0} onClick={() => selectRow(r.tx_id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          selectRow(r.tx_id);
                        }
                      }}>
                    <td className="num">{r.rank}</td>
                    <td>{r.tx_id}</td>
                    <td>
                      <span className="scorebar" style={{ width: `${Math.max(2, r.risk_score * 80)}px` }} />
                      {fmtScore(r.risk_score)}
                    </td>
                    <td className="num">{fmtPct(r.percentile)}</td>
                    <td><LabelChip status={r.label_status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {ranking && (
            <div className="pager">
              <span>{offset + 1}–{Math.min(offset + PAGE, ranking.total)} of {fmtInt(ranking.total)}</span>
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
              <button disabled={offset + PAGE >= ranking.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
            </div>
          )}
        </div>
        <div className={selected !== null ? "card detail has-selection" : "card detail"} ref={detailRef}>
          {selected === null ? (
            <p className="muted">Select a transaction to see its score, explanation and graph neighbourhood.</p>
          ) : (
            <TransactionDetail txId={selected} policy={policy} latency={info.label_latency_steps}
                               onSelect={setSelected} />
          )}
        </div>
      </div>
    </section>
  );
}
