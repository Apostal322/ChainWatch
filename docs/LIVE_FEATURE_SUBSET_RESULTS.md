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
| XGBoost | live-computable (16) | **0.646** | 0.792 | 0.028 | 0.885 | 0.639 | 0.51 |
| LogReg | all (182) | 0.231 | 0.326 | 0.036 | 0.871 | 0.405 | 0.08 |
| LogReg | augmented (17) | 0.236 | 0.425 | 0.017 | 0.785 | 0.506 | 0.08 |
| LogReg | live-computable (16) | 0.223 | 0.399 | 0.018 | 0.787 | 0.491 | 0.07 |

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
* **H2 supported (descriptive).** Every model collapses after step 42 (AP 0.017–0.043
  against a prevalence of 0.025). The restricted model is not more robust: 0.028 against
  0.043.
* Logistic regression does not differ measurably between feature sets. It is weak in every
  setting, so most of the signal is non-linear.
* Dropping `out_txs_degree`, the future information, did not lower AP (0.646 against 0.610
  with it). Its absence therefore does not account for the gap to `all`.

## Leakage and shortcut checks (XGBoost, live-computable)

* **Missing-value shortcut: none found.**
  * 324 test rows lack at least one augmented feature, and none of them is illicit
    (Phase 1 noted this pattern).
  * AP on complete rows only is 0.646, the same as on all rows.
* **Validation AP of the selected model is 0.731**, below the test AP for 35–42 (0.792).
  * Earlier working notes recorded validation APs of 0.97–0.98. Those values came from the
    `all` grid, not from this subset.
* **Feature reliance (gain share).**
  * Top features: `size` 0.26, `num_output_addresses` 0.24, `num_input_addresses` 0.11,
    `fees` 0.08.
  * Every BTC amount feature has a share below 0.05.
  * The model therefore mostly ranks transaction *shape*.
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
