"""Model artifact persistence.

A run directory is self-describing::

    artifacts/runs/<model>_<feature_set>_<timestamp>/
        model.joblib          fitted sklearn Pipeline (preprocessing + classifier)
        metadata.json         config snapshot, seed, features, threshold, val metrics, env
        val_predictions.parquet
        test_metrics.json     (written only by `chainwatch evaluate`)
        test_predictions.parquet
        test_per_time_step.csv

Security note: joblib/pickle executes code on load. Only load artifacts you created.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

from chainwatch.utils import read_json, write_json

MODEL_FILE = "model.joblib"
METADATA_FILE = "metadata.json"
TEST_METRICS_FILE = "test_metrics.json"
REQUIRED_METADATA_KEYS = ("model_name", "feature_columns", "threshold", "seed", "config")


def new_run_dir(artifacts_dir: Path, model_name: str, feature_set: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = artifacts_dir / f"{model_name}_{feature_set}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def save_artifact(run_dir: Path, model: Any, metadata: dict[str, Any]) -> None:
    missing = [k for k in REQUIRED_METADATA_KEYS if k not in metadata]
    if missing:
        raise ValueError(f"metadata missing required keys: {missing}")
    run_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, run_dir / MODEL_FILE)
    write_json(run_dir / METADATA_FILE, metadata)


def load_artifact(run_dir: Path) -> tuple[Any, dict[str, Any]]:
    model_path, meta_path = run_dir / MODEL_FILE, run_dir / METADATA_FILE
    if not model_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"{run_dir} is not a complete run directory")
    return joblib.load(model_path), read_json(meta_path)


def list_runs(artifacts_dir: Path) -> list[Path]:
    if not artifacts_dir.exists():
        return []
    return sorted(p for p in artifacts_dir.iterdir() if (p / METADATA_FILE).exists())
