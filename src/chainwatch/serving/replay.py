"""Historical replay over frozen model snapshots.

Replay answers: "what would the ranking for step *s* have looked like, at the end of
step *s*, under policy P?". It only uses (a) features of step *s* and (b) a snapshot
trained on labels available by the end of step *s - 1* (label latency from the
registry). It is a simulation over a published historical dataset, **not** real-time
monitoring.

Retrospective metrics use labels that, under the latency assumption, become known
only at the end of step *s + L*. They are computed over labelled transactions only;
unknown transactions are never treated as licit.
"""

from __future__ import annotations

import math
import threading
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from chainwatch.backtest.analysis import fast_average_precision
from chainwatch.data import schema as S
from chainwatch.evaluation.metrics import expected_hits_at_k
from chainwatch.serving.registry import ModelBundle
from chainwatch.serving.store import LABEL_STATUS, FeatureStore

POLICIES = ("expanding", "static", "phase1")
BUDGETS = (10, 20, 50)


class ReplayError(ValueError):
    """Request outside what replay can answer (step range, policy, causality)."""


class ReplayEngine:
    def __init__(
        self,
        store: FeatureStore,
        bundles: dict[str, ModelBundle],
        meta: dict[str, Any],
    ) -> None:
        self.store = store
        self.bundles = bundles
        self.meta = meta
        self.latency = int(meta["label_latency"])
        self.first_step, self.last_step = meta["replay_steps"]
        self.static_origins = {
            k: tuple(v) for k, v in meta["policies"]["static"]["origins"].items()
        }
        self._snap_by_tau = {b.tau: v for v, b in bundles.items() if b.card["kind"] == "snapshot"}
        self.phase1 = meta.get("phase1_reference")
        self._score_step = lru_cache(maxsize=256)(self._score_step_uncached)
        # Serialises cache fills: concurrent cold requests for the same step score it once
        # instead of oversubscribing the CPU (cached hits hold the lock for microseconds).
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ policy
    def replay_steps(self) -> list[int]:
        return [s for s in self.store.steps if self.first_step <= s <= self.last_step]

    def model_for(self, step: int, policy: str) -> str:
        if policy not in POLICIES:
            raise ReplayError(f"unknown policy {policy!r}; choose from {POLICIES}")
        if not self.first_step <= step <= self.last_step:
            raise ReplayError(f"replay covers steps {self.first_step}-{self.last_step}, not {step}")
        if policy == "expanding":
            tau = step - 1
        elif policy == "static":
            origins = [o for o, end in self.static_origins.values() if o < step <= end]
            if not origins:
                raise ReplayError(f"no static stage covers step {step}")
            tau = origins[0]
        else:
            if self.phase1 is None:
                raise ReplayError("no Phase 1 reference model in this registry")
            b = self.bundles[self.phase1]
            first = int(b.card.get("out_of_sample_from_step", b.tau + 1))
            if step < first:
                raise ReplayError(f"phase1 model is out-of-sample only from step {first}")
            return self.phase1
        if tau not in self._snap_by_tau:
            raise ReplayError(f"no snapshot trained at end of step {tau}")
        version = self._snap_by_tau[tau]
        self.assert_causal(version, step)
        return version

    def assert_causal(self, version: str, step: int) -> None:
        """The model must be trained before ``step`` began, on labels it could have known.

        Trained at the end of step tau < step, using labels of steps <= tau - latency.
        """
        b = self.bundles[version]
        tau, last_label = b.tau, b.card["train_steps"][1]
        latency = int(b.card.get("label_latency", 0))
        if tau is None or tau >= step or last_label > tau - latency:
            raise ReplayError(f"{version} is not a causally valid model for step {step}")

    def temporal_validity(self, version: str, step: int) -> dict[str, Any]:
        """For explicit-version requests: was this model available when step was scored?"""
        b = self.bundles[version]
        first_oos = int(b.card.get("out_of_sample_from_step", (b.tau or 0) + 1))
        valid = (
            b.tau is not None
            and b.tau < step
            and b.card["train_steps"][1] < step
            and step >= first_oos  # e.g. Phase 1 used steps 30-34 for early stopping
        )
        return {
            "model_trained_at_end_of_step": b.tau,
            "model_last_label_step": b.card["train_steps"][1],
            "temporally_valid": bool(valid),
            "note": None
            if valid
            else "model was trained on data from this step or later; "
            "score is in-sample or look-ahead, not a valid historical replay",
        }

    # ------------------------------------------------------------------ scoring
    def score(self, version: str, tx_ids: list[int]) -> np.ndarray:
        b = self.bundles[version]
        return b.model.predict_proba(self.store.features(tx_ids, b.feature_columns))[:, 1]

    def _score_step_uncached(self, step: int, policy: str) -> pd.DataFrame:
        version = self.model_for(step, policy)
        frame = self.store.step_frame(step)
        ids = frame[S.TX_ID].tolist()
        scores = self.score(version, ids)
        out = pd.DataFrame(
            {
                S.TX_ID: frame[S.TX_ID].to_numpy(),
                "score": scores,
                "raw_class": frame[S.RAW_LABEL].to_numpy(),
            }
        )
        out["rank"] = out["score"].rank(ascending=False, method="min").astype(int)
        out["percentile"] = out["score"].rank(pct=True, method="max")
        out["label_status"] = out["raw_class"].map(LABEL_STATUS)
        out = out.sort_values(["rank", S.TX_ID]).reset_index(drop=True)
        out.attrs["model_version"] = version
        return out

    def ranking(self, step: int, policy: str) -> pd.DataFrame:
        with self._lock:
            return self._score_step(step, policy)

    # ------------------------------------------------------------------ metrics
    def step_metrics(self, step: int, policy: str) -> dict[str, Any]:
        r = self.ranking(step, policy)
        lab = r[r["raw_class"] != S.CLASS_UNKNOWN]
        y = (lab["raw_class"] == S.CLASS_ILLICIT).to_numpy().astype(int)
        s = lab["score"].to_numpy()
        y_all = (r["raw_class"] == S.CLASS_ILLICIT).to_numpy().astype(int)
        n_pos = int(y.sum())
        out: dict[str, Any] = {
            "step": step,
            "policy": policy,
            "model_version": r.attrs["model_version"],
            "n_transactions": len(r),
            "n_labelled": len(lab),
            "n_unknown": int((r["raw_class"] == S.CLASS_UNKNOWN).sum()),
            "n_illicit": n_pos,
            "labels_revealed_at_step": step + self.latency,
            "average_precision": _nan_to_none(fast_average_precision(y, s)) if n_pos else None,
            "roc_auc": float(roc_auc_score(y, s)) if 0 < n_pos < len(y) else None,
            "budgets": {},
        }
        for k in BUDGETS:
            out["budgets"][str(k)] = {
                "illicit_found_labelled_only": expected_hits_at_k(y, s, k) if len(y) else 0.0,
                "illicit_found_all_transactions": expected_hits_at_k(
                    y_all, r["score"].to_numpy(), k
                ),
                "max_possible": min(k, n_pos),
            }
        return out

    def timeline(self, policy: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._timeline(policy)

    @lru_cache(maxsize=8)  # noqa: B019 - engine lives for the process lifetime
    def _timeline(self, policy: str) -> list[dict[str, Any]]:
        out = []
        for step in self.replay_steps():
            try:
                out.append(self.step_metrics(step, policy))
            except ReplayError:
                continue
        return out

    # ------------------------------------------------------------------ explanations
    def explain(self, version: str, tx_id: int, top_k: int) -> dict[str, Any]:
        """Exact TreeSHAP contributions (log-odds) on the model's transformed inputs."""
        b = self.bundles[version]
        X = self.store.features([tx_id], b.feature_columns)
        pre, clf = b.model.named_steps["preprocess"], b.model.named_steps["clf"]
        Xt = pre.transform(X)
        names = list(pre.get_feature_names_out())
        contrib = clf.get_booster().predict(
            xgb.DMatrix(Xt, feature_names=names), pred_contribs=True
        )[0]
        values, bias = contrib[:-1], float(contrib[-1])
        order = np.argsort(-np.abs(values))[:top_k]
        raw = X.iloc[0]
        return {
            "method": "TreeSHAP (xgboost pred_contribs), log-odds units",
            "base_value": bias,
            "logit": float(values.sum() + bias),
            "top_contributions": [
                {
                    "feature": names[i],
                    "contribution": float(values[i]),
                    "value": _nan_to_none(float(raw[names[i]])),
                    "transformed_value": float(Xt[0, i]),
                }
                for i in order
            ],
            "caveat": "Contributions explain this model's score, not criminality. Most features "
            "are anonymised by the dataset publisher and have no public meaning.",
        }


def _nan_to_none(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
