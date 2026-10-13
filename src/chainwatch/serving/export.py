"""Build the serving registry from training code that already exists.

* Snapshots reuse the Phase 2 backtest code path verbatim (``training_plan`` +
  ``fit_plan``), so a snapshot trained at the end of step τ is the *same* model the
  expanding-window backtest used to score step τ+1 (same data, parameters and seed).
* The Phase 1 reference model is copied from its run directory unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from chainwatch.backtest.runner import fit_plan
from chainwatch.backtest.schedule import StrategySpec, training_plan
from chainwatch.config import REPO_ROOT, Config
from chainwatch.data import schema as S
from chainwatch.data.synthetic import SYNTHETIC_MARKER
from chainwatch.models.artifacts import list_runs, load_artifact
from chainwatch.serving.registry import reset_index, save_bundle
from chainwatch.serving.store import FeatureStore, data_fingerprint
from chainwatch.utils import get_logger, git_commit, write_json

log = get_logger(__name__)

EXPANDING = StrategySpec("expanding", "expanding")
REGISTRY_META = "registry.json"


def snapshot_version(tau: int, latency: int) -> str:
    return f"snap-expanding-L{latency}-t{tau:03d}"


def data_mode(cfg: Config) -> str:
    return "synthetic" if (cfg.paths.raw_dir / SYNTHETIC_MARKER).exists() else "elliptic++"


def export_registry(cfg: Config, overwrite: bool = False) -> Path:
    bt, sv = cfg.backtest, cfg.serving
    if bt is None or sv is None:
        raise ValueError("config needs `backtest` and `serving` sections")
    store = FeatureStore.from_processed(cfg.paths.processed_dir)
    reg = sv.registry_dir
    reset_index(reg)  # bundles from earlier exports are never routed again
    latency = bt.label_latency
    feature_cols = S.feature_columns(bt.feature_set)
    fingerprint = data_fingerprint(cfg.paths.processed_dir)
    mode = data_mode(cfg)
    exported: list[str] = []

    for tau in range(sv.snapshot_first_tau, sv.snapshot_last_tau + 1):
        plan = training_plan(EXPANDING, tau, latency)
        # causality is checked at export time as well as on every replay request
        if plan.train_steps[-1] > tau - latency:
            raise ValueError(f"snapshot at tau={tau} would use labels after {tau - latency}")
        t0 = time.perf_counter()
        model = fit_plan(
            store.model_frame,
            plan,
            feature_cols,
            bt.xgboost,
            sv.snapshot_seed,
            cfg.preprocessing.log1p_augmented,
        )
        seconds = time.perf_counter() - t0
        labelled = store.model_frame[
            store.model_frame[S.TIME_STEP].isin(plan.train_steps)
            & (store.model_frame[S.LABEL] != S.LABEL_UNKNOWN)
        ]
        version = snapshot_version(tau, latency)
        save_bundle(
            reg,
            version,
            model,
            {
                "kind": "snapshot",
                "description": f"XGBoost trained at end of step {tau} on labels of steps "
                f"{plan.train_steps[0]}-{plan.train_steps[-1]} (label latency {latency})",
                "strategy": "expanding",
                "trained_at_end_of_step": tau,
                "label_latency": latency,
                "train_steps": [plan.train_steps[0], plan.train_steps[-1]],
                "n_train": len(labelled),
                "n_train_illicit": int((labelled[S.LABEL] == S.LABEL_ILLICIT).sum()),
                "feature_columns": feature_cols,
                "xgboost_params": bt.xgboost,
                "seed": sv.snapshot_seed,
                "fit_seconds": round(seconds, 2),
                "data_mode": mode,
                "data_fingerprint": fingerprint,
                "git_commit": git_commit(REPO_ROOT),
            },
            overwrite=overwrite,
        )
        exported.append(version)
        log.info("exported %s (%.1fs, %d training rows)", version, seconds, len(labelled))

    phase1 = _export_phase1(cfg, reg, fingerprint, mode, overwrite)
    write_json(
        reg / REGISTRY_META,
        {
            "data_mode": mode,
            "data_fingerprint": fingerprint,
            "label_latency": latency,
            "snapshots": exported,
            "phase1_reference": phase1,
            "policies": {
                "expanding": "step s is scored by the snapshot trained at the end of step s-1",
                "static": {
                    "description": "step s is scored by the snapshot frozen at the stage origin",
                    "origins": {name: [st.origin, st.end] for name, st in bt.stages.items()},
                },
            },
            "replay_steps": [sv.snapshot_first_tau + 1, sv.snapshot_last_tau + 1],
        },
    )
    return reg


def _export_phase1(
    cfg: Config, reg: Path, fingerprint: str, mode: str, overwrite: bool
) -> str | None:
    runs = [r for r in list_runs(cfg.paths.artifacts_dir) if r.name.startswith("xgboost_all_")]
    if not runs:
        log.info("no Phase 1 xgboost_all run found; skipping reference model")
        return None
    model, meta = load_artifact(runs[-1])
    split = meta["config"]["split"]
    version = "phase1-xgboost-all"
    card: dict[str, Any] = {
        "kind": "phase1",
        "description": f"Phase 1 XGBoost (all features) trained on steps {split['train'][0]}-"
        f"{split['train'][1]}; reference model, not a replay snapshot",
        "trained_at_end_of_step": int(split["train"][1]),
        "label_latency": 0,
        "train_steps": list(split["train"]),
        "feature_columns": meta["feature_columns"],
        "threshold_selected_on_validation": meta["threshold"],
        # validation steps were used for early stopping and thresholding
        "out_of_sample_from_step": int(split["test"][0]),
        "source_run": runs[-1].name,
        "data_mode": mode,
        "data_fingerprint": fingerprint,
    }
    save_bundle(reg, version, model, card, overwrite=overwrite)
    return version
