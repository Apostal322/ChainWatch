"""In-process inference latency measurement.

These numbers describe model + preprocessing compute on the current machine only
(no network, serialisation, or feature retrieval). They are useful for comparing
models with each other, not as an SLA for a deployed service.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd


def measure_latency(model, X: pd.DataFrame, repeats: int = 200, batch_repeats: int = 5) -> dict:
    """Single-row latency percentiles and full-batch throughput for ``predict_proba``."""
    rng = np.random.default_rng(0)
    model.predict_proba(X.iloc[:1])  # warm-up (lazy init, caches)

    single_ms = []
    for i in rng.integers(0, len(X), size=repeats):
        row = X.iloc[[int(i)]]
        t0 = time.perf_counter()
        model.predict_proba(row)
        single_ms.append((time.perf_counter() - t0) * 1e3)

    batch_s = []
    for _ in range(batch_repeats):
        t0 = time.perf_counter()
        model.predict_proba(X)
        batch_s.append(time.perf_counter() - t0)
    batch_median = float(np.median(batch_s))

    return {
        "single_row_ms_p50": float(np.percentile(single_ms, 50)),
        "single_row_ms_p95": float(np.percentile(single_ms, 95)),
        "batch_size": len(X),
        "batch_seconds_median": batch_median,
        "batch_us_per_tx": batch_median / len(X) * 1e6,
    }
