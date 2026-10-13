# Inference contract and architecture audit (Phase 3, Workstream A)

This document says what the ChainWatch service can legitimately predict, from which
inputs, and under which data-availability assumptions. It was written before the serving
code, and the code enforces it.

## 1. Audit summary (verified on 2026-10-13)

| Item | Finding |
|---|---|
| Tests and lint | 225 tests passed and `ruff check` / `ruff format --check` were clean at the start of Phase 3 (re-run, not assumed). |
| Phase 1 artifacts | `chainwatch audit`: all 6 runs reproduce their saved predictions and metrics exactly. |
| Phase 2 artifacts | Backtest *predictions* (all transactions, every strategy and seed) are saved. Backtest *models* are **not**: the runner discards them after scoring. Replay therefore requires exporting snapshot models (§4). |
| Model format | `model.joblib` is an sklearn `Pipeline(preprocess → XGBClassifier)` that takes a DataFrame with exactly the 182 named feature columns in training order (`feature_names_in_`). It pickles a reference to `chainwatch.features.preprocessing.signed_log1p`. |
| **Preprocessing boundary (risk)** | In/out degrees are repaired from the edge list by `fill_degrees_from_edges` **outside** the saved pipeline. Six code paths call it separately. A serving path that skipped it would median-impute 965 rows' degrees, a silent training/serving skew. **Fix:** a single `FeatureStore` builds every model input, and a test checks that serving scores equal Phase 2 backtest scores. |
| Library versions | Artifacts were trained with scikit-learn 1.9.1 and xgboost 3.4.1 (Python 3.13). Pickled pipelines are not guaranteed portable across versions, so the model card records versions and the loader refuses a major or minor mismatch unless explicitly overridden. |
| Pickle safety | joblib/pickle executes code on load, so **a registry is trusted code** and must only come from `export-snapshots`/`demo-setup` run by the operator. The SHA-256 stored in each model card detects corruption and bundles mixed from different exports. It is **not** tamper-proofing, because whoever can replace a pickle can rewrite its card. Tamper resistance comes from deployment: the demo image builds its registry at image-build time as root and serves it read-only to a non-root user, and the real-data profile mounts the registry read-only. Version names are allow-listed and must resolve inside the registry. No request path loads a pickle, and there is no upload endpoint. (Phase 4 audit; a pickle-free format such as XGBoost JSON plus a declarative preprocessor would remove this trust requirement and is listed as future work.) |
| Experiment tracking | Local JSON/Parquet artifacts are the source of truth; MLflow is optional. The service reads committed `reports/` files for experiment comparisons and never computes new research metrics on request. |

## 2. What the features are, and why there is no live-wallet endpoint

Every model input is an **Elliptic++ feature vector**:

* 93 local and 72 aggregate features, **anonymised and standardised by the publisher**
  using statistics that are not published. Nobody, including us, can compute them for a
  new Bitcoin transaction.
* 17 augmented features (degrees, BTC amounts, fees, size, address counts).
* Aggregate features and `out_txs_degree` summarise the **complete transaction graph of
  the time step**, including transactions that appear *later* in that step (about two
  weeks). They exist only once the step has closed.

Consequences, enforced by the API:

1. **No endpoint accepts a transaction hash, wallet address or raw transaction.** The
   trained pipeline cannot produce features for them. Exposing one would imply a
   capability the model does not have.
2. Predictions are served for **historical Elliptic++ transactions** (by `tx_id`), scored
   **as of the end of their time step** (end-of-step batch scoring, as assumed in
   Phase 2).
3. A research endpoint accepts **complete Elliptic++-format feature vectors** (all 182
   features, finite numbers; augmented features may be `null` except the two degrees).
   Its response states the feature space it assumes. It exists to test
   inference consistency and batch integration, not to score real-world transactions.

## 3. Labels, unknowns and what a score means

* Raw classes: 1 = illicit, 2 = licit, 3 = **unknown** (77% of transactions). The API
  never converts unknown into licit. Every transaction-level response (rankings,
  details, neighbourhoods, predictions) carries `label_status` ∈ {`illicit`, `licit`,
  `unknown`}. Prediction and detail responses also carry `label_available_at_step` (the
  step + L at which a label would become known under the Phase 2 latency assumption),
  and ranking and metrics responses carry `labels_revealed_at_step` for the whole step.
* A **risk score** is the model's probability output. Because training used
  class re-weighting, it is a **ranking signal, not a calibrated probability** and not a
  verdict. The API returns scores, ranks and percentiles within the time step. It returns
  **no** `illicit`/`licit` classification and no alert.
* Evaluation numbers shown in the dashboard are computed **over labelled transactions
  only**. In a real investigation, unknown transactions compete for the same review
  budget. Phase 2 showed that, in steps 43–49, ranking all transactions puts mostly
  unknown transactions at the top. Labelled-only metrics therefore overstate operational
  value. The dashboard shows both views where they are available.

## 4. Model versions and historical replay

Two kinds of model bundle are served from a registry (`artifacts/registry/`):

| Bundle | Source | Use |
|---|---|---|
| `phase1-xgboost-all` | Phase 1 artifact (trained on steps 1–29) | Reference model; reproduces Phase 1 numbers. |
| `snap-expanding-L1-t{τ}` | Exported by `chainwatch export-snapshots`: XGBoost trained at the end of step τ on all labels of steps ≤ τ − 1 (label latency L = 1), with the Phase 2 settings and seed 0 | Frozen historical snapshots for replay. |

**Replay policies** (identical to Phase 2's definitions):

| Policy | Model scoring step *s* |
|---|---|
| `expanding` | the snapshot with τ = s − 1 (retrained every step) |
| `static` | the snapshot with τ = stage origin: 19 for steps 20–34, 34 for steps 35–49 |

A replayed prediction for step *s* only uses a model whose training labels were
available by the end of step *s − 1*, and only uses features of step *s*. That is the
Phase 2 causality guarantee. It is checked when each snapshot is exported and again on every replay request, and the service only routes the bundles listed in `registry.json`, each checked for the registry's label latency and data fingerprint. **Replay is a
simulation over a published historical dataset. It is not real-time blockchain
monitoring, and nothing in the service is "live".**

Consistency guarantee (tested): for every replayed transaction, the score equals the
Phase 2 backtest prediction for the same strategy, step and seed 0, within floating-point
tolerance.

## 5. Explanations

Per-transaction explanations are exact TreeSHAP contributions computed by XGBoost
(`pred_contribs`) on the model's transformed inputs, in log-odds units. The feature names
are the anonymised Elliptic names (`Local_feature_53` and so on). An explanation says
which inputs moved this model's score. It does not say *why* a transaction is criminal,
and for anonymised features it cannot be translated into business terms.

## 6. Demo mode

The dataset has no explicit redistribution licence, so no Elliptic++ file is committed
or baked into the Docker image. `chainwatch demo-data` generates a **synthetic** dataset
with the same schema. In that mode the API and dashboard show a permanent
"SYNTHETIC DEMO DATA" banner, and every metric shown is computed on that synthetic data.
