"""Shared fixtures.

Tests use a small SYNTHETIC dataset with the verified Elliptic++ schema so they run
in seconds without the real data. Its values are random and carry no meaning; the
illicit class is given a shifted mean in a few features so models can learn something.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from chainwatch.config import DEFAULT_CONFIG_PATH, load_config
from chainwatch.data import schema as S
from chainwatch.data import synthetic

N_STEPS = 12
PER_STEP = 60


def make_raw_frames(seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return synthetic.make_raw_frames(N_STEPS, PER_STEP, seed)


@pytest.fixture
def raw_frames():
    return make_raw_frames()


def write_raw(d: Path, frames) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    features, classes, edges = frames
    features.to_csv(d / S.FEATURES_FILE, index=False)
    classes.to_csv(d / S.CLASSES_FILE, index=False)
    edges.to_csv(d / S.EDGES_FILE, index=False)
    return d


@pytest.fixture
def raw_dir(tmp_path: Path, raw_frames) -> Path:
    return write_raw(tmp_path / "raw", raw_frames)


@pytest.fixture
def cfg(tmp_path: Path, raw_dir: Path):
    return make_test_config(tmp_path, raw_dir)


def make_test_config(tmp_path: Path, raw_dir: Path):
    """A config pointing at temp dirs, with small grids and a 12-step split."""
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    raw["paths"] = {
        "raw_dir": str(raw_dir),
        "processed_dir": str(tmp_path / "processed"),
        "artifacts_dir": str(tmp_path / "artifacts"),
        "reports_dir": str(tmp_path / "reports"),
        "backtests_dir": str(tmp_path / "backtests"),
    }
    raw["split"] = {"train": [1, 6], "val": [7, 8], "test": [9, 12]}
    raw["models"]["logreg"]["grid"] = {"C": [0.1, 1.0]}
    raw["models"]["xgboost"].update(
        {
            "n_estimators": 50,
            "early_stopping_rounds": 10,
            "n_jobs": 1,
            "grid": {"max_depth": [3], "learning_rate": [0.1]},
        }
    )
    raw["evaluation"]["k_values"] = [5, 10]
    raw["evaluation"]["per_step_k"] = 5
    raw["evaluation"]["latency_repeats"] = 5
    raw["tracking"]["mlflow"] = False
    bt = raw["backtest"]
    bt["xgboost"].update({"n_estimators": 20, "n_jobs": 1})
    bt["stages"] = {
        "dev": {"origin": 4, "end": 8, "seeds": [0, 1]},
        "confirm": {"origin": 8, "end": 12, "seeds": [0, 1]},
    }
    bt["strategies"]["periodic"]["grid"] = {"frequency": [2]}
    bt["strategies"]["sliding"]["grid"] = {"window": [2, 3]}
    bt["strategies"]["recency"]["grid"] = {"half_life": [2]}
    bt["budgets"] = [5, 10]
    bt["primary_budget"] = 5
    bt["latency_sensitivity"] = [2]
    bt["confirm_split_after"] = 10
    bt["bootstrap"] = {"n_resamples": 50, "block_length": 2, "seed": 0}
    raw["serving"].update(
        {
            "registry_dir": str(tmp_path / "registry"),
            "snapshot_first_tau": 4,
            "snapshot_last_tau": 11,
            "max_batch_size": 50,
            "frontend_dist": None,
        }
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path)
