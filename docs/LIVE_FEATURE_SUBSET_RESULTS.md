# Results: live-computable feature subset (exploratory)

Protocol: [LIVE_FEATURE_SUBSET_PROTOCOL.md](LIVE_FEATURE_SUBSET_PROTOCOL.md), committed in
`6b16109` before any run. This report was written after the runs. All numbers come from
`reports/phase6/feature_subset/results.json`, produced by `scripts/feature_subset_report.py`
from the runs in `artifacts/runs*/` (git-ignored). The runs used the verified Elliptic++ CSVs,
seed 42, and the splits and grids unchanged from Phase 1.

**Status: exploratory.** Steps 35–49 were inspected in Phase 1. These results do not
validate anything for live use. **No model from this experiment is promoted** (see the
end of this report).

## Test-period results (labelled rows; unknown excluded)

| Model | Features | AP 35–49 | AP 35–42 | AP 43–49 | ROC-AUC | F1 @ val threshold | mean P@20 per step |
|---|---|---|---|---|---|---|---|
| XGBoost | all (182) | **0.797** | 0.917 | 0.043 | 0.930 | 0.800 | 0.56 |
| XGBoost | augmented (17) | 0.610 | 0.755 | 0.034 | 0.879 | 0.603 | 0.51 |
| XGBoost | live-computable (16) | **0.646** | 0.792 | 0.028 | 0.885 | 0.639 | 0.51 † |
| LogReg | all (182) | 0.231 | 0.326 | 0.036 | 0.871 | 0.405 | 0.08 |
| LogReg | augmented (17) | 0.236 | 0.425 | 0.017 | 0.785 | 0.506 | 0.08 |
| LogReg | live-computable (16) | 0.223 | 0.399 | 0.018 | 0.787 | 0.491 | 0.07 † |

† P@20 is tie-aware since v0.8 (0.5133 and 0.0727; `head(20)` gave 0.5100 and 0.0733).

Prevalence (the AP of a random ranking): 0.065 for 35–49, 0.092 for 35–42 and 0.025 for 43–49.

The re-run of the `all` setting reproduces the Phase 1 values exactly (AP 0.797, ROC-AUC 0.930,
F1 0.800).

## Paired comparison (primary)

ΔAP is subset minus `all`, computed on the same test transactions. The 95% intervals come
from a circular block bootstrap over test time steps (block length 3, 2,000 resamples,
seed 0).

| Comparison | ΔAP 35–49 | 95% interval | share of resamples with Δ ≤ 0 |
|---|---|---|---|
| XGBoost: live-computable − all | **−0.151** | [−0.216, −0.089] | 1.00 |
| XGBoost: augmented − all | −0.187 | [−0.269, −0.113] | 1.00 |
| LogReg: live-computable − all | −0.008 | [−0.045, +0.058] | 0.60 |
| LogReg: augmented − all | +0.005 | [−0.041, +0.084] | 0.43 |

## Hypotheses

* **H1 supported.** Restricted to the 16 live-computable features, XGBoost ranks clearly
  worse than with all 182 features. About 81% of the pooled AP remains
  (0.646 / 0.797), well above prevalence.
* **H2 supported (descriptive).** Every model collapses from step 43 on (AP 0.017–0.043
  against a prevalence of 0.025). The restricted model is not more robust: 0.028 against
  0.043. On 43–49 the logistic models rank *worse than chance* (ROC-AUC 0.31 augmented,
  0.34 live-computable), and the XGBoost live-computable model's ROC-AUC is 0.55 with recall
  0.0 at its validation threshold. Illicit counts fall from 239 at step 42 to 24, 24, 5, 2,
  22, 36, 56 at steps 43–49 (prevalence 0.111 to 0.018); this matches the dark-market
  shutdown Weber et al. (2019) report at step 43, which the data alone cannot confirm.
* Logistic regression does not differ measurably between feature sets. It is weak in every
  setting, so most of the signal is non-linear.
* Dropping `out_txs_degree`, the future information, did not lower AP (0.646 against 0.610
  with it). Its absence therefore does not account for the gap to `all`.

## Leakage and shortcut checks (XGBoost, live-computable)

* **The missing-value pattern does not inflate test AP.**
  * 324 test rows lack the augmented features, and none of them is illicit (Phase 1 noted
    this pattern). The model does treat them distinctly: all get one identical score
    (0.063, below the median licit score), so the pattern is learned.
  * Because those rows are all negatives, it can only lower AP: complete rows give 0.6460
    against 0.6459 for all rows.
* **Validation AP of the selected model is 0.731**, below the test AP for 35–42 (0.792).
  * Earlier working notes recorded validation APs of 0.97–0.98. Those values came from the
    `all` grid, not from this subset.
* **Feature reliance (share of total gain, trees up to the early-stopping iteration).**
  * Top features: `size` 0.46, `fees` 0.19, `num_output_addresses` 0.14, `out_BTC_min` 0.04,
    `num_input_addresses` 0.04.
  * Corrected in v0.8: the first version of this report published XGBoost's *mean* gain per
    split over all trees, including the 50 trees after early stopping that prediction does
    not use (it showed `fees` 0.08 and `num_input_addresses` 0.11).
  * Every BTC-amount feature other than `fees` has a share below 0.04.
  * The model mostly ranks transaction *shape* (`size` and the address counts: 0.64) and
    the fee.
  * Shape is exactly what changes most between 2016–2017 traffic (pre-SegWit, Elliptic's
    label era) and 2026 traffic (vsize, SegWit and Taproot, batching). That gap is a reason
    against transfer.
  * Gain describes the model's behaviour, not causes.

## What this does and does not show

* It does show that simple per-transaction quantities ranked Elliptic's 2016–2017 *illicit*
  label well above chance **in that era, in that dataset, before the step-43 shift**.
* It does **not** show that these quantities identify illicit activity in 2026 mempool
  traffic. Five reasons:
  * the labels are Elliptic's attributions;
  * `size` and `in_txs_degree` are defined differently from the live features;
  * the model collapses after the distribution shift at step 43;
  * the test period is not an untouched holdout;
  * no labelled live data exists to measure any of this.

## Decision for Layer C

**Rejected for promotion.** The models fail the supervised-layer gate (docs/RISK_ENGINE.md
§7):

* their feature schema is not `live-features/2`;
* no time-separated independent evaluation on live data exists;
* the observed collapse at step 43 is the failure mode a live deployment would face.

The live monitor's supervised layer keeps returning `NO_VALIDATED_MODEL`. What would change
this decision:

* labels attributable to live transactions with their availability time;
* a holdout recorded after the model is frozen;
* a pre-registered evaluation on that holdout.

## Amendments after the runs (v0.8 independent re-audit)

The protocol file is unchanged since its commit; these notes correct or sharpen it.

* **What "pre-registered" can mean here.** The protocol was committed (6b16109, 10:22:52 UTC)
  before the first run directory (10:23:50) and the results commit (10:31:53). That order
  is visible only in this repository's history and the local file times, not to a third
  party, and the hypotheses were written knowing the Phase 1 results. The bootstrap block
  length, resample count and seed (3, 2,000, 0) were fixed in the report script after the
  runs; the primary interval is stable for block lengths 1, 2 and 5 and other seeds
  (from [−0.21, −0.10] to [−0.20, −0.085]).
* **`in_txs_degree` is not computable from one live transaction.** It equals the number of
  parents present in the dataset within the same time step (true for all 202,804 rows; 27%
  of rows have 0 although every transaction has inputs). It does not leak future
  information (there are no cross-step edges). Dropping it costs ΔAP −0.022
  (95% [−0.075, +0.006]) in a scratch retrain.
* **`total_BTC` is resolvable:** it equals `out_BTC_total` in every row, and `fees` equals
  in-total minus out-total in every row.
* **Independent recomputation:** every number in the tables above was recomputed from the
  raw CSVs and saved predictions by an independent reviewer, and a scratch retrain
  reproduced the XGBoost live-computable run exactly (AP 0.6459, max score difference 0.0).

