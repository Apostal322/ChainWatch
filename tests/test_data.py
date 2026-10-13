"""Data loading, schema validation and label handling."""

from __future__ import annotations

import pandas as pd
import pytest

from chainwatch.data import schema as S
from chainwatch.data.loader import (
    SchemaError,
    build_dataset,
    load_processed,
    prepare,
    read_raw,
    validate_raw,
)


def test_schema_constants_match_published_counts():
    assert len(S.LOCAL_FEATURES) == 93
    assert len(S.AGGREGATE_FEATURES) == 72
    assert len(S.AUGMENTED_FEATURES) == 17
    assert len(S.EXPECTED_FEATURE_FILE_COLUMNS) == 184  # verified against txs_features.csv
    assert len(set(S.ALL_FEATURES)) == len(S.ALL_FEATURES)


@pytest.mark.parametrize("fs, n", [("local", 93), ("local_aggregate", 165), ("all", 182)])
def test_feature_sets(fs, n):
    cols = S.feature_columns(fs)
    assert len(cols) == n
    assert not set(cols) & set(S.NON_FEATURE_COLUMNS)


def test_validate_accepts_well_formed(raw_frames):
    report = validate_raw(*raw_frames)
    assert report.n_transactions == len(raw_frames[0])
    assert report.n_cross_time_step_edges == 0
    assert report.rows_with_missing == 10
    assert any("LEAKAGE RISK" in w for w in report.warnings)


def test_validate_rejects_missing_column(raw_frames):
    features, classes, edges = raw_frames
    with pytest.raises(SchemaError, match="missing columns"):
        validate_raw(features.drop(columns=["Local_feature_7"]), classes, edges)


def test_validate_rejects_duplicate_ids(raw_frames):
    features, classes, edges = raw_frames
    dup = pd.concat([features, features.iloc[[0]]], ignore_index=True)
    with pytest.raises(SchemaError, match="duplicate txId"):
        validate_raw(dup, classes, edges)


def test_validate_rejects_unknown_class_value(raw_frames):
    features, classes, edges = raw_frames
    bad = classes.copy()
    bad.loc[0, S.RAW_CLASS] = 4
    with pytest.raises(SchemaError, match="unexpected class"):
        validate_raw(features, bad, edges)


def test_validate_rejects_dangling_edges(raw_frames):
    features, classes, edges = raw_frames
    bad = pd.concat(
        [edges, pd.DataFrame({S.RAW_EDGE_SRC: [1], S.RAW_EDGE_DST: [2]})], ignore_index=True
    )
    with pytest.raises(SchemaError, match="unknown txIds"):
        validate_raw(features, classes, bad)


def test_validate_rejects_non_numeric_feature(raw_frames):
    features, classes, edges = raw_frames
    bad = features.copy()
    bad["fees"] = bad["fees"].astype(str)
    with pytest.raises(SchemaError, match="non-numeric"):
        validate_raw(bad, classes, edges)


def test_labels_joined_by_id_not_position(raw_frames):
    """The classes file is shuffled in the fixture; positional alignment would mislabel rows."""
    features, classes, edges = raw_frames
    ds = build_dataset(features, classes, edges)
    expected = classes.set_index(S.RAW_TX_ID)[S.RAW_CLASS]
    got = ds.transactions.set_index(S.TX_ID)[S.RAW_LABEL]
    assert (got == expected.loc[got.index]).all()


def test_label_mapping_keeps_unknown_separate(raw_frames):
    ds = build_dataset(*raw_frames)
    tx = ds.transactions
    assert set(tx[S.LABEL].unique()) == {S.LABEL_ILLICIT, S.LABEL_LICIT, S.LABEL_UNKNOWN}
    assert (tx.loc[tx[S.RAW_LABEL] == 1, S.LABEL] == 1).all()
    assert (tx.loc[tx[S.RAW_LABEL] == 2, S.LABEL] == 0).all()
    assert (tx.loc[tx[S.RAW_LABEL] == 3, S.LABEL] == -1).all()
    assert (ds.labeled[S.LABEL] != S.LABEL_UNKNOWN).all()
    assert len(ds.labeled) + len(ds.unknown) == len(tx)
    # identifiers are preserved
    assert set(tx[S.TX_ID]) == set(raw_frames[0][S.RAW_TX_ID])


def test_prepare_roundtrip(raw_dir, tmp_path):
    out = tmp_path / "processed"
    prepare(raw_dir, out)
    ds = load_processed(out)
    feats, _, _ = read_raw(raw_dir)
    assert len(ds.transactions) == len(feats)
    assert ds.transactions[S.TIME_STEP].is_monotonic_increasing
    assert (out / "manifest.json").exists()


def test_missing_raw_files_gives_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="chainwatch download"):
        read_raw(tmp_path)
