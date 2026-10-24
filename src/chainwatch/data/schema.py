"""Verified schema of the Elliptic++ *transactions* dataset.

Everything here was checked against the actual files (git-disl/EllipticPlusPlus,
LFS objects listed in ``download.py``), not inferred from documentation alone:

* ``txs_features.csv``  203,769 rows x 184 columns: ``txId``, ``Time step``,
  ``Local_feature_1..93``, ``Aggregate_feature_1..72`` and 17 augmented columns.
  It does NOT contain the label column.
* ``txs_classes.csv``   203,769 rows: ``txId``, ``class`` with 1=illicit, 2=licit, 3=unknown.
* ``txs_edgelist.csv``  234,355 directed edges: ``txId1`` -> ``txId2``.
"""

from __future__ import annotations

FEATURES_FILE = "txs_features.csv"
CLASSES_FILE = "txs_classes.csv"
EDGES_FILE = "txs_edgelist.csv"
RAW_FILES = (FEATURES_FILE, CLASSES_FILE, EDGES_FILE)

# Raw column names as published.
RAW_TX_ID = "txId"
RAW_TIME_STEP = "Time step"
RAW_CLASS = "class"
RAW_EDGE_SRC = "txId1"
RAW_EDGE_DST = "txId2"

# Canonical internal column names (snake_case, no spaces).
TX_ID = "tx_id"
TIME_STEP = "time_step"
RAW_LABEL = "raw_class"
LABEL = "label"
EDGE_SRC = "src"
EDGE_DST = "dst"

# Raw class encoding -> binary target. "Unknown" is NOT "licit": it means the
# transaction was never labelled, so it must never enter supervised train/eval.
CLASS_ILLICIT = 1
CLASS_LICIT = 2
CLASS_UNKNOWN = 3
LABEL_ILLICIT = 1
LABEL_LICIT = 0
LABEL_UNKNOWN = -1
CLASS_TO_LABEL = {
    CLASS_ILLICIT: LABEL_ILLICIT,
    CLASS_LICIT: LABEL_LICIT,
    CLASS_UNKNOWN: LABEL_UNKNOWN,
}

N_LOCAL = 93
N_AGGREGATE = 72
LOCAL_FEATURES = [f"Local_feature_{i}" for i in range(1, N_LOCAL + 1)]
AGGREGATE_FEATURES = [f"Aggregate_feature_{i}" for i in range(1, N_AGGREGATE + 1)]

# Features added by Elliptic++ on top of the original Elliptic dataset. Unlike the
# local/aggregate features (already standardised by the publisher) these are raw,
# non-negative and heavy-tailed. All 17 are missing together for 965 transactions.
DEGREE_FEATURES = ["in_txs_degree", "out_txs_degree"]
AUGMENTED_FEATURES = [
    *DEGREE_FEATURES,
    "total_BTC",
    "fees",
    "size",
    "num_input_addresses",
    "num_output_addresses",
    "in_BTC_min",
    "in_BTC_max",
    "in_BTC_mean",
    "in_BTC_median",
    "in_BTC_total",
    "out_BTC_min",
    "out_BTC_max",
    "out_BTC_mean",
    "out_BTC_median",
    "out_BTC_total",
]

ALL_FEATURES = LOCAL_FEATURES + AGGREGATE_FEATURES + AUGMENTED_FEATURES
EXPECTED_FEATURE_FILE_COLUMNS = [RAW_TX_ID, RAW_TIME_STEP, *ALL_FEATURES]

# Columns that must never be used as model inputs.
#  - identifiers carry no causal signal and can memorise;
#  - the time step is excluded because, under a chronological split, test steps
#    are never seen in training (pure extrapolation / a proxy for the period's base rate).
NON_FEATURE_COLUMNS = (TX_ID, TIME_STEP, RAW_LABEL, LABEL)

EXPECTED_N_TIME_STEPS = 49


# Elliptic++ augmented features that ChainWatch's live collector can also compute from a
# single mempool transaction with its prevouts (docs/LIVE_FEATURE_SUBSET_PROTOCOL.md).
# out_txs_degree is excluded: it counts LATER transactions spending this one's outputs,
# which is unknown when a transaction is first observed.
LIVE_COMPUTABLE_FEATURES = [f for f in AUGMENTED_FEATURES if f != "out_txs_degree"]


def feature_columns(feature_set: str) -> list[str]:
    """Return the ordered model input columns for a named feature set."""
    if feature_set == "augmented":
        return list(AUGMENTED_FEATURES)
    if feature_set == "live_computable":
        return list(LIVE_COMPUTABLE_FEATURES)
    if feature_set == "local":
        return list(LOCAL_FEATURES)
    if feature_set == "local_aggregate":
        return LOCAL_FEATURES + AGGREGATE_FEATURES
    if feature_set == "all":
        return list(ALL_FEATURES)
    raise ValueError(f"Unknown feature set: {feature_set!r}")
