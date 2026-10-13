"""Resampling-based uncertainty for temporally dependent evaluation data.

Rows are not independent here: all transactions in a time step belong to one connected
transaction graph and share a market regime, and neighbouring steps are correlated.
A row-level bootstrap would therefore understate uncertainty badly. We resample
**time steps** in contiguous blocks with the circular block bootstrap (Politis &
Romano, 1992), a moving-block variant that wraps around so that the first and last
steps are not under-sampled. ``block_length=1`` reduces to a plain step-cluster bootstrap.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np


def circular_block_indices(
    n_units: int, block_length: int, n_resamples: int, rng: np.random.Generator
) -> np.ndarray:
    """Return ``(n_resamples, n_units)`` indices of resampled units (e.g. time steps)."""
    if n_units < 1:
        raise ValueError("need at least one unit")
    if not 1 <= block_length <= n_units:
        raise ValueError(f"block_length must be in [1, {n_units}]")
    n_blocks = -(-n_units // block_length)  # ceil
    starts = rng.integers(0, n_units, size=(n_resamples, n_blocks))
    offsets = np.arange(block_length)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n_units
    return idx.reshape(n_resamples, -1)[:, :n_units]


def percentile_interval(samples: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    samples = np.asarray(samples, dtype=np.float64)
    samples = samples[np.isfinite(samples)]
    if samples.size == 0:
        return (float("nan"), float("nan"))
    alpha = (1 - level) / 2
    lo, hi = np.quantile(samples, [alpha, 1 - alpha])
    return float(lo), float(hi)


def block_bootstrap(
    units: Sequence,
    statistic: Callable[[list], float],
    block_length: int,
    n_resamples: int,
    seed: int = 0,
) -> np.ndarray:
    """Bootstrap distribution of ``statistic(resampled_units)``."""
    rng = np.random.default_rng(seed)
    idx = circular_block_indices(len(units), block_length, n_resamples, rng)
    return np.array([statistic([units[i] for i in row]) for row in idx])
