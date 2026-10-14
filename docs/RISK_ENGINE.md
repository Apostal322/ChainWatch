# ChainWatch Risk Intelligence: contracts and design

The live monitor assesses every fetched transaction with three **separate** layers and
never blends them into one score:

| Layer | Produces | Means | Never means |
|---|---|---|---|
| A. Source-backed screening | `SCREENING_MATCH` with evidence (address, role, source, version, reference) | an enabled source (e.g. OFAC SDN) lists an address the transaction spends from or pays to | proof of fraud or of who controls the funds; a sanctions listing is a legal designation |
| A'. Observed-graph relation | `RELATION_FOUND` with the path `listed address -> parent tx -> this tx` | within this collector's sampled observations, the transaction spends an output of a transaction that spent from a listed address | ownership, common control or culpability; unobserved parents are not checked |
| B. Behavioural anomaly | `ANOMALY` (`notable`/`strong`) with per-measurement explanations | measurements in the tail of recently sampled traffic | wrongdoing: batch payouts, consolidations and CoinJoins are all unusual and legitimate |
| C. Supervised model | `NO_VALIDATED_MODEL` for every transaction | no model is validated for live data (docs/RISK_DATA_FEASIBILITY.md) | it never falls back to the historical Elliptic++ models |

Code: `src/chainwatch/risk/`. Tests: `tests/test_risk.py`. Data decisions:
[RISK_DATA_FEASIBILITY.md](RISK_DATA_FEASIBILITY.md).

## 1. Event-time model (point-in-time correctness)

| Time | Field | Set when | Used for |
|---|---|---|---|
| Blockchain event | `status.block_time` (Esplora; not stored as a feature) | a block confirms the tx | nothing in scoring |
| First observation | `first_seen` (tx row, record `observed_at`) | the collector receives the txid on the WebSocket | ordering, latency |
| REST enrichment | `fetched_at` | the details are fetched (up to `max_queue_age_s` later) | features are computed at this time |
| Feature computation | = `fetched_at` (features carry `schema_version`) | immediately after the fetch | |
| Indicator availability | `indicators.available_at` = the `retrieved_at` of the first snapshot that contained the address; `removed_at` = retrieval of the first snapshot without it | `chainwatch-live indicators update` | screening at T uses only listings with `available_at <= T < removed_at` |
| Source publication | `indicator_versions.published_at` (e.g. OFAC `DateOfIssue`), `details.party_designated_on` | parsed from the source | shown as context only: OFAC's date belongs to the *party*, not to when an address was added |
| Evaluation | `evaluated_at` (record) | the engine assesses the tx | all layers use this instant |
| Lifecycle | `tx.lifecycle`, `lifecycle_at`, `replaced_by` | later `mined` / `removed` / `replaced` WebSocket events | display only; **never** a feature, and past records are never rewritten |
| Retrospective match | alert `retrospective = true` | a later snapshot lists an address of an earlier observed tx | a separate alert; the original record keeps what was known then |
| Supervised labels | none exist | | |

Guarantees and their tests:
* Screening ignores listings not yet available or already removed at `evaluated_at`
  (`test_screening_is_point_in_time`).
* The anomaly reference window contains only transactions fetched **before** the scored one
  (`test_anomaly_reference_window_excludes_the_scored_and_later_transactions`; the collector
  calls `score` before `update`, and a restart rebuilds the window from stored rows ordered
  by `fetched_at`).
* Retrospective screening never changes a stored record
  (`test_retrospective_screening_does_not_rewrite_history`).
* Lifecycle events update state columns only
  (`test_collector_screens_relates_and_tracks_lifecycle`).

Approximation stated plainly: `available_at` is when *this deployment* fetched a snapshot,
which is later than OFAC's publication by up to the refresh interval (daily). Replaying
history with snapshots fetched today would be optimistic; only snapshots retrieved at the
time reflect what was known.

## 2. Indicator sources

| Source id | What | Licence | Per-address provenance |
|---|---|---|---|
| `ofac-sdn` | official `sdn_advanced.xml` (~122 MB), parsed as a stream | US government work, public domain | party names (all parties if several), SDN profile ids, programmes, party designation date |
| `ofac-sdn-mirror` | third-party nightly extraction of the XBT addresses (github.com/0xB10C/...) | data public domain; tool MIT | none (addresses only) |

Each snapshot is versioned by the SHA-256 of its content (`indicator_versions`). Unchanged
content only refreshes `checked_at` (freshness). A source not refreshed for
`indicator_max_age_days` (default 7) is reported **stale**. Before the first snapshot, screening
returns `SCREENING_DATA_UNAVAILABLE`, never "no match".

External data is untrusted: content is parsed (never evaluated), DTDs and entities are
refused, sizes are capped (400 MB), downloads are HTTPS-only, and every address is validated
(base58check version 0x00/0x05; bech32/bech32m with HRP `bc`). Invalid entries are rejected
with a reason; for example OFAC files a TRON address under XBT.

Verified on the real 2026-01-30 file: 516 valid addresses, 56 parties, 1 rejected entry, 24 s,
16 MB peak Python memory. A byte-identical public-domain excerpt is the test fixture
`tests/fixtures/ofac/sdn_advanced_excerpt.xml`.

## 3. Live feature schema `live-features/2`

All values are computed from one Esplora transaction at `fetched_at`; amounts are integer
satoshis in [0, MAX_MONEY]; anything outside the documented ranges is a malformed payload.

| Feature | Unit / type | Null when | Notes |
|---|---|---|---|
| `size`, `weight`, `vsize` | bytes, weight units, vbytes | never | vsize = ceil(weight/4) (BIP141) |
| `fee`, `fee_rate` | sat, sat/vB | never | fee from the payload |
| `n_inputs`, `n_outputs`, `n_parent_txs` | counts | never | |
| `input_value_total` | sat | any prevout value missing | |
| `output_value_total`, `max_output_value`, `output_values[:25]` | sat | never | sums include change: **not a payment amount** |
| `n_dust_outputs`, `n_op_return`, `max_equal_value_outputs`, `n_output_types` | counts | never | equal-value groups ignore dust |
| `rbf_signal`, `segwit`, `locktime_enforced`, `coinbase` | bool | never | RBF: an input sequence <= 0xfffffffd |
| `version`, `locktime`, `locktime_kind` | int, int, none/height/time | never | |
| `confirmed_at_fetch` | bool | status missing | state at fetch time; **not** an input to any layer |
| `input_types`, `output_types` | counts per script type | never | |
| `input_refs[:25]` | `txid:vout` | never | observable spends; no ownership inference |

Addresses are extracted separately for screening (`features.extract_addresses`, up to 500
per side) and stored in `tx_address`. Scripts without an address (P2PK, bare multisig,
OP_RETURN, non-standard) and missing prevouts are counted in the screening `coverage`.

## 4. Screening outcomes

`SCREENING_MATCH` (direct: input = "spends from", output = "pays to"),
`NO_KNOWN_SCREENING_MATCH` (only the enabled sources; not proof of legitimacy),
`SCREENING_DATA_UNAVAILABLE` (no source loaded at T), `INSUFFICIENT_INFORMATION` (no
address could be screened). Each result lists the sources with version, last check, age and
staleness, and the coverage counts.

## 5. Observed-graph relations

Distance 2 only: a parent transaction **observed by this collector** whose assessment had
an input-side match. Coverage (`parents`, `parents_observed`) is reported, because the sample
sees only a few percent of parents. No clustering, no multi-hop search, no address reuse
inference.

## 6. Behavioural anomalies (`robust-tail/1`)

Eight measurements: fee rate (both tails), vsize, inputs, outputs, sum of outputs, dust
outputs, largest equal-value output group, output script-type count. For each, the empirical
tail probability `p = (#reference >= x + 1)/(n + 1)` against the previous `window` = 2,000
fetched transactions, plus a robust z-score. A measurement is flagged when `p <= 0.002` and
`|z| >= 3`; `strong` = two or more flags or a value outside the reference range. Fewer than
500 reference transactions gives `INSUFFICIENT_BASELINE` (with fewer, `p` could never reach
0.002; the configuration is validated). The score is `-log10(p)`: rarity in recent traffic,
not a probability of anything illicit. Cost: about 2 ms per assessment with a full window
(screening against 531 indicators included).

`scripts/anomaly_study.py` replays a collected database and compares this detector with
IsolationForest and fixed rules (alert volume, hourly stability, sensitivity, drift,
agreement, CPU), and shows that known-benign shapes (batch payout, consolidation,
CoinJoin-like) are flagged: anomalies are leads for review, nothing more. **No study results
on real traffic exist yet**; the build environment cannot reach mempool.space.

## 7. Supervised layer gate

A model is used only if its card states the live `feature_schema`, `validated_for_live: true`,
an evaluation with a time-separated independent holdout, pre-registered primary metrics and
the label source, and the artifact hash matches. No model passes; the layer abstains with
`NO_VALIDATED_MODEL`. The offline feature-subset experiment
([LIVE_FEATURE_SUBSET_PROTOCOL.md](LIVE_FEATURE_SUBSET_PROTOCOL.md)) cannot pass it by
construction.

## 8. Review priority and alerts

`priority` is ordinal and documented as "not a probability of wrongdoing":
1 source match, 2 observed-graph relation, 3 strong anomaly, 4 notable anomaly, none.
`insufficient_evidence` is true when screening could not run (no source, no screenable
address, error) **and** the anomaly layer had no baseline or failed.

Alerts are raised for priorities 1–3 and grouped: `screening:<source>:<address>`,
`relation:<source>:<address>`, `anomaly:<measurement>:<hour>`, and for retrospective matches
`retro:<source>:<address>`. A repeat of the same transaction is ignored. A repeat of a
dismissed alert is counted as `suppressed` (no new notification). A repeat of a resolved
alert reopens it. Statuses: `new`, `under_review`, `dismissed`, `resolved`,
`insufficient_evidence`. Changes are logged in `alert_events`. Over HTTP the queue is
**read-only**: statuses are changed on the collector host with
`chainwatch-live alerts set-status --id N --status S --note "..."`. No external notification
is sent anywhere.

## 9. Failure isolation and operations

* Each layer runs in its own guard: an exception becomes `status: ERROR` plus an entry in
  `errors`, and the other layers still report. A failure to assess or store a record is
  counted (`risk_errors`) and the observation is kept.
* Indicators are refreshed daily by `chainwatch-indicators.timer` (official XML, falling back
  to the mirror). A failed refresh keeps the previous version and exits 1. The collector
  reloads its screening index when a new version appears (checked every heartbeat).
* API: `/v1/live/risk/summary`, `/v1/live/findings`, `/v1/live/alerts`, and `risk` +
  `lifecycle` in `/v1/live/transactions/{txid}`. All are read-only, token-protected on the
  edge API, and proxied with the same cache and limits as the other live routes.

## 10. Not claimed

* No fraud detection, no fraud probabilities, no labels of people or organisations.
* No complete coverage: a sample of the public mempool.space view, with gaps recorded.
* No address clustering, so wallets that are not listed address by address are not found.
* No validated supervised model.
* No real-traffic results for the anomaly layer yet (the pipeline exists; the data must come
  from a running collector).
