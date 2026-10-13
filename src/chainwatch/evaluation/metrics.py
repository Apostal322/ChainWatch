"""Imbalance-aware evaluation metrics.

Why these metrics: with ~10% illicit (and as low as 0.3% in late time steps),
accuracy is dominated by the licit class and ROC-AUC can look excellent while
the top of the ranking is poor. Average precision (area under the PR curve)
and Precision/Recall@K describe what an investigator with a fixed review budget
actually experiences, so they are the primary metrics here.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)

from chainwatch.data import schema as S


def _check(y_true: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y_true).astype(np.int64).ravel()
    s = np.asarray(scores, dtype=np.float64).ravel()
    if y.shape != s.shape:
        raise ValueError(f"shape mismatch: y {y.shape} vs scores {s.shape}")
    if not np.isin(y, (0, 1)).all():
        raise ValueError("y_true must be binary {0, 1}")
    if not np.isfinite(s).all():
        raise ValueError("scores contain NaN/inf")
    return y, s


def ranking_order(scores: np.ndarray) -> np.ndarray:
    """Indices sorted by descending score; ties keep original order (deterministic)."""
    return np.argsort(-np.asarray(scores, dtype=np.float64), kind="stable")


def expected_hits_at_k(y_true: np.ndarray, scores: np.ndarray, k: int) -> float:
    """Expected number of positives in the top-K under random tie-breaking.

    Models with saturated probabilities (e.g. weakly regularised logistic regression)
    can assign the *same* score to hundreds of transactions. Picking "the first K" of a
    tied group would then depend on row order, an arbitrary artefact. Instead, the tied
    group straddling the cut-off contributes its positive rate times the remaining slots,
    which is the expectation over all orderings of the ties and is order-independent.
    """
    y, s = _check(y_true, scores)
    if k <= 0:
        raise ValueError("k must be positive")
    k = min(k, len(y))
    kth = s[ranking_order(s)[k - 1]]
    above = s > kth
    tied = s == kth
    remaining = k - int(above.sum())
    return float(y[above].sum() + remaining * y[tied].mean())


def precision_at_k(y_true: np.ndarray, scores: np.ndarray, k: int) -> float:
    """(Expected) fraction of illicit transactions among the K highest-scored ones."""
    k_eff = min(k, len(np.asarray(y_true))) if k > 0 else k
    return expected_hits_at_k(y_true, scores, k) / k_eff


def recall_at_k(y_true: np.ndarray, scores: np.ndarray, k: int) -> float:
    """(Expected) fraction of all illicit transactions captured in the top-K; NaN if none."""
    n_pos = np.asarray(y_true).sum()
    hits = expected_hits_at_k(y_true, scores, k)
    return math.nan if n_pos == 0 else float(hits / n_pos)


def n_tied_at_top(scores: np.ndarray) -> int:
    """How many items share the maximum score (diagnoses saturated probabilities)."""
    s = np.asarray(scores, dtype=np.float64)
    return int((s == s.max()).sum())


def threshold_free_metrics(y_true: np.ndarray, scores: np.ndarray) -> dict[str, float]:
    y, s = _check(y_true, scores)
    both = 0 < y.sum() < len(y)
    return {
        "average_precision": float(average_precision_score(y, s)) if y.sum() else math.nan,
        "roc_auc": float(roc_auc_score(y, s)) if both else math.nan,
        "prevalence": float(y.mean()),
    }


def thresholded_metrics(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, Any]:
    """Precision/recall/F1 and confusion matrix for predictions ``score >= threshold``."""
    y, s = _check(y_true, scores)
    pred = (s >= threshold).astype(np.int64)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "threshold": float(threshold),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "flagged_rate": float(pred.mean()),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def select_threshold(
    y_true: np.ndarray, scores: np.ndarray, strategy: str = "max_f1", fixed: float = 0.5
) -> float:
    """Choose a decision threshold. Must only ever be called on validation data."""
    if strategy == "fixed":
        return float(fixed)
    if strategy != "max_f1":
        raise ValueError(f"unknown threshold strategy {strategy!r}")
    y, s = _check(y_true, scores)
    precision, recall, thresholds = precision_recall_curve(y, s)
    # precision/recall have one more entry than thresholds (the final (1, 0) point).
    p, r = precision[:-1], recall[:-1]
    f1 = np.divide(2 * p * r, p + r, out=np.zeros_like(p), where=(p + r) > 0)
    return float(thresholds[int(np.argmax(f1))])


def evaluate_scores(
    y_true: np.ndarray, scores: np.ndarray, threshold: float, k_values: list[int]
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n": len(y_true),
        "n_illicit": int(np.sum(y_true)),
        "n_tied_at_top_score": n_tied_at_top(scores),
    }
    out.update(threshold_free_metrics(y_true, scores))
    out.update(thresholded_metrics(y_true, scores, threshold))
    out["at_k"] = {
        str(k): {
            "precision": precision_at_k(y_true, scores, k),
            "recall": recall_at_k(y_true, scores, k),
        }
        for k in k_values
    }
    return out


def per_time_step_metrics(
    df: pd.DataFrame, score_col: str, threshold: float, k: int
) -> pd.DataFrame:
    """Metrics for each time step separately: exposes drift that pooled metrics hide.

    ``precision_at_k`` here models a per-period investigation budget of K reviews.
    """
    rows = []
    for t, g in df.groupby(S.TIME_STEP, sort=True):
        y = g[S.LABEL].to_numpy()
        s = g[score_col].to_numpy()
        tf = threshold_free_metrics(y, s)
        th = thresholded_metrics(y, s, threshold)
        rows.append(
            {
                S.TIME_STEP: int(t),
                "n": len(g),
                "n_illicit": int(y.sum()),
                "average_precision": tf["average_precision"],
                "roc_auc": tf["roc_auc"],
                "precision": th["precision"],
                "recall": th["recall"],
                "f1": th["f1"],
                f"precision_at_{k}": precision_at_k(y, s, k),
                f"recall_at_{k}": recall_at_k(y, s, k),
            }
        )
    return pd.DataFrame(rows)


def prevalence_standardised_ap(
    y_true: np.ndarray, scores: np.ndarray, reference_prevalence: float
) -> float:
    """AP after re-weighting negatives so the effective prevalence equals a reference.

    AP's random baseline equals prevalence, so AP falls mechanically when positives become
    rarer even if the ranking (the ROC curve) is unchanged. Re-weighting every negative by
    ``(pi / (1 - pi)) * ((1 - ref) / ref)`` keeps each score's TPR/FPR unchanged while
    setting the prevalence to ``ref``, so this metric isolates ranking quality from
    prior-probability shift.
    """
    y, s = _check(y_true, scores)
    if not 0 < reference_prevalence < 1:
        raise ValueError("reference_prevalence must be in (0, 1)")
    pi = y.mean()
    if pi in (0.0, 1.0):
        return math.nan
    w_neg = (pi / (1 - pi)) * ((1 - reference_prevalence) / reference_prevalence)
    w = np.where(y == 1, 1.0, w_neg)
    return float(average_precision_score(y, s, sample_weight=w))


def calibration_metrics(
    y_true: np.ndarray, probs: np.ndarray, n_bins: int = 10
) -> dict[str, float]:
    """Brier score, expected calibration error (equal-width bins), mean predicted vs observed."""
    y, p = _check(y_true, probs)
    if (p < 0).any() or (p > 1).any():
        raise ValueError("probabilities must lie in [0, 1]")
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    ece = 0.0
    for b in range(n_bins):
        m = bins == b
        if m.any():
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
    return {
        "brier": float(np.mean((p - y) ** 2)),
        "ece": float(ece),
        "mean_predicted": float(p.mean()),
        "observed_rate": float(y.mean()),
    }
