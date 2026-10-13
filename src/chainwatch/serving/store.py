"""The single source of model inputs for serving and replay.

Training applies one stateless repair *outside* the saved pipeline: in/out degrees are
filled from the edge list (``fill_degrees_from_edges``). ``FeatureStore`` applies exactly
that repair once at load time, and every serving path reads its inputs from here, so
training and serving cannot drift apart (tested in tests/test_serving.py).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from chainwatch.data import schema as S
from chainwatch.data.loader import EDGES_PARQUET, TRANSACTIONS_PARQUET, load_processed
from chainwatch.features.preprocessing import fill_degrees_from_edges

LABEL_STATUS = {S.CLASS_ILLICIT: "illicit", S.CLASS_LICIT: "licit", S.CLASS_UNKNOWN: "unknown"}


def data_fingerprint(processed_dir: Path) -> str:
    h = hashlib.sha256()
    for name in (TRANSACTIONS_PARQUET, EDGES_PARQUET):
        with open(processed_dir / name, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
    return h.hexdigest()[:16]


class UnknownTransactionError(KeyError):
    pass


@dataclass
class FeatureStore:
    model_frame: pd.DataFrame  # repaired features + tx_id, time_step, raw_class, label
    edges: pd.DataFrame
    raw_missing: pd.Series  # tx_id -> bool: augmented features were missing in the source

    @classmethod
    def from_processed(cls, processed_dir: Path) -> FeatureStore:
        ds = load_processed(processed_dir)
        raw_missing = ds.transactions.set_index(S.TX_ID)[S.AUGMENTED_FEATURES].isna().any(axis=1)
        frame = fill_degrees_from_edges(ds.transactions, ds.edges)
        return cls(model_frame=frame, edges=ds.edges, raw_missing=raw_missing)

    def __post_init__(self) -> None:
        self._by_id = self.model_frame.set_index(S.TX_ID, drop=False)
        self._steps = {
            int(t): idx for t, idx in self.model_frame.groupby(S.TIME_STEP).groups.items()
        }
        ts = self._by_id[S.TIME_STEP]
        e = self.edges.assign(step=ts.loc[self.edges[S.EDGE_SRC]].to_numpy())
        self._edges_by_step = {int(t): g for t, g in e.groupby("step")}

    # ------------------------------------------------------------------ lookups
    @property
    def steps(self) -> list[int]:
        return sorted(self._steps)

    def has(self, tx_id: int) -> bool:
        return tx_id in self._by_id.index

    def row(self, tx_id: int) -> pd.Series:
        try:
            return self._by_id.loc[tx_id]
        except KeyError as exc:
            raise UnknownTransactionError(tx_id) from exc

    def step_of(self, tx_ids: list[int]) -> np.ndarray:
        missing = [t for t in tx_ids if t not in self._by_id.index]
        if missing:
            raise UnknownTransactionError(missing[:10])
        return self._by_id.loc[tx_ids, S.TIME_STEP].to_numpy()

    def step_frame(self, step: int) -> pd.DataFrame:
        if step not in self._steps:
            raise KeyError(step)
        return self.model_frame.loc[self._steps[step]]

    def features(self, tx_ids: list[int], columns: list[str]) -> pd.DataFrame:
        """Model inputs in the model's column order (the only serving input path)."""
        return self._by_id.loc[tx_ids, columns]

    def label_info(self, tx_id: int, latency: int) -> dict[str, Any]:
        r = self.row(tx_id)
        step = int(r[S.TIME_STEP])
        return {
            "label_status": LABEL_STATUS[int(r[S.RAW_LABEL])],
            "label_available_at_step": step + latency
            if int(r[S.RAW_LABEL]) != S.CLASS_UNKNOWN
            else None,
            "augmented_features_missing_in_source": bool(self.raw_missing.loc[tx_id]),
        }

    def label_info_many(self, tx_ids: list[int], latency: int) -> list[dict[str, Any]]:
        """Vectorised ``label_info`` for batches (same semantics)."""
        sub = self._by_id.loc[tx_ids, [S.TIME_STEP, S.RAW_LABEL]]
        raw = sub[S.RAW_LABEL].to_numpy().astype(int)
        steps = sub[S.TIME_STEP].to_numpy().astype(int)
        miss = self.raw_missing.loc[tx_ids].to_numpy()
        return [
            {
                "label_status": LABEL_STATUS[r],
                "label_available_at_step": int(t) + latency if r != S.CLASS_UNKNOWN else None,
                "augmented_features_missing_in_source": bool(m),
            }
            for r, t, m in zip(raw, steps, miss, strict=True)
        ]

    # ------------------------------------------------------------------ graph
    def neighbourhood(self, tx_id: int, hops: int = 1, max_nodes: int = 60) -> dict[str, Any]:
        """BFS over the (undirected view of the) transaction graph within the tx's step."""
        if hops not in (1, 2):
            raise ValueError("hops must be 1 or 2")
        step = int(self.row(tx_id)[S.TIME_STEP])
        e = self._edges_by_step.get(step, self.edges.iloc[0:0])
        adj: dict[int, set[int]] = {}
        for a, b in zip(e[S.EDGE_SRC].to_numpy(), e[S.EDGE_DST].to_numpy(), strict=True):
            adj.setdefault(int(a), set()).add(int(b))
            adj.setdefault(int(b), set()).add(int(a))
        depth = {tx_id: 0}
        frontier = [tx_id]
        truncated = False
        for d in range(1, hops + 1):
            nxt = []
            for node in frontier:
                for nb in sorted(adj.get(node, ())):
                    if nb in depth:
                        continue
                    if len(depth) >= max_nodes:
                        truncated = True
                        break
                    depth[nb] = d
                    nxt.append(nb)
            frontier = nxt
        nodes = set(depth)
        sub = e[e[S.EDGE_SRC].isin(nodes) & e[S.EDGE_DST].isin(nodes)]
        return {
            "step": step,
            "nodes": depth,
            "edges": list(
                zip(sub[S.EDGE_SRC].astype(int), sub[S.EDGE_DST].astype(int), strict=True)
            ),
            "truncated": truncated,
        }
