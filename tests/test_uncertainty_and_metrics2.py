"""Block bootstrap and Phase 2 metric additions."""

from __future__ import annotations

import math

import numpy as np
import pytest
from sklearn.metrics import average_precision_score

from chainwatch.backtest.analysis import fast_average_precision
from chainwatch.evaluation.metrics import calibration_metrics, prevalence_standardised_ap
from chainwatch.evaluation.uncertainty import (
    block_bootstrap,
    circular_block_indices,
    percentile_interval,
)


def test_circular_blocks_are_contiguous_mod_n():
    rng = np.random.default_rng(0)
    idx = circular_block_indices(10, 3, 100, rng)
    assert idx.shape == (100, 10)
    assert idx.min() >= 0 and idx.max() < 10
    for row in idx:
        for b in range(0, 9, 3):  # first three complete blocks
            block = row[b : b + 3]
            assert ((np.diff(block) % 10) == 1).all()


def test_circular_bootstrap_is_unbiased_in_coverage():
    """Every unit should be drawn equally often (the point of the circular variant)."""
    rng = np.random.default_rng(1)
    idx = circular_block_indices(15, 3, 20000, rng)
    freq = np.bincount(idx.ravel(), minlength=15) / idx.size
    np.testing.assert_allclose(freq, 1 / 15, atol=0.003)


def test_block_length_one_is_iid_cluster_bootstrap():
    rng = np.random.default_rng(2)
    idx = circular_block_indices(5, 1, 1000, rng)
    assert idx.shape == (1000, 5)
    with pytest.raises(ValueError):
        circular_block_indices(5, 6, 10, rng)
    with pytest.raises(ValueError):
        circular_block_indices(0, 1, 10, rng)


def test_block_bootstrap_mean_interval_contains_truth():
    units = list(np.random.default_rng(3).normal(5.0, 1.0, size=40))
    dist = block_bootstrap(units, lambda xs: float(np.mean(xs)), 4, 500, seed=0)
    lo, hi = percentile_interval(dist)
    assert lo < np.mean(units) < hi
    assert hi - lo < 1.5


def test_percentile_interval_ignores_nan():
    lo, hi = percentile_interval(np.array([np.nan, 1.0, 2.0, 3.0]))
    assert 1.0 <= lo < hi <= 3.0
    assert all(math.isnan(v) for v in percentile_interval(np.array([np.nan])))


def test_fast_ap_matches_sklearn_with_ties():
    rng = np.random.default_rng(4)
    for _ in range(200):
        n = int(rng.integers(2, 200))
        y = rng.integers(0, 2, n)
        if y.sum() == 0:
            continue
        s = np.round(rng.random(n), int(rng.integers(1, 3)))
        assert fast_average_precision(y, s) == pytest.approx(
            average_precision_score(y, s), abs=1e-12
        )
    assert math.isnan(fast_average_precision(np.zeros(4, int), np.arange(4.0)))


def test_prevalence_standardised_ap_removes_prior_shift():
    """Duplicating negatives lowers prevalence and AP, but not the standardised AP."""
    rng = np.random.default_rng(5)
    y = (rng.random(2000) < 0.2).astype(int)
    s = rng.normal(size=2000) + 1.2 * y
    neg = y == 0
    y2 = np.r_[y, y[neg], y[neg], y[neg]]
    s2 = np.r_[s, s[neg], s[neg], s[neg]]
    assert average_precision_score(y2, s2) < average_precision_score(y, s) - 0.05
    a = prevalence_standardised_ap(y, s, 0.1)
    b = prevalence_standardised_ap(y2, s2, 0.1)
    assert a == pytest.approx(b, abs=1e-9)
    # at the reference prevalence it equals plain AP
    ref = y.mean()
    assert prevalence_standardised_ap(y, s, ref) == pytest.approx(average_precision_score(y, s))


def test_calibration_metrics():
    y = np.array([0, 0, 1, 1])
    perfect = calibration_metrics(y, y.astype(float))
    assert perfect["brier"] == 0 and perfect["ece"] == 0
    flat = calibration_metrics(y, np.full(4, 0.5))
    assert flat["brier"] == pytest.approx(0.25)
    assert flat["ece"] == pytest.approx(0.0)
    assert flat["mean_predicted"] == 0.5 and flat["observed_rate"] == 0.5
    over = calibration_metrics(np.array([0, 0, 0, 1]), np.full(4, 0.9))
    assert over["ece"] == pytest.approx(0.65)
    with pytest.raises(ValueError):
        calibration_metrics(y, np.array([0.1, 1.2, 0.3, 0.4]))


def test_pooled_ap_resampled_equals_concatenation():
    from chainwatch.backtest.analysis import pooled_ap_resampled

    rng = np.random.default_rng(7)
    ys = [rng.integers(0, 2, int(rng.integers(5, 40))) for _ in range(6)]
    ss = [np.round(rng.random(len(y)), 2) for y in ys]  # rounded -> many ties across steps
    idx = circular_block_indices(6, 2, 50, rng)
    counts = np.zeros((51, 6))
    counts[0] = 1
    for r, row in enumerate(idx, start=1):
        np.add.at(counts[r], row, 1)
    got = pooled_ap_resampled(ys, ss, counts, chunk=7)
    for r in range(51):
        rows = [i for i in range(6) for _ in range(int(counts[r, i]))]
        y = np.concatenate([ys[i] for i in rows])
        s = np.concatenate([ss[i] for i in rows])
        expected = average_precision_score(y, s) if y.sum() else np.nan
        assert got[r] == pytest.approx(expected, abs=1e-12, nan_ok=True)
