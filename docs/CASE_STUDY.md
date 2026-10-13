# Case study: illicit-transaction ranking under temporal shift

*ChainWatch, a three-phase project: baselines → temporal robustness → production-style
replay service.*

## 1. Problem

Bitcoin compliance teams cannot investigate every transaction. They need a **ranking**
that puts likely-illicit activity first within a limited review budget. Elliptic++
(Elmougy & Liu, KDD 2023) provides 203,769 Bitcoin transactions over 49 time steps (about
two weeks each), a money-flow graph, and labels for only 23% of transactions (4,545
illicit, 42,019 licit; 157,205 **unknown**).

Two properties make the naive approach misleading:

1. **Time.** Illicit behaviour changes. A random train/test split lets a model see the
   future.
2. **Missing labels.** Unknown is not licit, and metrics computed on labelled rows ignore
   the unlabelled transactions that compete for the same review budget.

## 2. Hypotheses

* **H1 (Phase 1):** a well-tuned gradient-boosted tree baseline is strong under a
  leakage-safe chronological split.
* **H2 (Phase 2):** the performance collapse after time step 42 is a distribution-shift
  problem that adaptive retraining can mitigate, given realistic label latency.
* **H3 (Phase 3):** the research models can be served reproducibly, meaning the served
  scores equal the research scores, without claiming capabilities the features cannot
  support.

## 3. Methods

| Phase | Method highlights |
|---|---|
| 1 | Schema validation; join on `txId` (the label file is separate); unknown class excluded from supervision; chronological split 1–29 / 30–34 / 35–49; train-only preprocessing inside the artifact; LR and XGBoost; AP, ROC-AUC, tie-aware P@K/R@K, per-step metrics; one-time test evaluation. |
| 2 | Audit of Phase 1 (all metrics reproduced exactly). Pre-registered protocol committed before any experiment. Explicit **label latency** L (labels of step u known at end of u + L). Time-causal (no look-ahead) retraining schedules (static, expanding, periodic, sliding, recency-weighted) with per-fit assertions and leakage canaries. Dev backtest for selection, confirm backtest frozen. Circular block bootstrap over time steps. Shift diagnostics: prior, covariate, class-conditional, within-period learnability, calibration. |
| 3 | Inference contract; versioned registry with hash, version and data-fingerprint checks; snapshots exported through the backtest code path; replay engine with no-look-ahead policies; FastAPI service and React dashboard; Docker and CI; latency benchmark. |

## 4. Results (real Elliptic++)

### Phase 1: baselines (test steps 35–49)

| model | AP | ROC-AUC | F1 |
|---|---|---|---|
| Logistic regression (all features) | 0.231 | 0.871 | 0.405 |
| **XGBoost (all features)** | **0.797** | **0.930** | **0.800** |

Pooled AP hides a collapse: XGBoost reaches **0.917 on steps 35–42 and 0.043 on steps
43–49** (prevalence 0.025).

### Phase 2: why it fails and what helps (confirm steps 35–49 are exploratory, because they were inspected in Phase 1)

* **Not just fewer illicit transactions.** ROC-AUC, which does not depend on prevalence,
  drops from 0.97 to 0.70. A model fitted *within* the late period reaches AP 0.81 on it
  (a diagnostic, not deployable, because it trains on other late steps, including later
  ones), while the past-only model gets 0.05. Late illicit transactions are predictable
  from these features once same-period labels exist, but the boundary learned from the
  past does not transfer. This is consistent with concept or class-conditional shift; the
  data cannot identify its cause.
* **Retraining as labels arrive** (L = 1) raises pooled AP on 43–49 from **0.050 to
  0.458** (expanding window; +0.41 [95% CI 0.16, 0.62]) and to 0.493 with recency
  weighting. Over all of 35–49 the gain is +0.085 [0.024, 0.229]. The gain on AP is
  robust under the pre-registered rule; the gain on budget hits is not, because 35–42 is
  at its ceiling.
* **Limits.** At L = 3 the gain shrinks to +0.05–0.07. The first steps after the regime
  change stay poor for every method. When unlabelled transactions compete for review
  slots, late-period detections at small budgets fall to about 0.

### Phase 3: serving (verified)

* **Reproducibility.** `chainwatch verify-replay` re-scored **234,596** transactions
  (labelled and unknown, 60 step/policy pairs) through the service. The maximum absolute
  difference from the saved Phase 2 backtest predictions is **0.0**. The Phase 1
  reference model reproduces its reported test AP of 0.7969 exactly.
* **Latency** (Intel Xeon @ 2.8 GHz, 4 logical CPUs, loopback HTTP, one uvicorn worker;
  `reports/phase3/benchmark_ellipticpp.json`): batch of 822 IDs, p50 38 ms;
  single ID, p50 16 ms; transaction detail with exact TreeSHAP, p50 15 ms; cached step
  ranking, p50 3 ms. Model-only scoring is about 15 µs per transaction in batches.
  Service start-up takes 2.1 s; resident memory is about 660 MB with 31 models and
  203,769 transactions.

## 5. Engineering decisions (details in [ARCHITECTURE.md](ARCHITECTURE.md))

* **No live-wallet endpoint.** The features are anonymised and standardised by the
  publisher and need the complete end-of-step graph, so the honest product is a
  *historical replay*, and the API enforces that.
* **One input path.** A training-time repair (degrees recomputed from the edge list)
  lived outside the pickled model. The `FeatureStore` makes it impossible to skip at
  serving time.
* **Trust boundaries on artifacts.** Pickles are treated as trusted code. The demo
  registry is built at image-build time and served read-only by a non-root user; the
  SHA-256 per bundle is an integrity check (not tamper-proofing); library versions are
  pinned and checked; and every routed bundle is bound to the registry's data fingerprint
  and label latency.
* **Honest outputs.** Scores, ranks and percentiles only; `label_status` always present,
  with unknowns explicit; labelled-only versus all-transaction budget views side by side;
  research results served read-only from committed reports, never recomputed or
  embellished.
* **Demo without the dataset.** A schema-identical synthetic generator drives tests, CI
  and the public demo, with a permanent "SYNTHETIC DEMO DATA" banner.

## 6. Limitations

* There is no untouched holdout left on Elliptic++: steps 35–49 were inspected in
  Phase 1, so the Phase 2 adaptive-retraining result is exploratory.
* The label latency is assumed (L = 1 primary). Real investigation delays are unknown
  and probably longer.
* Labels are heuristic, and the labelled subset is not random. Detection numbers concern
  *labelled* illicit transactions.
* Explanations are exact for the model but refer to anonymised features.
* The model is **not validated for current or arbitrary Bitcoin transactions**.

## 7. What I would do next

1. **Label-efficiency research.** The value of adaptation depends on how quickly labels
   arrive. Prioritised or active labelling of top-ranked *unknown* transactions could
   shorten the effective latency. This needs a simulation protocol that does not assume
   the unknowns' labels.
2. **Cold start after regime change.** Test whether multi-hop graph representations
   transfer across regimes better than tabular features. First run a cheap control
   (XGBoost plus two-hop aggregates), then GraphSAGE inside the same backtest,
   pre-registered with steps 43–45 as the target. A temporal GNN is not motivated, because
   the graph has no edges between time steps.
3. **Fresh data** for an untouched holdout, for example a later release or a different
   chain, before making any further performance claims.
