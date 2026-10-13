"""Train / evaluate orchestration.

The protocol is split into two commands on purpose:

* ``train``    uses ONLY the train and validation splits: fits candidates from the
               hyper-parameter grid, selects by validation average precision, chooses the
               decision threshold on validation, and saves the artifact.
* ``evaluate`` touches the test split, once, using the frozen model and threshold.
               It refuses to overwrite an existing test evaluation unless forced, which
               makes "peek at test, tweak, re-run" visible instead of silent.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from chainwatch.config import REPO_ROOT, Config, PreprocessingConfig, SplitConfig
from chainwatch.data import schema as S
from chainwatch.data.loader import load_manifest, load_processed
from chainwatch.data.splits import (
    LeakageError,
    TemporalSplit,
    count_cross_split_edges,
    temporal_split,
)
from chainwatch.evaluation.latency import measure_latency
from chainwatch.evaluation.metrics import evaluate_scores, per_time_step_metrics, select_threshold
from chainwatch.features.preprocessing import fill_degrees_from_edges, make_xy
from chainwatch.models.artifacts import (
    TEST_METRICS_FILE,
    load_artifact,
    new_run_dir,
    save_artifact,
)
from chainwatch.models.baselines import describe_fitted, fit_model, param_grid
from chainwatch.training.tracking import log_run
from chainwatch.utils import environment_info, get_logger, git_commit, set_global_seed, write_json

log = get_logger(__name__)


def load_split(
    processed_dir: Path, split_cfg: SplitConfig, pre_cfg: PreprocessingConfig, feature_set: str
) -> TemporalSplit:
    ds = load_processed(processed_dir)
    tx = ds.transactions
    if pre_cfg.recompute_degrees:
        tx = fill_degrees_from_edges(tx, ds.edges)
    n_cross = count_cross_split_edges(ds.edges, tx, split_cfg)
    if n_cross and feature_set != "local":
        raise LeakageError(
            f"{n_cross} edges cross split boundaries; neighbourhood (aggregate) features "
            "would leak information between splits."
        )
    return temporal_split(tx, split_cfg)


def train(cfg: Config, model_name: str) -> Path:
    set_global_seed(cfg.seed)
    split = load_split(cfg.paths.processed_dir, cfg.split, cfg.preprocessing, cfg.feature_set)
    log.info("Split summary:\n%s", split.summary().to_string(index=False))

    feature_cols = S.feature_columns(cfg.feature_set)
    X_tr, y_tr = make_xy(split.train, feature_cols)
    X_va, y_va = make_xy(split.val, feature_cols)

    mcfg = cfg.model_params(model_name)
    base = {k: v for k, v in mcfg.items() if k != "grid"}
    candidates: list[dict[str, Any]] = []
    best: tuple[float, Any, dict[str, Any]] | None = None

    for params in param_grid(mcfg.get("grid", {})):
        t0 = time.perf_counter()
        model = fit_model(
            model_name,
            X_train=X_tr,
            y_train=y_tr,
            X_val=X_va,
            y_val=y_va,
            params=params,
            base=base,
            seed=cfg.seed,
            log1p_augmented=cfg.preprocessing.log1p_augmented,
        )
        val_scores = model.predict_proba(X_va)[:, 1]
        ap = evaluate_scores(y_va, val_scores, 0.5, [])["average_precision"]
        info = describe_fitted(model_name, model)
        candidates.append(
            {
                "params": params,
                "val_average_precision": ap,
                "fit_seconds": time.perf_counter() - t0,
                **info,
            }
        )
        log.info(
            "%s %s -> val AP=%.4f (%.1fs)", model_name, params, ap, candidates[-1]["fit_seconds"]
        )
        if best is None or ap > best[0]:
            best = (ap, model, params)

    assert best is not None, "empty hyper-parameter grid"
    _, model, best_params = best
    val_scores = model.predict_proba(X_va)[:, 1]
    ev = cfg.evaluation
    threshold = select_threshold(y_va, val_scores, ev.threshold_strategy, ev.fixed_threshold)
    val_metrics = evaluate_scores(y_va, val_scores, threshold, ev.k_values)
    log.info(
        "Selected %s; threshold=%.4f; val AP=%.4f F1=%.4f",
        best_params,
        threshold,
        val_metrics["average_precision"],
        val_metrics["f1"],
    )

    run_dir = new_run_dir(cfg.paths.artifacts_dir, model_name, cfg.feature_set)
    metadata = {
        "model_name": model_name,
        "feature_set": cfg.feature_set,
        "feature_columns": feature_cols,
        "best_params": best_params,
        "fitted_info": describe_fitted(model_name, model),
        "threshold": threshold,
        "threshold_strategy": ev.threshold_strategy,
        "seed": cfg.seed,
        "val_metrics": val_metrics,
        "grid_results": candidates,
        "split_summary": split.summary().to_dict(orient="records"),
        "config": cfg.to_dict(),
        "data_manifest": _safe_manifest(cfg.paths.processed_dir),
        "git_commit": git_commit(REPO_ROOT),
        "environment": environment_info(),
    }
    save_artifact(run_dir, model, metadata)
    preds = split.val[[S.TX_ID, S.TIME_STEP, S.LABEL]].assign(score=val_scores)
    preds.to_parquet(run_dir / "val_predictions.parquet", index=False)
    log.info("Saved artifact to %s", run_dir)

    log_run(
        cfg.tracking,
        run_name=run_dir.name,
        params={
            "model": model_name,
            "feature_set": cfg.feature_set,
            "seed": cfg.seed,
            **best_params,
        },
        metrics={"val": _headline(val_metrics)},
        run_dir=run_dir,
        tags={"stage": "train"},
    )
    return run_dir


def evaluate(cfg: Config, run_dir: Path, force: bool = False) -> dict[str, Any]:
    """Final, one-time test-set evaluation of a trained artifact."""
    out_path = run_dir / TEST_METRICS_FILE
    if out_path.exists() and not force:
        raise FileExistsError(
            f"{out_path} exists: this run was already evaluated on the test set. "
            "Use --force only if you understand you are re-reading test results."
        )
    model, meta = load_artifact(run_dir)
    # Use the split/preprocessing frozen in the artifact, NOT whatever the current config says.
    saved = meta["config"]
    split_cfg = SplitConfig(**saved["split"])
    pre_cfg = PreprocessingConfig(**saved["preprocessing"])
    split = load_split(cfg.paths.processed_dir, split_cfg, pre_cfg, meta["feature_set"])

    manifest = _safe_manifest(cfg.paths.processed_dir)
    if meta.get("data_manifest") and manifest and meta["data_manifest"] != manifest:
        log.warning("Processed data differs from the data the model was trained on!")

    X_te, y_te = make_xy(split.test, meta["feature_columns"])
    scores = model.predict_proba(X_te)[:, 1]
    ev = cfg.evaluation
    metrics = evaluate_scores(y_te, scores, meta["threshold"], ev.k_values)

    preds = split.test[[S.TX_ID, S.TIME_STEP, S.LABEL]].assign(score=scores)
    per_step = per_time_step_metrics(preds, "score", meta["threshold"], ev.per_step_k)
    latency = measure_latency(model, X_te, repeats=ev.latency_repeats)

    result = {
        "model_name": meta["model_name"],
        "feature_set": meta["feature_set"],
        "run_dir": run_dir.name,
        "test": metrics,
        "per_step_k": ev.per_step_k,
        "per_time_step_mean": {
            c: float(per_step[c].mean())
            for c in (
                "average_precision",
                f"precision_at_{ev.per_step_k}",
                f"recall_at_{ev.per_step_k}",
            )
        },
        "latency": latency,
        "environment": environment_info(),
    }
    write_json(out_path, result)
    preds.to_parquet(run_dir / "test_predictions.parquet", index=False)
    per_step.to_csv(run_dir / "test_per_time_step.csv", index=False)
    log.info(
        "TEST %s/%s: AP=%.4f ROC-AUC=%.4f P=%.4f R=%.4f F1=%.4f",
        meta["model_name"],
        meta["feature_set"],
        metrics["average_precision"],
        metrics["roc_auc"],
        metrics["precision"],
        metrics["recall"],
        metrics["f1"],
    )
    log_run(
        cfg.tracking,
        run_name=f"{run_dir.name}-test",
        params={"model": meta["model_name"], "feature_set": meta["feature_set"]},
        metrics={"test": _headline(metrics), "latency": latency},
        run_dir=run_dir,
        tags={"stage": "test"},
    )
    return result


def _headline(m: dict[str, Any]) -> dict[str, Any]:
    keys = ("average_precision", "roc_auc", "precision", "recall", "f1", "threshold")
    out = {k: m[k] for k in keys}
    out.update({f"p@{k}": v["precision"] for k, v in m["at_k"].items()})
    out.update({f"r@{k}": v["recall"] for k, v in m["at_k"].items()})
    return out


def _safe_manifest(processed_dir: Path) -> dict[str, Any] | None:
    try:
        raw = load_manifest(processed_dir)["raw_files"]
        return {k: v["sha256"] for k, v in raw.items()}
    except (FileNotFoundError, KeyError):
        return None


def runs_table(run_dirs: list[Path]) -> pd.DataFrame:
    """Collect test metrics from evaluated runs into a comparison table."""
    from chainwatch.utils import read_json

    rows = []
    for rd in run_dirs:
        p = rd / TEST_METRICS_FILE
        if not p.exists():
            continue
        r = read_json(p)
        t = r["test"]
        k = r["per_step_k"]
        rows.append(
            {
                "run": rd.name,
                "model": r["model_name"],
                "features": r["feature_set"],
                "AP (PR-AUC)": t["average_precision"],
                "ROC-AUC": t["roc_auc"],
                "precision": t["precision"],
                "recall": t["recall"],
                "F1": t["f1"],
                "threshold": t["threshold"],
                **{f"P@{kk}": v["precision"] for kk, v in t["at_k"].items()},
                **{f"R@{kk}": v["recall"] for kk, v in t["at_k"].items()},
                f"mean per-step P@{k}": r["per_time_step_mean"][f"precision_at_{k}"],
                "latency p50 (ms/row)": r["latency"]["single_row_ms_p50"],
                "batch (us/tx)": r["latency"]["batch_us_per_tx"],
            }
        )
    return pd.DataFrame(rows)
