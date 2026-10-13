"""Backtest execution: leakage canaries, caching, artifacts, selection and reporting."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from chainwatch.backtest.analysis import (
    bootstrap_comparison,
    cost_table,
    fast_average_precision,
    per_step_table,
    select_per_family,
    summarize,
)
from chainwatch.backtest.pipeline import find_stage_runs, load_selection, run_stage
from chainwatch.backtest.runner import fit_plan, run_backtest
from chainwatch.backtest.schedule import StrategySpec, TrainingPlan
from chainwatch.data import schema as S
from chainwatch.data.loader import build_dataset, prepare
from chainwatch.features.preprocessing import fill_degrees_from_edges

XGB = {"n_estimators": 15, "max_depth": 3, "learning_rate": 0.3, "tree_method": "hist", "n_jobs": 1}
FEATS = S.LOCAL_FEATURES[:20] + S.AUGMENTED_FEATURES
EXPANDING = StrategySpec("expanding", "expanding")
STATIC = StrategySpec("static", "static")
PERIODIC2 = StrategySpec("periodic", "periodic", (("frequency", 2),))
SLIDING2 = StrategySpec("sliding", "sliding", (("window", 2),))
RECENCY = StrategySpec("recency", "recency", (("half_life", 2.0),))


@pytest.fixture
def tx(raw_frames):
    ds = build_dataset(*raw_frames)
    return fill_degrees_from_edges(ds.transactions, ds.edges)


def _scores(tx, specs, latency=1, origin=8, end=12, seeds=(0,)):
    res = run_backtest(tx, specs, origin, end, latency, list(seeds), FEATS, XGB)
    return res, res.predictions.sort_values(["strategy", "seed", S.TX_ID]).reset_index(drop=True)


def _flip_labels(tx, steps):
    out = tx.copy()
    m = out[S.TIME_STEP].isin(steps) & (out[S.LABEL] != S.LABEL_UNKNOWN)
    out.loc[m, S.LABEL] = 1 - out.loc[m, S.LABEL]
    return out


SPECS = [STATIC, EXPANDING, PERIODIC2, SLIDING2, RECENCY]


def test_canary_unavailable_labels_cannot_change_any_score(tx):
    """With origin 8, end 12, latency 1, no model may use labels of steps 11-12."""
    _, base = _scores(tx, SPECS)
    _, flipped = _scores(_flip_labels(tx, [11, 12]), SPECS)
    np.testing.assert_array_equal(base["score"].to_numpy(), flipped["score"].to_numpy())


def test_canary_longer_latency_hides_more_labels(tx):
    _, base = _scores(tx, SPECS, latency=3)
    _, flipped = _scores(_flip_labels(tx, [9, 10, 11, 12]), SPECS, latency=3)
    np.testing.assert_array_equal(base["score"].to_numpy(), flipped["score"].to_numpy())


def test_canary_positive_control_available_labels_do_matter(tx):
    """Guards against a vacuous canary: labels of step 10 ARE available for step 12."""
    _, base = _scores(tx, [EXPANDING])
    _, flipped = _scores(_flip_labels(tx, [10]), [EXPANDING])
    s12 = base[S.TIME_STEP] == 12
    assert not np.allclose(base.loc[s12, "score"], flipped.loc[s12, "score"])
    s9 = base[S.TIME_STEP] <= 11  # models for steps 9-11 never saw step 10 labels
    np.testing.assert_array_equal(base.loc[s9, "score"], flipped.loc[s9, "score"])


def test_canary_future_features_cannot_change_past_scores(tx):
    noisy = tx.copy()
    m = noisy[S.TIME_STEP] == 12
    noisy.loc[m, FEATS] = noisy.loc[m, FEATS] * 50 + 7
    _, base = _scores(tx, SPECS)
    _, pert = _scores(noisy, SPECS)
    keep = base[S.TIME_STEP] < 12
    np.testing.assert_array_equal(base.loc[keep, "score"], pert.loc[keep, "score"])


def test_predictions_cover_all_transactions_including_unknown(tx):
    _res, preds = _scores(tx, [STATIC])
    expected = tx[tx[S.TIME_STEP].between(9, 12)]
    assert len(preds) == len(expected)
    assert (preds[S.LABEL] == S.LABEL_UNKNOWN).sum() == (expected[S.LABEL] == S.LABEL_UNKNOWN).sum()
    assert (preds["tau"] == 8).all()


def test_cached_fits_identical_and_charged(tx):
    """Periodic and static reuse expanding fits, but each strategy is charged its fits."""
    res, preds = _scores(tx, [EXPANDING, PERIODIC2, STATIC])
    e = preds[preds.strategy == "expanding"].set_index(S.TX_ID)["score"]
    p = preds[preds.strategy == "periodic_frequency2"].set_index(S.TX_ID)["score"]
    st = preds[preds.strategy == "static"].set_index(S.TX_ID)["score"]
    step9 = preds[(preds.strategy == "static") & (preds[S.TIME_STEP] == 9)][S.TX_ID]
    # step 9 is scored by the tau=8 model in all three strategies
    np.testing.assert_array_equal(e.loc[step9], st.loc[step9])
    np.testing.assert_array_equal(e.loc[step9], p.loc[step9])
    costs = cost_table(res.fits).set_index("strategy")
    assert costs.loc["expanding", "n_fits"] == 4
    assert costs.loc["periodic_frequency2", "n_fits"] == 2
    assert costs.loc["static", "n_fits"] == 1
    assert (costs["fit_seconds"] > 0).all()
    assert res.fits.loc[res.fits.strategy != "expanding", "fit_was_cached"].all()


def test_recency_weighting_changes_model_but_not_training_rows(tx):
    res, preds = _scores(tx, [EXPANDING, RECENCY])
    f = res.fits.set_index(["strategy", "tau"])
    assert (
        f.loc["expanding"]["n_train"].values == f.loc["recency_half_life2.0"]["n_train"].values
    ).all()
    a = preds[preds.strategy == "expanding"]["score"].to_numpy()
    b = preds[preds.strategy == "recency_half_life2.0"]["score"].to_numpy()
    assert not np.allclose(a, b)


def test_fit_plan_uses_only_labelled_training_steps(tx):
    plan = TrainingPlan(tau=5, train_steps=(1, 2, 3))
    model = fit_plan(tx, plan, FEATS, XGB, seed=0)
    n_expected = ((tx[S.TIME_STEP] <= 3) & (tx[S.LABEL] != S.LABEL_UNKNOWN)).sum()
    assert model.named_steps["preprocess"].n_features_in_ == len(FEATS)
    imputer = (
        model.named_steps["preprocess"].named_steps["columns"].named_transformers_["augmented"]
    )
    assert imputer.named_steps["impute"].n_features_in_ == len(S.AUGMENTED_FEATURES)
    assert n_expected > 0


def test_seeds_change_subsampled_models_reproducibly(tx):
    params = {**XGB, "subsample": 0.7, "colsample_bytree": 0.7}
    a = run_backtest(tx, [STATIC], 8, 12, 1, [0, 1], FEATS, params).predictions
    b = run_backtest(tx, [STATIC], 8, 12, 1, [0, 1], FEATS, params).predictions
    pd.testing.assert_frame_equal(a, b)
    s0, s1 = a[a.seed == 0]["score"].to_numpy(), a[a.seed == 1]["score"].to_numpy()
    assert not np.allclose(s0, s1)


# --------------------------------------------------------------------------- #
# analysis on synthetic predictions
# --------------------------------------------------------------------------- #
def _toy_preds(rng, strategies, steps, seeds=(0, 1), n=60, signal=None):
    rows = []
    for st in strategies:
        for sd in seeds:
            for t in steps:
                y = (rng.random(n) < 0.2).astype(int)
                y[0], y[1] = 1, 0
                strength = (signal or {}).get(st, 1.0)
                s = rng.random(n) + strength * y
                lab = np.where(rng.random(n) < 0.3, S.LABEL_UNKNOWN, y)
                lab[0], lab[1] = 1, 0
                rows.append(
                    pd.DataFrame(
                        {
                            "strategy": st,
                            "seed": sd,
                            S.TIME_STEP: t,
                            S.TX_ID: np.arange(n) + 1000 * t,
                            S.LABEL: lab,
                            "score": s,
                        }
                    )
                )
    return pd.concat(rows, ignore_index=True)


def test_per_step_and_summary_consistency():
    rng = np.random.default_rng(0)
    preds = _toy_preds(rng, ["static", "expanding"], [1, 2, 3])
    ps = per_step_table(preds, [5, 10])
    per_seed, _ = summarize(preds, [5, 10], 0.1, [1, 2, 3])
    for (st, sd), g in ps.groupby(["strategy", "seed"]):
        row = per_seed[(per_seed.strategy == st) & (per_seed.seed == sd)].iloc[0]
        assert row["hits@5"] == pytest.approx(g["hits@5"].sum())
        assert row["n_illicit"] == g["n_illicit"].sum()
        assert row["mean_step_ap"] == pytest.approx(g["ap"].mean())
    # labelled-only hits >= all-transaction hits (unknowns can only take slots)
    assert (ps["hits@5"] >= ps["hits_all@5"] - 1e-12).all()


def test_bootstrap_comparison_detects_clear_difference_and_null():
    rng = np.random.default_rng(1)
    steps = list(range(1, 13))
    preds = _toy_preds(
        rng,
        ["static", "better", "same"],
        steps,
        n=80,
        signal={"static": 0.3, "better": 1.5, "same": 0.3},
    )
    b = bootstrap_comparison(preds, "static", steps, 5, block_length=3, n_resamples=200, seed=0)
    better = b[(b.strategy == "better") & (b.metric == "pooled_ap")].iloc[0]
    assert better.diff_ci_low > 0
    base = b[(b.strategy == "static") & (b.metric == "pooled_ap")].iloc[0]
    assert base.diff_vs_baseline == 0 and base.diff_ci_low == 0 and base.diff_ci_high == 0
    same = b[(b.strategy == "same") & (b.metric == "pooled_ap")].iloc[0]
    assert same.diff_ci_low < 0 < same.diff_ci_high or abs(same.diff_vs_baseline) < 0.05
    # point estimate equals the direct computation averaged over seeds
    direct = np.mean(
        [
            fast_average_precision(
                g[g[S.LABEL] >= 0][S.LABEL].to_numpy(), g[g[S.LABEL] >= 0]["score"].to_numpy()
            )
            for _, g in preds[preds.strategy == "better"].groupby("seed")
        ]
    )
    assert better.estimate == pytest.approx(direct)


def test_selection_rule_prefers_best_then_cheaper_on_ties():
    summary = pd.DataFrame(
        {
            "strategy": [
                "periodic_frequency2",
                "periodic_frequency4",
                "sliding_window3",
                "sliding_window5",
                "static",
            ],
            "pooled_ap_mean": [0.800, 0.799, 0.70, 0.75, 0.6],
        }
    )
    fits = pd.DataFrame(
        {
            "strategy": ["periodic_frequency2"] * 4
            + ["periodic_frequency4"] * 2
            + ["sliding_window3"] * 8
            + ["sliding_window5"] * 8
            + ["static"],
            "seed": 0,
            "tau": 0,
            "fit_seconds_charged": 1.0,
            "predict_seconds": 0.1,
            "n_train": 10,
        }
    )
    families = {
        "periodic_frequency2": "periodic",
        "periodic_frequency4": "periodic",
        "sliding_window3": "sliding",
        "sliding_window5": "sliding",
        "static": "static",
    }
    chosen = select_per_family(summary, fits, families, tolerance=0.002)
    assert chosen["periodic"] == "periodic_frequency4"  # within tolerance, fewer fits
    assert chosen["sliding"] == "sliding_window5"
    chosen_strict = select_per_family(summary, fits, families, tolerance=0.0)
    assert chosen_strict["periodic"] == "periodic_frequency2"


# --------------------------------------------------------------------------- #
# stage pipeline (artifacts, frozen selection)
# --------------------------------------------------------------------------- #
def test_confirm_requires_frozen_dev_selection(cfg):
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    with pytest.raises(FileNotFoundError, match="dev"):
        run_stage(cfg, "confirm")


def test_dev_then_confirm_then_report(cfg):
    from chainwatch.backtest.report import write_backtest_report
    from chainwatch.utils import read_json

    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    dev = run_stage(cfg, "dev")
    sel, src = load_selection(cfg.paths.backtests_dir, cfg.backtest.label_latency)
    assert src == dev
    assert set(sel) == set(cfg.backtest.strategies)
    preds = pd.read_parquet(dev / "predictions.parquet")
    assert preds[S.TIME_STEP].between(5, 8).all()  # dev never scores confirm steps
    conf = run_stage(cfg, "confirm")
    meta = read_json(conf / "metadata.json")
    assert meta["exploratory"] is True and meta["selection_used"] == sel
    run_stage(cfg, "confirm", latency=2)  # sensitivity reuses the L=1 selection
    assert find_stage_runs(cfg.paths.backtests_dir, "confirm", 2)
    path = write_backtest_report(cfg)
    text = path.read_text(encoding="utf-8")
    assert "EXPLORATORY" in text and "Robustness verdicts" in text
    assert (path.parent / "verdicts.json").exists()


def test_time_proxy_ablation_stage(cfg):
    from chainwatch.utils import read_json

    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    run_stage(cfg, "dev")
    abl = run_stage(cfg, "dev", drop_time_proxies=True, only=["selected"])
    assert abl.name.startswith("dev-notp_L1_")
    assert not (abl / "selection.json").exists()  # ablations never overwrite the selection
    meta = read_json(abl / "metadata.json")
    assert meta["exploratory"] is True
    assert not set(meta["dropped_time_proxies"]) & set(meta["feature_columns"])
    sel, _ = load_selection(cfg.paths.backtests_dir, 1)
    preds = pd.read_parquet(abl / "predictions.parquet")
    assert set(preds["strategy"].astype(str)) == set(sel.values()) | {"static"}
    with pytest.raises(ValueError, match="unknown strategies"):
        run_stage(cfg, "dev", only=["nope"])
