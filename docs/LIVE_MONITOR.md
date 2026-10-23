# Live Bitcoin monitor (Phase 5 MVP)

A zero-cost pipeline that observes **real** Bitcoin mempool transactions through the public
mempool.space API, extracts observation-time features, stores a bounded window in SQLite
and streams it to a "Live monitor" tab. It is a data-engineering foundation for future
live-data ML. **It does not detect fraud and applies no model.**

## 1. Operating modes

| Mode | Data | Enabled by | Where |
|---|---|---|---|
| Historical research | Elliptic++ (real, 2019-era, anonymised features) | `configs/default.yaml` + your local download | replay tabs |
| Synthetic demo | random data in the Elliptic++ schema | `configs/demo.yaml`, `configs/public-demo.yaml` | replay tabs, public Render demo |
| Live Bitcoin monitor | real mempool observations from mempool.space | `configs/live.yaml` only (`live.enabled: true`) | "Live monitor" tab, `/v1/live/*` |

* Live mode is **off** in every config except `configs/live.yaml`, including the public
  demo. When it is off, every `/v1/live/*` route answers 404 `live_disabled`, and the tab
  explains how to run it locally (tested in `tests/test_live_api.py`).
* The API process **never** contacts mempool.space. It only opens the collector's SQLite
  file read-only (`mode=ro`). Collection is a separate process that you start explicitly.
* Labelling:
  * replay tabs keep the synthetic banner;
  * the live tab shows **REAL BITCOIN NETWORK DATA** only when the collector's source host is
    `mempool.space`;
  * any other source (for example `scripts/mock_mempool.py`) is shown as **LOCAL MOCK /
    CUSTOM SOURCE: not real network data**.
* No Elliptic++ model is applied to live data. Those models use 165 anonymised features
  that cannot be computed for new transactions (`docs/INFERENCE_CONTRACT.md`).

## 2. Data source: verified contract

mempool.space (and docs.mempool.space) is blocked from the environment where this was
built, so the contract was checked in **mempool's open-source backend** (`mempool/mempool`,
commit `4cf96d2`, 2026-10-13). The files used were:
* `backend/src/api/websocket-handler.ts`;
* `backend/src/mempool.interfaces.ts`;
* `backend/src/api/bitcoin/esplora-api.interface.ts`;
* the API docs data in `frontend/src/app/docs/api-docs/api-docs-data.ts`;
* the sample `nginx.conf`.

| Item | Contract |
|---|---|
| WebSocket | `wss://mempool.space/api/v1/ws` |
| Subscribe | `{"track-mempool-txids": true}` (documented as the low-bandwidth substitute for `track-mempool`) |
| Event | `{"mempool-txids": {"sequence": int, "added": [txid], "removed": [txid], "mined": [txid], "replaced": [{"replaced": txid, "by": txid}]}}`, sent on mempool updates to subscribed clients; other keys may share a frame |
| Keepalive | the server answers `{"action": "ping"}` with `pong`. The collector relies on WebSocket protocol pings (20 s) plus a 90 s staleness watchdog |
| Server-side drops | the server terminates clients that send oversized messages or read too slowly (`MAX_BUFFERED_AMOUNT`) |
| Details | `GET https://mempool.space/api/tx/{txid}` (Esplora schema): `txid, version, locktime, size, weight, fee, vin[{txid, vout, prevout{value, scriptpubkey_type}, sequence, witness}], vout[{value, scriptpubkey_type, scriptpubkey_address?}], status{confirmed, block_height?, block_time?}` |
| Rate limits | not published for the public instance. The sample `nginx.conf` in the repository limits the API zone to **200 requests/min per IP** and returns **429** above it. The collector defaults to **30 requests/min** and pauses on 429 for `Retry-After` (60 s if the header is absent) |

Unsupported assumptions avoided:
* there is no historical backfill;
* full transaction streams (`track-mempool`) are not used;
* the collector does not assume every announced transaction can be fetched. A 404
  (evicted, replaced or not yet indexed) is recorded as a failed observation.

## 3. Pipeline

```
mempool.space WS ─► parse + validate txids ─► dedupe (LRU 200k) ─► sample (hash < p)
   (reconnect: exp. backoff + full jitter,            │
    90 s staleness watchdog, pings)                   ▼
                                    bounded queue (200; drop newest when full, skip > 300 s old)
                                                      │
                     N=2 workers ─► token bucket (30/min, burst 5; 429 ⇒ pause) ─► GET /api/tx
                                                      │ (10 s timeout)
                       features + rule-based flags ─► SQLite (WAL, INSERT OR IGNORE, 20k rows)
                                                      │
                    FastAPI (read-only) ─► /v1/live/* + SSE ─► React "Live monitor" tab
```

| Concern | Mechanism (`src/chainwatch/live/collector.py`) |
|---|---|
| Reconnection | exponential backoff with full jitter (1 s base, 60 s cap), reset after a working stream |
| Health | `connected` / `reconnecting` / `disconnected` state, last message time, sequence, reconnect count; heartbeat to SQLite every 5 s. The API reports `disconnected` when the heartbeat is older than 15 s |
| Graceful shutdown | SIGINT/SIGTERM set a stop event; tasks are cancelled, the final state `stopped` and the counters are flushed, and the HTTP client is closed |
| Timeouts | 15 s WebSocket handshake, 10 s per REST request |
| HTTP 429 | `rate_limited` counter, token bucket paused for `Retry-After` (capped at 600 s), the observation is stored as failed and not retried (no amplification) |
| Backpressure | bounded `asyncio.Queue`; when it is full, new txids are counted as `dropped_backpressure` |
| Sampling | deterministic SHA-256 hash of the txid below `sample_rate` (default 5%): reproducible and independent of fee or arrival order |
| Idempotency | dedupe LRU plus `store.has(txid)` before fetching plus `UNIQUE(txid)` with `INSERT OR IGNORE` (safe across restarts) |
| Memory | bounded dedupe set, queue, fee-rate window (500), at most 25 stored outputs and input references per transaction; SQLite retention of 20k rows and 24 h of per-minute counters |
| Metrics | received, duplicates, already stored, invalid txids, malformed frames, not sampled, queued, dropped, expired, gone before fetch, fetched, failed, not found, rate limited, timeouts, malformed payloads, upstream (5xx) errors, internal errors, removed, mined, replacement pairs, connects, reconnects, stale reconnects, sequence gaps |
| Counter semantics | From mempool's backend source: `removed` lists every txid that left the mempool in one poll (replacements, conflicts, evictions, expiry) but **not** mined ones; `mined` arrives with each block; each `replaced` pair's old txid is also in `removed`, and one old txid can appear in several pairs (A→B, then A→C and B→C). So `removed` and `replacement_pairs` overlap and are not separate outcomes: equal counts in a short window without a block simply mean every removal was a replacement. Removed or mined txids still queued are not fetched (`gone_before_fetch`) |

## 4. Features (`src/chainwatch/live/features.py`)

Only information available when the details are fetched is used, and nothing is
back-filled after that moment:
* `confirmed_at_fetch` is the status at fetch time;
* `first_seen` is the time this collector first received the txid (not the network's
  first-seen time).

| Group | Fields |
|---|---|
| Identity and timing | `txid`, `first_seen`, `fetched_at`, `confirmed_at_fetch` |
| Size and fee | `size`, `weight`, `vsize` (= ⌈weight/4⌉, BIP141), `fee`, `fee_rate` (sat/vB) |
| Structure | `version`, `locktime`, `locktime_kind` (none/height/time), `n_inputs`, `n_outputs`, `n_parent_txs`, `segwit`, `input_types`, `output_types` |
| Values | `output_values` (first 25), `max_output_value`, `output_value_total` (**includes change; not a payment amount**), `input_value_total` (only when every prevout value is known) |
| Signals | `rbf_signal` (BIP125: some input sequence ≤ 0xfffffffd; under full-RBF node policies replaceability does not depend on it), `n_op_return`, `n_dust_outputs` (< 546 sat, a heuristic) |
| Relationships | `input_refs` (`parent_txid:vout`, first 25): observable spends only. **No wallet clustering, ownership or identity inference** |

**Rule-based indicators** (`INDICATORS`): `rbf_signal`, `high_fan_out` (≥ 50 outputs),
`consolidation` (≥ 50 inputs into ≤ 2 outputs), `dust_output`, `op_return`, `timelocked`,
`nonstandard_version` (not 1/2/3), and `fee_rate_outlier` (≥ 10× the rolling median of the
last 500 fetched, needing at least 20 values). They describe observable properties. They
are not risk scores, fraud predictions or probabilities, and they say nothing about
legality.

## 5. API (read-only; `src/chainwatch/serving/live_api.py`)

| Route | Returns |
|---|---|
| `GET /v1/live/status` | connection state, collector heartbeat and counters, coverage, source URL and whether it is the public mempool.space service, indicator descriptions |
| `GET /v1/live/transactions` | paged list; `limit≤200`, `sort` ∈ {id, first_seen, fee_rate, fee, vsize, n_inputs, n_outputs}, `order`, `min_fee_rate`, `max_fee_rate`, `flag`, `outcome` ∈ {fetched, failed, all} |
| `GET /v1/live/transactions/{txid}` | full features, explorer link, flag descriptions; 400 on a malformed txid, 404 if not observed |
| `GET /v1/live/stats?window_minutes=` | per-minute counters, fee-rate and vsize quantiles, fee-rate histogram, flag counts, failure reasons |
| `GET /v1/live/events` | SSE: `tx` per new observation (with `id:`, resumable through `Last-Event-ID` or `?since=`) and `status` every 5 s. At most 20 streams, each closed after 300 s (browsers reconnect) |

## 6. Run it locally (no paid resources)

```bash
pip install -e ".[dev]"
(cd frontend && npm ci && npm run build)
chainwatch --config configs/demo.yaml demo-setup           # synthetic replay tabs (once)
chainwatch --config configs/live.yaml live-collect          # terminal 1: real mempool data
chainwatch --config configs/live.yaml serve                 # terminal 2: http://127.0.0.1:8000
```

* **Bounded live check:** `chainwatch --config configs/live.yaml live-collect --duration
  120 --max-fetches 20 --db /tmp/check.sqlite`. It prints the counters; `received > 0` and
  `fetched > 0` mean real ingestion worked.
* **Offline UI development** (labelled as a mock in the UI):
  1. run `PYTHONPATH=src python scripts/mock_mempool.py`;
  2. point `live.ws_url` at `ws://127.0.0.1:8999/api/v1/ws` and `live.rest_base` at
     `http://127.0.0.1:8999/api` in a copy of `configs/live.yaml`.

## 7. Verification status

| Check | Result |
|---|---|
| Unit and integration tests (local mocks) | protocol, malformed frames, features, Bitcoin semantics, dedupe, sampling, backoff, token bucket, 429, backpressure, idempotent storage and retention, read-only reader, reconnection after a server drop, stale-stream reconnect, unreachable-server backoff, end-to-end WS → REST → SQLite, all `/v1/live` routes, SSE ordering, resume and client cap, mode isolation, frontend helpers |
| End-to-end with real processes and a **local mock** source | collector CLI → SQLite → `serve` → dashboard (§8) |
| **Real mempool.space ingestion** | **not verified.** From the build environment, every WebSocket attempt was refused by the egress proxy (`InvalidProxyStatus: proxy rejected connection: HTTP 403`); 0 real txids were received and 0 details fetched. The collector then backed off and stopped cleanly. Run the bounded live check above on a normal internet connection to verify |

### Real-network verification checklist (still to be run; v0.8.1)

The build environment's egress proxy refuses mempool.space (HTTP 403 on 2026-10-23 as well),
so the current risk pipeline has only run against the local mock. On a normal connection
(Windows machine or the VM), run a bounded, polite check with the shipped limits:

```bash
DB=data/live/real-check.sqlite
chainwatch-live --config configs/live.yaml indicators update --source ofac-sdn --fallback ofac-sdn-mirror --db $DB
chainwatch-live --config configs/live.yaml collect --force --duration 3600 --db $DB
chainwatch-live --config configs/live.yaml report --hours 1 --db $DB > real-check-report.json
chainwatch-live --config configs/live.yaml alerts list --db $DB > real-check-alerts.json
```

Record, from the final `collect` output (counters) and the report:

| What | Counter / field |
|---|---|
| Received txids / sampled / queued | `received`, `not_sampled`, `queued` |
| Enrichment attempts, successes, failures | `fetched`, `failed`, `not_found`, `rate_limited`, `timeouts`, `upstream_errors`, `malformed_payloads` |
| Retries | `retries_scheduled`, `retries_succeeded`, `retries_exhausted`, `retries_dropped` |
| Assessed records | `risk_assessed`, `risk_errors`, `layer_errors` |
| Source matches / relations / anomalies | `screening_matches`, `graph_relations`, `anomalies_strong`, `anomalies_notable` |
| Unavailable assessments | feed rows with `risk_state` `insufficient_evidence` or `failed` |
| Coverage | `availability` and `outages` in the report (`sequence_gap` includes re-sync stretches) |

Pass criteria: real txids received, at least one detail fetched and assessed, every fetched
row with a risk record, no traceback, a clean stop. The anomaly layer needs 1,000 fetched
transactions before it reports anything (`INSUFFICIENT_BASELINE` until then: about 83
minutes at 12 fetches per minute; at the local config's rate, less). A successful run shows
that the pipeline works on real observations; it says nothing about fraud-detection
accuracy, and a source match is a listing, not proof of wrongdoing.

## 8. Measurements (local mock, not mempool.space)

`scripts/live_load_test.py`, 180 s, mock announcing **20 tx/s** (above typical mainnet
volume). These measurements used the earlier 10% sample rate; the default is now 5%:

| Measure | Result |
|---|---|
| Announced → sampled | 3,600 → 357 (9.9% at `sample_rate` 0.1) |
| Outgoing REST requests | 94 in total; **max 34 in any 60 s window** (cap: 30/min + burst 5) |
| Backpressure | 61 sampled txids dropped (queue full); none expired |
| Memory | peak RSS 33.8 MiB for the whole Python process (+1.8 MiB during the run) |
| SQLite | 2.4 MB, mostly WAL pre-allocation, for 94 rows |
| Reconnects / errors | 0 / 0 |

At 10% sampling, announcements above about 5 tx/s exceed the 30/min budget. That is why
the default sample rate is now **5%** (about 18 requests/min at 6 tx/s). Real mainnet rates
were not measured here; check the `received` and `dropped` counters in `/v1/live/status`.

## 9. Known limitations

* **Partial coverage by design:** only transactions announced while the collector is
  connected, only a hash sample of them, and only those still fetchable. Statistics
  describe this sample, not the network.
* **Single public source:** mempool.space availability, its view of the mempool and its
  unpublished rate limits all apply. Results can differ from another node's mempool.
* `first_seen` is the collector's receipt time. Network-wide first-seen time is
  unavailable.
* **Replacements and confirmations after the fetch are not tracked** (by design, so that
  no post-observation information leaks into features). `removed`, `mined` and `replaced`
  events are counted, and used only to skip fetching txids that already left the mempool.
* **Prevout-derived fields** depend on the REST payload. Values that are missing make
  `input_value_total` unknown.
* **Indicator thresholds are conventions** (dust 546 sat, fan-out 50, …), not tuned or
  validated.
* **No labels exist for live data**, so no model can be evaluated yet
  (`docs/LIVE_ML_PROPOSAL.md`).
* SQLite with one writer suits one collector. Several collectors would need a different
  store. A single always-on hosted collector, with an authenticated edge API in front of
  the file, is described in [LIVE_CLOUD.md](LIVE_CLOUD.md).
