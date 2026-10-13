# Phase 2 protocol: temporal robustness and adaptive retraining

**Status: written and committed before any Phase 2 experiment was run.** The git history
of this file is the record of what was decided in advance. Any later change is listed
under "Deviations" at the end, with its reason.

## 0. Audit of Phase 1 (summary)

The audit was reproduced by `chainwatch audit` and written to `reports/phase2/audit.json`.

* Raw files still match the published SHA-256 hashes.
* Re-predicting from the six saved models reproduces the saved test scores exactly, and
  plain scikit-learn reproduces the reported AP, ROC-AUC and F1 exactly.
* Split boundaries are correct (train 1–29, val 30–34, test 35–49, labelled only). Each
  stored threshold equals the max-F1 threshold recomputed from the saved validation
  predictions.
* XGBoost/all AP is 0.917 on steps 35–42 and 0.043 on steps 43–49 (prevalence 0.025).
  These figures are confirmed.

Methodological weaknesses that Phase 2 must address or document:

| # | Weakness | Phase 2 response |
|---|---|---|
| W1 | The Phase 1 "static" model used labels from steps 1–29 only. Steps 30–34 were never folded back, which understates what a static deployment would know. | The static baseline is retrained at the backtest origin using **all labels available at that time**. |
| W2 | Labels were implicitly available immediately. | An explicit label-latency model (Section 2). |
| W3 | Aggregate features and `out_txs_degree` use the whole time step's graph, including transactions that appear later in that step. | A stated deployment assumption: **scoring happens in a batch at the end of each time step**. Real-time scoring would need different features (out of scope). |
| W4 | One validation window was used for early stopping, model selection and thresholding. | Fixed tree count (no early stopping). Strategy parameters are selected on a separate development backtest. Evaluation uses threshold-free and budget metrics. |
| W5 | Single seed and no uncertainty estimates. | 5 training seeds and a moving-block bootstrap over time steps. |
| W6 | Test steps 35–49 have been inspected. | Every result on 35–49 is labelled **exploratory confirmation**, never an untouched holdout. |
| W7 | Pooled AP conflates prior shift with ranking quality. | Also report ROC-AUC and prevalence-standardised AP. |
| W8 | @K budgets were computed among labelled transactions only. | Also report a conservative variant that ranks **all** transactions, with unlabelled ones counted as non-detections. |

## 1. Prediction-time assumptions

* **Features.** All features of transactions in step *s*, including neighbourhood
  aggregates within *s*, become available at the end of step *s*. That is when step *s*
  is scored. There are no cross-step edges, so no future structure enters.
* **Publisher standardisation.** Local and aggregate features were standardised by the
  publisher using all 49 steps. For XGBoost this leaks nothing: standardisation is a
  strictly increasing per-feature map, and histogram tree splits are invariant to such
  maps. Logistic regression is not used in Phase 2.
* **Labels.** The label of a transaction in step *u* becomes available at the end of
  step *u + L*. *L* is the label latency in steps (one step is about two weeks).
  **Primary analysis: L = 1. Sensitivity: L = 3.** True investigation latency is
  unknown, because Elliptic labels were assigned retrospectively. L = 1 is optimistic.
* **Retraining.** A model retrained at the end of step τ uses labels from steps
  u ≤ τ − L. It scores steps τ+1 onwards until the next retrain. The engine asserts this
  for every fit.

## 2. Strategies

Every strategy uses identical XGBoost settings: the Phase 1 selected hyper-parameters
(max_depth 6, learning_rate 0.05, subsample 0.8, colsample_bytree 0.8,
min_child_weight 1), a fixed 500 trees, the `all` feature set, the same train-only
preprocessing, and `scale_pos_weight` computed from the (weighted) training labels.

| family | definition | strategy grid (selected on dev) |
|---|---|---|
| static | trained once at the origin, on all available labels | none |
| expanding | retrained every step on all available labels | none |
| periodic | expanding window, retrained every *f* steps | f ∈ {2, 4, 8} |
| sliding | retrained every step on the most recent *W* available labelled steps | W ∈ {3, 5, 10} |
| recency | retrained every step on all available labels, sample weight 0.5^(age/h), where age = steps since the newest available labelled step | h ∈ {2, 5, 10} |

Static and expanding have no strategy parameter. Each other family gets the same
budget of 3 candidates.

## 3. Backtests

| stage | origin T0 | scored steps | role |
|---|---|---|---|
| **dev** | 19 | 20–34 (1,951 illicit labelled) | strategy-parameter selection. Never touches 35–49. |
| **confirm** | 34 | 35–49 (1,083 illicit labelled) | comparison of the frozen selections. **Exploratory**, because this period was inspected in Phase 1. |

Caveat: the XGBoost hyper-parameters were chosen in Phase 1 on steps 30–34, which lie
inside the dev period. This optimism applies equally to every strategy.

Seeds: dev uses {0, 1, 2}. Confirm uses {0, 1, 2, 3, 4}. Seeds change XGBoost row and
column subsampling only. They measure training stochasticity, not data uncertainty.

## 4. Metrics (evaluated on labelled transactions of the scored steps)

**Primary**, fixed in advance:

1. **Pooled AP** over all scored steps.
2. **Illicit detections with a budget of K = 50 reviews per step**, summed over the
   scored steps. This uses tie-aware expected hits among labelled transactions.

**Secondary:** ROC-AUC; prevalence-standardised AP (negatives re-weighted so the
prevalence is 0.109, the Phase 1 training prevalence); mean per-step AP; P@K and R@K
per step for K ∈ {10, 20, 50, 100}; detections when ranking **all** transactions with
unknowns as non-detections (a conservative lower bound); per-step detection rate;
sub-period results (35–42 versus 43–49); fit time and number of fits.

**Selection rule (dev):** within each family, choose the parameter with the highest
seed-averaged pooled AP on dev. Ties within 0.002 go to the cheaper option (fewer fits,
then the larger window or longer half-life). The selection is written to
`selection.json`, and the confirm stage refuses to run without one.

## 5. Uncertainty

* The **unit of resampling is the time step**. Transactions in a step form one connected
  transaction graph and share a market regime, so rows are not independent and a row
  bootstrap would understate uncertainty.
* **Moving-block bootstrap over scored steps**, block length 3, 2,000 resamples. This
  keeps short-range autocorrelation between neighbouring steps. Block length 1 (a plain
  step-cluster bootstrap) is reported as a sensitivity check.
* **Paired comparisons:** each replicate resamples the same steps for both strategies.
  The metric is computed per seed and averaged over seeds within the replicate. We report
  the difference (adaptive − static) with a 95% percentile interval, plus the seed
  standard deviation separately.
* With only 15 scored steps and a regime change inside the confirm period, intervals
  will be wide. A difference is called **robust** only if its 95% interval excludes 0 in
  both the dev and confirm backtests and the sign holds in both sub-periods of confirm.

## 6. Distribution-shift diagnostics

Computed by `chainwatch shift`, using the Phase 1 static model and the data:

* **Prior shift:** labelled prevalence per step, and the labelled fraction per step.
* **Covariate shift, P(x):** per step, a domain classifier separating training-period
  transactions (steps 1–29, labelled and unlabelled) from step *s* (cross-validated AUC);
  per-feature-group median Kolmogorov–Smirnov statistics; missingness by step.
* **Class-conditional shift, P(x|y):** domain classifiers per class (training illicit
  versus period illicit, and the same for licit) for periods 30–34, 35–42 and 43–49.
* **Signs of concept shift, P(y|x):** within-period learnability. Leave-one-step-out
  models are trained *inside* a period and compared with the static model on the same
  steps. If a within-period model ranks well while the past model does not, the
  boundary learned in the past no longer applies (concept shift and/or class-conditional
  shift into regions that training never covered). These two cannot be fully separated
  without counterfactual labels, and no causal claim is made.
* **Scores and calibration:** per-step score quantiles by class. A Platt calibrator
  fitted on validation predictions (steps 30–34) is used to report calibration-in-the-large
  (mean predicted versus observed prevalence), Brier score and ECE per step.
* **Ranking and budgets:** per-step AP, ROC-AUC, precision and recall at the Phase 1
  threshold, and P@K/R@K.

## Deviations

All deviations are listed here. None of them changed a pre-registered metric, split,
strategy grid, selection rule or seed set.

1. **Time-proxy features (not pre-registered; exploratory).** The first shift-diagnostic
   run produced a domain-classifier AUC of 1.000 for *every* step, including steps inside
   the training pool. The cause is five aggregate features that behave like a clock:
   |Spearman ρ| with the time step ≥ 0.9 on steps 1–34 (Aggregate_feature_8, 10, 43, 7,
   46; ρ = 0.95–0.997). Two consequences:
   * The shift diagnostics now report domain AUC both with all features and with these
     features removed. The all-feature version is saturated and uninformative.
   * An **exploratory ablation backtest** (static plus the four selected strategies, same
     seeds, dev and confirm, time proxies removed) was added. It was decided after the
     main dev, confirm and L = 3 backtests had finished. The dev summary had been seen at
     that point; the confirm results had not. The ablation never changes the frozen
     selection, and its results are reported as exploratory.
2. **Bootstrap variant.** "Moving-block" is implemented as the *circular* block bootstrap
   (Politis & Romano, 1992). It is a moving-block variant that does not under-sample the
   first and last steps. Block length and resample count are as pre-registered.
3. **Implementation-only changes:** a vectorised, exact pooled-AP computation for
   bootstrap replicates (tested against explicit concatenation), and a configurable
   confirm sub-period boundary (`confirm_split_after: 42`, the pre-registered 35–42 /
   43–49 split).
4. **Operational:** the first shift-diagnostic run was killed by a 2-hour tool time limit
   and was re-run in full.
