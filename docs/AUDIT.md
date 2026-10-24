# Engineering audits

* [v0.8.1 independent audit](#v081-independent-audit) (2026-10-23): seven parallel adversarial reviews (collector, persistence, screening, HTTP surface, dashboard in a browser, anomaly/ML, supply chain and deploy kit) of v0.8.0 (`d8fcb94`), then a second review of the fixes.
* [v0.8.0 independent hardening audit](#v080-independent-hardening-audit) (2026-10): all components, re-audited by independent reviewers; adversarial cases reproduced before fixing.
* [v0.7.0 risk intelligence audit](#v070-risk-intelligence-audit) (2026-10): screening, indicator parsing, anomaly detector, engine, alerts, dashboard.
* [v0.6.0 audit](#v060-engineering-audit-live-monitor-cloud-kit-research-claims) (2026-10): live collector, edge API and remote proxy, deployment kit, dashboard, research claims.
* [Phase 4 audit](#phase-4-engineering-audit-release-readiness): serving layer and release.

## v0.8.1 independent audit

Baseline `d8fcb94` (v0.8.0): 473 Python and 51 frontend tests passed, CI 8/8. Seven
independent reviewers worked on read-only copies with deterministic repros. Severity as
above (P1 = misleading risk or data state, data loss, or anonymous low-effort DoS in plausible
operation). Every code fix has a regression test that **fails on `d8fcb94`** and passes now.

| # | Sev | Component | Problem (root cause) | Fix | Test | Commit |
|---|---|---|---|---|---|---|
| W1 | P1 | supply chain | After v0.8.0 was validated, new advisories turned the audit gate red: certifi < 2026.10.18 trusts a CA with compromised intermediates (all locks; the collector's and proxy's HTTPS), pillow 12.3.0 (~40 advisories, serving image), vitest (dev) | certifi 2026.10.19, pillow 13.0.0, vitest 4.1.12; only these packages changed | pip-audit and npm audit: 0 | `1b12c2f` |
| W2 | P1 | retro screening | Re-run at every collector start for each source's latest version, it matched listings closed since (e.g. a mirror row the official file superseded): a P1 "now listed" alert for a correctly unmatched transaction | only listings in force | `test_retro_screening_ignores_listings_closed_since` | `bf8b7f8` |
| W3 | P1 | restore (was accepted as LOW) | Restore renamed the live files while other processes could open the database: a collector restarted by systemd (state still `stopped`) or a timer-run indicator refresh wrote to the renamed files or paired its fresh WAL with the restored file; a restore that reported OK lost its data | shared flock held by every writer; restore needs it exclusively and copies through the backup API (no renames); a writer starting meanwhile exits 75 | `test_restore_is_refused_while_any_writer_has_the_database_open`, `test_no_writer_can_open_the_database_during_a_restore`; systemd §6 | `bd7b900` |
| W4 | P1 | dashboard | The synthetic demo stated the research-dataset disclaimer, contradicting its SYNTHETIC banner | synthetic disclaimer from the API | `test_the_synthetic_demo_does_not_claim_the_research_dataset` | `b6acd8f` |
| W5 | P1 | dashboard | Replay kept the previous step's rows (and a mixed "Model used" tile) under the new step's caption after a failed request | cleared on change and error; announced | browser repro | `b6acd8f` |
| W6 | P2 | ingest | Superseding the mirror's extra rows counted towards the official file's mass-removal limit: one such mirror blocked every later official refresh | own rows only (first official snapshot against the copy) | `test_official_refresh_is_not_blocked_by_a_mirrors_extra_rows` | `bf8b7f8` |
| W7 | P2 | ingest | A mirror row closed by the official file left no trace; the same stale mirror content re-listed it | `indicators.closed_by`; veto | `test_a_stale_mirror_cannot_relist_what_the_official_file_superseded` | `bf8b7f8` |
| W8 | P2 | ingest | Older official content (stale CDN, old file) was a new version: re-listed delisted, delisted new | refused unless `--allow-older` | `test_older_official_content_is_refused` | `bf8b7f8` |
| W9 | P2 | collector | A retry waiting in the queue or for a token (the 30 s pause after a 5xx exceeds the retry delay) was lost on shutdown without a row | interrupted and queued retries recorded | `test_a_retry_waiting_in_the_queue_is_recorded_on_shutdown` | `b4e9236` |
| W10 | P2 | collector | `add_failed` in the failure path: a writer lock longer than the busy timeout killed the worker | contained (`db_errors`) | `test_a_locked_database_in_the_failure_path_is_not_fatal` | `b4e9236` |
| W11 | P2 | collector | mempool repeats its sequence during a whole out-of-sync stretch; skipped arrivals left no trace and availability looked complete | stretch recorded as a `sequence_gap` with duration | `test_resync_stretches_are_recorded_as_coverage_gaps` | `b4e9236` |
| W12 | P2 | persistence | The pre-restore copy lacked un-checkpointed WAL data | backup-API copy | `test_restore_keeps_uncheckpointed_data_in_the_pre_restore_copy` | `bd7b900` |
| W13 | P2 | persistence | A failed assessment looked like "risk off" and its addresses were rolled back (never re-screened) | addresses in their own savepoint; all-ERROR "Assessment failed" record; `risk_state` | `test_failed_risk_write_keeps_the_observation_and_leaves_a_failed_record` | `bd7b900` |
| W14 | P2 | persistence | Dismissed alerts with notes were deleted after 90 days; the next occurrence re-notified | only untouched `new` alerts expire | `test_alerts_a_person_acted_on_are_not_pruned` | `bd7b900` |
| W15 | P2 | anomaly | "strong" counted vsize and the count that determines it: ~1.2 priority-3 alert groups per hour of realistic noise | robust-tail/3 evidence groups | `test_correlated_size_measurements_do_not_make_noise_strong` | `42fbe1b` |
| W16 | P2 | deploy | `install.sh` checked out before verifying `EXPECTED_SHA`; upgrades ran the old installer (bash executes the old text) | verify first, staging clone, re-exec the new installer | systemd §8 | `4d8a5e5` |
| W17 | P2 | CI | The suite never ran on the deployed versions (dev lock resolves newer web stack) | `deployed locks / py3.12` job | CI | `4d8a5e5` |
| W18 | P2 | dashboard | Feed showed "no finding" for rows whose screening never ran; graph "no parents" when skipped; green "no match" from a stale source; green "Connected" with nothing ever fetched; tab switches into 429s | `risk_state` markers, wording and tones, live view kept mounted | vitest | `bd7b900`, `b6acd8f` |
| W19 | P2 | docs | The rollback runbook claimed an older collector can open a v0.8 database (false: duplicate alerts, lost dismissals); install examples named tags that do not exist; a too-low proxy hop count was called "safe" (it is an anonymous DoS) | corrected; restore-based rollback; hop verification procedure | — | this release |
| W20 | P3 | collector | Pongs satisfied the stale watchdog; duplicate announcement of a txid with a pending retry; expired retry counted twice; vin sequence not validated (bool/negative switched on rbf_signal); fee unbounded with missing prevouts | fixed | `tests/test_live_collector_audit.py` | `b4e9236` |
| W21 | P3 | persistence | Backups: a failed check left the final file (counted by rotation), `.partial` never cleaned, rotation deleted operator files, no checksum; corrupted database opened silently; orphans when every row aged out; anomaly window updated before commit | fixed; exit 78 on a failed `quick_check` | `test_backup_rotation_counts_only_verified_copies`, `test_collector_refuses_a_corrupted_database_without_a_restart_loop`, `test_orphans_are_removed_when_every_transaction_aged_out` | `bd7b900` |
| W22 | P3 | API | ids above SQLite's INTEGER range: 500 + traceback; edge 422 echoed input; no `no-store` on `/v1`; Caddy forwarded HEAD (405) | fixed | `test_edge_validation_errors_use_the_envelope_and_huge_ids_are_422`, `test_api_answers_are_not_cached` | `4038332` |
| W23 | P3 | CLI / release | A refused official snapshot with a working fallback exited 0; version tag pushed on any inspect failure; template expression in `run:`; entrypoint not shellchecked; Dependabot could bump the ML stack | exit 3; explicit "manifest unknown"; env; shellcheck; ignores | `test_a_refused_official_snapshot_fails_the_refresh_even_if_the_mirror_works` | `bf8b7f8`, `4d8a5e5` |
| W24 | P3 | anomaly / dashboard | Two-sided explanation used the doubled p; replay rows not buttons; tab not in the URL; empty replay page; legend contrast; panel errors not announced | fixed | `test_two_sided_explanation_states_the_actual_share`, vitest | `42fbe1b`, `b6acd8f` |
| W25 | P2 | second review | Found in the independent second review of the fixes above: rows closed before the upgrade had no `closed_by` (W7's veto missed them); rows screened by no list after the baseline showed `no_finding`; a damaged schema page raised before `quick_check` ran (restart loop, not exit 78); resync stretches counted as observed time; the skip link reset the dashboard tab; a replay selection survived a ranking change; backup removed a concurrent run's `.partial`; the installer left its staging clone on failure | backfill from `indicator_versions`; `screening_unavailable`; read-only `quick_check` before opening; gaps with duration subtracted; only tab slugs switch views; selection keyed to the ranking; only partials older than 1 h; `trap` cleanup | `test_closures_from_before_the_upgrade_still_block_a_stale_mirror`, `test_rows_screened_by_nothing_are_never_no_finding_after_the_baseline`, `test_collector_refuses_a_database_with_a_damaged_schema_page`, `test_resync_stretches_count_as_unobserved_time`, `test_backup_rotation_counts_only_verified_copies` (all fail on `62b42ad`); vitest | `dba5a82` |

Verified correct by the reviewers (not changed): every research number spot-checked
reproduces exactly (feature-subset report re-run: identical 120 values; dataset hashes
match); no fit on validation or test data; the supervised gate abstains for every malformed
card; the publish gate rejects fork PRs, fork branches named `main`, tag pushes and
non-`main` dispatches; the token is compared in constant time and never logged; security
headers on every status of both apps; ARM64 wheels exist for every VM package; no secrets
or datasets in Git history.

### Follow-up: paged risk lists

| # | Sev | Component | Problem | Fix | Test | Commit |
|---|---|---|---|---|---|---|
| X1 | P2 | dashboard / API | Only the 50 newest findings and the first 200 alerts reached the UI; no type, retrospective, source, priority or time filter; "open" filtered in the browser over a truncated list | server-side filters and keyset pagination with totals from one read snapshot; covering indexes; remote proxy validates and refuses filters an older edge would ignore; pager and filters in the UI (keyboard, 375 px) | `tests/test_risk_pages.py` (54), vitest "paged risk lists", e2e 5 checks; benchmark in RISK_ENGINE.md §9 | see PR |

### Accepted or open after v0.8.1

* **External:** Render's real `X-Forwarded-For` hop count (procedure in DEPLOYMENT.md;
  a wrong value is P1 on the public demo), real mempool.space traffic (the egress proxy
  here refuses it: HTTP 403, 0 txids), the Oracle VM, a 24-hour run.
* **Closed after v0.8.1 review (was open, P2, dashboard):** the alert queue and findings
  list showed at most 200 / 50 items with no layer or retrospective filter. Now filtered
  and paged on the server (row X1 above).
* **Open, P3:** a copy-txid control; the tab bar at 360 px scrolls without a cue; one
  649 kB JS bundle; uvicorn `limit_concurrency` counts idle keep-alives (safe behind Caddy,
  never expose uvicorn directly); the first frame of a connection cannot be recognised as
  a re-sync; a percentage mass-removal guard can be worn down over several days; graph
  worst case ~0.2-0.4 s for a transaction with 200 parents × 500 inputs (estimated).
* **Accepted:** an address re-designated while the official fetch keeps failing is missed
  until the official file lists it (the official source shows as stale meanwhile).

## v0.8.0 independent hardening audit

Baseline: `f9f8765` (v0.7.0). Independent reviewers audited the collector, risk engine,
API and proxy, deployment kit, dashboard and research claims. Each code defect was
reproduced first: **every new regression test fails on `f9f8765` and passes after the fix**
(checked by running the tests against a worktree of the baseline). Severity: P1 = wrong
results, data loss or availability loss reachable in normal operation or by an anonymous
visitor; P2 = wrong results under plausible faults; P3 = misleading output or robustness.
None of these problems was deployed: the risk layers had never run on the public network.

### Code and deployment

| # | Sev | Component | Problem (root cause) | Fix | Test / evidence | Commit |
|---|---|---|---|---|---|---|
| V1 | P1 | config | `retention_rows: 0` was accepted and `prune()` deleted every stored row; `heartbeat_s: 0` spun the heartbeat loop (no range validation) | `LiveConfig.__post_init__` validates every count, duration and URL scheme; invalid config exits 78 | `test_live_config_rejects_unsafe_values`; systemd §12 (exit 78, no restart loop) | `dc8d428` |
| V2 | P1 | indicator refresh | An empty or truncated snapshot (error page, schema change) silently delisted every address | an empty snapshot, or one that would delist more than max(5, 25 % of the active listings), is refused (`SnapshotRejected`) unless `--allow-mass-removal`; truncated XML falls back | `test_empty_or_mass_delisting_snapshot_is_refused`, `test_truncated_official_xml_falls_back_and_file_inputs_never_do` | `a2d0802` |
| V3 | P1 | indicator versions | `UNIQUE(source, sha256)` made a snapshot that reverts to earlier content "unchanged", so a re-listed address stayed delisted | diff against active listings; uniqueness dropped by an id-preserving migration | `test_content_reverting_to_an_earlier_version_relists_it` | `a2d0802` |
| V4 | P1 | screening | The official SDN file and its mirror were independent sources: an address delisted officially stayed listed through the mirror | one authority (`ofac-sdn`, official rank 0); an official snapshot closes the mirror's extra listings; one match per authority. The first version let the mirror close official listings too (see V28) | `test_official_list_and_its_mirror_are_one_list` | `a2d0802` |
| V5 | P1 | retro screening | Transactions assessed while a new snapshot was loading were missed by both the live path and the retrospective pass | retro covers every stored transaction whose own record lacks the match; the collector runs it on reload, in a transaction | `test_retro_screening_covers_the_reload_window_and_is_idempotent`, `test_collector_retro_screens_when_it_loads_a_new_snapshot` | `a2d0802` |
| V6 | P1 | collector | A trickling REST body held a fetch worker forever while health said connected (per-read timeout only) | overall `asyncio.timeout` deadline and a 16 MB body cap | `test_rest_read_has_an_overall_deadline` | `a2d0802` |
| V7 | P1 | proxy | An anonymous visitor cycling query parameters missed the cache each time, drained the shared upstream budget and turned the dashboard's own polling into 502s | fixed polling queries bypass the budget; `hours`/`window` snapped; 60 s last-good fallback; edge caches risk summary 15 s | `test_anonymous_parameter_cycling_cannot_starve_the_dashboards_own_polling`, `test_last_good_answer_is_served_when_the_edge_fails` | `064612f` |
| V8 | P2 | XML parser | UTF-16 input could hide a DOCTYPE from the byte scan; one party with 1 M features peaked at 749 MB | UTF-8 only; every finished top-level record freed; features dropped as they end; 10,000 addresses per party → 21 MB | `test_utf16_and_bom_inputs`; 1 M-feature stress run | `a2d0802` |
| V9 | P2 | indicator fetch | https was checked only on the first URL, not on redirect hops | checked on every hop | `test_fetch_refuses_a_redirect_through_plain_http` | `a2d0802` |
| V10 | P2 | graph relations | Parents' inputs were screened at the parent's time, duplicates per address, and coverage overstated | re-screened at the child's time; deduplicated per (parent, address, authority), capped at 20; `parents_checked` reported | `test_graph_relations_screen_parents_at_the_childs_time` | `a2d0802` |
| V11 | P2 | alerts | Upserts deduplicated only over the 50 displayed txids; keys ignored authority and role | `alert_txids` table; keys `screening:{authority}:{role}:{address}` | `test_alert_group_includes_the_role`, `test_alert_upserts_are_idempotent_beyond_the_displayed_txids` (added after the second review) | `a2d0802` |
| V12 | P2 | anomaly | One flag outside a small window was always "strong"; the two-sided fee test could not reach alpha | `robust-tail/2`: strong needs ≥ 2 flags; `min_reference` 1,000; reachability validated two-sided | `test_strong_means_several_measurements_not_a_small_window` | `a2d0802` |
| V13 | P2 | collector lifecycle | `replaced` could override `mined`; txids leaving the mempool during a token wait or an in-flight GET were forgotten; block conflicts were fetched | mined is final; `LifecycleMemo` applied at store time; gone txids not fetched | `test_mined_overrides_replaced`, `test_lifecycle_event_during_fetch_is_not_lost`, `test_gone_is_rechecked_after_waiting_for_a_token`, `test_block_conflicts_are_not_fetched` | `a2d0802` |
| V14 | P2 | collector protocol | A repeated `mempoolSequence` (mempool.space re-sync; checked in its `websocket-handler.ts`) re-announced the whole mempool as new | repeated sequence = re-sync: added txids skipped and counted | `test_live.py` backpressure test (resync counters) | `a2d0802` |
| V15 | P2 | collector | A DB error on lifecycle/gap writes dropped the frame's new txids and tore down the socket; a disk-full auto-rollback went undetected; counters were incremented before commit | `sqlite3.Error` contained per write; `_tx` checks `in_transaction`; counters after commit | `test_db_error_in_frame_does_not_drop_new_txids_or_the_connection`, `test_disk_full_in_the_risk_write_does_not_count_a_phantom_fetch` | `a2d0802` |
| V16 | P2 | migrations | Two processes starting together raced on `ALTER TABLE` ("duplicate column name") | one `BEGIN IMMEDIATE` migration | `test_concurrent_migrations_do_not_collide` (deterministic interleaving) | `a2d0802` |
| V16b | P3 | reader | `LiveReader.get` read the row and its risk record in separate snapshots, so a concurrent prune could return a half-deleted transaction | one read transaction | `test_reader_get_survives_a_concurrent_prune` | `a2d0802` |
| V17 | P2 | proxy | A dripping edge held callers past the timeout; an exception stranded single-flight waiters | overall deadline; any exception becomes a clean 502 and releases waiters | `test_upstream_has_an_overall_deadline_and_errors_never_strand_waiters` | `064612f` |
| V18 | P2 | SSE | A client that never reads held its slot past the stream deadline | hard `move_on_after` deadline | `test_a_client_that_never_reads_cannot_hold_a_stream_slot` | `064612f` |
| V19 | P2 | deploy | Slow-header clients could hold the 32 uvicorn slots; non-GET reached the edge | Caddy read/idle timeouts; `GET`/`HEAD` only | systemd §9 (`caddy validate`) | `064612f` |
| V20 | P2 | token | A whitespace-padded token passed the length check; rotation needed downtime | token regex, exit 78; `CHAINWATCH_LIVE_TOKEN_NEXT` | `test_token_rotation_and_token_format`; systemd §13–14 | `064612f` |
| V21 | P2 | features, protocol | Output sum, fee = in − out and weight/size were not validated; a sequence of 2**63 raised `OverflowError` in the heartbeat task (fatal exit, restart loop) | validated; invalid input rejected; sequence outside 0 ≤ s < 2**53 ignored | `test_sum_of_outputs_above_max_money_is_malformed`, `test_fee_must_equal_inputs_minus_outputs_when_all_prevouts_are_known`, `test_huge_sequence_does_not_break_the_heartbeat` | `a2d0802` |
| V22 | P3 | status | `/v1/live/status` could echo `user:password@` from URLs and error texts | redacted | `test_status_never_shows_credentials_from_a_source_url` | `064612f` |
| V23 | P3 | install.sh | A failed token generation wrote an empty token; umask leaked to later steps; the firewall rule could land after Oracle's REJECT | checked, scoped umask, inserted before REJECT | shellcheck job; systemd §1 | `064612f` |
| V24 | P3 | supervised gate | Malformed model-card evidence raised instead of abstaining | types checked; never raises | `test_supervised_gate_rejects_empty_or_malformed_evidence` | `a2d0802` |
| V25 | P3 | performance | `risk_summary` scanned the risk table (0.48 s cold at the cap) | covering indexes, one pass: 0.10 s cold, 2 ms cached | 50k-row bench | `53f6f65` |
| V26 | P3 | CI | No job timeouts (6 h default), PR checks ran twice, no shell lint | timeouts, concurrency, shellcheck | CI | `dc8d428` |
| V28 | P1 | screening (second review, regression from V4) | Supersession ran both ways: when the official fetch failed, a mirror a day behind delisted the newest official designations (up to the mass-removal threshold, ~25 % per run) and re-listed addresses OFAC had just delisted | ranked supersession: a lower-ranked copy never closes a higher-ranked listing and never re-lists what it delisted (`skipped_delisted`); the official file is never "superseded" by its mirror, so a failing official refresh shows its real age | `test_a_lagging_mirror_never_delists_or_relists_official_listings` | this release |
| V29 | P2 | alerts (second review) | The new group keys had no migration: alerts dismissed in v0.7 came back as `new` on their next occurrence | one-time re-keying from the evidence's role during the v0.8 migration | `test_v07_alert_states_survive_the_new_group_keys` | this release |
| V30 | P3 | collector (second review) | The reload's retrospective pass ran unguarded in the heartbeat (a locked database killed the collector); after a restart the pass was skipped; matches the CLI had alerted on were counted again | guarded (`db_errors`, retried next beat); latest version per source re-screened at start-up; only new matches counted | `test_collector_retro_survives_a_locked_database_and_a_restart` | this release |
| V31 | P3 | collector (second review) | `LifecycleMemo` let `removed` override `replaced` (mempool.space sends both for a block conflict), unlike `mark_lifecycle` | replaced refines removed in both | `test_memo_keeps_replaced_when_removed_follows` | this release |
| V32 | P3 | proxy (second review) | Budget-refused failures shared the LRU with the polling answers, so a flood of distinct keys evicted them; the stream and the feed polled different keys, so the feed could look older than the stream and reset itself; `/status` was served stale for up to 60 s after the edge went down; `make_backend` accepted tokens the edge refuses | refusals not cached; one query for both; `/status` never stale; the edge's token rule | `test_refused_keys_cannot_evict_the_polling_answers_from_the_cache`, `test_event_stream_and_feed_poll_share_one_query`, `test_status_is_never_served_stale`, `test_remote_mode_requires_https_and_a_token` | this release |
| V33 | P3 | soak test | The soak's indicator refreshes replaced the whole TEST list, which the new mass-removal guard (V2) refuses: 4 of 5 refreshes failed in the 90-minute run | realistic updates (keep the list, add up to 1,000, delist a few); 40-minute re-run 17/17 | `docs/LIVE_CLOUD.md` §11 | `4501f4e` |
| V27 | P3 | demo | Generated synthetic data printed a "published hashes" mismatch warning | info message for synthetic data | — | `4cb20c1` |

### Dashboard (`f1b552d`)

| # | Sev | Problem | Fix |
|---|---|---|---|
| D1 | P1 | The feed could freeze silently while the badge said Connected (event stream closed, no fallback) | polling fallback, lag notice, txid dedupe, recovery after a restore |
| D2 | P2 | "No alerts" / "No findings" shown when risk or screening data was unavailable; unassessed rows looked clean | explicit unavailable states; `n/a` for unassessed rows |
| D3 | P2 | Anomaly baseline progress used a hardcoded 500 | the server's `min_reference` |
| D4 | P2 | Superseded sources counted as current; no authority, metadata version or graph coverage | labelled and shown |
| D5 | P3 | Accessibility: duplicate toggle names, no skip link or `<main>` landmark, very long alert lists | fixed; first 20 alerts with "show all" |
| D6 | P3 | Explorer links for mock data; replay label bug; clock skew applied inconsistently; overflow at 390 px; "-dirty" lost | fixed |

### Research claims (`62bf8bd`)

No published AP, interval or split changed; an independent reviewer recomputed every
headline number from the raw CSVs and saved predictions, and a scratch retrain reproduced
the live-computable XGBoost run exactly.

| # | Sev | Problem | Fix |
|---|---|---|---|
| M1 | P2 | The feature-subset "gain share" was XGBoost's mean gain per split over all trees, including 50 after early stopping; the "mostly shape" claim rested on it | share of total gain over the trees prediction uses (`fees` 0.08 → 0.19, `size` 0.26 → 0.46); claim corrected |
| M2 | P3 | Per-step P@20 used `head(20)`, not the tie-aware repository metric | tie-aware (0.5100 → 0.5133) |
| M3 | P3 | Below-chance late ranking (LR ROC-AUC 0.31 / 0.34 on steps 43–49) not reported; "pre-registered" overstated what the commit order proves | reported; amendments section |
| M4 | P3 | A third-party count presented as local data; missingness means not labelled as per-step vs pooled | corrected |

### Release blockers closed before approval (final v0.8.0 pass, baseline `69ec57d`)

| # | Sev | Component | Problem (root cause) | Fix | Test / evidence | Commit |
|---|---|---|---|---|---|---|
| B1 | MEDIUM | dashboard, edge | 500 answers had no security headers: Starlette sends them from `ServerErrorMiddleware`, outside every user middleware; the edge had no 500 handler (plain text). 413 and 429 answers already had them (checked) | both 500 handlers set the headers, `cache-control: no-store` and the request id, with a JSON body without internal details | `test_internal_errors_carry_the_security_headers`, `test_edge_internal_errors_are_json_with_security_headers` (fail on `69ec57d`) | `32f5c07` |
| B2 | MEDIUM | collector | A transient detail-fetch failure was final: one 503, timeout or 429 lost the transaction | bounded retries: transient = timeout, network, 429, 5xx, a first 404 (indexing lag); permanent = malformed/oversized payload, other 4xx. At most `fetch_max_attempts` (3; capped at 10), exponential backoff with jitter (2.5–5 s, 5–10 s), never past `max_queue_age_s`, at most `queue_max/2` pending, every attempt through the same token bucket; pending retries recorded on shutdown | 6 tests in `tests/test_live_faults.py` (recovery, cap, permanent, 404 once, age limit, shutdown; fail on `69ec57d`: settings absent); soak check "retries bounded" | `32f5c07`, `6bd4f52` |
| B3 | MEDIUM | CI | `pip install -e ".[dev]"` resolved unpinned versions on every run | `requirements/dev.lock.txt`: hash-locked, universal (3.11–3.13; Linux, macOS, Windows), build backend included; CI installs it with `--require-hashes` and the package with `--no-deps --no-build-isolation` | `test_ci_installs_only_hash_locked_dependencies`; CI green on Linux and Windows | `32f5c07` |
| B4 | MEDIUM | supply chain | No dependency update automation, no vulnerability audit | Dependabot (pip, npm, GitHub Actions, Docker base images; PRs only); CI `dependency audit` job: pip-audit on the dev, serve and live locks (from its own hash lock), `npm audit --audit-level=high`; the Docker job needs it | first run: no known vulnerabilities in any lock, 0 npm vulnerabilities | `32f5c07` |
| B5 | MEDIUM | release | The public image workflow ran on every push to `main` in parallel with CI: a commit failing CI was still published | `workflow_run` after CI **succeeded** for a push to `main`, same-repository only; builds and tags exactly the tested commit; first step re-checks the CI result through the API (manual dispatch too); `packages: write` and `actions: read` only in that job | `test_image_is_published_only_after_successful_ci` (fails on `69ec57d`), actionlint + shellcheck clean | `32f5c07` |
| B6 | LOW | performance | `stats(1440)` at the cap: a per-row linear scan over the fee bins and a JSON parse of every row's flags | binary search into the bins; empty flag lists not parsed. Output identical (hash of stats for 60/240/1,440 min equals the previous code's) | p50 220 → 160 ms, p95 293 → 211 ms (50k-row DB, warm); `test_fee_histogram_binary_search_matches_the_linear_definition` | `9065d7d` |

Final validation on `6bd4f52`: 90-minute fault-injection soak 18/18 (retries: 30 scheduled, 29
succeeded, no transient failure stored; docs/LIVE_CLOUD.md §11), real-systemd container
test 32/32 plus reboot, CI 8/8.

Re-measured and **not changed**: flag + fee range + sort by fee with a deep offset is
43–49 ms through the reader (the 210 ms of the earlier HTTP measurement did not reproduce
there); `sort=vsize` with a deep offset (117–134 ms) needs a full sort, and an index would
add a write-path cost for a rarely used sort, so it stays. Already in place and verified:
every action pinned to a commit (`test_every_action_is_pinned_to_a_commit`), workflow-level
`permissions: contents: read`, the image build content-reproducible (two builds, one
without cache: identical package set, application files and native libraries; image
digests differ by timestamps only).

### Accepted or open (after v0.8.0)

* **Open, LOW:** `restore` is refused while the collector runs but not while systemd is
  about to restart it (stop both units first, as the runbook says).
* **Open, LOW (performance):** `sort=vsize` with a deep offset (~120 ms) and the 24 h stats
  (~160 ms) at the cap; both are cached by the dashboard proxy.
* **Accepted:** uvicorn `limit_concurrency=32` (mitigated by Caddy timeouts); a single
  150 MB XML attribute still peaks near 470 MB (expat limitation; the source is a trusted
  HTTPS host and the refresh unit has a memory cap).
* **Accepted (second review):** on an unmigrated v0.7 database the risk summary reads as
  unavailable until the collector restarts and migrates (the upgrade restarts it); the SSE
  outer deadline adds at most 10 s of slack, so a slow remote poll can end a stream early
  (the browser reconnects); right after a reconnect the first re-sync delta of an
  out-of-sync backend is still taken as new arrivals (bounded by the queue); an old
  collector writing to a database already migrated by the new CLI (between `pip install`
  and the restart) adds alerts without `alert_txids` rows.
* **External:** Render's real `X-Forwarded-For` hop count, real mempool.space traffic, the
  Oracle VM and a 24-hour run on the public network were not available here.

## v0.7.0 risk intelligence audit

Problems found while building and testing the risk layers. Sources of evidence:
* the real OFAC files (official XML of 2026-01-30, mirror);
* a dashboard built against the API, which surfaced several API problems;
* the browser end-to-end and soak tests.

None of these problems was deployed.

| # | Sev | Component | Problem | Fix | Evidence |
|---|---|---|---|---|---|
| R1 | P1 | indicator parser | Parsing the official XML as a tree peaked at 457 MB on an 85 MB file, above the service's 400 MB cap (the real file is 122 MB), so the refresh would have been OOM-killed. A first streaming version still used 206 MB on the real file because the Locations and other list sections were never cleared | streaming parse that clears each finished record of the list sections: 16 MB peak | `test_sdn_xml_streams_from_a_file_with_bounded_memory`; real file measured |
| R2 | P1 | indicator parser | DTD/ENTITY declarations were refused only in the first 64 KB; a padded file could bypass the check | whole-file scan with overlapping chunks | `test_xml_with_dtd_or_entities_is_refused` (3 MB padding) |
| R3 | P1 | screening | Availability used the latest snapshot's retrieval time, so addresses listed long ago looked unavailable before each refresh | `available_at` = the first snapshot that contained the address | `test_screening_is_point_in_time` |
| R4 | P2 | indicator parser | An address shared by two SDN parties kept only the first party (real case: profiles 38419 and 38420) | parties merged into `details.parties`, labels joined | authentic excerpt test |
| R5 | P2 | indicator semantics | The OFAC entry date (the *party's* designation) was shown as the address listing date | `listed_on` null for OFAC; `party_designated_on` as context | `test_official_sdn_xml_parser_keeps_provenance_and_rejects_non_bitcoin` |
| R6 | P2 | anomaly detector | Default `min_reference` 200 could never reach `alpha` 0.002 (the smallest tail probability is 1/(n+1)), so no anomaly could ever be flagged | default 500; the configuration is validated | `test_detector_config_must_be_able_to_reach_alpha` |
| R7 | P2 | collector | The fetched row and its risk record were separate commits, so the event stream could deliver a row before its assessment (priority shown as null) | one transaction; risk writes in a savepoint, so a failed write keeps the observation | `test_failed_risk_write_keeps_the_observation_…`; e2e "every fetched row is assessed" |
| R8 | P3 | API | `risk_priority: null` was ambiguous; no source staleness or anomaly settings in the summary (the UI hardcoded them); retrospective alerts lacked label/programs; mirror titles repeated the source; "illicit" in layer text | `risk_assessed`, `stale`/`age_s`, settings, full retrospective evidence, source display names, the mirror carries no per-address label, wording | `test_risk_routes_…`, `test_alert_titles_…`, `test_retrospective_screening_…` |
| R9 | P3 | indicator refresh | The official source cannot be fetched from the build environment (proxy 403) | `--fallback` to the mirror; failures reported, previous version kept, exit 1 | `test_indicator_update_falls_back_and_never_breaks_on_failure` |
| R10 | P3 | address validation | OFAC files a TRON address under XBT | strict Bitcoin validation; rejected with a reason | real file: 1 rejected entry |

Accepted, not changed:
* The alert status history (with operator notes) is CLI-only, by design.
* `available_at` is this deployment's retrieval time, so it lags OFAC's publication by up
  to a day.
* Graph relations see only the sampled parents (coverage is reported).

## v0.6.0 engineering audit (live monitor, cloud kit, research claims)

Four independent read-only reviews (ingestion correctness against mempool's backend source,
security and API, frontend with fault injection in a browser, ML scientific claims),
followed by fixes, a real-systemd container test, a browser end-to-end test and fault-injection soak runs (6 min and 110 min). Every fixed item has a regression test or a recorded check; nothing
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
| 19 | P3 | collector | (Found by the 110-min soak) the database could end above `retention_rows` (3,026 / 3,000) | retention runs every 60 s; the process stopped between prunes | prune on clean stop; documented bound while running (cap + one minute of fetches) | `test_clean_stop_leaves_the_database_within_the_row_cap`; soak check |

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
