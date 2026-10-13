"""Chronological train/validation/test splitting.

Why chronological: a deployed detector scores *future* transactions with a model
trained on the past. A random split lets the model see transactions from the same
~2-week window (and the same illicit campaigns) it is tested on, which inflates
scores and hides concept drift. Because Elliptic++ has no edges between time steps
(verified in ``validate_raw``), splitting by time step also guarantees that no
graph neighbour of a test node is ever part of the training set.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from chainwatch.config import SplitConfig
from chainwatch.data import schema as S


class LeakageError(RuntimeError):
    """Raised when a split violates temporal or label-isolation guarantees."""


@dataclass
class TemporalSplit:
    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame

    def parts(self) -> dict[str, pd.DataFrame]:
        return {"train": self.train, "val": self.val, "test": self.test}

    def summary(self) -> pd.DataFrame:
        rows = []
        for name, df in self.parts().items():
            n_pos = int((df[S.LABEL] == S.LABEL_ILLICIT).sum())
            rows.append(
                {
                    "split": name,
                    "time_steps": f"{df[S.TIME_STEP].min()}-{df[S.TIME_STEP].max()}",
                    "n": len(df),
                    "n_illicit": n_pos,
                    "n_licit": len(df) - n_pos,
                    "illicit_rate": n_pos / max(len(df), 1),
                }
            )
        return pd.DataFrame(rows)


def _in_range(ts: pd.Series, bounds: tuple[int, int]) -> pd.Series:
    return ts.between(bounds[0], bounds[1], inclusive="both")


def temporal_split(transactions: pd.DataFrame, split: SplitConfig) -> TemporalSplit:
    """Split *labelled* transactions by time step. Unknown-class rows are dropped here."""
    labeled = transactions[transactions[S.LABEL] != S.LABEL_UNKNOWN]
    ts = labeled[S.TIME_STEP]
    parts = {name: labeled[_in_range(ts, bounds)].copy() for name, bounds in split.ranges().items()}
    result = TemporalSplit(**parts)
    assert_no_temporal_leakage(result)
    return result


def assert_no_temporal_leakage(split: TemporalSplit) -> None:
    """Guard rails executed on every split; cheap enough to always run."""
    parts = split.parts()
    for name, df in parts.items():
        if df.empty:
            raise LeakageError(f"{name} split is empty")
        if (df[S.LABEL] == S.LABEL_UNKNOWN).any():
            raise LeakageError(f"{name} split contains unknown-class transactions")
        if df[S.LABEL].nunique() < 2:
            raise LeakageError(f"{name} split contains a single class")

    if split.train[S.TIME_STEP].max() >= split.val[S.TIME_STEP].min():
        raise LeakageError("train overlaps or follows validation in time")
    if split.val[S.TIME_STEP].max() >= split.test[S.TIME_STEP].min():
        raise LeakageError("validation overlaps or follows test in time")

    ids = [set(df[S.TX_ID]) for df in parts.values()]
    if ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2]:
        raise LeakageError("transaction ids shared between splits")


def count_cross_split_edges(
    edges: pd.DataFrame, transactions: pd.DataFrame, split: SplitConfig
) -> int:
    """Number of edges whose endpoints fall in different splits (incl. unknown nodes).

    A non-zero count would mean neighbourhood-aggregated features (and future GNN
    message passing) could carry information across the split boundary.
    """
    ts = transactions.set_index(S.TX_ID)[S.TIME_STEP]

    def split_id(t: np.ndarray) -> np.ndarray:
        out = np.full(t.shape, -1)
        for i, (lo, hi) in enumerate(split.ranges().values()):
            out[(t >= lo) & (t <= hi)] = i
        return out

    a = split_id(ts.loc[edges[S.EDGE_SRC]].to_numpy())
    b = split_id(ts.loc[edges[S.EDGE_DST]].to_numpy())
    return int((a != b).sum())
