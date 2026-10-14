"""Load, validate and canonicalise the raw Elliptic++ transaction files.

Pipeline: raw CSV -> schema validation -> join features with labels on ``txId``
(never by row position) -> canonical column names -> Parquet cache + manifest.

The Parquet cache exists purely for speed (the feature CSV is ~700 MB); it is a
lossless re-encoding apart from casting features to float32.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from chainwatch.data import schema as S
from chainwatch.data.download import REMOTE_FILES
from chainwatch.data.synthetic import SYNTHETIC_MARKER
from chainwatch.utils import get_logger, read_json, sha256_file, write_json

log = get_logger(__name__)

TRANSACTIONS_PARQUET = "transactions.parquet"
EDGES_PARQUET = "edges.parquet"
MANIFEST_JSON = "manifest.json"


class SchemaError(ValueError):
    """Raised when raw data violates an assumption the pipeline depends on."""


@dataclass
class ValidationReport:
    n_transactions: int
    n_edges: int
    class_counts: dict[int, int]
    time_step_range: tuple[int, int]
    n_time_steps: int
    missing_by_column: dict[str, int]
    rows_with_missing: int
    rows_with_missing_by_class: dict[int, int]
    n_cross_time_step_edges: int
    n_self_loops: int
    n_duplicate_edges: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dataset:
    """Canonical in-memory dataset: one row per transaction plus the edge list."""

    transactions: pd.DataFrame  # tx_id, time_step, raw_class, label, <features>
    edges: pd.DataFrame  # src, dst (tx ids)

    @property
    def labeled(self) -> pd.DataFrame:
        return self.transactions[self.transactions[S.LABEL] != S.LABEL_UNKNOWN]

    @property
    def unknown(self) -> pd.DataFrame:
        return self.transactions[self.transactions[S.LABEL] == S.LABEL_UNKNOWN]


# --------------------------------------------------------------------------- #
# Raw IO
# --------------------------------------------------------------------------- #
def read_raw(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    missing = [f for f in S.RAW_FILES if not (raw_dir / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing raw files in {raw_dir}: {missing}. Run `chainwatch download` or "
            "place the files there manually (see README, 'Dataset setup')."
        )
    log.info("Reading raw CSVs from %s", raw_dir)
    features = pd.read_csv(raw_dir / S.FEATURES_FILE)
    classes = pd.read_csv(raw_dir / S.CLASSES_FILE)
    edges = pd.read_csv(raw_dir / S.EDGES_FILE)
    return features, classes, edges


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def validate_raw(
    features: pd.DataFrame, classes: pd.DataFrame, edges: pd.DataFrame
) -> ValidationReport:
    """Hard-fail on violated structural assumptions; record soft data-quality issues."""
    # --- columns ---
    cols = list(features.columns)
    missing_cols = [c for c in S.EXPECTED_FEATURE_FILE_COLUMNS if c not in cols]
    extra_cols = [c for c in cols if c not in S.EXPECTED_FEATURE_FILE_COLUMNS]
    _require(not missing_cols, f"features file missing columns: {missing_cols[:10]}")
    _require(not extra_cols, f"features file has unexpected columns: {extra_cols[:10]}")
    _require(
        list(classes.columns) == [S.RAW_TX_ID, S.RAW_CLASS],
        f"classes columns {list(classes.columns)}",
    )
    _require(
        list(edges.columns) == [S.RAW_EDGE_SRC, S.RAW_EDGE_DST],
        f"edge columns {list(edges.columns)}",
    )

    # --- dtypes ---
    for c in (S.RAW_TX_ID, S.RAW_TIME_STEP):
        _require(pd.api.types.is_integer_dtype(features[c]), f"{c} must be integer")
    non_numeric = [c for c in S.ALL_FEATURES if not pd.api.types.is_numeric_dtype(features[c])]
    _require(not non_numeric, f"non-numeric feature columns: {non_numeric[:10]}")
    _require(pd.api.types.is_integer_dtype(classes[S.RAW_CLASS]), "class must be integer")
    for c in (S.RAW_EDGE_SRC, S.RAW_EDGE_DST):
        _require(pd.api.types.is_integer_dtype(edges[c]), f"edge column {c} must be integer")

    # --- identifiers & labels ---
    _require(not features[S.RAW_TX_ID].duplicated().any(), "duplicate txId in features")
    _require(not classes[S.RAW_TX_ID].duplicated().any(), "duplicate txId in classes")
    feat_ids = pd.Index(features[S.RAW_TX_ID])
    _require(
        feat_ids.sort_values().equals(pd.Index(classes[S.RAW_TX_ID]).sort_values()),
        "features and classes contain different txId sets",
    )
    bad_classes = set(classes[S.RAW_CLASS].unique()) - set(S.CLASS_TO_LABEL)
    _require(not bad_classes, f"unexpected class values: {bad_classes}")

    ts = features[S.RAW_TIME_STEP]
    _require(ts.min() >= 1, f"time step < 1 found: {ts.min()}")
    _require(not ts.isna().any(), "missing time steps")

    feature_values = features[S.ALL_FEATURES].to_numpy(dtype=np.float64)
    _require(not np.isinf(feature_values).any(), "infinite feature values found")

    # --- edges ---
    _require(
        bool(
            edges[S.RAW_EDGE_SRC].isin(feat_ids).all()
            and edges[S.RAW_EDGE_DST].isin(feat_ids).all()
        ),
        "edge list references unknown txIds",
    )

    warnings: list[str] = []
    n_ts = int(ts.nunique())
    if n_ts != S.EXPECTED_N_TIME_STEPS:
        warnings.append(f"expected {S.EXPECTED_N_TIME_STEPS} time steps, found {n_ts}")

    missing_by_col = features[S.ALL_FEATURES].isna().sum()
    missing_by_col = {c: int(n) for c, n in missing_by_col.items() if n > 0}
    unexpected_missing = set(missing_by_col) - set(S.AUGMENTED_FEATURES)
    if unexpected_missing:
        warnings.append(f"missing values outside augmented features: {sorted(unexpected_missing)}")

    row_has_nan = features[S.ALL_FEATURES].isna().any(axis=1)
    cls = classes.set_index(S.RAW_TX_ID)[S.RAW_CLASS]
    nan_ids = features.loc[row_has_nan, S.RAW_TX_ID]
    nan_by_class = cls.loc[nan_ids].value_counts().to_dict()
    if row_has_nan.any() and nan_by_class.get(S.CLASS_ILLICIT, 0) == 0:
        warnings.append(
            "LEAKAGE RISK: no illicit transaction has missing augmented features, so "
            "missingness itself predicts the label. Missing indicators are deliberately not used."
        )

    ts_map = features.set_index(S.RAW_TX_ID)[S.RAW_TIME_STEP]
    t_src = ts_map.loc[edges[S.RAW_EDGE_SRC]].to_numpy()
    t_dst = ts_map.loc[edges[S.RAW_EDGE_DST]].to_numpy()
    n_cross = int((t_src != t_dst).sum())
    if n_cross:
        warnings.append(f"{n_cross} edges connect different time steps")

    report = ValidationReport(
        n_transactions=len(features),
        n_edges=len(edges),
        class_counts={
            int(k): int(v) for k, v in classes[S.RAW_CLASS].value_counts().sort_index().items()
        },
        time_step_range=(int(ts.min()), int(ts.max())),
        n_time_steps=n_ts,
        missing_by_column=missing_by_col,
        rows_with_missing=int(row_has_nan.sum()),
        rows_with_missing_by_class={int(k): int(v) for k, v in nan_by_class.items()},
        n_cross_time_step_edges=n_cross,
        n_self_loops=int((edges[S.RAW_EDGE_SRC] == edges[S.RAW_EDGE_DST]).sum()),
        n_duplicate_edges=int(edges.duplicated().sum()),
        warnings=warnings,
    )
    for w in warnings:
        log.warning(w)
    return report


# --------------------------------------------------------------------------- #
# Canonicalisation
# --------------------------------------------------------------------------- #
def build_dataset(features: pd.DataFrame, classes: pd.DataFrame, edges: pd.DataFrame) -> Dataset:
    """Join labels by ``txId`` and convert to canonical column names/dtypes."""
    tx = features.merge(classes, on=S.RAW_TX_ID, how="inner", validate="one_to_one")
    tx = tx.rename(
        columns={S.RAW_TX_ID: S.TX_ID, S.RAW_TIME_STEP: S.TIME_STEP, S.RAW_CLASS: S.RAW_LABEL}
    )
    meta = pd.DataFrame(
        {
            S.TX_ID: tx[S.TX_ID].astype("int64"),
            S.TIME_STEP: tx[S.TIME_STEP].astype("int16"),
            S.RAW_LABEL: tx[S.RAW_LABEL].astype("int8"),
            S.LABEL: tx[S.RAW_LABEL].map(S.CLASS_TO_LABEL).astype("int8"),
        }
    )
    tx = pd.concat([meta, tx[S.ALL_FEATURES].astype("float32")], axis=1)
    tx = tx.sort_values([S.TIME_STEP, S.TX_ID], kind="stable").reset_index(drop=True)
    e = edges.rename(columns={S.RAW_EDGE_SRC: S.EDGE_SRC, S.RAW_EDGE_DST: S.EDGE_DST})
    return Dataset(transactions=tx, edges=e.reset_index(drop=True))


MANIFEST_VERSION = 2


def raw_file_provenance(raw_dir: Path) -> dict[str, dict]:
    """Fingerprint each raw file actually on disk and compare it with the published one.

    ``sha256``/``size_bytes`` describe the file that was read; ``published_sha256`` is the
    upstream LFS object id; ``matches_published`` is True only if both size and digest agree.
    """
    out: dict[str, dict] = {}
    for rf in REMOTE_FILES:
        path = raw_dir / rf.name
        actual = sha256_file(path)
        size = path.stat().st_size
        out[rf.name] = {
            "sha256": actual,
            "size_bytes": size,
            "published_sha256": rf.sha256,
            "matches_published": actual == rf.sha256 and size == rf.size,
        }
    return out


def prepare(raw_dir: Path, processed_dir: Path) -> ValidationReport:
    """Validate raw files and write the canonical Parquet cache plus a manifest."""
    features, classes, edges = read_raw(raw_dir)
    report = validate_raw(features, classes, edges)
    ds = build_dataset(features, classes, edges)
    processed_dir.mkdir(parents=True, exist_ok=True)
    ds.transactions.to_parquet(processed_dir / TRANSACTIONS_PARQUET, index=False)
    ds.edges.to_parquet(processed_dir / EDGES_PARQUET, index=False)

    raw_files = raw_file_provenance(raw_dir)
    write_json(
        processed_dir / MANIFEST_JSON,
        {
            "manifest_version": MANIFEST_VERSION,
            "source": "Elliptic++ transactions dataset (git-disl/EllipticPlusPlus)",
            "synthetic": (raw_dir / SYNTHETIC_MARKER).exists(),
            "raw_files": raw_files,
            "validation": report.to_dict(),
        },
    )
    if not all(v["matches_published"] for v in raw_files.values()):
        log.warning(
            "Raw files do not match published hashes: %s",
            {k: v["matches_published"] for k, v in raw_files.items()},
        )
    log.info("Wrote processed dataset to %s", processed_dir)
    return report


def load_processed(processed_dir: Path) -> Dataset:
    tx_path = processed_dir / TRANSACTIONS_PARQUET
    if not tx_path.exists():
        raise FileNotFoundError(f"{tx_path} not found. Run `chainwatch prepare` first.")
    return Dataset(
        transactions=pd.read_parquet(tx_path),
        edges=pd.read_parquet(processed_dir / EDGES_PARQUET),
    )


def load_manifest(processed_dir: Path) -> dict:
    return read_json(processed_dir / MANIFEST_JSON)
