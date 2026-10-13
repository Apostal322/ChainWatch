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
