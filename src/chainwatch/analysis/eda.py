"""Exploratory data analysis: summary statistics and figures written to ``reports/eda``.

Everything here is descriptive and computed directly from the processed data. Plots
over *all* time steps (including test) are fine for understanding the dataset; they
are never fed back into modelling decisions beyond the split design documented in
docs/EVALUATION.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless and Windows-safe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from chainwatch.config import Config
from chainwatch.data import schema as S
from chainwatch.data.loader import load_processed
from chainwatch.features.preprocessing import fill_degrees_from_edges
from chainwatch.utils import get_logger, write_json

log = get_logger(__name__)

# Validated categorical palette (slot 1 blue, slot 2 orange) + neutral for "unknown".
COLOR_LICIT = "#2a78d6"
COLOR_ILLICIT = "#eb6834"
COLOR_UNKNOWN = "#a3a29c"
INK, INK_MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def _style(ax: plt.Axes, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=16)
    ax.set_xlabel(xlabel, color=INK_MUTED)
    ax.set_ylabel(ylabel, color=INK_MUTED)
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED)


def _shade_splits(ax: plt.Axes, cfg: Config) -> None:
    for name, (lo, hi) in cfg.split.ranges().items():
        if name != "train":
            ax.axvspan(lo - 0.5, hi + 0.5, color=GRID, alpha=0.5 if name == "test" else 0.25, lw=0)
        ax.text(
            (lo + hi) / 2,
            1.0,
            name,
            transform=ax.get_xaxis_transform(),
            ha="center",
            va="bottom",
            fontsize=8,
            color=INK_MUTED,
        )


def class_table(tx: pd.DataFrame) -> pd.DataFrame:
    tab = pd.crosstab(tx[S.TIME_STEP], tx[S.RAW_LABEL]).rename(
        columns={S.CLASS_ILLICIT: "illicit", S.CLASS_LICIT: "licit", S.CLASS_UNKNOWN: "unknown"}
    )
    tab["labeled"] = tab["illicit"] + tab["licit"]
    tab["illicit_rate_labeled"] = tab["illicit"] / tab["labeled"]
    tab["labeled_fraction"] = tab["labeled"] / (tab["labeled"] + tab["unknown"])
    return tab


def standardized_mean_shift(tx: pd.DataFrame, cfg: Config, feature_cols: list[str]) -> pd.DataFrame:
    """|mean_test - mean_train| / std_train per feature (labelled rows only).

    A simple, model-free covariate-shift indicator; proper drift detection is a later phase.
    """
    lab = tx[tx[S.LABEL] != S.LABEL_UNKNOWN]
    tr = lab[lab[S.TIME_STEP].between(*cfg.split.train)]
    te = lab[lab[S.TIME_STEP].between(*cfg.split.test)]
    mu_tr, sd_tr = tr[feature_cols].mean(), tr[feature_cols].std().replace(0, np.nan)
    smd = ((te[feature_cols].mean() - mu_tr).abs() / sd_tr).rename("smd")
    group = pd.Series(
        [
            "local"
            if c in S.LOCAL_FEATURES
            else "aggregate"
            if c in S.AGGREGATE_FEATURES
            else "augmented"
            for c in feature_cols
        ],
        index=feature_cols,
        name="group",
    )
    return pd.concat([smd, group], axis=1).sort_values("smd", ascending=False)


def run_eda(cfg: Config) -> dict[str, Any]:
    out_dir = cfg.paths.reports_dir / "eda"
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    ds = load_processed(cfg.paths.processed_dir)
    tx = ds.transactions

    # ---- class distribution ------------------------------------------------
    tab = class_table(tx)
    tab.to_csv(out_dir / "class_counts_by_time_step.csv")

    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    t = tab.index.to_numpy()
    ax.bar(t, tab["licit"], color=COLOR_LICIT, width=0.8, label="licit", edgecolor=SURFACE, lw=1)
    ax.bar(
        t,
        tab["illicit"],
        bottom=tab["licit"],
        color=COLOR_ILLICIT,
        width=0.8,
        label="illicit",
        edgecolor=SURFACE,
        lw=1,
    )
    ax.bar(
        t,
        tab["unknown"],
        bottom=tab["licit"] + tab["illicit"],
        color=COLOR_UNKNOWN,
        width=0.8,
        label="unknown (unlabelled)",
        edgecolor=SURFACE,
        lw=1,
    )
    _style(ax, "Transactions per time step by class", "time step", "transactions")
    ax.legend(frameon=False, ncols=3, loc="upper right", labelcolor=INK_MUTED)
    fig.tight_layout()
    fig.savefig(fig_dir / "class_counts_by_time_step.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 3.6), facecolor=SURFACE)
    _shade_splits(ax, cfg)
    ax.plot(t, tab["illicit_rate_labeled"], color=COLOR_ILLICIT, lw=2, marker="o", ms=4)
    _style(ax, "Illicit share of labelled transactions per time step", "time step", "illicit rate")
    ax.set_ylim(0, None)
    fig.tight_layout()
    fig.savefig(fig_dir / "illicit_rate_by_time_step.png", dpi=150)
    plt.close(fig)

    # ---- missingness ---------------------------------------------------------
    miss_rows = tx[S.AUGMENTED_FEATURES].isna().any(axis=1)
    missing_by_class = (
        tx.loc[miss_rows, S.RAW_LABEL].map({1: "illicit", 2: "licit", 3: "unknown"}).value_counts()
    )

    # ---- covariate shift train -> test --------------------------------------
    tx_filled = fill_degrees_from_edges(tx, ds.edges)
    shift = standardized_mean_shift(tx_filled, cfg, S.ALL_FEATURES).astype({"smd": "float64"})
    shift.to_csv(out_dir / "feature_shift_train_vs_test.csv")
    top = shift.head(20).iloc[::-1]
    fig, ax = plt.subplots(figsize=(8, 6), facecolor=SURFACE)
    ax.barh(top.index, top["smd"], color=COLOR_LICIT, height=0.6)
    _style(ax, "Largest train -> test mean shifts (labelled rows)", "|Δmean| / train std", "")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(fig_dir / "feature_shift_top20.png", dpi=150)
    plt.close(fig)

    # ---- duplicates & graph ---------------------------------------------------
    dup_mask = tx.duplicated(subset=S.ALL_FEATURES, keep=False)
    dups = tx[dup_mask]
    conflicting = 0
    if not dups.empty:
        g = (
            dups[dups[S.LABEL] != S.LABEL_UNKNOWN]
            .groupby(pd.util.hash_pandas_object(dups[S.ALL_FEATURES], index=False))[S.LABEL]
            .nunique()
        )
        conflicting = int((g > 1).sum())

    deg = pd.concat([ds.edges[S.EDGE_SRC], ds.edges[S.EDGE_DST]]).value_counts()
    isolated = int((~tx[S.TX_ID].isin(deg.index)).sum())

    lab = tx[tx[S.LABEL] != S.LABEL_UNKNOWN]
    split_counts = {
        name: {
            "n_labeled": int(lab[S.TIME_STEP].between(lo, hi).sum()),
            "n_illicit": int(((lab[S.LABEL] == 1) & lab[S.TIME_STEP].between(lo, hi)).sum()),
        }
        for name, (lo, hi) in cfg.split.ranges().items()
    }

    summary = {
        "n_transactions": len(tx),
        "n_edges": len(ds.edges),
        "class_counts": tx[S.RAW_LABEL]
        .map({1: "illicit", 2: "licit", 3: "unknown"})
        .value_counts()
        .to_dict(),
        "illicit_rate_among_labeled": float((lab[S.LABEL] == 1).mean()),
        "n_time_steps": int(tx[S.TIME_STEP].nunique()),
        "split_counts": split_counts,
        "rows_with_missing_augmented": int(miss_rows.sum()),
        "rows_with_missing_by_class": missing_by_class.to_dict(),
        "exact_duplicate_feature_rows": int(tx.duplicated(subset=S.ALL_FEATURES).sum()),
        "duplicate_groups_with_conflicting_labels": conflicting,
        "isolated_transactions": isolated,
        "mean_degree": float(2 * len(ds.edges) / len(tx)),
        "top10_shifted_features": shift.head(10)["smd"].round(3).to_dict(),
        "mean_shift_by_group": shift.groupby("group")["smd"].mean().round(3).to_dict(),
        "augmented_feature_quantiles": tx[S.AUGMENTED_FEATURES]
        .quantile([0.0, 0.5, 0.99, 1.0])
        .T.round(4)
        .to_dict(orient="index"),
    }
    write_json(out_dir / "summary.json", summary)
    _write_markdown(out_dir / "README.md", summary, tab)
    log.info("EDA written to %s", out_dir)
    return summary


def _write_markdown(path: Path, s: dict[str, Any], tab: pd.DataFrame) -> None:
    lines = [
        "# Elliptic++ transactions: EDA summary",
        "",
        "Generated by `chainwatch eda` from the processed dataset. Do not edit by hand.",
        "",
        f"- Transactions: **{s['n_transactions']:,}**, directed edges: **{s['n_edges']:,}**, "
        f"time steps: **{s['n_time_steps']}**",
        f"- Class counts: {s['class_counts']}",
        f"- Illicit share among labelled: **{s['illicit_rate_among_labeled']:.4f}**",
        f"- Labelled split sizes: {s['split_counts']}",
        f"- Rows with missing augmented features: {s['rows_with_missing_augmented']} "
        f"(by class: {s['rows_with_missing_by_class']})",
        f"- Exact duplicate feature rows: {s['exact_duplicate_feature_rows']} "
        f"(groups with conflicting labels: {s['duplicate_groups_with_conflicting_labels']})",
        f"- Isolated transactions (no edges): {s['isolated_transactions']:,}; "
        f"mean degree {s['mean_degree']:.3f}",
        f"- Mean standardised train->test shift by feature group: {s['mean_shift_by_group']}",
        "",
        "![class counts](figures/class_counts_by_time_step.png)",
        "",
        "![illicit rate](figures/illicit_rate_by_time_step.png)",
        "",
        "![feature shift](figures/feature_shift_top20.png)",
        "",
        "## Per time step",
        "",
        tab.to_markdown(floatfmt=".4f"),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
