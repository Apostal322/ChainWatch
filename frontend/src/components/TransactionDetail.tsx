import { useEffect, useState } from "react";
import { api, type Neighbourhood, type Policy, type TxDetail } from "../lib/api";
import { describeVersion, fmtPct, fmtScore } from "../lib/format";
import { LabelChip } from "./common";
import ExplanationChart from "./ExplanationChart";
import NeighbourhoodGraph from "./NeighbourhoodGraph";

export default function TransactionDetail(props: {
  txId: number;
  policy: Policy;
  latency: number;
  onSelect: (id: number) => void;
}) {
  const [d, setD] = useState<TxDetail | null>(null);
  const [nb, setNb] = useState<Neighbourhood | null>(null);
  const [hops, setHops] = useState(1);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    api.tx(props.txId, props.policy).then(setD).catch((e) => setError(e.message));
  }, [props.txId, props.policy]);
  useEffect(() => {
    api.neighbourhood(props.txId, props.policy, hops).then(setNb).catch((e) => setError(e.message));
  }, [props.txId, props.policy, hops]);

  if (error) return <p className="err">{error}</p>;
  if (!d) return <p className="muted">Loading…</p>;
  return (
    <div>
      <h2>Transaction {d.tx_id}</h2>
      <dl className="kv">
        <dt>Time step</dt><dd>{d.time_step}</dd>
        <dt>Risk score</dt><dd>{fmtScore(d.risk_score, 4)} (rank {d.rank_in_step} of {d.n_in_step}, top {fmtPct(d.rank_in_step / d.n_in_step)})</dd>
        <dt>Model</dt><dd>{describeVersion(d.model_version)}</dd>
        <dt>Label</dt>
        <dd>
          <LabelChip status={d.label_status} />{" "}
          {d.label_available_at_step !== null
            ? <span className="muted">not known when scored; available at end of step {d.label_available_at_step} under L={props.latency}</span>
            : <span className="muted">never labelled in the dataset</span>}
        </dd>
        {d.augmented_features_missing_in_source && (
          <><dt>Data quality</dt><dd>Augmented features missing in source; degrees recomputed from edges, others median-imputed (training statistics).</dd></>
        )}
      </dl>

      <h3>What moved this score</h3>
      <ExplanationChart items={d.explanation.top_contributions} />
      <p className="muted" style={{ fontSize: 12 }}>{d.explanation.method}. {d.explanation.caveat}</p>

      <h3>
        Graph neighbourhood (same time step){" "}
        <span className="seg" style={{ marginLeft: 8 }}>
          {[1, 2].map((h) => (
            <button key={h} aria-pressed={hops === h} onClick={() => setHops(h)}>{h} hop{h > 1 ? "s" : ""}</button>
          ))}
        </span>
      </h3>
      {nb ? <NeighbourhoodGraph data={nb} onSelect={props.onSelect} /> : <p className="muted">Loading…</p>}

      <h3>Augmented (non-anonymised) features</h3>
      <div className="scroll">
        <table>
          <tbody>
            {Object.entries(d.augmented_features).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="num">{v === null ? "missing" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 6 })}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
