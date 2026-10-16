# Protocol: how much illicit-ranking signal survives with live-computable features?

**Status: written and committed before any run of this experiment** (this file's first commit
precedes the result files under `reports/phase6/`; both are in the published history).

## Why

ChainWatch's live monitor applies no model (docs/RISK_DATA_FEASIBILITY.md). The feasibility
assessment found no labelled data whose features a live collector can compute, with one
partial exception: 16 of Elliptic++'s 17 "augmented" transaction features are plain
quantities a collector *can* compute from one mempool transaction and its prevouts. This
experiment measures, on historical data, how much of the Phase 1 ranking performance remains
when a model may only use those quantities. It is a **diagnostic**, not a path to a live
model.

## Question and hypotheses

* **Q.** On Elliptic++, what is the test-period average precision (AP) of models restricted to
  live-computable features, relative to the same models with all 182 features?
* **H1.** XGBoost on the 16 live-computable features has lower pooled test AP (steps 35–49)
  than XGBoost on all 182 features (Phase 1: 0.797).
* **H2.** Both collapse after step 42; the restricted model is not expected to be more robust.
  Reported for 35–42 and 43–49 separately, descriptively.

## Data and features

* Elliptic++ transactions (`txs_features.csv`, `txs_classes.csv`, `txs_edgelist.csv`),
  downloaded by `chainwatch download` and verified against the published SHA-256. Labelled
  rows only (illicit vs licit); unknown is excluded, never treated as negative.
* Feature sets:
  * `all`: 93 local + 72 aggregate + 17 augmented (the Phase 1 setting, re-run here).
  * `augmented`: the 17 augmented features.
  * `live_computable`: the augmented features **minus `out_txs_degree`**, which counts later
    transactions spending this one's outputs and is unknown at observation time.
* Known mismatches with the live collector, accepted and stated: `in_txs_degree` counts
  in-dataset parents within the time step (live: all parents); BTC amounts and `size` come
  from 2016–2017 transactions (pre-SegWit majority; live uses vsize, 2026 traffic); the
  publisher's `total_BTC` definition is not documented in detail.

## Method (unchanged from Phase 1)

* Chronological split: train 1–29, validation 30–34, test 35–49 (configs/default.yaml).
* Models and grids: logistic regression and XGBoost with the Phase 1 grids, selection and
  early stopping on validation only, threshold by max F1 on validation.
* Preprocessing: log1p of augmented features, fitted on training rows only.
* Seed 42. Configs: `configs/subset-augmented.yaml`, `configs/subset-live.yaml`.

## Metrics (chosen now)

* **Primary:** pooled test AP (35–49), XGBoost, `live_computable` vs `all`.
* Secondary: test AP for 35–42 and 43–49; ROC-AUC; illicit F1 at the validation threshold;
  per-step precision@20; the same for logistic regression and for `augmented`.
* Uncertainty: block bootstrap over test time steps (the repository's existing utilities),
  paired between feature sets.

## Caveats fixed in advance

* **Not an untouched holdout.** Steps 35–49 were inspected in Phase 1, so every result is
  exploratory.
* **No live validity.** Whatever the result, no model from this experiment is promoted to the
  live monitor. It fails the Layer C gate by construction (`feature_schema` is not the live
  schema, labels are 2016–2017 Elliptic attributions, and no live evaluation exists).
* A good restricted-feature score would mean only that simple per-transaction quantities
  carried signal **for Elliptic's label definition in that era**, not that they identify
  illicit activity today.

## Commands

```bash
chainwatch download                                   # verified Elliptic++ CSVs
chainwatch --config configs/subset-live.yaml prepare
chainwatch --config configs/subset-live.yaml train --model xgboost
chainwatch --config configs/subset-live.yaml evaluate <run_dir>
# ... the same for logreg, configs/subset-augmented.yaml and configs/default.yaml
python scripts/feature_subset_report.py               # comparison table + bootstrap CIs
```
