# Portfolio material

Everything below describes work that exists in this repository and numbers that can be
re-checked with its commands. ChainWatch has **no users, no production deployment and no
business impact**; do not add any.

## Resume bullet points (pick 2–3)

- Built a leakage-guarded temporal ML pipeline (Python, scikit-learn, XGBoost) on 204k
  Bitcoin transactions from the Elliptic++ dataset. Per-time-step evaluation exposed a
  post-shift collapse (AP 0.92 → 0.04) that a pooled metric (AP 0.80) hid.
- Designed and pre-registered a rolling-origin backtest of 5 retraining strategies with
  explicit label latency and block-bootstrap confidence intervals. Retraining as labels
  arrive raised late-period AP from 0.05 to 0.46 (+0.41, 95% CI 0.16–0.62); reported as
  exploratory, together with negative results.
- Shipped a FastAPI and React/TypeScript replay service that reproduces research
  predictions exactly (234,596 scores, max deviation 0.0), backed by a provenance-checked
  model registry, TreeSHAP explanations, 261 automated tests, CI on Linux and Windows, and
  a hardened Docker demo (read-only, non-root, rate-limited, hash-locked dependencies).

Shorter variants:
- "Temporal-drift study and replay service for illicit Bitcoin transaction ranking
  (XGBoost, FastAPI, React, Docker); exact research-to-serving reproducibility."
- "Found and quantified a regime-change failure (AP 0.92 → 0.04) with leakage-safe
  backtesting; retraining under label delay recovered AP to 0.46 (exploratory)."

## GitHub repository description (≤ 350 characters)

> Leakage-aware ranking of illicit Bitcoin transactions under temporal distribution shift
> (Elliptic++): pre-registered retraining backtests with label latency, plus a FastAPI +
> React replay service that reproduces research scores exactly. Synthetic Docker demo.

Topics: `machine-learning`, `distribution-shift`, `concept-drift`, `temporal-validation`,
`xgboost`, `fastapi`, `react`, `typescript`, `docker`, `mlops`, `bitcoin`,
`elliptic-dataset`. (Applying these needs the owner's approval; see RELEASE_READINESS.md.)

## Internship application summary (cover letter or "projects" paragraph)

ChainWatch is my end-to-end ML engineering project on the public Elliptic++ Bitcoin
dataset. I designed a leakage-safe chronological evaluation and found that a strong
XGBoost model (AP 0.80 overall) collapsed after a regime change (AP 0.92 → 0.04). I then
pre-registered a backtest of retraining strategies under delayed labels: retraining
recovered late-period AP to 0.46, which I report as exploratory because no untouched
holdout remained. I shipped the research models as a FastAPI and React replay service whose
scores match the research predictions exactly (234,596 of them), with 262 tests, CI on
Linux and Windows, a hardened Docker demo on synthetic data, and a security review that
found and fixed 18 issues. The project taught me to treat evaluation design, label
availability and training/serving consistency as engineering problems, not afterthoughts.

## Portfolio website summary

**ChainWatch: what happens to a fraud-style model when behaviour changes?**

ChainWatch is a three-phase project on the public Elliptic++ Bitcoin dataset. First, I
built a strictly chronological evaluation that never treats unlabelled transactions as
legitimate. It showed that a strong XGBoost model, which looks good on average, collapses
after a regime change in the data. Second, I pre-registered and ran a backtest of
retraining strategies that only use labels available at prediction time. Retraining
recovered much of the lost ranking quality, but only once new labels arrived, and less
when labels were slower. Third, I turned the research models into a replay application:
a FastAPI service and React dashboard that show, for any historical period, how frozen
models would have ranked transactions, with explanations and graph neighbourhoods. The
service reproduces the research numbers exactly, and its public demo runs entirely on
synthetic data. The write-up keeps the limitations in view: there is no untouched
holdout, labels are heuristic, and the system does not score live transactions.

## LinkedIn project description

**ChainWatch: temporal robustness of illicit-transaction ranking (independent project)**

- **Problem:** models that rank transactions for review are trained on the past but used
  on the future. On Elliptic++ (204k Bitcoin transactions, 77% unlabelled) I measured how
  much that matters.
- **Method:** chronological evaluation with leakage guards; distribution-shift
  diagnostics (prior, covariate, class-conditional, within-period learnability); a
  pre-registered rolling-origin backtest with label latency, multiple seeds and a
  time-step block bootstrap.
- **Findings:** XGBoost AP fell from 0.92 to 0.04 after a regime change. Ranking quality
  itself dropped (ROC-AUC 0.97 → 0.70), not just prevalence. Retraining as labels arrived
  raised late-period AP to 0.46 (exploratory). There was no gain before the shift, and
  the gain shrank with slower labels.
- **Engineering:** FastAPI service with a versioned, provenance-checked model registry;
  exact research-to-serving reproducibility (234,596 scores); React/TypeScript dashboard;
  262 tests including leakage canaries; CI on Linux and Windows; hardened Docker demo on
  synthetic data.
- **Stack:** Python, pandas, scikit-learn, XGBoost, FastAPI, pydantic, React, TypeScript,
  Recharts, Docker, GitHub Actions.

## What to showcase

| Asset | File | Why it works |
|---|---|---|
| Per-step collapse and recovery | `reports/phase2/backtest/figures/per_step_ap_confirm.png` | One picture tells the research story: static vs retraining across the regime change. |
| Shift diagnosis | `reports/phase2/shift/figures/within_period_learnability.png` | Shows *why* in a defensible way: learnable from late labels, but the past model fails. |
| Labelled-only vs all-transaction budget | `docs/images/demo_performance.png` (middle chart; synthetic data, so describe the effect, not the numbers) | Shows the unknown-label caveat, which is a mature point for an interviewer. |
| Replay dashboard | `docs/images/demo_replay.png` (synthetic data) | Proves a working application with explanations and a graph view. |
| Uncertainty | `reports/phase2/backtest/figures/forest_delta_ap.png` | Confidence intervals rather than point estimates. |
| Architecture | Mermaid diagram in `README.md` / `docs/ARCHITECTURE.md` | Research and serving share one code path. |

Avoid: cropping out the "SYNTHETIC DEMO DATA" banner, showing demo numbers as research
results, or calling the system "real-time", "production" or "deployed". Mention a demo
URL only after `scripts/smoke_demo.sh` has passed against it.
