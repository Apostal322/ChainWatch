"""Execute retraining schedules on the transaction table.

For each strategy and seed, every deployment in its schedule fits an XGBoost pipeline
on the labelled transactions of the plan's training steps, then scores **all**
transactions (labelled and unknown) of the steps it covers. Unknown transactions are
scored so that budget metrics can also be computed in the conservative setting where
unlabelled transactions compete for review slots.

Identical training problems (same training steps, same weighting, same seed) are fitted
once and reused. In a real deployment each strategy would pay for its own fits, so cost
reporting charges every strategy for all its fits (``fit_seconds_charged``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from chainwatch.backtest.schedule import CausalityError, StrategySpec, TrainingPlan, build_schedule
from chainwatch.data import schema as S
from chainwatch.features.preprocessing import build_preprocessor
from chainwatch.utils import get_logger

log = get_logger(__name__)


@dataclass
class BacktestResult:
    predictions: pd.DataFrame  # strategy, family, seed, time_step, tx_id, label, score, tau
    fits: pd.DataFrame  # one row per (strategy, seed, deployment)


def fit_plan(
    tx: pd.DataFrame,
    plan: TrainingPlan,
    feature_cols: list[str],
    xgb_params: dict[str, Any],
    seed: int,
    log1p_augmented: bool = True,
) -> Pipeline:
    """Fit preprocessing + XGBoost on the labelled rows of ``plan.train_steps`` only."""
    train = tx[tx[S.TIME_STEP].isin(plan.train_steps) & (tx[S.LABEL] != S.LABEL_UNKNOWN)]
    if train[S.TIME_STEP].max() > plan.tau:
        raise CausalityError("training rows from after the training time")
    y = train[S.LABEL].to_numpy(dtype=np.int64)
    if y.sum() == 0 or y.sum() == len(y):
        raise ValueError(f"training set at tau={plan.tau} has a single class")
    weights = plan.step_weights()
    w = train[S.TIME_STEP].map(weights).to_numpy(dtype=np.float64)
    # Imbalance re-weighting on the *weighted* class totals, so recency weights and
    # class weights compose consistently.
    spw = float(w[y == 0].sum() / w[y == 1].sum())
    params = {k: v for k, v in xgb_params.items() if k != "scale_pos_weight"}
    model = Pipeline(
        [
            (
                "preprocess",
                build_preprocessor(feature_cols, log1p_augmented=log1p_augmented, scale=False),
            ),
            ("clf", XGBClassifier(random_state=seed, scale_pos_weight=spw, **params)),
        ]
    )
    model.fit(train[feature_cols], y, clf__sample_weight=w)
    return model


def run_backtest(
    tx: pd.DataFrame,
    specs: list[StrategySpec],
    origin: int,
    end: int,
    latency: int,
    seeds: list[int],
    feature_cols: list[str],
    xgb_params: dict[str, Any],
    log1p_augmented: bool = True,
) -> BacktestResult:
    by_step = {t: g for t, g in tx.groupby(S.TIME_STEP)}
    schedules = {spec: build_schedule(spec, origin, end, latency) for spec in specs}
    # Reference counts let us evict a cached model after its last use (bounded memory).
    remaining: dict[tuple, int] = {}
    for sched in schedules.values():
        for dep in sched.deployments:
            for seed in seeds:
                key = (*dep.plan.cache_key, seed)
                remaining[key] = remaining.get(key, 0) + 1
    cache: dict[tuple, tuple[Pipeline, float]] = {}
    pred_frames: list[pd.DataFrame] = []
    fit_rows: list[dict[str, Any]] = []

    for spec in specs:
        sched = schedules[spec]
        for seed in seeds:
            t_strategy = time.perf_counter()
            for dep in sched.deployments:
                plan = dep.plan
                key = (*plan.cache_key, seed)
                cached = key in cache
                if not cached:
                    t0 = time.perf_counter()
                    model = fit_plan(tx, plan, feature_cols, xgb_params, seed, log1p_augmented)
                    cache[key] = (model, time.perf_counter() - t0)
                model, fit_seconds = cache[key]
                remaining[key] -= 1
                if remaining[key] == 0:
                    del cache[key]
                scored = pd.concat([by_step[s] for s in dep.scored_steps])
                t0 = time.perf_counter()
                scores = model.predict_proba(scored[feature_cols])[:, 1]
                predict_seconds = time.perf_counter() - t0
                pred_frames.append(
                    pd.DataFrame(
                        {
                            "strategy": spec.name,
                            "family": spec.family,
                            "seed": seed,
                            S.TIME_STEP: scored[S.TIME_STEP].to_numpy(),
                            S.TX_ID: scored[S.TX_ID].to_numpy(),
                            S.LABEL: scored[S.LABEL].to_numpy(),
                            "score": scores.astype(np.float64),
                            "tau": plan.tau,
                        }
                    )
                )
                train_mask = tx[S.TIME_STEP].isin(plan.train_steps) & (
                    tx[S.LABEL] != S.LABEL_UNKNOWN
                )
                fit_rows.append(
                    {
                        "strategy": spec.name,
                        "family": spec.family,
                        "seed": seed,
                        "tau": plan.tau,
                        "train_first_step": min(plan.train_steps),
                        "train_last_step": max(plan.train_steps),
                        "n_train_steps": len(plan.train_steps),
                        "n_train": int(train_mask.sum()),
                        "n_train_illicit": int((tx.loc[train_mask, S.LABEL] == 1).sum()),
                        "half_life": plan.half_life,
                        "scored_steps": ",".join(map(str, dep.scored_steps)),
                        "fit_seconds_charged": fit_seconds,
                        "fit_was_cached": cached,
                        "predict_seconds": predict_seconds,
                    }
                )
            log.info(
                "%-22s seed=%d fits=%d (%.1fs wall)",
                spec.name,
                seed,
                sched.n_fits,
                time.perf_counter() - t_strategy,
            )

    preds = pd.concat(pred_frames, ignore_index=True)
    preds["strategy"] = preds["strategy"].astype("category")
    preds["family"] = preds["family"].astype("category")
    return BacktestResult(predictions=preds, fits=pd.DataFrame(fit_rows))
