# Changelog

## v0.8.0: independent hardening audit (2026-10, not deployed)

Every defect below was reproduced first; each regression test fails on v0.7.0 (`f9f8765`)
and passes now. Full matrix: [docs/AUDIT.md](docs/AUDIT.md#v080-independent-hardening-audit).

- **Indicator data.**
  - Empty or mass-delisting snapshots are refused unless `--allow-mass-removal`.
  - Snapshots are diffed against active listings (reverted content re-lists).
  - The official SDN file and its mirror are one ranked authority: an official snapshot
    closes the mirror's extra listings; the fallback mirror never delists or re-lists
    official listings. One match per authority. v0.7 alerts are re-keyed, keeping their
    status.
  - Truncated XML falls back to the mirror; `--fallback` with `--file` is refused.
  - XML parser: UTF-8 only, bounded memory per record and per party, https on every
    redirect hop.
- **Screening and alerts.**
  - Retrospective screening covers the reload window and runs in the collector on reload.
  - Graph relations re-screen parents at the child's time, deduplicated, with coverage.
  - Alert keys carry authority and role; `alert_txids` makes upserts idempotent.
  - Anomaly detector `robust-tail/2`: "strong" needs two flagged measurements;
    `min_reference` 1,000. Risk engine `risk-engine/2`.
- **Collector.**
  - Overall REST deadline and 16 MB body cap; lifecycle (mined is final) remembered
    across waits; block conflicts not fetched.
  - A repeated `mempoolSequence` is a re-sync, not new traffic.
  - DB errors contained per write; disk-full rollback detected; counters after commit.
  - Feature and sequence validation; single-transaction migrations; consistent reads.
  - Configuration ranges validated (exit 78 on invalid config).
- **Edge API and dashboard proxy.**
  - The dashboard's own polling cannot be starved by anonymous cache-busting; snapped
    windows; 60 s last-good fallback; overall upstream deadline; 15 s risk summary cache
    (cold 0.48 s → 0.10 s at the cap).
  - SSE hard deadline; token format checked; `CHAINWATCH_LIVE_TOKEN_NEXT` for rotation
    without downtime; credentials redacted from status.
- **Deployment.** Caddy read/idle timeouts and GET/HEAD only; `install.sh` token and
  firewall fixes; CI job timeouts, one run per PR, shellcheck.
- **Dashboard.** No silent feed freeze; explicit unavailable states; server-provided
  anomaly baseline; superseded sources labelled; accessibility fixes.
- **Research documents.** Feature-subset gain shares recomputed over the trees prediction
  uses; tie-aware P@20; below-chance late ranking reported; amendments section. No
  published AP, interval or split changed.

## v0.7.0: risk intelligence for the live monitor (2026-10, not deployed)

- **Separate assessment layers.** Every fetched transaction is assessed by separate layers
  that are never blended into one score (`src/chainwatch/risk/`, [RISK_ENGINE.md](docs/RISK_ENGINE.md)):
  - **A. Source-backed screening**:
    - address extraction (`live-features/2`);
    - base58check / bech32 / bech32m validation;
    - versioned OFAC SDN snapshots: the official XML is streamed, and a third-party
      mirror is the fallback;
    - point-in-time matching with full provenance;
    - outcomes `SCREENING_MATCH`, `NO_KNOWN_SCREENING_MATCH`,
      `SCREENING_DATA_UNAVAILABLE`, `INSUFFICIENT_INFORMATION`.
  - **A'. Observed-graph relations** at distance 2, within this collector's observations
    only.
  - **B. Behavioural anomalies** (`robust-tail/1`):
    - empirical tail probability and robust z over 8 measurements, against the previous
      2,000 fetched transactions;
    - `INSUFFICIENT_BASELINE` below 500;
    - per-measurement explanations.
  - **C. Supervised gate**: a model card must prove live validation, so the layer returns
    `NO_VALIDATED_MODEL`.
- **Engine behaviour.**
  - Each layer runs in its own guard, so one failure does not stop the others.
  - An ordinal review priority (P1–P4) that is not a probability.
  - `insufficient_evidence` abstention.
  - About 2 ms per transaction.
- **Alerts.**
  - Grouped and deduplicated review queue; a repeat of a dismissed alert is suppressed, a
    repeat of a resolved one reopens it.
  - Retrospective alerts when a later snapshot lists an earlier observed address; the
    original record is never rewritten.
  - Statuses change through the CLI only (`chainwatch-live alerts`), with an audit log.
  - No external notifications.
- **Lifecycle.** mined / removed / replaced states are tracked for display only.
- **API.**
  - New routes: `/v1/live/risk/summary`, `/v1/live/findings`, `/v1/live/alerts`.
  - `risk` and `lifecycle` in the transaction detail; `risk_priority` and `risk_assessed`
    in feed rows.
  - Proxied by the remote backend.
- **Dashboard.** Risk overview, investigation queue, recent findings, a per-transaction
  assessment with evidence and limitations, and a priority column. Mock data is labelled
  everywhere, and the wording is checked by tests.
- **Deploy.**
  - `chainwatch-indicators.service` / `.timer`: daily refresh, `MemoryMax=400M`.
  - `chainwatch-live indicators update|status` with `--fallback` and `--file`.
- **Data feasibility and Layer C decision** ([RISK_DATA_FEASIBILITY.md](docs/RISK_DATA_FEASIBILITY.md)):
  - no labelled dataset maps to live features under usable terms, so no live model is
    trained;
  - an offline diagnostic whose protocol was committed before the runs ([results](docs/LIVE_FEATURE_SUBSET_RESULTS.md))
    restricted Elliptic++ to the 16 live-computable features;
  - XGBoost AP fell from 0.797 to 0.646 (paired Δ −0.151, 95% [−0.216, −0.089]) and
    collapses after step 42;
  - rejected for promotion.
- **Verification.**
  - 34 risk tests, 410 Python and 41 frontend tests in total.
  - The browser end-to-end test now covers TEST indicator list → matches → UI.
  - The soak test now refreshes indicators mid-run and checks that every row is
    assessed.
  - Real OFAC data parsed: official XML 516 addresses, mirror 531.
  - Audit findings: [AUDIT.md](docs/AUDIT.md#v070-risk-intelligence-audit).
- **Not done.**
  - No measurement of the anomaly layer on real traffic.
  - Nothing deployed.

## v0.6.0: always-on collector kit (2026-10, prepared, not deployed)

- **`chainwatch-live`** is a lightweight entry point with no ML imports. Commands:
  - `collect` runs the collector;
  - `serve-edge` serves a token-protected, read-only edge API on 127.0.0.1;
  - `backup` takes an online SQLite backup, runs an integrity check and rotates copies;
  - `report` prints reliability figures.
- **Recovery and gap tracking:**
  - an `outages` table records `collector_down` (clean or unclean exit, measured from the
    last heartbeat), `disconnected`, and `sequence_gap` (the mempool update sequence jumps
    within a connection);
  - "observed share" availability is computed from those outages;
  - old databases migrate automatically.
- **Storage:** retention by age as well as by row count, hourly WAL checkpoints and an
  index on fetch time. 50,000 rows measured at about 63 MB.
- **Edge API:**
  - bearer token of at least 32 characters, compared in constant time;
  - per-client rate limit, body size limit, `no-store`, security headers;
  - no docs or OpenAPI routes;
  - `/health/live` and `/health/ready`.
- **Dashboard remote mode** (`RemoteBackend`, `configs/public-demo-live.yaml`): the
  Render service proxies the edge API over HTTPS with the token, through a short shared
  cache. The Live tab shows how the data arrives, the last successful observation, the
  24-hour observed share and a table of gaps and outages.
- **Deployment kit** (`deploy/oracle/`):
  - hardened systemd units (restart on crash, start on boot, memory and CPU caps);
  - daily backup timer;
  - Caddy HTTPS template;
  - idempotent `install.sh` that installs from a hash-locked, ML-free
    `requirements/live.lock.txt`;
  - `smoke_live.sh`.
- **Tests:** 24 new Python tests in `tests/test_live_cloud.py`, plus 1 frontend test. The
  install was also tested in a fresh Ubuntu 24.04 container, including a `kill -9`
  restart; see `docs/LIVE_CLOUD.md` §11.
- **Engineering audit** (four independent reviews; table in `docs/AUDIT.md`; no P0):
  - **P1 fixed:**
    - the reconnect backoff overflowed after ~8.5 h of outage and silently stopped
      reconnecting;
    - a dead worker or heartbeat left a live-looking process that no longer collected
      (now fatal exit 1, so systemd restarts it);
    - concurrent event-stream opens bypassed the cap and could take the Render app's health
      check down;
    - the dashboard labelled mock data as real, and showed "Connected", whenever the
      status call failed;
    - feed rows were not reachable by keyboard;
    - the protocol-timing claim in the research docs is now stated as unverifiable.
  - **Remote proxy:** single flight, cached failures, LRU, upstream request budget,
    bounded reads.
  - **Edge API:** the token holder is exempt from the per-IP limit.
  - **Collector:**
    - removed and mined txids are not fetched;
    - outages start at the last message;
    - payload amounts are range-checked;
    - `Retry-After` dates are honoured; 5xx pauses fetching;
    - `restore` command.
  - **Store:** a covering index and planner hints: at 50k rows, `stats(60)` 92 → 14 ms and
    the default page 15.7 → 2.8 ms.
  - **Deploy:**
    - token file 0600 root:root;
    - host, REF and `EXPECTED_SHA` checks;
    - Caddyfile validated before replacing;
    - `SystemCallFilter`, loopback-only edge API (systemd exposure 3.0 → 1.3 / 1.1).
  - **Dashboard:**
    - honest source and connection states;
    - stale-response guards;
    - event-stream reconnect after HTTP errors;
    - WCAG AA colours;
    - no horizontal scroll at 390 px;
    - charts without a fake final-minute drop.
  - **Research:**
    - `manifest.json` records the actual raw-file hashes; it used to record the
      published constants, even for synthetic data;
    - misquoted numbers corrected against `reports/`, and the README leads with the
      primary pooled result. No result changed.
- **Verification:**
  - 33 new regression tests (`tests/test_live_faults.py`, `tests/test_live_security.py`)
    plus 13 frontend tests.
  - A real-systemd container test of `install.sh` and the units: crash restart,
    lock-contention fatal exit, backup/restore, upgrade, input validation, reboot.
  - Fault-injection soak runs with `scripts/soak_test.py`:
    - 6 min: 11 of 11 checks passed;
    - 110 min: 52 faults, 99.97 % API availability, 0 tracebacks, integrity ok; it found that
      the database can end a little above the row cap (now pruned on stop).
  - A browser end-to-end test, `scripts/e2e_live_browser.py`: 10 of 10 checks passed.
- **Not done:** no cloud resource has been created, real mempool.space ingestion is
  still unverified (the build proxy blocks it), and no 24/7 reliability is claimed.
  `render.yaml` and the public demo are unchanged.

## v0.5.0: live Bitcoin mempool monitor MVP (2026-10)

- **Collector** (`chainwatch live-collect`, `src/chainwatch/live/`): asynchronous pipeline
  from the mempool.space WebSocket (`track-mempool-txids`) through REST details to
  observation-time features and SQLite.
  - reconnection with exponential backoff and jitter, plus a staleness watchdog;
  - bounded dedupe, queue and retention; deterministic sampling;
  - token-bucket request limit that honours HTTP 429 `Retry-After`;
  - timeouts, idempotent storage, graceful shutdown and metrics.
- **API** (`/v1/live/status|transactions|transactions/{txid}|stats|events`): read-only,
  with SSE streaming. Disabled (404 `live_disabled`) unless `live.enabled`; the API never
  contacts mempool.space.
- **Dashboard:** "Live monitor" tab with connection state, throughput and fee-rate charts,
  a filterable feed, a detail panel with mempool.space links, and explicit labels for
  real, synthetic and mock data.
- **Features and indicators** respect Bitcoin semantics (change outputs, no ownership
  inference). Indicators are rule-based, not predictions. No Elliptic++ model is applied
  to live data.
- **Tests:** 44 new Python tests (local WebSocket and REST mocks), 3 frontend helper tests;
  `scripts/live_load_test.py`; `scripts/mock_mempool.py`.
- **Not verified:** real mempool.space ingestion. The build environment's proxy refused
  every connection (HTTP 403).
- The public demo image and `render.yaml` (0.4.1) are unchanged. No research result
  changed.

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
- **Phase 2 rolling-origin backtest (protocol fixed before the runs, author's record).** Five retraining strategies with
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
