"""Feature preparation.

Two kinds of transformation live here and the distinction matters for leakage:

1. **Stateless, per-row** transforms that use no labels and no statistics pooled
   across rows (``fill_degrees_from_edges``, ``signed_log1p``). They can safely be
   applied to the whole table before splitting.
2. **Fitted** transforms (median imputation, standardisation). These are wrapped in
   an sklearn ``Pipeline`` and fitted on the training split only. Because the fitted
   preprocessor is serialised inside the model artifact, inference applies exactly
   the training-time statistics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from chainwatch.data import schema as S


def fill_degrees_from_edges(transactions: pd.DataFrame, edges: pd.DataFrame) -> pd.DataFrame:
    """Fill missing ``in_txs_degree``/``out_txs_degree`` from the edge list.

    For the 202,804 rows where Elliptic++ provides degrees, they match the edge list
    exactly, so recomputing them for the 965 rows with missing values is a lossless
    repair rather than an imputation. Since there are no edges between time steps,
    this uses only same-period structure (the same information the published
    Aggregate features already rely on).
    """
    out = transactions.copy()
    in_deg = edges[S.EDGE_DST].value_counts()
    out_deg = edges[S.EDGE_SRC].value_counts()
    ids = out[S.TX_ID]
    for col, deg in (("in_txs_degree", in_deg), ("out_txs_degree", out_deg)):
        mask = out[col].isna()
        out.loc[mask, col] = ids[mask].map(deg).fillna(0).astype("float32").to_numpy()
    return out


def signed_log1p(x: np.ndarray) -> np.ndarray:
    """sign(x) * log(1 + |x|): compresses heavy tails, defined for all reals, monotone."""
    x = np.asarray(x, dtype=np.float64)
    return np.sign(x) * np.log1p(np.abs(x))


def build_preprocessor(feature_cols: list[str], *, log1p_augmented: bool, scale: bool) -> Pipeline:
    """Return an *unfitted* preprocessing pipeline for the given feature columns.

    * Augmented features: median imputation (fit on train) -> optional signed log1p.
      No missing-value indicator is added: in Elliptic++ every transaction with missing
      augmented features is licit or unknown, so an indicator would let the model learn
      a dataset-construction artifact rather than transaction behaviour.
    * Local/aggregate features: passed through (already standardised by the publisher).
    * ``scale=True`` appends a StandardScaler for scale-sensitive models (logistic
      regression with L2 penalty); tree models do not need it.
    """
    augmented = [c for c in feature_cols if c in S.AUGMENTED_FEATURES]
    other = [c for c in feature_cols if c not in S.AUGMENTED_FEATURES]

    transformers = []
    if other:
        transformers.append(("base", "passthrough", other))
    if augmented:
        aug_steps: list[tuple[str, object]] = [("impute", SimpleImputer(strategy="median"))]
        if log1p_augmented:
            aug_steps.append(
                ("log1p", FunctionTransformer(signed_log1p, feature_names_out="one-to-one"))
            )
        transformers.append(("augmented", Pipeline(aug_steps), augmented))

    steps: list[tuple[str, object]] = [
        (
            "columns",
            ColumnTransformer(transformers, remainder="drop", verbose_feature_names_out=False),
        )
    ]
    if scale:
        steps.append(("scale", StandardScaler()))
    return Pipeline(steps)


def make_xy(df: pd.DataFrame, feature_cols: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    """Select model inputs and the binary target, refusing leaky columns."""
    leaky = [c for c in feature_cols if c in S.NON_FEATURE_COLUMNS]
    if leaky:
        raise ValueError(f"Refusing to use non-feature columns as inputs: {leaky}")
    if (df[S.LABEL] == S.LABEL_UNKNOWN).any():
        raise ValueError("make_xy received unknown-class rows; filter them out first")
    return df[feature_cols], df[S.LABEL].to_numpy(dtype=np.int64)


def time_proxy_features(
    transactions: pd.DataFrame, feature_cols: list[str], max_step: int, threshold: float
) -> dict[str, float]:
    """Features that behave like a clock: |Spearman(feature, time step)| >= threshold.

    Computed from features only (labelled and unknown rows, never labels) on steps
    ``<= max_step``, so the rule cannot look at a later evaluation period. A tree model
    given such a feature can learn the period itself; under a chronological split every
    future value lies beyond the training range, which is the same problem as using the
    time step as an input.
    """
    df = transactions[transactions[S.TIME_STEP] <= max_step]
    t = df[S.TIME_STEP].rank().to_numpy()
    out = {}
    for c in feature_cols:
        x = df[c]
        r = np.corrcoef(x.fillna(x.median()).rank().to_numpy(), t)[0, 1]
        if np.isfinite(r) and abs(r) >= threshold:
            out[c] = float(r)
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1])))
