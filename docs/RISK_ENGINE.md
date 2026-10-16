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

Each snapshot is versioned by the SHA-256 of its content (`indicator_versions`) and **diffed
against the active listings**: new addresses become available at its retrieval time, missing
ones are closed. Content that reverts to an earlier version therefore re-lists what that
version listed (v0.7 treated a previously seen hash as "unchanged"). Nothing to change and the
same content as the source's latest version only refreshes `checked_at` (freshness).

* **Refusals.** A snapshot with no valid address, or one that would delist more than 25% of
  the active listings (beyond 5), is refused (`SnapshotRejected`): an error page, a truncated
  download or a schema change must not silently empty the list. The CLI then tries the
  fallback; `--allow-mass-removal` accepts a genuine mass delisting after checking it.
* **One list, two channels.** `ofac-sdn` and `ofac-sdn-mirror` share the authority
  `ofac-sdn`. A newer snapshot from either closes the listings the other copy still has but
  the newer one does not, so a superseded copy cannot keep a delisted address alive.
  Screening reports one match per authority and address (the official file preferred, for
  its party metadata). The older copy is reported `superseded_by`, not stale.
* A source not refreshed for `indicator_max_age_days` (default 7) is reported **stale**.
  Before the first snapshot, screening returns `SCREENING_DATA_UNAVAILABLE`, never "no match".
* The mirror is a third-party extraction, not an authoritative copy: on 2026-01-30 it listed
  531 addresses against 516 valid ones in the official file (16 added, 1 removed between the
  two). Every record shows which source matched.

External data is untrusted: content is parsed (never evaluated), only UTF-8 is accepted (a
UTF-16 document could hide a DOCTYPE from the byte scan), DTDs and entities are refused anywhere
in the file, sizes are capped (400 MB, and 10,000 addresses per party), every finished record
is freed while streaming, downloads are HTTPS-only on every redirect hop, and every address is validated
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

Distance 2 only: a parent transaction **observed by this collector** that spent from an
address listed *at the child's evaluation time*. The parents' stored input addresses are
screened again (a listing added after the parent was assessed counts; a delisting does too),
at most 200 parents per transaction, one relation per (parent, address, authority).
Coverage (`parents`, `parents_checked`, `parents_observed`) is reported, because the sample
sees only a few percent of parents. No clustering, no multi-hop search, no address reuse
inference. A relation that only appears after the child was assessed is not raised
retrospectively.

## 6. Behavioural anomalies (`robust-tail/2`)

Eight measurements: fee rate (both tails), vsize, inputs, outputs, sum of outputs, dust
outputs, largest equal-value output group, output script-type count. For each, the empirical
tail probability `p = (#reference >= x + 1)/(n + 1)` against the previous `window` = 2,000
fetched transactions, plus a robust z-score. A measurement is flagged when `p <= 0.002` and
`|z| >= 3`; `strong` = two or more flagged measurements (robust-tail/1 also counted one value
outside the reference range, which with a small window is true of every flag: on pure noise
all flags were "strong"). Fewer than 1,000 reference transactions gives
`INSUFFICIENT_BASELINE`: the two-sided fee test needs `2/(n+1) <= alpha`, and the
configuration is validated (alpha in (0, 1), finite `min_z`, `min_reference <= window`). On the
VM (12 fetches per minute) a fresh install needs about 83 minutes to build the baseline; a
restart rebuilds it from stored rows of the current feature schema. The score is `-log10(p)`: rarity in recent traffic,
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
construction. Its results ([LIVE_FEATURE_SUBSET_RESULTS.md](LIVE_FEATURE_SUBSET_RESULTS.md)):
on Elliptic++ test steps 35-49, XGBoost on the 16 live-computable features reaches AP 0.646
against 0.797 with all 182 features (paired difference -0.151, 95% interval [-0.216, -0.089]).
Like every model it collapses after step 42 (AP 0.028). Exploratory and rejected for
promotion.

## 8. Review priority and alerts

`priority` is ordinal and documented as "not a probability of wrongdoing":
1 source match, 2 observed-graph relation, 3 strong anomaly, 4 notable anomaly, none.
`insufficient_evidence` is true when screening could not run (no source, no screenable
address, error) **and** the anomaly layer had no baseline or failed.

Alerts are raised for priorities 1–3 and grouped: `screening:<authority>:<role>:<address>`,
`relation:<authority>:<address>`, `anomaly:<measurement>:<hour>`, and for retrospective
matches `retro:<authority>:<role>:<address>`. A repeat of the same transaction is ignored
(`alert_txids` remembers every transaction an alert counted; the API shows the latest 50).

Retrospective screening runs when a snapshot is stored (CLI) and again when the collector
loads it: every stored transaction that involves a newly available address and whose own
record has no match for it under the same authority gets a retrospective alert. That covers
transactions observed before the listing and those assessed in the seconds between the
snapshot being stored and the collector loading it. Only retained rows (the retention window)
can be screened retrospectively. A repeat of a
dismissed alert is counted as `suppressed` (no new notification). A repeat of a resolved
alert reopens it. Statuses: `new`, `under_review`, `dismissed`, `resolved`,
`insufficient_evidence`. Changes are logged in `alert_events`. Over HTTP the queue is
**read-only**: statuses are changed on the collector host with
`chainwatch-live alerts set-status --id N --status S --note "..."`. The status history
(`alert_events`, including operator notes) is deliberately not served over HTTP; read it with
`chainwatch-live alerts history --id N`. No external notification is sent anywhere.

## 9. Failure isolation and operations

* Each layer runs in its own guard: an exception becomes `status: ERROR` plus an entry in
  `errors`, and the other layers still report. A failure to assess or store a record is
  counted (`risk_errors`) and the observation is kept.
* An observation and its risk record (addresses, record, alerts) are committed in one SQLite
  transaction; the risk writes sit in a savepoint, so a failed write rolls back only them.
  Readers and the event stream therefore never see a fetched row whose assessment is
  pending. List rows carry `risk_assessed` so that "assessed, no finding" (`risk_priority`
  null) is distinguishable from "not assessed".
* Indicators are refreshed daily by `chainwatch-indicators.timer` (official XML, falling back
  to the mirror). A failed or refused refresh keeps the previous version and exits 1. The
  collector reloads its screening index when a new version appears (checked every heartbeat)
  and screens the transactions it assessed meanwhile retrospectively.
* If the risk engine cannot be built at start, the collector still collects and reports
  `risk_unavailable`; rows are then marked not assessed.
* API: `/v1/live/risk/summary` (per-source `age_s` and `stale` by the collector's
  `indicator_max_age_days`; the anomaly `min_reference`, `window` and `alpha`; alert counts
  are all-time, marked `alerts_by_status_scope`), `/v1/live/findings` (with
  `insufficient_evidence` and `errors`), `/v1/live/alerts`, and `risk` + `lifecycle` in
  `/v1/live/transactions/{txid}`. All are read-only, token-protected on the
  edge API, and proxied with the same cache and limits as the other live routes.

## 10. Not claimed

* No fraud detection, no fraud probabilities, no labels of people or organisations.
* No complete coverage: a sample of the public mempool.space view, with gaps recorded.
* No address clustering, so wallets that are not listed address by address are not found.
* No validated supervised model.
* No real-traffic results for the anomaly layer yet (the pipeline exists; the data must come
  from a running collector).
