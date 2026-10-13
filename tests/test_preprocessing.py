"""Preprocessing correctness and fit-on-train-only guarantees."""

from __future__ import annotations

import numpy as np
import pandas as pd

from chainwatch.data import schema as S
from chainwatch.data.loader import build_dataset
from chainwatch.features.preprocessing import (
    build_preprocessor,
    fill_degrees_from_edges,
    signed_log1p,
)


def test_signed_log1p_properties():
    x = np.array([-10.0, -1.0, 0.0, 1.0, 1e6])
    y = signed_log1p(x)
    assert y[2] == 0.0
    assert np.all(np.diff(y) > 0)  # monotone
    np.testing.assert_allclose(y[3], np.log(2.0))
    np.testing.assert_allclose(signed_log1p(-x), -y)  # odd function


def test_degrees_recomputed_exactly_from_edges(raw_frames):
    ds = build_dataset(*raw_frames)
    tx = ds.transactions
    assert tx["in_txs_degree"].isna().sum() == 10
    filled = fill_degrees_from_edges(tx, ds.edges)
    assert filled[S.DEGREE_FEATURES].isna().sum().sum() == 0
    expected_in = filled[S.TX_ID].map(ds.edges[S.EDGE_DST].value_counts()).fillna(0)
    np.testing.assert_array_equal(filled["in_txs_degree"].to_numpy(), expected_in.to_numpy())
    # non-degree augmented features stay missing (to be imputed with train statistics)
    assert filled["total_BTC"].isna().sum() == 10
    # input frame not mutated
    assert tx["in_txs_degree"].isna().sum() == 10


def _frame(values: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(values)


def test_imputer_and_scaler_use_training_statistics_only():
    cols = ["Local_feature_1", "fees"]
    train = _frame({"Local_feature_1": [0.0, 2.0, 4.0], "fees": [1.0, np.nan, 3.0]})
    test = _frame({"Local_feature_1": [1000.0, 2000.0], "fees": [np.nan, 1e9]})
    pre = build_preprocessor(cols, log1p_augmented=False, scale=True)
    pre.fit(train)

    imputer = pre.named_steps["columns"].named_transformers_["augmented"].named_steps["impute"]
    assert imputer.statistics_[0] == 2.0  # median of train fees [1, 3]; test values ignored
    scaler = pre.named_steps["scale"]
    np.testing.assert_allclose(scaler.mean_, [2.0, 2.0])  # train means (fees after imputation)

    out = pre.transform(test)
    # missing test value imputed with TRAIN median -> standardised value of 2.0 is 0
    assert out[0, 1] == 0.0


def test_refitting_on_different_data_changes_nothing_already_fitted():
    """Transforming new data must never update fitted statistics (no online leakage)."""
    cols = ["Local_feature_1", "fees"]
    train = _frame({"Local_feature_1": [0.0, 1.0, 2.0], "fees": [1.0, 2.0, 3.0]})
    pre = build_preprocessor(cols, log1p_augmented=True, scale=True).fit(train)
    before = pre.named_steps["scale"].mean_.copy()
    pre.transform(_frame({"Local_feature_1": [1e6], "fees": [1e6]}))
    np.testing.assert_array_equal(before, pre.named_steps["scale"].mean_)


def test_preprocessor_output_has_no_missing_indicator_columns():
    cols = S.feature_columns("all")
    df = pd.DataFrame(np.random.default_rng(0).random((20, len(cols))), columns=cols)
    df.iloc[:5, -3:] = np.nan
    pre = build_preprocessor(cols, log1p_augmented=True, scale=False).fit(df)
    out = pre.transform(df)
    assert out.shape[1] == len(cols)
    assert not np.isnan(out).any()


def test_local_only_feature_set_has_no_augmented_branch():
    pre = build_preprocessor(S.LOCAL_FEATURES, log1p_augmented=True, scale=False)
    names = [name for name, _, _ in pre.named_steps["columns"].transformers]
    assert names == ["base"]


def test_time_proxy_detection_uses_only_allowed_steps():
    from chainwatch.features.preprocessing import time_proxy_features

    rng = np.random.default_rng(0)
    steps = np.repeat(np.arange(1, 11), 50)
    df = pd.DataFrame(
        {
            S.TIME_STEP: steps,
            "clock": steps + rng.normal(0, 0.1, len(steps)),
            "noise": rng.normal(size=len(steps)),
            # clock-like only after step 5: invisible when max_step=5
            "late_clock": np.where(steps > 5, steps * 10.0, rng.normal(size=len(steps))),
        }
    )
    found = time_proxy_features(df, ["clock", "noise", "late_clock"], max_step=5, threshold=0.9)
    assert list(found) == ["clock"]
    # with later steps allowed, late_clock becomes strongly time-correlated (rho ~0.87)
    found_all = time_proxy_features(
        df, ["clock", "noise", "late_clock"], max_step=10, threshold=0.8
    )
    assert set(found_all) == {"clock", "late_clock"}
