"""Metrics, selection and uncertainty for backtest predictions.

All quantities are computed from saved ``predictions.parquet`` files. The unit of
analysis for uncertainty is the time step (see ``evaluation/uncertainty.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from chainwatch.data import schema as S
from chainwatch.evaluation.metrics import expected_hits_at_k, prevalence_standardised_ap
from chainwatch.evaluation.uncertainty import circular_block_indices, percentile_interval


def fast_average_precision(y: np.ndarray, s: np.ndarray) -> float:
    """Average precision identical to ``sklearn.metrics.average_precision_score``.

    Ties are handled as sklearn does (one operating point per distinct score), which is
    order-independent. A vectorised version lets the bootstrap run ~10^5 evaluations.
    """
    n_pos = y.sum()
    if n_pos == 0:
        return math.nan
    order = np.argsort(-s, kind="mergesort")
    s_sorted, y_sorted = s[order], y[order]
    last_of_group = np.r_[np.flatnonzero(np.diff(s_sorted)), len(s_sorted) - 1]
    tps = np.cumsum(y_sorted)[last_of_group]
    precision = tps / (last_of_group + 1)
    recall = tps / n_pos
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


@dataclass
class StepArrays:
    """Per (strategy, seed): labelled y/score per step, plus all-transaction arrays."""

    steps: list[int]
    y: dict[int, np.ndarray]
    s: dict[int, np.ndarray]
    y_all: dict[int, np.ndarray]  # illicit indicator over ALL scored txs (unknown -> 0)
    s_all: dict[int, np.ndarray]


def step_arrays(preds: pd.DataFrame) -> dict[tuple[str, int], StepArrays]:
    out = {}
    for (strategy, seed), g in preds.groupby(["strategy", "seed"], observed=True, sort=True):
        arrays = StepArrays([], {}, {}, {}, {})
        for t, gs in g.groupby(S.TIME_STEP, sort=True):
            lab = gs[S.LABEL].to_numpy() != S.LABEL_UNKNOWN
            arrays.steps.append(int(t))
            arrays.y[t] = gs[S.LABEL].to_numpy()[lab].astype(np.int64)
            arrays.s[t] = gs["score"].to_numpy()[lab]
            arrays.y_all[t] = (gs[S.LABEL].to_numpy() == S.LABEL_ILLICIT).astype(np.int64)
            arrays.s_all[t] = gs["score"].to_numpy()
        out[(str(strategy), int(seed))] = arrays
    return out


def per_step_table(preds: pd.DataFrame, budgets: list[int]) -> pd.DataFrame:
    rows = []
    for (strategy, seed), a in step_arrays(preds).items():
        for t in a.steps:
            y, s = a.y[t], a.s[t]
            n_pos = int(y.sum())
            row: dict[str, Any] = {
                "strategy": strategy,
                "seed": seed,
                S.TIME_STEP: t,
                "n": len(y),
                "n_illicit": n_pos,
                "prevalence": n_pos / len(y),
                "ap": fast_average_precision(y, s),
                "roc_auc": roc_auc_score(y, s) if 0 < n_pos < len(y) else math.nan,
            }
            for k in budgets:
                hits = expected_hits_at_k(y, s, k)
                row[f"hits@{k}"] = hits
                row[f"p@{k}"] = hits / min(k, len(y))
                row[f"r@{k}"] = hits / n_pos if n_pos else math.nan
                row[f"hits_all@{k}"] = expected_hits_at_k(a.y_all[t], a.s_all[t], k)
            rows.append(row)
    return pd.DataFrame(rows)


def pooled_metrics(
    a: StepArrays, steps: list[int], budgets: list[int], reference_prevalence: float
) -> dict[str, float]:
    y = np.concatenate([a.y[t] for t in steps])
    s = np.concatenate([a.s[t] for t in steps])
    per_step_ap = [fast_average_precision(a.y[t], a.s[t]) for t in steps]
    out = {
        "pooled_ap": fast_average_precision(y, s),
        "roc_auc": float(roc_auc_score(y, s)),
        "std_ap": prevalence_standardised_ap(y, s, reference_prevalence),
        "mean_step_ap": float(np.nanmean(per_step_ap)),
        "n_illicit": int(y.sum()),
        "prevalence": float(y.mean()),
    }
    for k in budgets:
        out[f"hits@{k}"] = float(sum(expected_hits_at_k(a.y[t], a.s[t], k) for t in steps))
        out[f"hits_all@{k}"] = float(
            sum(expected_hits_at_k(a.y_all[t], a.s_all[t], k) for t in steps)
        )
        out[f"max_hits@{k}"] = float(sum(min(k, int(a.y[t].sum())) for t in steps))
    return out


def summarize(
    preds: pd.DataFrame, budgets: list[int], reference_prevalence: float, steps: list[int]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-seed pooled metrics and their mean/std over seeds, per strategy."""
    arrays = step_arrays(preds)
    rows = [
        {"strategy": st, "seed": seed, **pooled_metrics(a, steps, budgets, reference_prevalence)}
        for (st, seed), a in arrays.items()
    ]
    per_seed = pd.DataFrame(rows)
    metric_cols = [c for c in per_seed.columns if c not in ("strategy", "seed")]
    agg = per_seed.groupby("strategy")[metric_cols].agg(["mean", "std"])
    agg.columns = [f"{m}_{stat}" for m, stat in agg.columns]
    return per_seed, agg.reset_index()


def cost_table(fits: pd.DataFrame) -> pd.DataFrame:
    """Compute charged to each strategy (cached fits still count: a deployment pays them)."""
    per_seed = fits.groupby(["strategy", "seed"]).agg(
        n_fits=("tau", "size"),
        fit_seconds=("fit_seconds_charged", "sum"),
        predict_seconds=("predict_seconds", "sum"),
        mean_train_rows=("n_train", "mean"),
    )
    return per_seed.groupby("strategy").mean().reset_index()


def select_per_family(
    summary: pd.DataFrame, fits: pd.DataFrame, families: dict[str, str], tolerance: float
) -> dict[str, str]:
    """Pre-registered rule: best seed-mean pooled AP; near-ties -> fewer fits, then the
    larger window / longer half-life / lower frequency (closest to the expanding model)."""
    n_fits = cost_table(fits).set_index("strategy")["n_fits"]
    chosen = {}
    for family in sorted(set(families.values())):
        cands = summary[summary["strategy"].map(families) == family].copy()
        best = cands["pooled_ap_mean"].max()
        near = cands[cands["pooled_ap_mean"] >= best - tolerance].copy()
        near["n_fits"] = near["strategy"].map(n_fits)
        near["param"] = near["strategy"].str.extract(r"(\d+(?:\.\d+)?)$")[0].astype(float).fillna(0)
        near = near.sort_values(["n_fits", "param"], ascending=[True, False])
        chosen[family] = str(near.iloc[0]["strategy"])
    return chosen


def pooled_ap_resampled(
    ys: list[np.ndarray], ss: list[np.ndarray], counts: np.ndarray, chunk: int = 256
) -> np.ndarray:
    """Pooled AP for many step-multisets at once.

    ``counts[r, i]`` is how often step ``i`` appears in replicate ``r``. Concatenating a
    step ``m`` times is identical to giving its rows weight ``m`` (precision and recall
    are ratios of weighted counts and tie groups are unchanged), so rows are sorted once
    and every replicate is a weighted cumulative sum, which is exact and avoids
    re-sorting.
    """
    y = np.concatenate(ys).astype(np.float64)
    s = np.concatenate(ss)
    step_of_row = np.concatenate([np.full(len(v), i) for i, v in enumerate(ys)])
    order = np.argsort(-s, kind="mergesort")
    y, s, step_of_row = y[order], s[order], step_of_row[order]
    group_end = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]
    out = np.empty(len(counts))
    for start in range(0, len(counts), chunk):
        w = counts[start : start + chunk][:, step_of_row]  # (chunk, n_rows)
        tp = np.cumsum(w * y, axis=1)[:, group_end]
        allw = np.cumsum(w, axis=1)[:, group_end]
        n_pos = tp[:, -1:]
        with np.errstate(invalid="ignore", divide="ignore"):
            precision = np.where(allw > 0, tp / allw, 0.0)
            recall = tp / n_pos
        d_recall = np.diff(np.concatenate([np.zeros((len(w), 1)), recall], axis=1), axis=1)
        ap = np.sum(d_recall * precision, axis=1)
        out[start : start + chunk] = np.where(n_pos[:, 0] > 0, ap, np.nan)
    return out


def bootstrap_comparison(
    preds: pd.DataFrame,
    baseline: str,
    steps: list[int],
    primary_budget: int,
    block_length: int,
    n_resamples: int,
    seed: int,
) -> pd.DataFrame:
    """Paired circular-block bootstrap over time steps of (strategy - baseline).

    For each replicate the same resampled steps are used for every strategy; each metric
    is computed per training seed and then averaged over seeds.
    """
    arrays = step_arrays(preds)
    strategies = sorted({st for st, _ in arrays})
    seeds = sorted({sd for _, sd in arrays})
    rng = np.random.default_rng(seed)
    idx = circular_block_indices(len(steps), min(block_length, len(steps)), n_resamples, rng)
    k = primary_budget

    def stats(st: str) -> dict[str, np.ndarray]:
        """Point estimate (row 0) followed by bootstrap replicates."""
        res = {"pooled_ap": [], f"hits@{k}": [], "mean_step_ap": []}
        for sd in seeds:
            a = arrays[(st, sd)]
            step_ap = np.array([fast_average_precision(a.y[t], a.s[t]) for t in steps])
            step_hits = np.array([expected_hits_at_k(a.y[t], a.s[t], k) for t in steps])
            counts = np.zeros((len(idx) + 1, len(steps)))
            counts[0] = 1.0  # row 0: the original sample (point estimate)
            for r, row in enumerate(idx, start=1):
                np.add.at(counts[r], row, 1.0)
            ap_rows = pooled_ap_resampled([a.y[t] for t in steps], [a.s[t] for t in steps], counts)
            res["pooled_ap"].append(ap_rows)
            res[f"hits@{k}"].append(np.r_[step_hits.sum(), step_hits[idx].sum(axis=1)])
            res["mean_step_ap"].append(np.r_[np.nanmean(step_ap), np.nanmean(step_ap[idx], axis=1)])
        return {m: np.mean(np.array(v, dtype=float), axis=0) for m, v in res.items()}

    base = stats(baseline)
    rows = []
    for st in strategies:
        cur = base if st == baseline else stats(st)
        for metric in cur:
            diff = cur[metric] - base[metric]
            lo, hi = percentile_interval(cur[metric][1:])
            dlo, dhi = percentile_interval(diff[1:])
            rows.append(
                {
                    "strategy": st,
                    "metric": metric,
                    "estimate": cur[metric][0],
                    "ci_low": lo,
                    "ci_high": hi,
                    "diff_vs_baseline": diff[0],
                    "diff_ci_low": dlo,
                    "diff_ci_high": dhi,
                    "p_diff_le_0": float(np.mean(diff[1:] <= 0)),
                }
            )
    return pd.DataFrame(rows)
