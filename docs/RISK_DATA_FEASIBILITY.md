# Risk data feasibility: sources, labels, licences and the supervised-learning decision

**Research summary for ChainWatch's risk intelligence** (2026-10-14). It decides what each
detection layer can honestly be built on:

| Layer | Decision | Basis |
|---|---|---|
| A. Source-backed screening | **Build**: exact address matches against OFAC SDN (official XML; a third-party nightly extraction as fallback) | public-domain, versioned, verified on the real 2026-01-30 file (section 3) |
| B. Behavioural anomalies | **Build**, unsupervised and interpretable; no labels needed or claimed | docs/RISK_ENGINE.md section 6 |
| C. Supervised risk model | **Not feasible for live scoring** with any legitimate public data today. Offline diagnostic only (docs/LIVE_FEATURE_SUBSET_PROTOCOL.md) | decision gate, section 2 |

Evidence tags: **[local]** checked from this repository's build environment by downloading the
data (via raw/media.githubusercontent.com); **[search]** from search-result snippets of the
cited page (Kaggle, UCI, Zenodo, arXiv, elliptic.co, treasury.gov and chainabuse were not
reachable from the build environment); **unverified** where no source confirmed a claim.
Re-check licences and terms before relying on them.

Researched on 2026-10-14 for two uses: (A) exact-address risk screening, and (C) a supervised
classifier whose features ChainWatch can compute for **live, sampled mempool transactions** at
observation time. The live features are those in `src/chainwatch/live/features.py`: one
Esplora tx JSON gives fee, vsize/weight, fee rate, n_in/n_out, input/output script types,
output values, prevout addresses/values, locktime, nSequence/RBF, OP_RETURN and dust counts,
and parent txids. There is no address history, no multi-hop graph and no clustering.

How to read the evidence tags:
* **[local]** I downloaded and checked the data myself from this sandbox, via raw/media.githubusercontent.com.
* **[search]** The claim comes from a web-search snippet of the cited page. Kaggle, UCI, Zenodo, arXiv, elliptic.co, treasury.gov and chainabuse were unreachable by curl, and WebFetch could not resolve them.
* **unverified** No source confirmed the claim.

## 1. Comparison matrix

| Source | What is labelled | Level | Ground truth from | Timestamps | Unknown class? | Features reproducible from public chain at prediction time? | Live-compatible with ChainWatch's sampled mempool features? | Licence / MIT-repo use | Size / compute | Known biases |
|---|---|---|---|---|---|---|---|---|---|---|
| **Elliptic (2019)** [E1–E5] | tx illicit / licit | tx | Elliptic's attribution of tx to entities (scams, malware, ransomware, darknet...) vs licit services, details unpublished [E3] | 49 anonymised ~2-week steps, no dates [E4] | **Yes**, 77% unknown (4,545 illicit, 42,019 licit of 203,769) [E3] | **No.** 165 anonymised, standardised features (local + 1-hop aggregates); construction not disclosed and reverse-engineered only partially by third parties [E5] | **No** | Kaggle lists **CC BY-NC-ND 4.0** [search, E1]: non-commercial, no derivatives. **Download by user only; commit nothing** | 203,769 nodes / 234,355 edges [E3]; Elliptic's own CSV size unverified (Elliptic++'s superset txs_features.csv is 694.8 MB [local]); laptop-scale | Regime shift at step 43 (attributed to darknet-market closures, AML tightening and the 2018–19 bear market [E5, arXiv 2604.19514]): ChainWatch measured AP 0.917 → 0.043 (README). Split leakage reported [E5]. Labels are one vendor's heuristic. 2016–2017 era, no SegWit/Taproot |
| **Elliptic++ (git-disl, KDD'23)** [P1–P3] | tx (as Elliptic) **plus wallet addresses** illicit / licit / unknown | tx + address | tx labels from Elliptic; address labels derived from them (method in paper, unverified here) | 49 steps; address features include `first/last_block_appeared_in` (block heights, e.g. 439,586–485,959 in sample rows) [local] | **Yes.** Actors: 14,266 illicit, 251,088 licit, 557,588 unknown (822,942 addresses) [local, P1] | tx: **No** (Elliptic's anonymised features + 17 raw augmented). Address: **partly**: 56 interpretable features (BTC sent/received, fees, lifetimes, tx counts) with **real addresses** [local], but computed over the dataset's tx subset and time window, not the full chain | **No** for tx. Address features would need full address history (many API calls), which the sampled collector does not have | **No LICENSE file** (raw LICENSE 404, [local]); inherits Elliptic's terms. **Download by user only** (ChainWatch already does this, docs/DATASET.md) | LFS sizes [local]: txs_features 694.8 MB, wallets_features 606.5 MB, AddrAddr edges 200.6 MB, wallets_classes 30.4 MB (SHA-256 4e5132c9…); ~1.6 GB total; laptop-scale with care | As Elliptic. **Only `1…`/`3…` addresses, 0 bech32** [local], so script-type distribution differs entirely from 2026 mempool. **2 OFAC-listed addresses are in Elliptic++ labelled *unknown*** (`19D1iGzDr7FyAdiy3ZZdxMd6ttHj1kj6WW`, `1EYitrwBYNWuTBcjZFbEUdqHppe2raLpaF`) [local]: unknown ≠ licit |
| **Elliptic2 (Elliptic + MIT-IBM, 2024)** [L1–L3] | connected subgraphs "suspicious" (laundering-like) vs licit | **subgraph** of Elliptic address clusters | Elliptic's proprietary attribution / clustering [L1] | Unverified whether usable dates are released | Background graph is unlabelled | **No.** Nodes are Elliptic's proprietary clusters; cluster→address mapping and clustering cannot be reproduced (unverified whether addresses are released) | **No** | Kaggle reportedly **CC BY-NC-ND 4.0** [search, L2]. **Download by user only** | 49.3 M nodes, 196 M edges, 121,810 labelled subgraphs (119,092 licit, 2,718 suspicious) [search, L3]; ~26 GB [search]; needs a large-RAM box or graph DB | One vendor's labels; 2.2% positives; subgraph task does not map to single-tx scoring |
| **BitcoinHeist (UCI 526)** [H1–H2] | address-day rows: ransomware family vs "white" | address × 24 h window | Montreal, Princeton, Padua ransomware studies [H1] | Year + day-of-year per row (2009–2018) [H1] | **Yes, de facto**: authors say they "do not know if all white addresses are in fact not related to ransomware" [H1] | **Partly.** income, neighbors, weight, length, count, loop are defined on a daily graph with edges < 0.3 BTC filtered [H1]; recomputable only with a full-chain daily graph (BlockSci/node), not from one tx | **No.** Needs the full 24 h graph around the address, and "length"/"loop" need multi-hop chains | **CC BY 4.0** [search, H1]. Could be committed with attribution, but 2.9 M rows is too big; download by user | 2,916,697 rows, 10 cols [H1]; laptop-scale. Ransom/white split ~41 k / 2.88 M (unverified exact) | ≤2018; heavy Locky/CryptoLocker skew; 4 addresses with conflicting labels [search, H3]; the 0.3 BTC filter defines the population |
| **Paquet-Clouston, Haslhofer, Dupont (2019)** [R1–R3] | ransomware payment addresses (seed + expanded clusters) | address (seed); cluster (expanded) | APWG feed, VirusTotal user, blogs, news [local, R1] | Paper analyses payments 2013–mid-2017 [R2]; seed CSV has no dates [local] | No negatives at all (positives only) | Seed addresses: yes as **exact matches**. Expansion uses multi-input clustering in GraphSense [R1] | Only as an **exact-match list** (A), not as model training data | Zenodo **CC BY 4.0** [search, R3]. GitHub repo has no LICENSE (404) [local]. A CC BY seed list could be committed with attribution, but bundling stale ransomware addresses has little value | 7,222 seed rows, 67 families [local]; Zenodo ~113 MB [search] | **97.5% Locky (7,038) from APWG** [local]. 11 strings are not Bitcoin mainnet (`2c…`, `N…`) [local]. 2016–2017 only |
| **BABD-13 (Xiang et al., IEEE TIFS 2024)** [B1–B3] | address behaviour type (13 classes: exchange, mining pool, gambling, mixer, darknet, ...) | address | **WalletExplorer** labels via its API [local, B2] | Collected Jul 2019 – May 2021 [search, B3]; partial ledger at heights 600,000–605,999 [B2] | Not a risk label set; no "illicit vs licit" ground truth | **Partly.** 148 statistical/structural features (amount, degree, time, local subgraph) [B2], recomputable only with each address's full history plus a 2-hop subgraph | **No.** Needs address history + subgraph per address | Kaggle licence **unverified**; WalletExplorer labels have no licence (unverified). Download by user only | 544,462 addresses [search, B3]; feature extraction needs a full graph (graph-tool) | Labels = WalletExplorer heuristic clusters of large services circa 2019–2021; reported 93–97% accuracy reflects that easy service-type task [search] |
| **OFAC SDN digital-currency addresses** (section 3) | addresses of sanctioned parties | address (linked to party/entity) | US Treasury legal designation | Party designation date; per-address addition date **not** given (must be inferred from snapshots) [local] | n/a (lists positives only; non-exhaustive, FAQ 562) | Yes: exact string | **Yes for (A)**: exact match on prevout/output addresses | **Public domain** (17 U.S.C. §105). An excerpt **may be committed**; fetch the current list at runtime | 517 XBT addresses / 56 parties (2026-01-30) [local]; 532 lines in mirror today [local] | Top-5 parties = 56% of addresses (Hydra 117) [local]; 1 TRON address under XBT; shared and duplicate addresses; removals happen |
| **Chainabuse (TRM Labs)** (section 4) | community scam reports per address/URL | address (report) | user reports; `checked` by moderators, `trusted` contributors, confidence score [search] | Report dates (API `since`/`before`) [search] | Unverified reports are explicitly "not spam" but "use with caution" [search] | Exact string only | **On-demand lookup only**: 10 calls/month on a standard key [search] | Proprietary API; site ToS: personal non-commercial use [search]. **Nothing may be committed**; BYO key | Unknown total; no bulk export [search] | Reporter self-selection; spam; scam-type skew; possibly removed reports |
| **Ransomwhere (Cable)** [W1–W3] | ransomware payment addresses + payments | address (+ tx payments) | crowdsourced reports with ransom-note screenshot, manually reviewed; plus "Showing the Receipts" set (family low-confidence) [local, W1] | Payment tx dates (USD at day rate) [local, W1] | Positives only; many payments family-unlabelled [W1] | Exact string; payments are on-chain txs | Only as an **exact-match list** (A) | Site: "entirely publicly available", addresses published after 90 days [local, W1]; Zenodo DOI 10.5281/zenodo.6512122 v1.1.0 (2024); **licence unverified**; site code MIT [local]. Prefer download by user until licence confirmed | ~10 k addresses / $278 M in one snapshot; >$1 B later [search, W2]; small JSON | Self-reported victims; big-game families; mostly pre-2023 [search] |
| **Bitcoin Abuse (historical)** [X1–X2] | community abuse reports | address | user reports, little verification | report dates | n/a | Exact string | Exact match only | Merged into Chainabuse (2023); no official dump today; old CSV feeds only in a deprecated XSOAR integration [search]. **Do not use third-party dumps** (ToS/provenance unknown) | Unknown | Heavy sextortion-spam skew (unverified); unverifiable |
| **Bartoletti et al. Ponzi (2018)** [Z1] | Bitcoin Ponzi-scheme addresses | address | manual analysis of known schemes [Z1] | ≤2017 | No | Features need address history | No | Release location/licence **unverified** | ~30 schemes (unverified) | Tiny, old |
| **WalletExplorer labels** (via BABD) | service clusters (exchanges, pools, markets) | cluster/address | multi-input heuristic + naming by the site author | ≤2021 (site largely static, unverified) | Most addresses unlabelled | Clustering not reproducible exactly | No | No licence (unverified); scraping terms unknown. **Do not commit** | Large | Heuristic; dated |

### Citations for the matrix
* E1 https://www.kaggle.com/datasets/ellipticco/elliptic-data-set [search]
* E2 Weber et al. 2019, https://arxiv.org/abs/1908.02591
* E3 PyG docstring (counts, label semantics), https://raw.githubusercontent.com/pyg-team/pytorch_geometric/master/torch_geometric/datasets/elliptic.py [local]
* E4 ChainWatch docs/DATASET.md (time-step semantics, 93 local + 72 aggregate features)
* E5 Šafář et al., "The Enemy of Reproducibility is Opacity: What's Inside the Elliptic Bitcoin Dataset (and Why It Is Wrong)", DFRWS USA 2026, https://dfrws.org/?p=20997 [search]: reverse-engineered features, train/test leakage. Also https://arxiv.org/pdf/2604.19514 [search]: an RF on raw features beats GNNs under a leakage-free inductive protocol, and a sharp illicit-rate drop in steps 43–49 (step 46 has 3 illicit out of 960 labelled).
* P1 https://raw.githubusercontent.com/git-disl/EllipticPlusPlus/main/README.md [local]
* P2 LFS files at https://media.githubusercontent.com/media/git-disl/EllipticPlusPlus/main/Actors%20Dataset/wallets_classes.csv and `wallets_features.csv` [local; header and rows inspected]
* P3 https://doi.org/10.1145/3580305.3599803
* L1 https://arxiv.org/abs/2404.19109
* L2 https://www.kaggle.com/datasets/ellipticco/elliptic2-data-set [search]
* L3 https://dl.acm.org/doi/pdf/10.1145/3677052.3698635 [search]
* H1 https://archive.ics.uci.edu/dataset/526/bitcoinheist+ransomware+address+dataset [search]
* H2 https://www.ijcai.org/proceedings/2020/0612.pdf
* H3 https://git.disroot.org/shelldweller/ransomware/raw/commit/b1c41d674ce6c15cf2bfd40d6eb6e4011a8be29e/Detecting_Bitcoin_Ransomware.pdf [search]
* R1 https://raw.githubusercontent.com/behas/ransomware-dataset/master/README.md, plus `data/seed_addresses.csv` via media.githubusercontent.com [local]
* R2 https://arxiv.org/abs/1804.04080; J. Cybersecurity 5(1) tyz003, https://doi.org/10.1093/cybsec/tyz003 [search]
* R3 https://zenodo.org/records/1238041 [search]
* B1 https://www.kaggle.com/datasets/lemonx/babd13
* B2 https://raw.githubusercontent.com/Y-Xiang-hub/Bitcoin-Address-Behavior-Analysis/main/README.md [local]
* B3 https://arxiv.org/pdf/2204.05746; https://doi.org/10.1109/TIFS.2023.3347894 [search]
* W1 https://raw.githubusercontent.com/cablej/ransomwhere/main/docs/index.html [local]
* W2 https://arxiv.org/pdf/2205.05028 and https://arxiv.org/html/2408.15420 [search]
* W3 https://zenodo.org/records/6512123 [search]
* X1 https://pam2024.cs.northwestern.edu/pdfs/paper-92.pdf [search]
* X2 https://xsoar.pan.dev/docs/reference/integrations/bitcoin-abuse [search]
* Z1 https://arxiv.org/pdf/1803.00646 [search]

Not assessed in depth: HBTBD (https://www.mdpi.com/2076-3417/13/15/8766) and commercial
attribution feeds. Commercial licences typically forbid redistribution and ML training (for
example, Nansen's API terms, https://nansen.ai/legal/api [search]).

## 2. Decision gate

**Question:** can ChainWatch meaningfully train a supervised classifier for real-time scoring of
the features of its live, sampled mempool transactions?

**Answer: No, not for risk or illicit labels, with any legitimate public data available
today.** Exact-address screening (A) is feasible and should be built. A supervised risk
classifier (C) on live features fails on all of the points below at once, and each one alone
would be disqualifying.

1. **No labelled source shares ChainWatch's feature space.**
   * Elliptic and Elliptic++ transaction features are anonymised, standardised and built from a 1-hop graph inside two-week windows. Their construction is undisclosed and reportedly leaky [E5], so they cannot be computed for a new mempool transaction. ChainWatch's own docs already state this (`docs/LIVE_ML_PROPOSAL.md` §1).
   * Elliptic2 runs on proprietary clusters.
   * BitcoinHeist and BABD features need daily or full address histories and multi-hop subgraphs, which a sampled single-tx Esplora collector does not have.
   * The only overlap is a handful of raw tx attributes: Elliptic++'s 17 augmented features (fees, size, number of inputs/outputs, BTC in/out stats).
2. **The labels are about addresses or entities, while the live object is a transaction.**
   * OFAC, Ransomwhere, Paquet-Clouston, Chainabuse and BABD label addresses.
   * Turning them into transaction labels ("pays a listed address") makes the label a deterministic function of the screened address. A model trained on it learns the lookup, so it is better implemented as the rule (A).
3. **Positives are tiny and concentrated in a few entities. This is a shortcut and leakage risk.**
   * OFAC has 517 XBT addresses over 56 parties, and 5 parties hold 56% of them [local].
   * The Paquet-Clouston seed is 97.5% one family (Locky) [local].
   * Any random split puts the same entity's addresses, and their wallet software's fingerprints (script type, fee policy, change pattern), on both sides. The model then learns *which wallet* sent the transaction rather than illicitness. Splitting grouped by entity leaves roughly 10–50 effective positive groups, too few for a stable estimate.
4. **"White" or unknown is not negative.**
   * The BitcoinHeist authors say so explicitly [H1].
   * Elliptic and Elliptic++ have 77% and 68% unknown labels respectively.
   * Two OFAC-sanctioned addresses sit in Elliptic++ labelled *unknown* [local].
   * Treating unlabelled mempool traffic as negatives would give an unknown, non-random label-noise rate. PU-learning cannot rescue this without a known labelling mechanism.
5. **Temporal and population shift.**
   * Every labelled dataset ends by 2021 (most by 2018–2019), and Elliptic++ contains 0 bech32 addresses [local].
   * The 2026 mempool is dominated by SegWit and Taproot, and the OFAC XBT list has 0 Taproot addresses [local].
   * Even inside Elliptic, a strong model collapses after step 43: ChainWatch measured AP 0.917 → 0.043, and an independent study reports a sharp drop in illicit rate after step 43 [E5].
6. **Licences.**
   * Elliptic and Elliptic2 are reportedly CC BY-NC-ND [search], and Elliptic++ has no licence. Their data cannot be shipped in an MIT repo, and a model trained on them can at most be shared with care (NC, no derivatives).
   * Chainabuse is proprietary, with personal non-commercial use and 10 calls a month.
7. **No live ground truth arrives later.** Unlike confirmation outcomes (Stage A in `LIVE_ML_PROPOSAL.md`), illicit labels for sampled transactions arrive rarely, years late (for example, an OFAC address was added in 2026 to a party designated in 2018 [local]), and selectively. A live evaluation set therefore cannot be built either.

**Consequences for ChainWatch:**
* (A) **Build exact-address screening**, with OFAC as the source of record and Ransomwhere/Paquet-Clouston as optional CC BY lists after licence confirmation. Chainabuse is an on-demand BYO-key lookup.
* Report hits as "listed by <source> on <snapshot date>" and never as a score.
* Keep the live monitor model-free for risk.
* The only defensible live supervised task remains the label-free-of-crime Stage A (confirmation within *k* blocks).

### Defensible OFFLINE-only experiment (does NOT validate live scoring)

**Elliptic++ transactions, restricted to the features ChainWatch can also compute.**
* Train on the 17 augmented raw features only: degree, fees, size, number of input/output addresses, BTC in/out min/max/mean/median/total.
* Use the same chronological splits as Phase 1/2 (train ≤34, test 35–49, with steps 43–49 reported separately), labelled rows only.
* Compare against the full 182-feature model.

This measures how much signal survives when the features are restricted to quantities a
mempool collector could observe. It does not show that the signal transfers to 2026 traffic
or to other label definitions, because labels, era and population all differ.

Optional add-on: **BitcoinHeist** with *grouped-by-family* and *chronological (by year)*
splits, AP/precision@k only, and white treated as unlabelled (PU evaluation). This would
quantify how much "ransomware detection" on that benchmark is family memorisation.

Both are research curiosities to report as negative or diagnostic results. Neither may feed
the live score.


## 3. OFAC SDN digital-currency addresses

Researched on 2026-10-14. "Verified locally" means I checked it against the authentic
`sdn_advanced.xml` copy described in section 3. "Search summary" means the claim comes from a
web-search result snippet for the cited URL. From this sandbox, `ofac.treasury.gov`,
`treasury.gov` and `sanctionslistservice.ofac.treas.gov` could be reached neither by curl
(proxy 403) nor by WebFetch (DNS failure), so I could not open those pages myself.

### 1. Official download path

| What | URL | Evidence |
|---|---|---|
| Legacy URL (still used by most open-source tools) | `https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml` | Used by OpenSanctions' production crawler config ([us_ofac_sdn.yml](https://raw.githubusercontent.com/opensanctions/opensanctions/main/datasets/us/ofac/us_ofac_sdn.yml), `data.url`) and by [0xB10C's tool](https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/main/README.md) |
| Sanctions List Service (SLS) export | `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML` | data.gov catalog entry ([catalog.data.gov](https://catalog.data.gov/dataset/specially-designated-nationals-sdn-list-advanced-xml-file-standard), search summary) |
| SLS download endpoint | `https://sanctionslistservice.ofac.treas.gov/api/download/sdn_advanced.xml` (it is said to answer with a short-lived signed redirect) | Third-party guides only ([compliapi](https://compliapi.com/data-sources/ofac-sdn-advanced-xml), [sanctionslookup](https://sanctionslookup.com/learn/ofac-sanctions-list-service-api)). **Unverified against an OFAC page.** |
| XSD | `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML.xsd` | Verified locally: it is the `xsi:schemaLocation` in the authentic file's root element |
| Human-readable SLS page | https://ofac.treasury.gov/sanctions-list-service | [search result](https://ofac.treasury.gov/sanctions-list-service) |

* **SLS launched on 2024-05-06.** The old links keep working through redirects to SLS-hosted files ([OFAC recent action 20240506_33](https://ofac.treasury.gov/recent-actions/20240506_33), search summary).
* **The SLS host rejects requests that have no `User-Agent` header** (HTTP 403). OFAC published a technical notice about this ([20240516_44](https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/ofac-technical-actions-in-reverse-chronological-order/20240516_44), search summary). A downloader must send a User-Agent and follow redirects.
* **The namespace and schema location of `SDN_ADVANCED.XML` changed** during the SLS rollout ([20240507_44](https://ofac.treasury.gov/recent-actions/20240507_44), search summary).
* **Update cadence: there is no fixed schedule.** OFAC says the list is "frequently updated" and that names are added or removed "as necessary and appropriate" ([FAQ 20](https://ofac.treasury.gov/faqs/20), search summary). Changes are announced under [Recent Actions](https://ofac.treasury.gov/recent-actions). How often users download is their own risk decision ([FAQ topic 1641](https://ofac.treasury.gov/faqs/topic/1641/print), search summary). In practice OpenSanctions crawls every 2 hours (`schedule: "0 */2 * * *"` in [us_ofac_sdn.yml](https://raw.githubusercontent.com/opensanctions/opensanctions/main/datasets/us/ofac/us_ofac_sdn.yml)), and 0xB10C regenerates its lists nightly at 00:00 UTC ([README](https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/main/README.md)).
* **Licensing.** The file is a work of the U.S. Government, and such works have no copyright protection ([17 U.S.C. §105](https://www.law.cornell.edu/uscode/text/17/105)). data.gov lists the dataset as unrestricted (search summary). USA.gov notes that federal sites can also carry third-party material ([usa.gov/government-copyright](https://www.usa.gov/government-copyright)), which does not apply to OFAC's own list data. **A small excerpt may therefore be committed to an MIT repository**, with attribution and with a note that it is a dated snapshot that is not for compliance use.
* **Semantics.** OFAC's address listings "are not likely to be exhaustive" ([FAQ 562](https://ofac.treasury.gov/faqs/562)). Each address is its own entry with a currency label, up to 256 characters ([FAQ 563](https://ofac.treasury.gov/faqs/563)). OFAC's own search tool matches IDs exactly ([FAQ 594](https://ofac.treasury.gov/faqs/topic/1641/print)). Unlisted addresses in the same wallet as a listed address may still carry sanctions risk ([virtual currency guidance](https://ofac.treasury.gov/media/913571/download?inline=)). All four were checked through search snippets only. ChainWatch does no clustering, so it can only report exact-address hits.

### 2. Exact structure (ADVANCED_XML), verified on the 2026-01-30 file

Namespace (default namespace, no prefix):
`https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML`
(the same value appears in 0xB10C's `NAMESPACE` constant in
[generate-address-list.py](https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/main/generate-address-list.py)).
The root element is `<Sanctions Version="3" ...>`. The file uses CRLF line endings.
Its top-level children appear in this order: `DateOfIssue`, `ReferenceValueSets`, `Locations`,
`IDRegDocuments`, `DistinctParties`, `ProfileRelationships`, `SanctionsEntries`,
`SanctionsEntryLinks`.

```xml
<DateOfIssue CalendarTypeID="1">
  <Year>2026</Year>
  <Month>1</Month>
  <Day>30</Day>
</DateOfIssue>                       <!-- child elements, NOT attributes -->

<ReferenceValueSets>
  <FeatureTypeValues>
    <FeatureType ID="344" FeatureTypeGroupID="1">Digital Currency Address - XBT</FeatureType>
  </FeatureTypeValues>
  <SanctionsTypeValues> <SanctionsType ID="1">Program</SanctionsType> <SanctionsType ID="1705">Block</SanctionsType> </SanctionsTypeValues>
  <ListValues> <List ID="1550">SDN List</List> </ListValues>
</ReferenceValueSets>

<DistinctParties>
  <DistinctParty FixedRef="26137">
    <Profile ID="26137" PartySubTypeID="4">
      <Identity ID="17841" FixedRef="26137" Primary="true" False="false">
        <Alias FixedRef="26137" AliasTypeID="1403" Primary="true" LowQuality="false">
          <DocumentedName ID="34915" FixedRef="26137" DocNameStatusID="1">
            <DocumentedNamePart><NamePartValue NamePartGroupID="67600" ScriptID="215" ...>Khorashadizadeh</NamePartValue></DocumentedNamePart>
            <DocumentedNamePart><NamePartValue NamePartGroupID="67601" ScriptID="215" ...>Ali</NamePartValue></DocumentedNamePart>
      ...
      <Feature ID="30464" FeatureTypeID="344">
        <FeatureVersion ID="28203" ReliabilityID="1560">
          <Comment />
          <VersionDetail DetailTypeID="1432">149w62rY42aZBox8fGcmqNsXUzSStKeq8C</VersionDetail>
        </FeatureVersion>
        <IdentityReference IdentityID="17841" IdentityFeatureLinkTypeID="1" />
      </Feature>

<SanctionsEntries>
  <SanctionsEntry ID="26137" ProfileID="26137" ListID="1550">
    <EntryEvent ID="26137" EntryEventTypeID="1" LegalBasisID="91711">
      <Comment />
      <Date CalendarTypeID="1"><Year>2018</Year><Month>11</Month><Day>28</Day></Date>
    </EntryEvent>
    <SanctionsMeasure ID="18235" SanctionsTypeID="1705"> ... </SanctionsMeasure>      <!-- Block -->
    <SanctionsMeasure ID="152733" SanctionsTypeID="1">                                <!-- Program -->
      <Comment>CYBER2</Comment>
```

(Abbreviated with `...`. Every element name, attribute and ID shown is copied from the fixture.
The complete XBT Feature is at lines 265–271 of `tests/fixtures/ofac/sdn_advanced_excerpt.xml`.)

XPaths (namespace prefix `s`), checked against both the full file and the excerpt:

* XBT feature id: `s:ReferenceValueSets/s:FeatureTypeValues/s:FeatureType[.='Digital Currency Address - XBT']/@ID` gives `344`. Resolve the id **by its text**, as 0xB10C does, and do not hard-code 344.
* Addresses: `s:DistinctParties/s:DistinctParty/s:Profile/s:Feature[@FeatureTypeID=$id]/s:FeatureVersion/s:VersionDetail/text()`
* Primary name: `s:Profile/s:Identity/s:Alias[@Primary='true']/s:DocumentedName/s:DocumentedNamePart/s:NamePartValue`. A name has several parts; their order follows `NamePartGroup`/`NamePartTypeID` (for example 1520 = last name, 1521 = first name in this file). A party can also have several `DocumentedName`s in different scripts.
* Designation: `s:SanctionsEntries/s:SanctionsEntry[@ProfileID=$profile]`. Within it, `s:EntryEvent/s:Date/{Year,Month,Day}`, and the programme codes are in `s:SanctionsMeasure[@SanctionsTypeID=<id of 'Program'>]/s:Comment`. OpenSanctions resolves `SanctionsTypeID` to "Program" before it reads `Comment` ([ofac_advanced.py](https://raw.githubusercontent.com/opensanctions/opensanctions/main/datasets/us/ofac/ofac_advanced.py), `extract_sanctions_measure_name`). It also filters on `ListID` to separate "SDN List" from non-SDN lists.

### 3. Fixture: `tests/fixtures/ofac/sdn_advanced_excerpt.xml` (authentic, trimmed)

* **Source copy:** `https://media.githubusercontent.com/media/keithrozario/sanctions-checker/main/sdn_advanced.xml`, which is the Git LFS object behind [keithrozario/sanctions-checker `sdn_advanced.xml`](https://github.com/keithrozario/sanctions-checker). The LFS pointer at commit `c092957464c85b526e2e1134edade85a272af8a0` (2026-02-09, message "updated"; the date comes from the GitHub commits page via WebFetch) gives `oid sha256:9ada1b25ec51909b25ddb37c04d6ca22425a8202d406aa92d21e467aee662928`, `size 122273646`. The downloaded file's SHA-256 matches that oid exactly. `DateOfIssue` is **2026-01-30**. The repo has no LICENSE file (raw `LICENSE` returns 404), but the content is OFAC's public-domain list, so no repo licence is needed for an excerpt of it.
* **Profiles kept:**
  * 26137, Khorashadizadeh Ali, CYBER2, entry date 2018-11-28, 1 P2PKH address. This is the earliest XBT-party entry date in the file, shared with 26138.
  * 38665, Task Force Rusich, RUSSIA-EO14024, entry date 2022-09-15, 2 bech32 v0 addresses.
  * 45404, Wang Mingming, ILLICIT-DRUGS-EO14059, entry date 2023-10-03. It has 3 entries filed as XBT, one of them the TRON address `TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq`. This makes it a ready-made negative test.
* **How it was cut:** I wrote `the research script excerpt_sdn.py (not in the repository)`. The script copies whole elements byte-for-byte (CRLF kept) and drops other elements whole. It self-checks that every retained element line occurs verbatim in the source. Retained: the XML declaration, the root start tag, `DateOfIssue`, the 3 `DistinctParty` elements, their 3 `SanctionsEntry` elements, and the `ReferenceValueSets` entries whose IDs those elements reference, plus all 18 "Digital Currency Address - *" FeatureTypes. Changed: an XML comment stating the provenance was added after the declaration. `Locations`, `IDRegDocuments` and `ProfileRelationships` are emptied (the country `Location`s alone were about 50 KB), so `LocationID` references dangle.
* **Size:** 32,951 bytes. Parsed results:
  * all 6 XBT entries come out with names, entry dates and programmes;
  * ChainWatch's existing `parse_ofac_sdn_advanced` (run read-only) returns 5 records and rejects the `T...` entry. On the full file it returns 516 records with 1 rejection.
* **Other candidates I checked and rejected:**
  * [cylon56/ofac-naughtylist tests/fixtures/sample_sdn_advanced.xml](https://raw.githubusercontent.com/cylon56/ofac-naughtylist/main/tests/fixtures/sample_sdn_advanced.xml) is constructed, not authentic. It uses BIP-173's example address `bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh` and writes `DateOfIssue` as attributes, which is wrong.
  * [bitnob/ofac-address-feed testdata](https://raw.githubusercontent.com/bitnob/ofac-address-feed/main/internal/ofac/testdata/sdn_advanced_sample.xml) is constructed ("EVIL CORP").
  * [sscodings/SIH_2 docs/research/ofac_sample.xml](https://raw.githubusercontent.com/sscodings/SIH_2/main/docs/research/ofac_sample.xml) contains real values but was re-serialised by ElementTree (`ns0:` prefixes) and has no `SanctionsEntries`.
  * [GENERALBYTESCOM/batm_public ofac_sdn_advanced.xml](https://raw.githubusercontent.com/GENERALBYTESCOM/batm_public/master/server_extensions_extra/src/test/resources/ofac_sdn_advanced.xml) is authentic (2021-01-19) but pretty-printed and has no XBT addresses.
* **Supporting data:** a research JSON (not in the repository) has all 519 XBT rows from the full file, with profile, name, programmes, entry dates and address type. `the research script analyze_sdn.py (not in the repository)` regenerates it.

### 4. Counts in the 2026-01-30 file (verified locally)

* 519 XBT `VersionDetail`s, 517 unique strings, across 56 profiles (27 entities, 29 individuals by PartySubTypeID 3/4). All are on `ListID=1550` (SDN List).
* By type:
  * 202 P2PKH and 178 P2SH. All pass base58check with version 0x00 or 0x05.
  * 138 bech32 v0, all valid checksums.
  * 0 Taproot.
  * 1 TRON-format address.
* **Concentration:**
  * Hydra Market has 117 addresses (23% of all XBT entries).
  * Blender.io and Wang Yunhe (911 S5) have 46 each.
  * The top 5 parties hold 56% of all addresses.
  * 21 parties have a single address.
* **Programmes over the 56 parties:**
  * CYBER2 25
  * ILLICIT-DRUGS-EO14059 13
  * RUSSIA-EO14024 6
  * ELECTION-EO13848 5
  * DPRK3 4
  * SDNTK, SDGT 3 each
  * others 1–2
* **Party entry year (counted by address):**
  * 2018: 2
  * 2019: 11
  * 2020: 38
  * 2021: 64
  * 2022: 227
  * 2023: 36
  * 2024: 59
  * 2025: 82
* **Compared with the current mirror** ([0xB10C lists branch](https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_XBT.txt), fetched 2026-10-14): 532 lines, made of 208 `1…`, 177 `3…`, 146 `bc1…` and 1 `T…`. Since 2026-01-30, 16 addresses were added and 1 was removed (`17a5bpKvEp1j1Trs4qTbcNZrby53JbaS9C`).

### 5. Data-quality pitfalls (each verified locally unless cited)

1. **Non-Bitcoin addresses filed under XBT.** `TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq` (profile 45404, Feature 65924) is a TRON address. It decodes as a *checksum-valid* base58check string with version byte 0x41 (TRON mainnet), so a validator that checks only the checksum accepts it. Validate the version byte (0x00/0x05) and the bech32 HRP `bc`, then reject and log the entry rather than silently dropping it. The mirror list carries it too.
2. **One address listed under two parties.** `1H939dom7i4WDLCKyGbXUp3fs9CSTNRzgL` appears under both 38419 (Khatibi Aghada) and 38420 (Nikaeen Ravari). A map keyed by address keeps only the first party. ChainWatch's current parser does this (`if addr not in records`), so it loses profile 38420. Store a list of listings per address.
3. **Duplicates within one party.** `3F6bbvS1krsc1qR8FsbTDfYQyvkMm3QvmR` appears twice under Blender.io (37070).
4. **`EntryEvent` date is the party's designation date, not the date the address was added.**
   * MESRI Behzad (24003) was designated on 2018-03-23 (CYBER2) and has *no* XBT feature in the 2026-01-30 file.
   * Yet `12aNKp2iDKuhEde2YfPdd4DFGenRUTKupL` sits under that profile in a later file: it is in the SIH_2 sample (Feature 96516) and in today's mirror.
   * Treating `listed_on` as the address's listing date would therefore backdate the label by about 8 years.
   * For point-in-time correctness, use **the first snapshot (by `DateOfIssue`/retrieval time) that contains the address**.
5. **Removals happen** (one XBT address removed between January and October 2026). Screening and any label set must be versioned per snapshot, and must not be a monotonic "ever listed" set.
6. **The mirror list has no provenance per address.** The 0xB10C TXT has no party, programme, date or snapshot hash. It is fine as a convenience source, but the official XML is the source of record. The mirror author disclaims any warranty on completeness or correctness ([README](https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/main/README.md)).
7. **Programme codes.** Take `Comment` only from `SanctionsMeasure`s whose `SanctionsTypeID` resolves to "Program". Other measure types (directives, "Block") exist ([OpenSanctions](https://raw.githubusercontent.com/opensanctions/opensanctions/main/datasets/us/ofac/ofac_advanced.py)). A profile can have several `SanctionsEntry`s (several lists), and a dict keyed by `ProfileID` keeps only the last one.
8. **Format traps.**
   * `DateOfIssue` uses child elements, not attributes.
   * The namespace changed in May 2024 (see above).
   * The file is about 122 MB with CRLF line endings. ChainWatch's comment "~100 MB in 2026" understates it. A full ElementTree parse took a few seconds here but uses roughly 1–2 GB of RAM (not measured precisely); `iterparse` with element clearing is safer for a cloud container.
   * Bech32 is case-insensitive and base58 is case-sensitive, so canonicalise before exact matching.
9. **Coverage is tiny and entity-specific.** There are 517 addresses for 56 parties. FAQ 562 says the listings are non-exhaustive. Most listed addresses are reused, legacy-type addresses from 2018–2022 cases, and the list has 0 Taproot addresses, while a live 2026 mempool contains many. Exact-match screening is meaningful; using these addresses as ML positives is not (see datasets.md, Decision gate).


## 4. Chainabuse (optional, bring-your-own-key provider; not implemented)

Researched on 2026-10-14. From this sandbox, `api.chainabuse.com`, `docs.chainabuse.com`,
`docs.trmlabs.com` and `chainabuse.com` were blocked for curl (proxy 403) and could not be
resolved by WebFetch. **Every item below therefore comes from web-search snippets of the
cited pages. Re-read each page before relying on it.** "Unverified" means that no source
confirmed the point.

### Facts

| Topic | Finding | Source |
|---|---|---|
| Operator | Chainabuse is run by TRM Labs. TRM acquired Bitcoinabuse.com (announced 2023-10-11), and Bitcoinabuse's earlier reports were merged into Chainabuse. | [GlobeNewswire](https://www.globenewswire.com/news-release/2023/10/11/2758400/0/en/TRM-Labs-Acquires-Bitcoinabuse-com.html); [TRM launch blog](https://www.trmlabs.com/resources/blog/announcing-the-launch-of-chainabuse-the-multi-chain-scam-reporting-tool-that-empowers-crypto-users-against-fraud) |
| Base URL / endpoints | `https://api.chainabuse.com/v0`. `GET /v0/reports` takes an `address` filter (the screening use case). `GET /v0/reports/{reportId}` returns one report. Reporting is also possible (POST). | [Get Reports](https://docs.chainabuse.com/reference/reports-1); [Get Single Report](https://docs.chainabuse.com/reference/report); [Screening addresses](https://docs.chainabuse.com/docs/get-reports-parameters) |
| Auth | **HTTP Basic, with the user's or organisation's own API key.** The docs show the key as the username, with the key or empty as the password. Any account generates a key under Profile → Settings → API key. **Yes, an integration needs the user's own key.** | [Authentication](https://docs.chainabuse.com/docs/getting-started) |
| Rate limits / quota | Standard key: **10 calls per month**, at most 50 reports per call. Partner access: at most 5,000 calls per hour. | [Request limits and rate](https://docs.chainabuse.com/docs/getting-started-2-1) |
| Pricing | No public price list was found. Partner and Pro access go through `hello@chainabuse.com`, and law-enforcement access through a partner form. **Unverified.** | [TRM-hosted API docs](https://docs.trmlabs.com/guides/chainabuse/welcome-to-chainabuse-api) |
| Query params | Documented: `address`, `chain`, `category`, `orderByField` (date), `orderByDirection`, `before`/`since` (date thresholds), `page` (at most 50 per page). `perPage`/`includePrivate` were **unverified**. | [Screening addresses](https://docs.chainabuse.com/docs/get-reports-parameters) |
| Verification status | `checked` means moderators, including blockchain-intelligence experts, verified the report (green badge). `trusted` means the reporter is a pre-vetted Trusted Contributor (blue badge). An unverified report "does not mean it is spam" but should be "used with caution". Each report has a confidence score that can be filtered on, and the docs say they cannot guarantee 100% accuracy. Flagged spam is reviewed and removed. | [Spam detection and reports accuracy](https://docs.trmlabs.com/guides/chainabuse/verifying-the-accuracy-of-reported-information); [FAQ](https://chainabuse.com/en/faq/GENERAL) |
| Withheld fields | Evidence, geolocation and contact details are not in the public API. They go only to pre-vetted partners through Chainabuse Pro, depending on the reporter's opt-in. | [TRM-hosted API docs](https://docs.trmlabs.com/guides/chainabuse/welcome-to-chainabuse-api) |
| API licence | Listed as "Proprietary API". | [docs.trmlabs.com](https://docs.trmlabs.com/guides/chainabuse/welcome-to-chainabuse-api) |
| Website ToS | Users get a limited, non-sublicensable right to use Chainabuse Content for **personal, non-commercial purposes**, and TRM reserves all other rights. I did **not** find explicit clauses on scraping, bulk download, redistribution or ML training: **unverified**, read the PDF before any bulk use. | [Terms of Use PDF](https://chainabuse.com/ChainAbuse_TermsofUse.pdf) |
| Bulk / historical snapshots | No official bulk export or historical dump was found. Researchers report that the merged service "is no longer making its full database freely available". The API is restrictive, rate-limited and uses a different categorisation. Old BitcoinAbuse "Forever/30 days" CSV feeds appear only in a **deprecated** XSOAR integration. | [PAM 2024 paper](https://pam2024.cs.northwestern.edu/pdfs/paper-92.pdf); [LiU thesis](https://liu.diva-portal.org/smash/get/diva2:1828499/FULLTEXT01.pdf); [XSOAR BitcoinAbuse (Deprecated)](https://xsoar.pan.dev/docs/reference/integrations/bitcoin-abuse) |
| Ransomwhere link | Ransomwhere lists Chainabuse as a partner. | [ransomwhere docs/index.html](https://raw.githubusercontent.com/cablej/ransomwhere/main/docs/index.html) |

### What follows for ChainWatch

1. **Optional, off by default, BYO key.** Read the key from an env var or secret, never ship one, and never send it anywhere except `api.chainabuse.com` over HTTPS with Basic auth.
2. **The quota rules out live per-transaction screening on a standard key.** 10 calls a month cannot cover a sampled mempool (tens of addresses per transaction). The realistic uses are:
   * an analyst clicks "look up this address" on demand;
   * a partner key, with ChainWatch enforcing a client-side budget and per-address caching with a TTL.
   Count and display the remaining quota.
3. **Report the result as an allegation, not a label.**
   * Show the number of reports, `checked` and `trusted` counts, categories and the latest report date, with a link to the report.
   * Default to showing all reports while visually separating unverified ones. Optionally offer a "checked-only" filter.
   * Never derive a risk score from report counts. Counts are easy to manipulate and are biased toward visible scams.
4. **Do not persist or redistribute.**
   * Commit no Chainabuse data (no fixtures with real responses) to the MIT repo; use synthetic response fixtures in the documented shape.
   * Cache locally only for as long as the user's ToS allows. Treat bulk harvesting and ML training as **not permitted unless TRM grants it in writing**: the website ToS limits content use to personal, non-commercial purposes, and the API is proprietary.
5. **Point-in-time.** Store `retrieved_at` with each lookup result. Reports can be removed (spam review), so a past result cannot be reproduced later.

