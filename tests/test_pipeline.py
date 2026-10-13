"""End-to-end train/evaluate on the synthetic fixture, plus artifact round-trips."""

from __future__ import annotations

import numpy as np
import pytest

from chainwatch.config import load_config
from chainwatch.data import schema as S
from chainwatch.data.loader import prepare
from chainwatch.models.artifacts import (
    TEST_METRICS_FILE,
    list_runs,
    load_artifact,
    save_artifact,
)
from chainwatch.training.pipeline import evaluate, load_split, runs_table, train
from chainwatch.utils import read_json


@pytest.fixture
def prepared_cfg(cfg):
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    return cfg


@pytest.mark.parametrize("model_name", ["logreg", "xgboost"])
def test_train_then_evaluate(prepared_cfg, model_name):
    cfg = prepared_cfg
    run_dir = train(cfg, model_name)
    meta = read_json(run_dir / "metadata.json")
    assert meta["seed"] == cfg.seed
    assert meta["feature_columns"] == S.feature_columns(cfg.feature_set)
    assert 0.0 <= meta["threshold"] <= 1.0
    assert not (run_dir / TEST_METRICS_FILE).exists(), "train must not touch the test split"

    result = evaluate(cfg, run_dir)
    assert result["test"]["average_precision"] > result["test"]["prevalence"]  # better than random
    assert (run_dir / "test_per_time_step.csv").exists()
    assert result["latency"]["single_row_ms_p50"] > 0

    with pytest.raises(FileExistsError):
        evaluate(cfg, run_dir)  # second look at test is refused without --force

    table = runs_table(list_runs(cfg.paths.artifacts_dir))
    assert len(table) == 1


def test_artifact_roundtrip_gives_identical_predictions(prepared_cfg):
    cfg = prepared_cfg
    run_dir = train(cfg, "logreg")
    model, meta = load_artifact(run_dir)
    split = load_split(cfg.paths.processed_dir, cfg.split, cfg.preprocessing, cfg.feature_set)
    X = split.val[meta["feature_columns"]]
    preds_saved = read_json(run_dir / "metadata.json")["val_metrics"]["average_precision"]

    save_artifact(run_dir / "copy", model, meta)
    model2, meta2 = load_artifact(run_dir / "copy")
    np.testing.assert_array_equal(model.predict_proba(X), model2.predict_proba(X))
    assert meta2 == meta
    assert preds_saved == meta["val_metrics"]["average_precision"]


def test_save_artifact_requires_metadata(tmp_path):
    with pytest.raises(ValueError, match="missing required keys"):
        save_artifact(tmp_path / "x", object(), {"model_name": "logreg"})


def test_training_is_reproducible(prepared_cfg):
    cfg = prepared_cfg
    a = read_json(train(cfg, "xgboost") / "metadata.json")
    b = read_json(train(cfg, "xgboost") / "metadata.json")
    assert a["val_metrics"] == b["val_metrics"]
    assert a["threshold"] == b["threshold"]


def test_evaluate_uses_split_frozen_in_artifact(prepared_cfg, tmp_path):
    """Changing the config after training must not change what 'test' means for a run."""
    cfg = prepared_cfg
    run_dir = train(cfg, "logreg")
    other = load_config(cfg.source_path)
    other.split.test = (11, 12)  # would shrink the test set if (wrongly) used
    result = evaluate(other, run_dir)
    split = load_split(cfg.paths.processed_dir, cfg.split, cfg.preprocessing, cfg.feature_set)
    assert result["test"]["n"] == len(split.test)


def test_param_grid_order_independent_of_key_order():
    from chainwatch.models.baselines import param_grid

    a = list(param_grid({"b": [1, 2], "a": [3, 4]}))
    b = list(param_grid({"a": [3, 4], "b": [1, 2]}))
    assert a == b
    assert len(a) == 4
