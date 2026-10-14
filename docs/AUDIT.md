# Engineering audits

* [v0.6.0 audit](#v060-engineering-audit-live-monitor-cloud-kit-research-claims) (2026-10): live collector, edge API and remote proxy, deployment kit, dashboard, research claims.
* [Phase 4 audit](#phase-4-engineering-audit-release-readiness): serving layer and release.

## v0.6.0 engineering audit (live monitor, cloud kit, research claims)

Four independent read-only reviews (ingestion correctness against mempool's backend source,
security and API, frontend with fault injection in a browser, ML scientific claims),
followed by fixes. Every fixed item has a regression test or a recorded check; nothing
was deployed. Severity: P0 critical, P1 high, P2 medium, P3 low. **No P0 was found.**

| # | Sev | Component | Problem | Root cause | Fix | Evidence |
|---|---|---|---|---|---|---|
| 1 | P1 | collector | After about 1,024 failed reconnects (~8.5 h of upstream outage) the WebSocket task died for good while the process stayed up; systemd never restarted it | `base * 2**attempt` overflows a float at attempt 1024; the call was outside the try block | exponent clamped at 30 | `test_backoff_never_overflows_after_many_failures` |
| 2 | P1 | collector | A worker or the heartbeat that hit an unexpected exception (SQLite locked or full, an out-of-range integer) died silently: the process looked alive, every new txid was dropped | tasks were only awaited at shutdown; `gather(return_exceptions=True)` hid errors | per-item exception handling (`internal_errors`); `run()` supervises the tasks, sets `fatal`, state `failed`, exit code 1 → systemd restart | `test_worker_survives_…`, `test_dead_pipeline_task_stops_…`, `test_cli_collect_exits_nonzero_on_fatal`; systemd container: 25 s exclusive lock → exit 1 → restarted |
| 3 | P1 | dashboard API (Render) | Concurrent `/v1/live/events` opens bypassed the stream cap; ~40 streams filled uvicorn's concurrency limit and `/health/ready` returned 503 (Render marks the service unhealthy) | cap checked before an `await`, counter incremented after it; no per-client cap | slot reserved synchronously, released in the response wrapper; 2 per client; 10 total (< 32) | `test_stream_slots_are_reserved_before_any_await`, `test_per_client_stream_cap_…` |
| 4 | P1 | dashboard UI | Mock data was labelled "REAL BITCOIN NETWORK DATA" whenever the status call failed or returned an unreadable body; the badge stayed "Connected" with the API unreachable | REAL was the fallback for a null status; last good status never aged | badge is REAL only when the status says so; "Status unknown" on errors or status older than 20 s; "Connected, no new data" after 2 min without observations | 13 frontend tests; Playwright before/after (API down, truncated body, server killed) |
| 5 | P1 | dashboard UI | Live feed rows were not reachable by keyboard | `onClick` on `<tr>` only | row buttons, WAI-ARIA tabs | Playwright keyboard checks |
| 6 | P1 | research docs | "Pre-registered / committed before the experiments" cannot be verified: the protocol first appears in git in the same commit as the results | pre-release history not published | wording: fixed before the runs per the author's record, not independently verifiable | docs diff; `git log -- docs/PHASE2_PROTOCOL.md` |
| 7 | P2 | remote proxy | Cache stampede (20 concurrent misses → 20 upstream calls), failures not cached, unbounded key space, whole cache cleared at 512 keys | lock released before the fetch; errors raised before caching | single flight, negative cache, LRU 256, upstream token bucket 3/s, streamed byte cap, invalid JSON → 502 | `tests/test_live_security.py` (5 tests) |
| 8 | P2 | edge API | Unauthenticated requests drained the per-IP bucket shared with the token holder (Render's egress IP is shared) | rate limit ran before auth | valid token exempt from the per-IP limit | `test_edge_rate_limit` |
| 9 | P2 | collector | Replaced/evicted/mined txids still fetched (404s waste ~20 % of the budget) | `removed`/`mined` only counted | skipped before fetch (`gone_before_fetch`); `mined` counted; `replaced` → `replacement_pairs` with documented overlap | `test_removed_or_mined_txids_are_skipped_before_fetch` |
| 10 | P2 | collector | Disconnect outages started at detection, under-reporting up to `stale_after_s` per incident | outage opened when the watchdog fired | starts at the last received message | `test_disconnect_outage_starts_at_the_last_message` |
| 11 | P2 | features | Negative or > 2^63 amounts accepted; the latter crashed the SQLite insert (finding 2) | no range checks | amounts in [0, MAX_MONEY], weight ≤ 4M, locktime/version ranges | `test_out_of_range_*` (7) |
| 12 | P2 | store | At the 50k-row cap: `stats(60)` 92 ms, fee-filtered list 69 ms, COUNT full scans | `ORDER BY fee_rate` forced a fee-index walk; no covering index | `(outcome, fee_rate)` index, sort in Python, `+column` planner hints | before/after table in LIVE_CLOUD.md §8 |
| 13 | P2 | dashboard UI | Horizontal scroll at 390 px; stale-response races (live detail, replay paging); EventSource gave up for good after an HTTP error; partial minute plotted as a drop; errors shown as "no gaps" | `1fr` grid track; effects without cancellation; no manual reconnect | `minmax(0,1fr)`; ignore-late-response guards; backoff reconnect with `?since=`; complete minutes only, gaps as breaks; per-panel error states | Playwright before/after at 390/768/1440 |
| 14 | P2 | research code | Run metadata recorded the *published* Elliptic++ hashes even for synthetic runs, so the "data changed" check could never fire | the published constant was written as the file hash | actual raw-file hashes + `matches_published` + processed fingerprint; evaluate warns on change | `tests/test_data_provenance.py` (4) |
| 15 | P2 | research docs | Misquoted or overstated numbers (43–49 range, 0.92→0.17, P@20 zero-steps, "no gain before the shift", "close to random"); headline used a sub-period instead of the primary comparison | — | corrected against `reports/`; primary pooled result first; CI-coverage caveat | docs diff (no result changed) |
| 16 | P3 | collector | HTTP-date `Retry-After` ignored; burst right after a 429 pause; no pause on 5xx; queued items not re-aged after waiting; backoff reset after one frame; token-bucket float spin; 4 MB frame cap below resync deltas; `timelocked` on non-enforced locktimes; missing status read as unconfirmed | — | each fixed | `tests/test_live_faults.py` |
| 17 | P3 | deploy | `CHAINWATCH_HOST` unvalidated (a bad value emptied the Caddyfile); moving `main` installed as root; token readable by the collector's user; no syscall filter or loopback-only rule | — | host/REF validation, `EXPECTED_SHA`, Caddyfile validated before replace, token 0600 root:root, `SystemCallFilter`, `IPAddressDeny`; exposure 3.0 → 1.3 / 1.1 | systemd container test (LIVE_CLOUD.md §11) |
| 18 | P3 | API | Hostile `Last-Event-ID` → 500; 429/413 without security headers; IPv6 rate-limit rotation within a /64; shutdown waited up to 300 s for SSE | — | strict parsing; headers on every error; /64 keys; `timeout_graceful_shutdown=10` | `tests/test_live_security.py` |

Accepted, not changed: the collector and edge API share a service user (a compromised
collector could read the edge's environment via `/proc`); `render.yaml` pulls a version tag
rather than a digest (changing it is a production change); CI installs unlocked dev
dependencies; public `/v1/live/status` passes collector error strings (no secrets).

---

# Phase 4 engineering audit (release readiness)

An independent review of the serving layer (findings 1–15) and a release review (16–18), carried out by a separate reviewer working
read-only with probes against the synthetic test registry, plus a review of every
scientific claim in the README and case study. Findings are ordered by severity. Each
"fixed" item has a regression test in `tests/test_serving.py` unless noted otherwise.

## Engineering findings

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | High | Request bodies were unbounded and parsed and validated on the event loop. A 95 MB `/v1/predict/features` body took 4.5 s, stalling all requests including health checks. | **Fixed:** pure-ASGI `BodySizeLimitMiddleware` (rejects on Content-Length and counts chunked bodies before parsing), schema-level `max_length=1000` on both lists, uvicorn `limit_concurrency`. |
| 2 | Medium | The SHA-256 in the model card is self-attesting (stored next to the pickle). Index keys such as `../x` or absolute paths could make the loader read pickles outside the registry. | **Fixed:** version names are allow-listed and must resolve inside the registry. The docs now state that the hash is an integrity check, not tamper-proofing. The demo registry is built at image-build time as root and served read-only by uid 10001. *Remaining:* pickle is still a trust requirement (see "Not changed"). |
| 3 | Medium | An explicit `model_version` of the Phase 1 model was flagged `temporally_valid=true` on its validation steps (30–34 on real data), which it used for early stopping and thresholding. | **Fixed:** temporal validity honours `out_of_sample_from_step`. |
| 4 | Medium | The registry index only ever grew. Stale bundles (for example from another label latency) could be routed, and per-card latency and data fingerprint were never checked. | **Fixed:** exports rebuild the index; the service loads only the bundles listed in `registry.json`, each checked against the registry's latency and fingerprint; causality is checked at export time. |
| 5 | Medium | Supply chain: the lock had no hashes; build tools were fetched unpinned; base images used mutable tags; Actions used floating tags with the default token scope; the CPU image carried a 305 MB NVIDIA NCCL wheel. | **Fixed:** `requirements/serve.lock.txt` is hash-pinned (installed with `--require-hashes`, `--no-build-isolation`); base images are pinned by digest; Actions are pinned to commit SHAs with `permissions: contents: read`; `xgboost-cpu` 3.4.1 (same library and version; image 1.86 → 1.2 GB). Replay consistency was re-verified inside the new image: 234,596 scores, max diff 0.0. |
| 6 | Low–Med | In the demo container the server user owned, and could rewrite, the pickles it loads. | **Fixed:** build-time registry (`demo` target), root-owned and read-only; compose uses `read_only: true` plus tmpfs. |
| 7 | Low–Med | No rate limiting; cache stampede on cold steps; `/v1/experiments` re-read CSVs on every call. | **Fixed:** per-client token bucket (opt-in; on in `public-demo.yaml`), a lock around cache fills, research results loaded once at start-up. |
| 8 | Low | Client-controlled `x-request-id` was echoed unvalidated (5,000-character values accepted). | **Fixed:** accepted only if it matches `^[A-Za-z0-9._-]{1,64}$`, otherwise generated. |
| 9 | Low | Starlette routing errors (405, unmatched routes) bypassed the error envelope. | **Fixed:** the handler is registered on `starlette.exceptions.HTTPException`. |
| 10 | Low | The SPA catch-all used prefix matching (`/docsx` → 404; `/v1` → index.html) and returned a soft 200 for missing files. | **Fixed:** exact first-segment matching; a missing file-like path returns 404. |
| 11 | Low | Public `/docs` loads third-party JavaScript; there were no security headers. | **Fixed:** `expose_docs` switch (off in `public-demo.yaml`), a same-origin CSP on dashboard responses, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`. |
| 12 | Low | The inference contract overstated enforcement (pickle safety, "every response carries `label_available_at_step`", "re-checked when built"). | **Fixed:** contract wording now matches the code. |
| 13 | Low | Neighbourhood lookup could return 500 on an inconsistent edge endpoint. | **Fixed:** missing endpoints are skipped. Transactions in steps 1–19 still return 400 `replay_unavailable` (documented behaviour). |
| 14 | Low | The demo container trained its models on first start (minutes), against a 180 s health start period. | **Fixed:** models are built at image-build time; ready in 6 s (measured). |
| 15 | Low | Info exposure (`git_commit`, full model cards). | **Accepted:** no secrets; useful for provenance in a research demo. |
| 16 | Medium | (Release review) Behind a platform proxy, the rate limiter keyed clients on the *leftmost* `X-Forwarded-For` entry. Proxies such as Render's and Hugging Face's append to that header, so a client could send a new fake address with every request and bypass the limit. | **Fixed:** `trusted_proxy_hops` selects the entry appended by the trusted proxy (counted from the right); `public-demo.yaml` uses 1. Regression test `test_rate_limit_client_key_cannot_be_spoofed`; verified in the container (spoofed headers get 429 after the burst). |
| 17 | Low | (Release review) Frontend dev tooling (vite 5.4, vitest 2.1) had 5 published advisories (2 critical), all in the dev server and test runner, not the shipped bundle. | **Fixed:** vite 6.4.4, vitest 4.1.11, @vitejs/plugin-react 4.7.0; `npm audit` reports 0 vulnerabilities; the production dependency tree is unchanged. |
| 18 | Low | (Release review) Four screenshots from a local real-data run showed a small excerpt of Elliptic++ (transaction IDs, labels, one transaction's features), and the dataset has no redistribution licence. | **Fixed in the tree:** removed; every screenshot is now synthetic. *Remaining:* the files are still in the development branch's history (see RELEASE_READINESS.md §3). |

## Scientific-claim review

| Claim (before) | Problem | Now |
|---|---|---|
| "Mainly a stale decision boundary" (README) | Causal-sounding interpretation | "Consistent with concept or class-conditional shift; the cause is not identifiable." |
| "This matches the dark-market shutdown" (RESULTS) | Asserted a cause from the literature | "Coincides with the regime change Weber et al. attributed to …; our data cannot confirm that cause." |
| "Late illicit activity is learnable" (CASE_STUDY) | Omitted that the diagnostic trains on later steps | Qualified: predictable once same-period labels exist; the diagnostic is not deployable. |
| "Causal policies" | Ambiguous with causal inference | "No-look-ahead (time-causal) policies." |
| "SHA-256 verified before unpickling" as a safety claim | Overstated (self-attesting hash) | Integrity check only; trust comes from read-only, operator-built registries. |
| "Production replay service" | Implies production use | "Production-style". |

Unchanged on purpose: all experimental metrics. No result was re-run or re-tuned in
Phase 4. The only numbers measured anew are deployment facts (image size, start-up time,
memory) and the in-image replay-consistency check.

## Not changed (known, documented)

- **Pickle-based artifacts** remain a trust requirement. A pickle-free format (XGBoost
  JSON plus a declarative preprocessor) would remove it, and is listed as future work.
- **CI installs version ranges**, not the lock (Python 3.11 cannot use every locked
  version). The serving image is fully locked, and research artifacts record their
  library versions.
- **No authentication:** the public demo is read-only over synthetic data.
- **Code licence:** MIT, chosen by the owner for v0.4.0. It covers the code only, not the dataset.
