# Interview guide

Every number here comes from a committed report or a re-runnable command. The source is
given in brackets. If a number is not listed here, do not quote it.

## 60-second explanation

"ChainWatch asks how well a fraud-style ranking model holds up when the world changes. I
used Elliptic++, a public dataset of about 204,000 Bitcoin transactions over 49 two-week
time steps, where only 23% are labelled. With a strictly chronological evaluation,
XGBoost looked strong overall (AP 0.80), but per-time-step reporting showed it collapsing
after step 42: AP fell from 0.92 to 0.04, and ROC-AUC fell too, so the ranking itself
broke, not just the base rate. I then pre-registered a backtest of retraining strategies
with an explicit delay before labels arrive. Retraining as labels arrived lifted the
late-period AP to about 0.46. I report that as exploratory, because I had already seen
that period. Finally I built a FastAPI and React replay service that serves the frozen
research models and reproduces their scores exactly, with a hardened synthetic-data demo
in Docker and CI on Linux and Windows."

## 3-minute technical walkthrough

1. **Data and leakage (Phase 1).** The labels sit in a separate file, so I join on
   `txId`, never by row position. Class 3 means *unknown*, not licit, so it is excluded
   from training and evaluation. The split is chronological (train 1–29, validate 30–34,
   test 35–49). There are no edges between time steps (verified), so graph neighbours
   cannot leak across splits. Imputation and scaling are fitted on train only, inside the
   saved sklearn pipeline. The test set is read by a separate command that refuses to run
   twice. [docs/EVALUATION.md]
2. **Baselines.** Logistic regression got AP 0.23 and XGBoost 0.80 on test. Per-step
   metrics exposed the collapse after step 42. [reports/baseline_results.md]
3. **Diagnosis (Phase 2).** I separated prior shift from ranking failure: ROC-AUC
   0.97 → 0.70, and AP re-weighted to a fixed prevalence 0.92 → 0.17. A model fitted only
   on other late steps reaches AP 0.81 on the late period, while the past-only model gets
   0.05. So late illicit transactions are predictable from these features, but the old
   boundary does not transfer. This is a diagnostic, not a deployable model.
   [docs/TEMPORAL_ROBUSTNESS.md §3]
4. **Backtest.** Explicit label latency (the label of step *u* becomes usable at *u + L*).
   Five strategies: static, expanding, periodic, sliding window, recency-weighted. Strategy
   parameters are selected on steps 20–34, then frozen and confirmed on 35–49. Block
   bootstrap over time steps; 3 to 5 seeds. [docs/PHASE2_PROTOCOL.md]
5. **Result.** Late-period AP 0.050 → 0.458 (+0.41, 95% CI 0.16–0.62). The gain shrinks
   at L = 3, and the first steps after the shift stay poor for every method.
6. **Service (Phase 3).** Frozen snapshots are re-exported through the exact backtest code
   path; a single `FeatureStore` builds every model input; the replay engine scores step
   *s* with the snapshot from *s−1*. `verify-replay` re-scores 234,596 transactions and
   matches the research predictions with a maximum absolute difference of 0.0.
7. **Hardening (Phase 4 and release).** An independent audit and a release review found
   18 issues: a high-severity body-size DoS, registry path traversal, a temporal-validity
   flag bug, supply-chain gaps, and a rate-limit bypass through spoofed `X-Forwarded-For`
   headers. I fixed them, with a regression test or an in-container check for each.

## Core questions

### What was the main research question?
Can temporal adaptation (and, later, graph representations) improve illicit-transaction
detection under distribution shift and limited investigation budgets? Phases 1–2 answer
the temporal-adaptation part. No graph neural network was built: the graph enters only
through the dataset's one-hop aggregate features.

### Why XGBoost?
- **Measured, not assumed:** on the same split it beat logistic regression by 3.2–3.6×
  in AP. [docs/RESULTS.md]
- **Fits the data:** tabular, heavy-tailed, interaction-rich features. Trees are
  invariant to monotone per-feature transforms, so the publisher's global standardisation
  (which used future time steps) leaks nothing into the tree splits. Logistic regression
  extrapolated badly on extreme late-period values: its scores saturated and 110–349 test
  items tied at the top.
- **Practical:** fast (about 8 s per retrain on about 37k rows), so retraining every step
  is affordable, and exact TreeSHAP comes from the model itself.
- **Limitation:** I did not tune XGBoost heavily. The Phase 1 hyper-parameters were
  chosen on steps 30–34, which also sit inside the Phase 2 dev window. That optimism
  applies equally to every strategy.

### How was temporal leakage prevented?
- Chronological splits, with checks run on every split: disjoint time ranges, no unknown
  labels, no shared IDs, no edges crossing a split boundary.
- `Time step` is not a feature. Five aggregate features turned out to behave like a
  clock (|ρ| ≥ 0.95 with time); an ablation showed removing them changes AP by at most
  0.004 overall.
- Preprocessing is fitted on training rows only and saved inside the artifact.
- In the backtest, a model retrained at τ only sees labels from steps ≤ τ − L. This is
  asserted for every fit, and **canary tests** corrupt the labels or features that should
  be unavailable and check that every score is bit-for-bit unchanged.
- The protocol was committed to git before any Phase 2 experiment ran. Strategy
  selection happens on dev only; the confirm run refuses to start without a frozen
  selection.
- **Residual issues (state them):** the publisher standardised features using all time
  steps (irrelevant for trees, a mild leak for linear models), and steps 35–49 were
  inspected in Phase 1, so no untouched holdout remains.

### Why did performance collapse after the shift?
What the data shows:
- **Prior shift:** the illicit share of labelled transactions fell from about 0.09 to
  0.025.
- **More than prior shift:** ROC-AUC (prevalence-invariant) fell to 0.70, and late
  illicit transactions get a median score of 0.002 from the old model.
- **Learnable from later labels:** a model trained on late labels ranks the late period
  well (AP 0.81).
- **Calibration:** degrades (ECE 0.010 → 0.047).
- **Not visible in the marginals:** bulk feature distributions (median KS) do not jump at
  step 43.

Interpretation: this is consistent with concept or class-conditional shift into a region
the training data never covered. **The cause is not identifiable** from this data. Weber et
al. attribute a regime change in the original Elliptic data to a dark-market shutdown,
but I can only say my results are consistent with that, not confirm it.

### What did adaptive retraining actually improve?
- **Ranking in the late period (labelled transactions):** pooled AP 0.050 → 0.458
  (expanding) and 0.493 (recency h = 10). Labelled illicit found at 50 reviews per step
  rose from 22.6 to about 120 of 163 possible.
- **Little elsewhere:** on steps 35–42 the gain is +0.008 to +0.016 AP, because the
  static model is already near its ceiling.
- **Mechanism:** recovery follows label arrival. At steps 43–44 the gain comes only from
  labels of steps 34–41 that the static model never saw (AP about 0.05 → about 0.25).
  Steps 47–49 recover to 0.49–0.99 once post-shift labels arrive.
- **What it did not fix:** steps 43–45 stay poor for every method; the gain shrinks at
  L = 3 (+0.05 to +0.07, some CIs include 0); and in the budget metric the pre-registered
  robustness rule fails (a ceiling effect in 35–42).

### Why are unknown labels a problem?
- **Volume and bias:** 77% of transactions are unlabelled, and the labelled subset is not
  a random sample.
- **Unknown is not negative:** treating it as licit would inject label noise of unknown
  size and direction, so it is excluded from training and labelled-only metrics are
  reported.
- **Unknowns compete in practice:** a real analyst ranks *all* transactions. In steps
  43–49 the top of the full ranking is almost entirely unknown, and known-illicit hits at
  K = 20 drop to about 0. Whether those unknowns are illicit cannot be determined from
  the data, so labelled-only metrics overstate operational value. The dashboard shows
  both views.

### How does inference work?
1. Read-only registry: 30 expanding-window snapshots plus the Phase 1 reference. Each
   card records the training window, label latency, library versions, feature contract,
   data fingerprint and SHA-256.
2. At start-up the service checks the fingerprint, latency, versions and hash of every
   routed bundle.
3. A request for step *s* goes to the snapshot trained at the end of *s−1* (no look-ahead,
   asserted).
4. Features come only from the `FeatureStore`, which applies the training-time degree
   repair.
5. Scores and ranks are cached per step and policy. Explanations use XGBoost
   `pred_contribs` (path-dependent TreeSHAP; additivity is tested).
6. Latency on real data: a batch of 822 IDs takes p50 38 ms. There is no endpoint for live
   transactions, because 165 of 182 features are anonymised and standardised by the
   publisher and cannot be computed for new data.

### How was it tested?
- **Python (262 tests):** schema and join, split and leakage guards, label-latency
  canaries, schedule causality (parametrised over strategies, origins and latencies),
  metrics against hand-computed values (including tie-aware P@K), block bootstrap,
  registry integrity (tampering, version mismatch, path traversal, per-card provenance),
  API contract (validation envelopes, 413/429/405, rate limiter, security headers, SPA
  routing), and replay equal to backtest on synthetic data.
- **Real-data checks (commands):** `chainwatch audit` (Phase 1 metrics recomputed with
  plain sklearn) and `chainwatch verify-replay` (234,596 scores, max |diff| 0.0, also run
  inside the Docker image).
- **Frontend:** strict TypeScript, unit tests, production build.
- **CI:** Ubuntu and Windows × Python 3.11 and 3.13, frontend checks, and a Docker build
  plus smoke test of the public-demo configuration.

### Important limitations and trade-offs
- **No untouched holdout:** the Phase 2 gains are exploratory, and the label latency is
  assumed.
- **Labels:** heuristic, and labelled-only metrics.
- **Pickles:** they are trusted code. I mitigated this with read-only, build-time
  registries, but a pickle-free format would be better.
- **Scale:** one process with in-memory state is simple and fast at this size (about
  660 MB on real data), but it scales vertically only.
- **Explanations:** exact for the model, but over anonymised features.

## Questions a senior ML engineer might ask

**"Your confirm result reuses a period you had already looked at. Why should I believe it?"**
The selection was frozen on dev (steps 20–34) and committed before confirm ran. The
improvement also appears on dev (+0.16 AP, CI excludes 0). The mechanism is consistent:
the gain tracks label arrival. But you are right that it is exploratory; the honest fix
is fresh data.

**"Why AP and not accuracy or F1?"**
Prevalence is 2.5–17%, and accuracy is dominated by the licit class. AP and
precision/recall at K match a review-budget workflow. I also report prevalence-standardised
AP and ROC-AUC, to separate ranking quality from base-rate changes.

**"Why a block bootstrap over time steps?"**
Transactions within a step share one graph and one regime, and neighbouring steps are
correlated, so a row-level bootstrap would understate uncertainty. The circular block
bootstrap (block length 3) keeps short-range dependence; block length 1 is the
sensitivity check.

**"How much of the late-period gain is just more training data?"**
Part of it. At steps 43–44 the gain comes only from labels of steps 34–41 that the static
model lacks. Recency weighting was not reliably better than plain expanding, so the
evidence supports "use recent labels", not "down-weight old ones".

**"Would you deploy this?"**
Not as is. Real deployment needs features computable for new transactions, a measured
label-latency process, calibration and thresholds tied to analyst capacity, and drift
monitoring. The marginal-feature drift detectors I tried would *not* have flagged step 43.

**"Why no GNN?"**
There are no edges between time steps, so message passing cannot carry information
through time. A within-period tabular model already reaches AP 0.81 once labels exist. The
bottleneck is stale labels, not graph capacity. A GNN is worth testing only as a
pre-registered comparison against a cheaper multi-hop-feature baseline on steps 43–45.

**"What was the most important bug you found?"**
Two candidates. A training-time feature repair ran outside the saved pipeline, which
would have caused silent training/serving skew; the fix was the `FeatureStore` plus a
test. And, from the Phase 4 audit, request bodies were parsed before any size check, so
one large request could stall the event loop; the fix was ASGI body limits and schema
caps.

**"How variable are the results across seeds?"**
Seed standard deviation of pooled AP is ≤ 0.003 overall and ≤ 0.026 on steps 43–49. That
is much smaller than the time-step uncertainty, which is why the CIs come from resampling
time steps.
