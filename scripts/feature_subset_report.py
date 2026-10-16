"""Results of the live-computable feature-subset experiment
(docs/LIVE_FEATURE_SUBSET_PROTOCOL.md). Reads the newest evaluated run per model and feature
set and writes aggregate results only (no row-level data) to reports/phase6/feature_subset/.

    python scripts/feature_subset_report.py
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

from chainwatch.config import REPO_ROOT
from chainwatch.evaluation.metrics import precision_at_k
from chainwatch.evaluation.uncertainty import block_bootstrap, percentile_interval

RUN_ROOTS = {
    "all": REPO_ROOT / "artifacts" / "runs",
    "augmented": REPO_ROOT / "artifacts" / "runs-subset-augmented",
    "live_computable": REPO_ROOT / "artifacts" / "runs-subset-live",
}
OUT = REPO_ROOT / "reports" / "phase6" / "feature_subset"
PERIODS = {"35-49": (35, 49), "35-42": (35, 42), "43-49": (43, 49)}
BLOCK, RESAMPLES, SEED = 3, 2000, 0


def newest(model: str, feature_set: str) -> Path | None:
    runs = sorted(RUN_ROOTS[feature_set].glob(f"{model}_{feature_set}_*/test_predictions.parquet"))
    return runs[-1].parent if runs else None


def ap(df: pd.DataFrame) -> float:
    return float(average_precision_score(df["label"], df["score"])) if df["label"].any() else np.nan


def p_at_k(df: pd.DataFrame, k: int = 20) -> float:
    """Mean per-step precision@k, tie-aware (the repository's metric, not head(k))."""
    vals = [
        precision_at_k(g["label"].to_numpy(), g["score"].to_numpy(), k)
        for _, g in df.groupby("time_step")
    ]
    return float(np.mean(vals))


def leakage_checks(run: Path) -> dict:
    """Missing-value shortcut and what the restricted XGBoost model relies on."""
    meta = json.loads((run / "metadata.json").read_text())
    cols = meta["feature_columns"]
    tx = pd.read_parquet(REPO_ROOT / "data" / "processed" / "transactions.parquet")
    df = pd.read_parquet(run / "test_predictions.parquet").merge(tx[["tx_id", *cols]], on="tx_id")
    miss = df[cols].isna().any(axis=1)
    est = joblib.load(run / "model.joblib").steps[-1][1]
    # share of TOTAL gain over the trees prediction uses (up to best_iteration): mean gain
    # per split ("gain") over all trees, as published before, misstated fees ~2.4x
    trees = est.get_booster().trees_to_dataframe()
    best = getattr(est, "best_iteration", None)
    if best is not None:
        trees = trees[trees["Tree"] <= best]
    splits = trees[trees["Feature"] != "Leaf"]
    gain = splits.groupby("Feature")["Gain"].sum().to_dict()
    names = {f"f{i}": c for i, c in enumerate(cols)}
    total = sum(gain.values())
    return {
        "test_rows_missing_any_feature": int(miss.sum()),
        "illicit_among_missing": int(df.loc[miss, "label"].sum()),
        "ap_all_rows": round(ap(df), 4),
        "ap_complete_rows_only": round(ap(df[~miss]), 4),
        "val_ap_selected_model": round(meta["val_metrics"]["average_precision"], 4),
        "total_gain_share": {
            names.get(k, k): round(v / total, 3)
            for k, v in sorted(gain.items(), key=lambda kv: -kv[1])
        },
    }


def main() -> int:
    preds: dict[tuple[str, str], pd.DataFrame] = {}
    meta: dict[tuple[str, str], dict] = {}
    for fs in RUN_ROOTS:
        for model in ("logreg", "xgboost"):
            run = newest(model, fs)
            if run is None:
                continue
            preds[(model, fs)] = pd.read_parquet(run / "test_predictions.parquet")
            m = json.loads((run / "test_metrics.json").read_text())["test"]
            meta[(model, fs)] = {"run": run.name, "f1_at_val_threshold": m["f1"],
                                 "threshold": m["threshold"]}  # fmt: skip
    rows = []
    for (model, fs), df in preds.items():
        row = {"model": model, "feature_set": fs, **meta[(model, fs)]}
        for name, (a, b) in PERIODS.items():
            part = df[(df["time_step"] >= a) & (df["time_step"] <= b)]
            row[f"ap_{name}"] = round(ap(part), 4)
            row[f"prevalence_{name}"] = round(float(part["label"].mean()), 4)
        row["roc_auc_35-49"] = round(float(roc_auc_score(df["label"], df["score"])), 4)
        row["mean_step_p_at_20"] = round(p_at_k(df), 4)
        rows.append(row)

    comparisons = {}
    for model in ("logreg", "xgboost"):
        for fs in ("live_computable", "augmented"):
            if (model, fs) not in preds or (model, "all") not in preds:
                continue
            a = preds[(model, fs)].set_index("tx_id")
            b = preds[(model, "all")].set_index("tx_id")
            joined = a.join(b["score"], rsuffix="_all", how="inner")
            steps = sorted(joined["time_step"].unique())
            groups = {s: joined[joined["time_step"] == s] for s in steps}

            def delta(units, frame_groups=groups):
                d = pd.concat([frame_groups[s] for s in units])
                if not d["label"].any():
                    return np.nan
                return average_precision_score(d["label"], d["score"]) - average_precision_score(
                    d["label"], d["score_all"]
                )

            dist = block_bootstrap(steps, delta, BLOCK, RESAMPLES, SEED)
            lo, hi = percentile_interval(dist)
            comparisons[f"{model}: {fs} - all"] = {
                "delta_ap_35-49": round(float(delta(steps)), 4),
                "ci95": [round(lo, 4), round(hi, 4)],
                "share_resamples_delta_le_0": round(float(np.mean(dist <= 0)), 4),
            }

    per_step = []
    for (model, fs), df in preds.items():
        for s, g in df.groupby("time_step"):
            per_step.append({"model": model, "feature_set": fs, "time_step": int(s),
                             "n": len(g), "illicit": int(g["label"].sum()),
                             "ap": round(ap(g), 4)})  # fmt: skip

    OUT.mkdir(parents=True, exist_ok=True)
    result = {
        "protocol": "docs/LIVE_FEATURE_SUBSET_PROTOCOL.md",
        "status": "EXPLORATORY (steps 35-49 were inspected in Phase 1); NOT a live model",
        "bootstrap": {"unit": "test time step", "block_length": BLOCK, "resamples": RESAMPLES},
        "results": rows,
        "paired_comparisons": comparisons,
    }
    live_run = newest("xgboost", "live_computable")
    if live_run is not None:
        result["xgboost_live_computable_checks"] = leakage_checks(live_run)
    (OUT / "results.json").write_text(json.dumps(result, indent=2))
    pd.DataFrame(per_step).sort_values(["model", "feature_set", "time_step"]).to_csv(
        OUT / "per_step_ap.csv", index=False
    )

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for fs, style in (("all", "-"), ("augmented", "--"), ("live_computable", ":")):
        if ("xgboost", fs) in preds:
            df = preds[("xgboost", fs)]
            p, r, _ = precision_recall_curve(df["label"], df["score"])
            ax.plot(r, p, style, label=f"XGBoost, {fs} (AP {ap(df):.3f})")
    ax.axhline(float(next(iter(preds.values()))["label"].mean()), color="grey", lw=0.8,
               label="prevalence")  # fmt: skip
    ax.set(xlabel="Recall (illicit)", ylabel="Precision", title="Test steps 35-49 (exploratory)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "pr_curves_xgboost.png", dpi=120)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
