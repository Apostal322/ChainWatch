"""Check that historical replay reproduces the saved Phase 2 backtest predictions.

For every replay step and policy (expanding, static) with a matching Phase 2 backtest
artifact, compare the service's score for every transaction (labelled and unknown) with
the backtest prediction for seed = serving.snapshot_seed. Writes a JSON report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from chainwatch.backtest.pipeline import find_stage_runs
from chainwatch.config import Config
from chainwatch.data import schema as S
from chainwatch.serving.app import ServiceState
from chainwatch.utils import get_logger, write_json

log = get_logger(__name__)


def verify_replay(cfg: Config, out: Path | None = None, tolerance: float = 1e-6) -> dict[str, Any]:
    svc = ServiceState(cfg)
    seed = cfg.serving.snapshot_seed
    latency = cfg.backtest.label_latency
    rows = []
    for stage in ("dev", "confirm"):
        runs = find_stage_runs(cfg.paths.backtests_dir, stage, latency)
        if not runs:
            log.warning("no %s backtest at L=%d; skipping", stage, latency)
            continue
        preds = pd.read_parquet(runs[-1] / "predictions.parquet", filters=[("seed", "==", seed)])
        for policy in ("expanding", "static"):
            p = preds[preds["strategy"].astype(str) == policy]
            for step, g in p.groupby(S.TIME_STEP):
                ours = svc.engine.ranking(int(step), policy).set_index(S.TX_ID)["score"]
                diff = np.abs(ours.loc[g[S.TX_ID]].to_numpy() - g["score"].to_numpy())
                rows.append(
                    {
                        "stage": stage,
                        "run": runs[-1].name,
                        "policy": policy,
                        "step": int(step),
                        "n": len(g),
                        "max_abs_diff": float(diff.max()),
                    }
                )
    df = pd.DataFrame(rows)
    phase1 = _verify_phase1(cfg, svc)
    result = {
        "seed": seed,
        "label_latency": latency,
        "tolerance": tolerance,
        "n_steps_checked": len(df),
        "n_predictions_checked": int(df["n"].sum()) if len(df) else 0,
        "max_abs_diff": float(df["max_abs_diff"].max()) if len(df) else None,
        "all_within_tolerance": bool(
            len(df)
            and (df["max_abs_diff"] <= tolerance).all()
            and (phase1 is None or phase1["max_abs_diff"] <= tolerance)
        ),
        "phase1_reference": phase1,
        "per_step": rows,
    }
    out = out or cfg.paths.reports_dir / "phase3" / "replay_consistency.json"
    write_json(out, result)
    log.info(
        "replay consistency: %d predictions, max |diff| = %s -> %s",
        result["n_predictions_checked"],
        result["max_abs_diff"],
        out,
    )
    return result


def _verify_phase1(cfg: Config, svc: ServiceState) -> dict[str, Any] | None:
    """Phase 1 reference replay vs the Phase 1 run's saved test predictions."""
    from sklearn.metrics import average_precision_score

    from chainwatch.utils import read_json

    v = svc.registry_meta.get("phase1_reference")
    if not v:
        return None
    run = cfg.paths.artifacts_dir / svc.bundles[v].card["source_run"]
    saved = pd.read_parquet(run / "test_predictions.parquet")
    ours = pd.concat(
        [
            svc.engine.ranking(int(t), "phase1").set_index(S.TX_ID)["score"]
            for t in sorted(saved[S.TIME_STEP].unique())
        ]
    )
    diff = np.abs(ours.loc[saved[S.TX_ID]].to_numpy() - saved["score"].to_numpy())
    reported = read_json(run / "test_metrics.json")["test"]["average_precision"]
    return {
        "run": run.name,
        "n": len(saved),
        "max_abs_diff": float(diff.max()),
        "replay_pooled_ap": float(
            average_precision_score(saved[S.LABEL], ours.loc[saved[S.TX_ID]])
        ),
        "reported_test_ap": reported,
    }
