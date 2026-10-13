"""Synthetic data with the verified Elliptic++ transaction schema.

Used by the test-suite and by the public demo (``chainwatch demo-data``) so that the
whole system can run without redistributing Elliptic++ files. The values are random:
illicit rows get a shifted mean in five local features so that models can learn
*something*. Nothing produced from this data says anything about real Bitcoin activity.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from chainwatch.data import schema as S

SYNTHETIC_MARKER = "SYNTHETIC_DEMO_DATA"


def make_raw_frames(
    n_steps: int = 12,
    per_step: int = 60,
    seed: int = 0,
    n_missing: int = 10,
    signal: float = 2.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (features, classes, edges) frames in the raw Elliptic++ CSV layout."""
    rng = np.random.default_rng(seed)
    n = n_steps * per_step
    tx_ids = rng.choice(np.arange(1_000, 10_000_000), size=n, replace=False)
    steps = np.repeat(np.arange(1, n_steps + 1), per_step)
    raw_class = rng.choice([1, 2, 3], size=n, p=[0.15, 0.45, 0.40])
    # every step has both labelled classes
    raw_class[::per_step] = 1
    raw_class[1::per_step] = 2

    feats = rng.normal(size=(n, len(S.LOCAL_FEATURES) + len(S.AGGREGATE_FEATURES)))
    feats[raw_class == 1, :5] += signal
    aug = np.abs(rng.lognormal(mean=0.0, sigma=2.0, size=(n, len(S.AUGMENTED_FEATURES))))

    features = pd.DataFrame(feats, columns=S.LOCAL_FEATURES + S.AGGREGATE_FEATURES)
    aug_frame = pd.DataFrame(aug, columns=S.AUGMENTED_FEATURES)
    features = pd.concat([features, aug_frame], axis=1)
    features.insert(0, S.RAW_TIME_STEP, steps)
    features.insert(0, S.RAW_TX_ID, tx_ids)

    # edges only within a time step (as in the real data)
    src, dst = [], []
    for t in range(1, n_steps + 1):
        ids = tx_ids[steps == t]
        for _ in range(per_step):
            a, b = rng.choice(ids, size=2, replace=False)
            src.append(a)
            dst.append(b)
    edges = pd.DataFrame({S.RAW_EDGE_SRC: src, S.RAW_EDGE_DST: dst}).drop_duplicates()

    # degrees consistent with the edge list, then blank some out on licit rows
    in_deg = edges[S.RAW_EDGE_DST].value_counts()
    out_deg = edges[S.RAW_EDGE_SRC].value_counts()
    features["in_txs_degree"] = features[S.RAW_TX_ID].map(in_deg).fillna(0).astype(float)
    features["out_txs_degree"] = features[S.RAW_TX_ID].map(out_deg).fillna(0).astype(float)
    missing_rows = np.where(raw_class == 2)[0][:n_missing]
    features.loc[missing_rows, S.AUGMENTED_FEATURES] = np.nan

    # classes file in a different row order than features: loaders must join on txId
    classes = (
        pd.DataFrame({S.RAW_TX_ID: tx_ids, S.RAW_CLASS: raw_class})
        .sample(frac=1.0, random_state=seed)
        .reset_index(drop=True)
    )
    return features, classes, edges


def write_synthetic_raw(
    out_dir: Path, n_steps: int = 49, per_step: int = 120, seed: int = 0
) -> Path:
    """Write synthetic raw CSVs plus a marker file that serving surfaces as a banner."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # a deliberately weak signal so the demo does not look unrealistically perfect
    features, classes, edges = make_raw_frames(
        n_steps, per_step, seed, n_missing=n_steps, signal=0.6
    )
    features.to_csv(out_dir / S.FEATURES_FILE, index=False)
    classes.to_csv(out_dir / S.CLASSES_FILE, index=False)
    edges.to_csv(out_dir / S.EDGES_FILE, index=False)
    (out_dir / SYNTHETIC_MARKER).write_text(
        "Random data with the Elliptic++ schema. Not real Bitcoin transactions.\n",
        encoding="utf-8",
    )
    return out_dir
