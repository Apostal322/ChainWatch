import type { Info } from "../lib/api";

export default function AboutView({ info }: { info: Info }) {
  return (
    <section className="prose">
      <h2>What this application is</h2>
      <p>
        A replay of a published, historical research dataset (Elliptic++: Bitcoin transactions from
        49 time steps of roughly two weeks each). For each step it shows how a frozen model snapshot
        would have ranked that step's transactions, using only labels available at the time
        (label latency L = {info.label_latency_steps}). It is <b>not</b> real-time blockchain monitoring.
      </p>
      <h2>What a score is (and is not)</h2>
      <ul>
        <li>A ranking signal: how strongly a transaction resembles transactions labelled illicit in the
          past. Class re-weighting during training means it is not a calibrated probability.</li>
        <li>Not evidence that a transaction is criminal. Labels come from heuristics, and the labelled
          subset is not a random sample.</li>
        <li>Not valid for current or arbitrary Bitcoin transactions. 165 of the 182 features are
          anonymised and standardised by the dataset publisher and cannot be computed for new data,
          so the service has no endpoint for transaction hashes or wallet addresses.</li>
      </ul>
      <h2>Unknown labels</h2>
      <ul>
        <li>About 77% of Elliptic++ transactions are unlabelled (“unknown”). They are never treated as licit.</li>
        <li>Evaluation metrics use labelled transactions only. In a real investigation, unknown
          transactions compete for the same review budget. The Performance tab shows how much this
          lowers the number of known-illicit transactions found.</li>
      </ul>
      <h2>Data availability</h2>
      <ul>
        <li>{info.scoring_assumption}. Aggregate (neighbourhood) features and out-degree only exist
          once the step's transaction graph is complete.</li>
        <li>The dataset has no edges between time steps; the neighbourhood view shows money flows within one step.</li>
      </ul>
      <h2>Research caveats</h2>
      <ul>
        <li>Adaptive retraining substantially improved detection after the regime change at step 43 in
          the Phase 2 backtest, but steps 35–49 had been inspected before, so this result is exploratory.</li>
        <li>The benefit shrinks with slower label arrival (L = 3), and no method handles the first steps after a regime change well.</li>
        <li>Explanations are exact TreeSHAP contributions for this model, but most features are
          anonymised and have no public meaning.</li>
      </ul>
      <p className="muted">{info.disclaimer}</p>
    </section>
  );
}
