# Changelog

## v0.4.1: deployable on Render Free (2026-10)

- **Fix:** the first Render deployment failed after 2 h (exit 143), because Render's
  Docker build trained the 31 synthetic snapshot models (600–920 s each on its build
  hardware). GitHub Actions now builds, gates and publishes the synthetic-demo image to
  GHCR (`publish-demo-image.yml`), and `render.yaml` pulls it (`runtime: image`, free
  plan).
- **Release gate:** `scripts/check_demo_image.sh`. The image must contain synthetic data
  and models only, start without any training, pass the end-to-end smoke test at
  512 MB / 0.1 CPU and stay below 450 MiB.
- **Safety:** the demo image sets `CHAINWATCH_PREBUILT_ONLY=1`, so the container refuses
  to train at start-up if its registry is missing (new tests in `tests/test_entrypoint.py`).
- No research result, model or metric changed.

## v0.4.0: first public release (2026-10)

ChainWatch is a leakage-aware study of how an illicit-transaction ranking model behaves
under temporal distribution shift on the public Elliptic++ Bitcoin dataset, plus a replay
service that serves the frozen research models.

### Research (unchanged since it was produced; reported with its limitations)
- **Phase 1, chronological baselines.** XGBoost pooled test AP 0.797 (logistic regression
  0.231). Per-step evaluation shows a collapse after step 42: AP 0.917 on steps 35–42 vs
  0.043 on 43–49.
- **Phase 2, pre-registered rolling-origin backtest.** Five retraining strategies with
  explicit label latency, several seeds and a block bootstrap over time steps. Retraining
  as labels arrive raises AP on steps 43–49 from 0.050 to 0.458 (+0.41, 95% CI
  0.16–0.62). This is **exploratory**, because that period had been inspected in Phase 1.
  Shift diagnostics are reported, along with the negative results.

### Engineering
- FastAPI replay service with a provenance-checked model registry, no-look-ahead replay
  policies, exact TreeSHAP explanations and a typed API contract.
- `verify-replay`: 234,596 replayed scores equal the research predictions (maximum
  difference 0.0), both natively and in Docker.
- React and TypeScript dashboard: replay, performance, models and experiments, and
  limitations.
- Synthetic demo: read-only, non-root container; request-size limits; per-client rate
  limiting that resists spoofed `X-Forwarded-For` headers; security headers.
- Supply chain: hash-locked serving dependencies, digest-pinned base images, SHA-pinned
  GitHub Actions with read-only token; `pip-audit` and `npm audit` clean at release.
- CI on Ubuntu and Windows (Python 3.11 and 3.13), frontend checks, and Docker
  build plus end-to-end smoke test (`scripts/smoke_demo.sh`).
- Render Blueprint (free plan) and a Hugging Face Space configuration; see
  `docs/DEPLOYMENT.md`.
- MIT License for the project code. The Elliptic++ dataset is not covered and is not
  redistributed.

### Known limitations
No untouched holdout remains; 77% of transactions are unlabelled and labels are heuristic;
the model cannot score new transactions (its features are anonymised and need each step's
full graph); model artifacts are pickles and must be self-built; the demo has no
authentication by design.
