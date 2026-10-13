"""Metric calculations against hand-computed values."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from chainwatch.data import schema as S
from chainwatch.evaluation.metrics import (
    evaluate_scores,
    per_time_step_metrics,
    precision_at_k,
    recall_at_k,
    select_threshold,
    thresholded_metrics,
)

Y = np.array([1, 0, 1, 0, 0, 1, 0, 0])
SCORES = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2])


def test_precision_recall_at_k_by_hand():
    # ranking: 1,0,1,0,0,1,0,0
    assert precision_at_k(Y, SCORES, 1) == 1.0
    assert precision_at_k(Y, SCORES, 2) == 0.5
    assert precision_at_k(Y, SCORES, 3) == pytest.approx(2 / 3)
    assert recall_at_k(Y, SCORES, 3) == pytest.approx(2 / 3)
    assert recall_at_k(Y, SCORES, 6) == 1.0


def test_at_k_caps_k_and_handles_no_positives():
    assert precision_at_k(Y, SCORES, 100) == pytest.approx(Y.mean())
    assert math.isnan(recall_at_k(np.zeros(4, int), np.arange(4.0), 2))
    with pytest.raises(ValueError):
        precision_at_k(Y, SCORES, 0)


def test_at_k_ties_use_expected_value_not_row_order():
    y = np.array([0, 1, 0, 1])
    s = np.array([0.5, 0.5, 0.5, 0.5])
    # all tied: expected precision for any K equals the tied group's positive rate
    assert precision_at_k(y, s, 1) == 0.5
    assert precision_at_k(y[::-1], s, 1) == 0.5  # row order no longer matters
    # partial tie at the cut-off: 1 sure hit above + 1 slot from a tie with rate 1/3
    y2 = np.array([1, 0, 1, 0, 0])
    s2 = np.array([0.9, 0.4, 0.4, 0.4, 0.1])
    assert precision_at_k(y2, s2, 2) == pytest.approx((1 + 1 / 3) / 2)
    assert recall_at_k(y2, s2, 2) == pytest.approx((1 + 1 / 3) / 2)


def test_at_k_matches_naive_definition_without_ties():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 200)
    s = rng.random(200)
    order = np.argsort(-s)
    for k in (1, 7, 50, 200):
        assert precision_at_k(y, s, k) == pytest.approx(y[order[:k]].mean())


def test_thresholded_metrics_confusion_matrix():
    m = thresholded_metrics(Y, SCORES, threshold=0.65)
    # predicted positive: 0.9, 0.8, 0.7 -> tp=2, fp=1; fn=1; tn=4
    assert m["confusion_matrix"] == {"tn": 4, "fp": 1, "fn": 1, "tp": 2}
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)


def test_threshold_is_inclusive():
    m = thresholded_metrics(np.array([1, 0]), np.array([0.5, 0.4]), threshold=0.5)
    assert m["confusion_matrix"]["tp"] == 1


def test_select_threshold_maximises_f1():
    thr = select_threshold(Y, SCORES, "max_f1")
    best = thresholded_metrics(Y, SCORES, thr)["f1"]
    for t in SCORES:
        assert thresholded_metrics(Y, SCORES, t)["f1"] <= best + 1e-12
    assert select_threshold(Y, SCORES, "fixed", fixed=0.3) == 0.3


def test_evaluate_scores_reference_values():
    out = evaluate_scores(Y, SCORES, 0.65, [2, 4])
    # AP = mean of precision at each positive's rank: (1/1 + 2/3 + 3/6) / 3
    assert out["average_precision"] == pytest.approx((1 + 2 / 3 + 0.5) / 3)
    # ROC-AUC = P(score_pos > score_neg): positives at ranks 1, 3, 6 beat 5 + 4 + 2 of 5 negatives
    assert out["roc_auc"] == pytest.approx(11 / 15)
    assert out["at_k"]["2"]["precision"] == 0.5
    assert out["n_illicit"] == 3


def test_metric_input_validation():
    with pytest.raises(ValueError):
        precision_at_k(np.array([0, 2]), np.array([0.1, 0.2]), 1)
    with pytest.raises(ValueError):
        precision_at_k(np.array([0, 1]), np.array([0.1, np.nan]), 1)


def test_per_time_step_handles_steps_without_positives():
    df = pd.DataFrame(
        {
            S.TIME_STEP: [1, 1, 1, 2, 2],
            S.LABEL: [1, 0, 0, 0, 0],
            "score": [0.9, 0.1, 0.2, 0.3, 0.4],
        }
    )
    out = per_time_step_metrics(df, "score", 0.5, k=1)
    assert list(out[S.TIME_STEP]) == [1, 2]
    assert out.loc[0, "average_precision"] == 1.0
    assert math.isnan(out.loc[1, "average_precision"])
    assert out.loc[0, "precision_at_1"] == 1.0
