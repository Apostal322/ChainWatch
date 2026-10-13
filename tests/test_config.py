from __future__ import annotations

import pytest

from chainwatch.config import DEFAULT_CONFIG_PATH, REPO_ROOT, load_config


def test_default_config_loads_with_repo_relative_paths():
    cfg = load_config(DEFAULT_CONFIG_PATH)
    assert cfg.paths.processed_dir.is_absolute()
    assert cfg.paths.processed_dir.is_relative_to(REPO_ROOT)
    assert cfg.split.test == (35, 49)
    assert set(cfg.models) == {"logreg", "xgboost"}


def test_env_var_overrides_raw_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAINWATCH_RAW_DIR", str(tmp_path))
    assert load_config().paths.raw_dir == tmp_path.resolve()


def test_overrides():
    cfg = load_config(overrides={"feature_set": "local", "seed": 7})
    assert cfg.feature_set == "local"
    assert cfg.seed == 7


def test_unknown_feature_set_rejected():
    with pytest.raises(ValueError):
        load_config(overrides={"feature_set": "everything"})


def test_backtest_config_loaded_and_validated():
    cfg = load_config()
    bt = cfg.backtest
    assert bt is not None and bt.label_latency == 1
    assert bt.stages["dev"].scored_steps == list(range(20, 35))
    assert bt.stages["confirm"].scored_steps == list(range(35, 50))
    assert bt.stages["dev"].end < bt.stages["confirm"].origin + 1  # dev never scores 35+


def test_backtest_config_rejects_overlapping_stages():
    from chainwatch.config import BacktestStage

    cfg = load_config()
    raw = dict(cfg.backtest.__dict__)
    raw["stages"] = {"dev": BacktestStage(19, 40, [0]), "confirm": BacktestStage(34, 49, [0])}
    with pytest.raises(ValueError, match="dev backtest"):
        type(cfg.backtest)(**raw)
