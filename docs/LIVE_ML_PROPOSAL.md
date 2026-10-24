# Proposal: a genuine live-data ML model (not implemented)

Phase 5 collects live, observation-time features but **trains no model**, because live data
has no labels yet. This proposal describes how a properly evaluated model could be added.
It reuses the temporal-evaluation discipline from Phases 1–2: chronological splits,
explicit label latency, pre-registration, and a sealed holdout.

## 1. Why the Elliptic++ models cannot be reused

They were trained on 165 anonymised, publisher-standardised features of 2019-era
transactions, aggregated over the full graph of a two-week time step. None of these
features can be computed for a transaction seen in today's mempool, and the label
semantics ("illicit" per Elliptic's heuristics) do not transfer. Retraining is the only
honest route.

## 2. Stage A (recommended first): a task with free, objective labels

**Task:** for an unconfirmed transaction observed at time *t*, predict whether it confirms
within *k* blocks (for example k = 1, 3, 6), or whether it is replaced or evicted first.

* **Labels arrive later, for free.** Re-query the collected txids after *k* blocks (or
  subscribe to `mined` and `replaced` events) and store the outcome in a separate table
  with its own timestamp. Features are never updated after `fetched_at`, but `fetched_at`
  is not the first observation (see "Collector gaps" below), so the observation time
  must be fixed explicitly.
* **The label latency is explicit:** a label for *t* becomes usable after *t + k* blocks.
  This is exactly Phase 2's setting.
* **Baselines to beat:** the fee rate relative to the mempool's projected-block minimum fee
  at time *t* (available from mempool.space `projected-block` data; the collector does
  **not** record it yet), and logistic regression on fee rate.
* **Evaluation:**
  * a rolling-origin backtest over days, with block bootstrap over time;
  * calibration (reliability, ECE), because a probability of confirmation is useful only if
    it is calibrated;
  * analysis of fee-market regime shifts (fee spikes) as distribution shift.
* **Value:** it validates the pipeline, label joins and temporal evaluation end to end on
  real data without any claims about crime.

### Collector gaps to close before a Stage A study

The current collector (Phase 5) was built for monitoring, not for this study:

* **`confirmed_at_fetch` leaks the label.** It is the status at `fetched_at`, which can be
  up to `max_queue_age_s` (300 s by default) after `first_seen`. For a "confirms within
  *k* blocks of *t*" task, either exclude it from the features, or define *t* as
  `fetched_at` (not `first_seen`) and count blocks from there.
* **No per-row feature-schema version.** Rows do not record which feature code produced
  them; add a version column before collecting study data.
* **`fee_rate_outlier` is not reproducible from stored rows.** It depends on the
  collector's in-memory rolling window (the last 500 fee rates), which is not persisted
  and resets on restart. Exclude it, or persist the window statistic per row.
* **Retention is too short.** Defaults keep 7 days (and 20,000 rows locally, 50,000 rows
  in `configs/live-cloud.yaml`); `minute_stats` keeps 24 h. A study of at least 4 weeks
  needs these raised (and disk sized) for its duration.
* **Selection bias from drops.** Back-pressure drops, queue expiry and fetch failures
  cluster in time (bursts, fee spikes, outages), so busy periods, the regimes of most
  interest, are under-sampled. Persist per-minute drop and coverage counts for the
  study period and report coverage by period.
* **No projected-block minimum fee** is recorded, so the main baseline cannot be computed
  from collected data yet.

## 3. Stage B: risk-related labels (only with careful sourcing)

Possible label sources, all sparse, delayed and biased:

| Source | Notes |
|---|---|
| OFAC SDN list (digital currency addresses) | public US-government data; addresses only; exposure is better expressed as a **rule** ("pays a listed address") than as a model |
| Public research datasets of ransomware payment addresses | check each licence; coverage is small and skewed toward known families |
| Public law-enforcement seizure announcements | very sparse; manual curation |
| Commercial attribution data | not zero-cost; licensing usually forbids redistribution |

**Constraints before any model is trained:**
* **Labels are about addresses and events, not people.** No identity inference and no
  wallet clustering presented as fact.
* **Positives are rare and found late.** Report precision at an analyst review budget
  (as in Phase 2) and the base rate. Never report accuracy.
* **Unknown is not negative.** Unlabelled transactions are excluded or modelled as
  positive-unlabelled data, the same rule as for Elliptic++'s "unknown" class.
* **Graph features need more API calls** (k-hop ancestors), which conflicts with the
  zero-cost, rate-limited design. A self-hosted node with an indexer (mempool or electrs
  on own hardware) would be needed. That is local and still free, but it is new
  infrastructure, so it needs a separate decision.

## 4. Protocol (applies to both stages)

1. Freeze the feature set and code version and **pre-register** the splits, metrics and
   decision rules before looking at the outcomes, and publish that protocol (a
   timestamped commit or registry entry) so its timing can be verified.
2. Collect at least 4 weeks of observations. Reserve the **last week as a sealed holdout**
   that is not inspected until the final evaluation. This avoids repeating Phase 1, where
   the test period became exploratory.
3. Use rolling-origin evaluation with explicit label latency, report confidence intervals
   from a time-block bootstrap, and include the negative results.
4. Publish a model card: data window, coverage (sample rate, fetch failures), known
   biases, intended use, and "not for decisions about individuals".
5. Train offline, locally or in CI on GitHub's runners. **Never train on the Render
   Free instance.** Serve only frozen artifacts through the existing registry pattern
   (provenance and hashes), preferably in a pickle-free format.

## 5. Stop conditions

Do not ship a model if:
* it does not beat the fee-rate baseline outside the holdout's confidence intervals;
* its calibration error exceeds the agreed bound;
* label coverage is too small for stable estimates (for Stage B, fewer than about 100
  positive events in the holdout).

Report the outcome either way.
