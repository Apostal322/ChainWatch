"""Temporal splitting and leakage guards."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from chainwatch.config import SplitConfig
from chainwatch.data import schema as S
from chainwatch.data.loader import build_dataset
from chainwatch.data.splits import (
    LeakageError,
    TemporalSplit,
    assert_no_temporal_leakage,
    count_cross_split_edges,
    temporal_split,
)
from chainwatch.features.preprocessing import make_xy

SPLIT = SplitConfig(train=(1, 6), val=(7, 8), test=(9, 12))


@pytest.fixture
def ds(raw_frames):
    return build_dataset(*raw_frames)


def test_split_is_chronological_and_disjoint(ds):
    sp = temporal_split(ds.transactions, SPLIT)
    assert sp.train[S.TIME_STEP].max() < sp.val[S.TIME_STEP].min()
    assert sp.val[S.TIME_STEP].max() < sp.test[S.TIME_STEP].min()
    ids = [set(p[S.TX_ID]) for p in sp.parts().values()]
    assert not (ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2])


def test_split_excludes_unknown_but_keeps_all_labelled(ds):
    sp = temporal_split(ds.transactions, SPLIT)
    for part in sp.parts().values():
        assert (part[S.LABEL] != S.LABEL_UNKNOWN).all()
    assert sum(len(p) for p in sp.parts().values()) == len(ds.labeled)


def test_split_is_deterministic(ds):
    a = temporal_split(ds.transactions, SPLIT)
    b = temporal_split(ds.transactions.sample(frac=1.0, random_state=1), SPLIT)
    for name in ("train", "val", "test"):
        assert set(a.parts()[name][S.TX_ID]) == set(b.parts()[name][S.TX_ID])


@pytest.mark.parametrize(
    "train, val, test",
    [((1, 7), (7, 8), (9, 12)), ((1, 6), (9, 10), (7, 8)), ((5, 1), (6, 7), (8, 9))],
)
def test_invalid_split_configs_rejected(train, val, test):
    with pytest.raises(ValueError):
        SplitConfig(train=train, val=val, test=test)


def test_guard_detects_overlapping_time(ds):
    sp = temporal_split(ds.transactions, SPLIT)
    leaked = TemporalSplit(train=pd.concat([sp.train, sp.test.iloc[:3]]), val=sp.val, test=sp.test)
    with pytest.raises(LeakageError):
        assert_no_temporal_leakage(leaked)


def test_guard_detects_unknown_rows(ds):
    sp = temporal_split(ds.transactions, SPLIT)
    unk = ds.unknown[ds.unknown[S.TIME_STEP] <= 6].iloc[:2]
    with pytest.raises(LeakageError, match="unknown"):
        assert_no_temporal_leakage(TemporalSplit(pd.concat([sp.train, unk]), sp.val, sp.test))


def test_cross_split_edges_counted(ds):
    assert count_cross_split_edges(ds.edges, ds.transactions, SPLIT) == 0
    a = ds.transactions.loc[ds.transactions[S.TIME_STEP] == 1, S.TX_ID].iloc[0]
    b = ds.transactions.loc[ds.transactions[S.TIME_STEP] == 12, S.TX_ID].iloc[0]
    edges = pd.concat([ds.edges, pd.DataFrame({S.EDGE_SRC: [a], S.EDGE_DST: [b]})])
    assert count_cross_split_edges(edges, ds.transactions, SPLIT) == 1


@pytest.mark.parametrize("col", [S.TX_ID, S.TIME_STEP, S.LABEL, S.RAW_LABEL])
def test_make_xy_refuses_leaky_columns(ds, col):
    with pytest.raises(ValueError, match="non-feature"):
        make_xy(ds.labeled, [*S.LOCAL_FEATURES[:3], col])


def test_make_xy_refuses_unknown_rows(ds):
    with pytest.raises(ValueError, match="unknown"):
        make_xy(ds.transactions, S.LOCAL_FEATURES)


def test_make_xy_target_is_binary(ds):
    _, y = make_xy(ds.labeled, S.LOCAL_FEATURES)
    assert set(np.unique(y)) == {0, 1}
