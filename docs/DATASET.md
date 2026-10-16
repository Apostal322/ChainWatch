# Dataset card: Elliptic++ (transactions)

Everything below was verified against the actual files. Where a number appears, it
was computed by `chainwatch prepare` / `chainwatch eda` (see `reports/eda/`).

## Source and integrity

| | |
|---|---|
| Repository | <https://github.com/git-disl/EllipticPlusPlus> ("Transactions Dataset" folder) |
| Paper | Elmougy & Liu, *Demystifying Fraudulent Transactions and Illicit Nodes in the Bitcoin Network for Financial Forensics*, KDD 2023. <https://doi.org/10.1145/3580305.3599803> |
| Hosting | CSVs are stored with Git LFS on GitHub; the authors also mirror them on Google Drive |
| Integrity | SHA-256 of each file equals the Git LFS object ID in the repository's pointer files (hard-coded in `src/chainwatch/data/download.py`, checked by `download`). `prepare` records the SHA-256 and size of each file actually read, the published digest and `matches_published` in `manifest.json` (`manifest_version` 2); run metadata copies these plus the processed-data fingerprint. Manifests and runs from before this change recorded the published digests regardless of the files on disk, so they do not fingerprint the data. |

### Licensing and redistribution

The Elliptic++ repository has **no LICENSE file**, and GitHub's API reports no licence.
Without an explicit grant, redistribution rights cannot be assumed. Elliptic++ also
builds on the original Elliptic dataset (Weber et al., 2019), whose own terms apply to
the features it inherits. For these reasons:

* the raw and processed data are **excluded from version control** (`.gitignore`);
* users download the files themselves from the authors' sources;
* only aggregate statistics and figures derived from the data are committed under `reports/`.

If you plan anything beyond personal research use, contact the dataset authors.

**Screenshots.** Every screenshot in the current tree (`docs/images/demo_*.png`) was
captured from the synthetic demo and contains no Elliptic++ data. Earlier commits on the
development branch contained four screenshots of a local real-data run (aggregate metrics
plus a small excerpt of transaction IDs, scores, labels and one transaction's augmented
features). They were removed from the tree; see docs/RELEASE_READINESS.md for how to keep
them out of the published history. Real-data results are shown only through the
aggregate figures under `reports/`.

## Files used for transaction classification

| File | Shape | Columns |
|---|---|---|
| `txs_features.csv` | 203,769 x 184 | `txId`, `Time step`, `Local_feature_1..93`, `Aggregate_feature_1..72`, 17 augmented features |
| `txs_classes.csv` | 203,769 x 2 | `txId`, `class` |
| `txs_edgelist.csv` | 234,355 x 2 | `txId1`, `txId2` (directed money flow) |

The "Actors" (wallet address) files are not used in Phase 1.

**The feature file does not contain the label.** The authors' tutorial notebook attaches
labels with `df.insert(..., value=classes['class'])`, i.e. by *row position*. The rows
happen to be aligned in the published files, but ChainWatch joins on `txId` and a test
(`test_labels_joined_by_id_not_position`) guards against positional mistakes.

### Labels

| raw `class` | meaning | count | ChainWatch `label` |
|---|---|---|---|
| 1 | illicit | 4,545 | 1 |
| 2 | licit | 42,019 | 0 |
| 3 | unknown | 157,205 | -1 (excluded from supervised train/eval) |

"Unknown" means *not labelled*, not "probably licit". Treating unknown as licit would
inject label noise of unknown size and direction. Unknown transactions are kept in the
processed table (they are graph neighbours and will matter for semi-supervised/GNN
work) but `temporal_split` drops them and `make_xy` refuses them.

### Features

* **Local (93):** properties of the transaction itself (anonymised by Elliptic).
* **Aggregate (72):** one-hop neighbourhood statistics (anonymised by Elliptic).
* **Augmented (17, added by Elliptic++):** `in_txs_degree`, `out_txs_degree`, `total_BTC`,
  `fees`, `size`, `num_input_addresses`, `num_output_addresses`,
  `in_BTC_{min,max,mean,median,total}`, `out_BTC_{min,max,mean,median,total}`.

Local and aggregate features are already standardised (mean ~= 0 over **all** 203,769
rows). The augmented features are raw, non-negative and heavy-tailed (for example,
`size` ranges up to 445,268 bytes).

### Time

`Time step` takes values 1 to 49. According to the original Elliptic description, each
step covers roughly two weeks and contains a connected component of transactions that
appeared on the blockchain within less than three hours. There are no finer-grained
timestamps.

## Verified structural properties

| Property | Value | Why it matters |
|---|---|---|
| Duplicate `txId` | 0 | ids can be used as keys |
| Edges between different time steps | **0** | splitting by time step cannot leak through graph neighbours (checked on every run by `count_cross_split_edges`) |
| Self-loops / duplicate edges | 0 / 0 | |
| Edges referencing unknown ids | 0 | |
| Isolated transactions | 0 | every node has at least one edge |
| Published degrees vs. edge list | identical for all 202,804 rows that have them | missing degrees can be recomputed exactly |
| Exact duplicate feature vectors | 32 rows, none with conflicting labels | negligible |

## Data-quality findings and leakage risks

1. **Missingness is label-dependent.** 965 rows have *all* 17 augmented features
   missing: 519 licit, 446 unknown, **0 illicit**. A model given a missing-value indicator
   (or rows dropped by `dropna()`, as in the authors' notebook) would learn this
   dataset-construction artefact. Mitigations: no missing indicators are added; degrees
   are recomputed exactly from the edge list; the remaining 15 columns are filled with the
   training-split median. A tree model could still, in principle, recognise the exact
   imputed values. This residual risk is small (519 of 46,564 labelled rows) and is
   documented rather than hidden.
2. **Global standardisation by the publisher.** Local and aggregate features were
   standardised with statistics that include future time steps. This cannot be undone
   because the raw values are not published. It is an affine transform, so tree models
   are unaffected and logistic regression re-standardises on the training split. It is a
   leak in principle but carries no label information.
3. **Aggregate features summarise neighbours.** Because all published edges stay within
   a single time step, a neighbour of a test transaction in the edge list is always
   another test-period transaction. According to the publisher, the features use
   neighbour *features*, not neighbour *labels*. **Caveat:** the 72 aggregate features are
   publisher-computed, and the neighbourhood they aggregate over is not fully documented.
   They cannot be verified to use only within-step, pre-step-end information. A partial
   check (not yet run): recompute one-hop aggregates of the local features from the
   published edge list and measure how closely they reproduce the published aggregate
   columns.
4. **Label provenance is opaque.** Labels come from Elliptic's heuristics. Illicit means
   linked to known illicit categories (scams, ransomware, malware, darknet markets, and
   so on). Licit and illicit labels are therefore not ground truth about intent, and the
   labelled subset is not a random sample of transactions.
5. **Strong temporal drift.** The illicit share of labelled transactions ranges from
   0.3% (step 46) to 36% (step 13), and falls sharply after step 42. Weber et al. (2019)
   attribute this to a dark-market shutdown in the original Elliptic data. See
   `reports/eda/figures/illicit_rate_by_time_step.png`.
6. **Clock-like features (found in the Phase 2 audit).** Aggregate_feature_8, 10, 43, 7
   and 46 have |Spearman ρ| between 0.95 and 0.997 with the time step on steps 1–34
   (`time_proxy_features` in `features/preprocessing.py`). A model can use them to infer
   the period, much as it could use `Time step` itself, which Phase 1 excluded on
   purpose. In the exploratory ablation, removing them changed pooled AP by ≤ 0.004
   overall and did not change the static model's collapse
   ([TEMPORAL_ROBUSTNESS.md §4.4](TEMPORAL_ROBUSTNESS.md)). They are kept in the default
   feature set, and the ablation flag `--drop-time-proxies` is available.
7. **Every time step is identifiable from its features.** A gradient-boosted domain
   classifier separates any single step from steps 1–29 with AUC ≥ 0.997, even without
   the time proxies. Step-level domain AUC is therefore not a usable shift measure on
   this dataset.
