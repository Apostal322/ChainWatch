# Evaluation protocol

This protocol was fixed before any model was trained. Changes to it are listed under
"Protocol log" at the end of this document.

## 1. Chronological split

| split | time steps | labelled txs | illicit | illicit rate |
|---|---|---|---|---|
| train | 1 to 29 | 26,381 | 2,871 | 10.9% |
| validation | 30 to 34 | 3,513 | 591 | 16.8% |
| test | 35 to 49 | 16,670 | 1,083 | 6.5% |

* **Why not a random split?** A deployed detector scores the future using the past.
  A random split mixes transactions from the same two-week window, and often the same
  illicit campaign, into both training and test. That inflates scores and hides drift.
* **Why steps 35 to 49 for test?** This is the held-out range used by Weber et al. (2019)
  and by the Elliptic++ authors, which makes rough comparison possible.
* **Why validation 30 to 34?** It is the latest data before the test period, so model
  selection reflects the most recent conditions. Five steps contain 591 illicit
  examples, which is enough to estimate AP and choose a threshold with reasonable
  stability.
* **Graph isolation.** No edges cross time steps (verified on every run), so no
  transaction in one split has a neighbour in another.
* `Time step` is **not** a model input. Under a chronological split, every test value is
  unseen, so the feature could only act as an extrapolating proxy for the base rate.

## 2. What each split is used for

| | train | validation | test |
|---|---|---|---|
| fit preprocessing (median imputer, scaler) | yes | no | no |
| fit model weights | yes | no | no |
| XGBoost early stopping | no | yes | no |
| hyper-parameter selection (by AP) | no | yes | no |
| decision threshold (max F1) | no | yes | no |
| reported metrics | no | yes (optimistic, since it is used for selection) | **yes, once** |

`chainwatch train` uses no test labels for fitting, selection or thresholding. It is not
blind to the test period, though: it loads the full dataset, and the run metadata
records each split's row and illicit counts (so the test prevalence), and the published
EDA reports per-step prevalence for the test steps. `chainwatch evaluate`
reads test data with the split and threshold **frozen in the artifact**, not the current
config. It refuses to overwrite an existing test evaluation unless `--force` is given,
so repeated test-set looks are an explicit, logged action.

## 3. Class imbalance

* Loss re-weighting computed from training labels only: `class_weight="balanced"` for
  LR and `scale_pos_weight = n_neg / n_pos` (about 8.2) for XGBoost. No resampling and
  no synthetic data.
* Re-weighting makes predicted probabilities uncalibrated. That is why the threshold is
  chosen on validation rather than fixed at 0.5. Calibration is planned future work.
* Metrics are chosen to be informative under imbalance (see below). Accuracy is not
  reported.

## 4. Metrics

| metric | what it answers |
|---|---|
| **Average precision (PR-AUC)**, primary | ranking quality concentrated on the positive class; its random baseline equals prevalence (0.065 on test) |
| ROC-AUC | ranking quality across both classes; can look good under heavy imbalance even when the top of the ranking is poor |
| Precision / Recall / F1 at the validation-chosen threshold | the operating point a fixed alerting rule would produce |
| Confusion matrix | absolute error counts |
| **Precision@K / Recall@K** (K = 50, 100, 200, 500 over the whole test period) | quality of a fixed investigation budget |
| Mean per-step Precision@20 | quality of a budget of 20 reviews per two-week window, which is closer to how an analyst team works |
| Per-time-step AP / P / R / F1 | exposes drift that pooled metrics hide |
| Inference latency | in-process `predict_proba` time (single-row p50/p95, batch µs per transaction) for preprocessing + model on the local CPU |

**Ties in @K metrics.** Some models produce many identical top scores (saturated
probabilities). P@K and R@K are computed as the *expected* value under random ordering
within the tied group at the cut-off, so they do not depend on row order. The number of
items tied at the top score is reported as `n_tied_at_top_score`.

## 5. Interpreting outputs

A score is a **ranking signal for investigation priority**. It is not evidence that a
transaction is criminal. Labels are heuristic, the labelled subset is not representative,
and a high score means "resembles transactions labelled illicit in the training period".

## 6. Known limitations of this protocol

* **One test period with strong drift.** Pooled test metrics are dominated by steps 35
  to 42. Per-step results show performance collapsing after step 42. Report both.
* **Few positives late in the test period.** Steps 45 and 46 contain 5 and 2 illicit
  transactions, so their per-step metrics have very high variance.
* **Validation metrics are optimistic** because the same data is used for early stopping,
  model selection and threshold choice.
* **Single seed.** Results are deterministic for a given seed and environment, but
  seed-to-seed variance has not been estimated yet (planned).
* **No uncertainty intervals yet.** Bootstrap confidence intervals over transactions or
  time steps are planned.
* Latency excludes feature retrieval, network and serialisation. Use it to compare
  models, not as a service-level number.

## Protocol log

* **Logistic-regression C grid widened** from max 10 to max 1000 after validation AP
  increased monotonically up to the grid boundary. This decision used validation data
  only, before any test evaluation.
* **@K tie handling.** The first test evaluation used stable-sort tie-breaking for P@K
  and R@K. Logistic regression then had 110 to 349 test items tied at the maximum score,
  which made its @K values depend on row order. The metric definition was changed to
  the tie-aware expectation, and all six runs were re-scored with `evaluate --force`.
  Models, thresholds and every non-@K metric were unchanged (both evaluations are
  visible in MLflow). XGBoost @K values were unaffected (no ties).

## Phase 2 extension

The rolling-origin backtest protocol (explicit label latency, dev/confirm stages, block
bootstrap over time steps, robustness rule) is in
[PHASE2_PROTOCOL.md](PHASE2_PROTOCOL.md). Its results are in
[TEMPORAL_ROBUSTNESS.md](TEMPORAL_ROBUSTNESS.md). Because Phase 1 inspected steps 35–49,
all later analyses of that period are labelled exploratory.
