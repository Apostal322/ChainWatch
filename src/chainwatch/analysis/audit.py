"""Independent re-verification of Phase 1 artifacts (`chainwatch audit`).

Re-predicts every evaluated run from its saved model and recomputes metrics with plain
scikit-learn, independent of ``chainwatch.evaluation.metrics``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_recall_curve,
    roc_auc_score,
)

from chainwatch.config import Config
from chainwatch.data import schema as S
from chainwatch.data.download import verify_raw_dir
from chainwatch.data.loader import load_processed
from chainwatch.features.preprocessing import fill_degrees_from_edges
from chainwatch.models.artifacts import TEST_METRICS_FILE, list_runs, load_artifact
from chainwatch.utils import get_logger, read_json, write_json

log = get_logger(__name__)


def audit_phase1(cfg: Config) -> dict[str, Any]:
    ds = load_processed(cfg.paths.processed_dir)
    tx = fill_degrees_from_edges(ds.transactions, ds.edges).set_index(S.TX_ID)
    report: dict[str, Any] = {"raw_files_match_published_hashes": None, "runs": {}}
    try:
        report["raw_files_match_published_hashes"] = verify_raw_dir(cfg.paths.raw_dir)
    except OSError as exc:  # raw files may legitimately be absent after `prepare`
        report["raw_files_match_published_hashes"] = f"unavailable: {exc}"

    for run in list_runs(cfg.paths.artifacts_dir):
        if not (run / TEST_METRICS_FILE).exists():
            continue
        model, meta = load_artifact(run)
        reported = read_json(run / TEST_METRICS_FILE)["test"]
        test = pd.read_parquet(run / "test_predictions.parquet")
        val = pd.read_parquet(run / "val_predictions.parquet")
        split = meta["config"]["split"]
        rescored = model.predict_proba(tx.loc[test[S.TX_ID], meta["feature_columns"]])[:, 1]
        y, s = test[S.LABEL].to_numpy(), test["score"].to_numpy()
        pr, rc, th = precision_recall_curve(val[S.LABEL], val["score"])
        f1 = 2 * pr[:-1] * rc[:-1] / np.maximum(pr[:-1] + rc[:-1], 1e-12)
        late = test[S.TIME_STEP].to_numpy() >= 43
        recomputed = {
            "average_precision": average_precision_score(y, s),
            "roc_auc": roc_auc_score(y, s),
            "f1": f1_score(y, s >= meta["threshold"]),
        }
        report["runs"][run.name] = {
            "test_steps_ok": bool(test[S.TIME_STEP].between(*split["test"]).all()),
            "val_steps_ok": bool(val[S.TIME_STEP].between(*split["val"]).all()),
            "no_unknown_labels": bool((test[S.LABEL] >= 0).all() and (val[S.LABEL] >= 0).all()),
            "n_test_matches_data": len(test)
            == int(
                (
                    (ds.transactions[S.TIME_STEP].between(*split["test"]))
                    & (ds.transactions[S.LABEL] >= 0)
                ).sum()
            ),
            "max_abs_rescore_diff": float(np.abs(rescored - s).max()),
            "max_abs_metric_diff": float(max(abs(recomputed[k] - reported[k]) for k in recomputed)),
            "threshold_is_val_max_f1": bool(abs(th[int(np.argmax(f1))] - meta["threshold"]) < 1e-9),
            "ap_35_42": float(average_precision_score(y[~late], s[~late])),
            "ap_43_49": float(average_precision_score(y[late], s[late])),
            "roc_auc_35_42": float(roc_auc_score(y[~late], s[~late])),
            "roc_auc_43_49": float(roc_auc_score(y[late], s[late])),
            "prevalence_43_49": float(y[late].mean()),
            **{f"recomputed_{k}": float(v) for k, v in recomputed.items()},
        }
    out = cfg.paths.reports_dir / "phase2" / "audit.json"
    write_json(out, report)
    log.info("Audit written to %s", out)
    return report
