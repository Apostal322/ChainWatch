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

  const rankingKey = useRef("");
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
    // never show the previous step's rows under the new step's caption (paging within one
    // step keeps them while loading, so keyboard focus on the pager survives)
    const key = `${step}|${policy}|${labels}`;
    if (rankingKey.current !== key) setRanking(null);
    rankingKey.current = key;
    api.ranking(step, policy, labels, offset, PAGE)
      .then((r) => { if (!ignore) setRanking(r); })
      .catch((e) => { if (!ignore) { setRanking(null); setError(e.message); } });
    return () => { ignore = true; };
  }, [step, policy, labels, offset, available]);

  useEffect(() => {
    if (step === null || !available.includes(policy)) return;
    let ignore = false;
    setMetrics(null);
    api.metrics(step, policy)
      .then((m) => { if (!ignore) setMetrics(m); })
      .catch((e) => { if (!ignore) { setMetrics(null); setError(e.message); } });
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
        <div className="field">
          <span aria-hidden="true">Replay policy</span>
          <PolicyPicker value={policy} available={available} onChange={changePolicy} />
        </div>
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

      {error && <p className="err" role="alert">{error}</p>}
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
                      onClick={() => selectRow(r.tx_id)}>
                    <td className="num">{r.rank}</td>
                    <td>
                      <button type="button" className="rowbtn" aria-pressed={selected === r.tx_id}
                              aria-label={`Show details for transaction ${r.tx_id}`}
                              onClick={(e) => { e.stopPropagation(); selectRow(r.tx_id); }}>
                        {r.tx_id}
                      </button>
                    </td>
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
          {ranking && ranking.total === 0 && (
            <p className="muted">No transactions match this filter in step {step}.</p>
          )}
          {ranking && ranking.total > 0 && (
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
