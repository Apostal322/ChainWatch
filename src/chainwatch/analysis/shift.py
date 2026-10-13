"""Distribution-shift diagnostics (docs/PHASE2_PROTOCOL.md §6).

Vocabulary used throughout (x = features, y = label):

* prior-probability shift: P(y) changes;
* covariate shift: P(x) changes;
* class-conditional shift: P(x | y) changes (e.g. illicit transactions look different);
* concept shift: P(y | x) changes (the same x now carries a different label).

Labels permit only partial separation of these. In particular, "new illicit behaviour in
regions the training data never covered" and "concept shift" both make a past model fail
while a model fitted on recent labels succeeds; the within-period learnability analysis
reports that pattern without claiming which mechanism, let alone a cause, produced it.

Several analyses use steps 35-49, which were inspected in Phase 1. They are diagnostic,
not evaluation, and nothing here feeds into model or strategy selection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from xgboost import XGBClassifier

from chainwatch.backtest.analysis import fast_average_precision
from chainwatch.config import Config
from chainwatch.data import schema as S
from chainwatch.data.loader import load_processed
from chainwatch.evaluation.metrics import (
    calibration_metrics,
    expected_hits_at_k,
    prevalence_standardised_ap,
    thresholded_metrics,
)
from chainwatch.features.preprocessing import (
    build_preprocessor,
    fill_degrees_from_edges,
    time_proxy_features,
)
from chainwatch.models.artifacts import list_runs, load_artifact
from chainwatch.utils import get_logger, write_json

log = get_logger(__name__)

GROUPS = {
    "local": S.LOCAL_FEATURES,
    "aggregate": S.AGGREGATE_FEATURES,
    "augmented": S.AUGMENTED_FEATURES,
}
PERIODS = {"train 1-29": (1, 29), "val 30-34": (30, 34), "35-42": (35, 42), "43-49": (43, 49)}
INK, INK_MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _imputed_matrix(tx: pd.DataFrame, reference: pd.DataFrame) -> np.ndarray:
    pre = build_preprocessor(S.ALL_FEATURES, log1p_augmented=True, scale=False).fit(
        reference[S.ALL_FEATURES]
    )
    return pre.transform(tx[S.ALL_FEATURES])


def _cv_auc(X: np.ndarray, y: np.ndarray, seed: int = 0, folds: int = 3) -> float:
    """Cross-validated ROC-AUC of a small gradient-boosted domain classifier."""
    folds = min(folds, int(min(y.sum(), len(y) - y.sum())))
    if folds < 2:
        return float("nan")
    oof = np.zeros(len(y))
    for tr, te in StratifiedKFold(folds, shuffle=True, random_state=seed).split(X, y):
        clf = XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            tree_method="hist",
            n_jobs=-1,
            random_state=seed,
        )
        clf.fit(X[tr], y[tr])
        oof[te] = clf.predict_proba(X[te])[:, 1]
    return float(roc_auc_score(y, oof))


def _style(ax: plt.Axes, title: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=9)
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=8)


def _periods_shade(ax: plt.Axes) -> None:
    ax.axvspan(29.5, 34.5, color=GRID, alpha=0.35, lw=0)
    ax.axvspan(34.5, 49.5, color=GRID, alpha=0.7, lw=0)
    ax.axvline(42.5, color=INK_MUTED, lw=0.8, ls=":")


# --------------------------------------------------------------------------- #
# analyses
# --------------------------------------------------------------------------- #
def prior_and_missingness(tx: pd.DataFrame) -> pd.DataFrame:
    lab = tx[tx[S.LABEL] != S.LABEL_UNKNOWN]
    miss = tx["total_BTC"].isna()
    out = pd.DataFrame(
        {
            "n_tx": tx.groupby(S.TIME_STEP).size(),
            "n_labeled": lab.groupby(S.TIME_STEP).size(),
            "n_illicit": lab[lab[S.LABEL] == 1].groupby(S.TIME_STEP).size(),
            "missing_rate_all": miss.groupby(tx[S.TIME_STEP]).mean(),
            "missing_rate_labeled": lab["total_BTC"].isna().groupby(lab[S.TIME_STEP]).mean(),
        }
    ).fillna({"n_illicit": 0})
    out["prevalence"] = out["n_illicit"] / out["n_labeled"]
    out["labeled_fraction"] = out["n_labeled"] / out["n_tx"]
    return out


def covariate_shift(
    tx: pd.DataFrame,
    train_range=(1, 29),
    pool_size=20000,
    seed=0,
    time_proxies: list[str] | None = None,
) -> pd.DataFrame:
    """Per step: domain-classifier AUC (pool vs step) and median KS per feature group.

    Uses features only (labelled and unknown transactions), never labels. For steps
    inside the training range the step itself is removed from the pool.
    """
    rng = np.random.default_rng(seed)
    pool_all = tx[tx[S.TIME_STEP].between(*train_range)]
    X_all = _imputed_matrix(tx, pool_all)
    feat_idx = {g: [S.ALL_FEATURES.index(c) for c in cols] for g, cols in GROUPS.items()}
    ts = tx[S.TIME_STEP].to_numpy()
    rows = []
    for t in sorted(tx[S.TIME_STEP].unique()):
        pool_ix = np.flatnonzero((ts >= train_range[0]) & (ts <= train_range[1]) & (ts != t))
        pool_ix = rng.choice(pool_ix, size=min(pool_size, len(pool_ix)), replace=False)
        step_ix = np.flatnonzero(ts == t)
        X = np.vstack([X_all[pool_ix], X_all[step_ix]])
        y = np.r_[np.zeros(len(pool_ix)), np.ones(len(step_ix))].astype(int)
        keep = [j for j, c in enumerate(S.ALL_FEATURES) if c not in (time_proxies or [])]
        row: dict[str, Any] = {
            S.TIME_STEP: int(t),
            "domain_auc": _cv_auc(X, y, seed),
            "domain_auc_no_time_proxy": _cv_auc(X[:, keep], y, seed),
        }
        for g, idx in feat_idx.items():
            ks = [ks_2samp(X_all[pool_ix, j], X_all[step_ix, j]).statistic for j in idx]
            row[f"median_ks_{g}"] = float(np.median(ks))
        rows.append(row)
        log.info("covariate shift step %d: domain AUC %.3f", t, row["domain_auc"])
    return pd.DataFrame(rows)


def class_conditional_shift(
    tx: pd.DataFrame, seed: int = 0, time_proxies: list[str] | None = None
) -> tuple[pd.DataFrame, dict]:
    """Domain AUC between the training period and later periods, separately per class."""
    lab = tx[tx[S.LABEL] != S.LABEL_UNKNOWN]
    train = lab[lab[S.TIME_STEP].between(1, 29)]
    X_all = _imputed_matrix(lab, train)
    ts = lab[S.TIME_STEP].to_numpy()
    yl = lab[S.LABEL].to_numpy()
    rows = []
    top_features: dict[str, list[str]] = {}
    comparisons = {
        "train vs 30-34": ((1, 29), (30, 34)),
        "train vs 35-42": ((1, 29), (35, 42)),
        "train vs 43-49": ((1, 29), (43, 49)),
        "35-42 vs 43-49": ((35, 42), (43, 49)),
    }
    for cls_name, cls in (("illicit", 1), ("licit", 0)):
        for name, (a, b) in comparisons.items():
            ia = np.flatnonzero((yl == cls) & (ts >= a[0]) & (ts <= a[1]))
            ib = np.flatnonzero((yl == cls) & (ts >= b[0]) & (ts <= b[1]))
            X = np.vstack([X_all[ia], X_all[ib]])
            y = np.r_[np.zeros(len(ia)), np.ones(len(ib))].astype(int)
            auc = _cv_auc(X, y, seed, folds=5)
            keep = [j for j, c in enumerate(S.ALL_FEATURES) if c not in (time_proxies or [])]
            auc_np = _cv_auc(X[:, keep], y, seed, folds=5)
            rows.append(
                {
                    "class": cls_name,
                    "comparison": name,
                    "n_a": len(ia),
                    "n_b": len(ib),
                    "domain_auc": auc,
                    "domain_auc_no_time_proxy": auc_np,
                }
            )
            if cls_name == "illicit" and name == "train vs 43-49":
                clf = XGBClassifier(
                    n_estimators=100,
                    max_depth=4,
                    learning_rate=0.1,
                    tree_method="hist",
                    n_jobs=-1,
                    random_state=seed,
                ).fit(X, y)
                gain = clf.get_booster().get_score(importance_type="total_gain")
                ranked = sorted(gain.items(), key=lambda kv: -kv[1])[:10]
                top_features[name] = [S.ALL_FEATURES[int(k[1:])] for k, _ in ranked]
    return pd.DataFrame(rows), top_features


def within_period_learnability(
    tx: pd.DataFrame, xgb_params: dict[str, Any], past_until: int = 34, seed: int = 0
) -> pd.DataFrame:
    """Leave-one-step-out models *inside* a period vs a past-only model (diagnostic only).

    * past:            trained on labelled steps 1..past_until, scores each period step;
    * within (LOSO):   trained on the other steps of the same period only;
    * past + within:   trained on 1..past_until plus the other steps of the period.

    LOSO uses later steps to score earlier ones, so it is NOT a deployable strategy. It
    asks whether the period's labels are predictable from its features at all, and
    whether adding the past helps or hurts.
    """
    lab = tx[tx[S.LABEL] != S.LABEL_UNKNOWN]
    cols = S.ALL_FEATURES

    def fit(df: pd.DataFrame):
        y = df[S.LABEL].to_numpy()
        pre = build_preprocessor(cols, log1p_augmented=True, scale=False)
        clf = XGBClassifier(
            random_state=seed, scale_pos_weight=(len(y) - y.sum()) / y.sum(), **xgb_params
        )
        from sklearn.pipeline import Pipeline

        return Pipeline([("p", pre), ("c", clf)]).fit(df[cols], y)

    past = lab[lab[S.TIME_STEP] <= past_until]
    past_model = fit(past)
    rows = []
    for period, (lo, hi) in {"35-42": (35, 42), "43-49": (43, 49)}.items():
        scores = {"past": [], "within (LOSO)": [], "past + within (LOSO)": []}
        ys = []
        per = lab[lab[S.TIME_STEP].between(lo, hi)]
        for t in range(lo, hi + 1):
            held = per[per[S.TIME_STEP] == t]
            rest = per[per[S.TIME_STEP] != t]
            ys.append(held[S.LABEL].to_numpy())
            scores["past"].append(past_model.predict_proba(held[cols])[:, 1])
            scores["within (LOSO)"].append(fit(rest).predict_proba(held[cols])[:, 1])
            scores["past + within (LOSO)"].append(
                fit(pd.concat([past, rest])).predict_proba(held[cols])[:, 1]
            )
        y = np.concatenate(ys)
        for name, sc in scores.items():
            s = np.concatenate(sc)
            rows.append(
                {
                    "period": period,
                    "model": name,
                    "n_illicit": int(y.sum()),
                    "prevalence": float(y.mean()),
                    "pooled_ap": fast_average_precision(y, s),
                    "roc_auc": float(roc_auc_score(y, s)),
                    "std_ap": prevalence_standardised_ap(y, s, 0.109),
                }
            )
        log.info("learnability %s done", period)
    return pd.DataFrame(rows)


def static_model_diagnostics(artifacts_dir: Path, budgets: list[int]) -> tuple[pd.DataFrame, dict]:
    """Per-step scores, ranking, budget and calibration for the Phase 1 XGBoost/all model."""
    runs = [r for r in list_runs(artifacts_dir) if r.name.startswith("xgboost_all_")]
    if not runs:
        raise FileNotFoundError("Phase 1 xgboost_all run not found; run Phase 1 first")
    run = runs[-1]
    _, meta = load_artifact(run)
    val = pd.read_parquet(run / "val_predictions.parquet")
    test = pd.read_parquet(run / "test_predictions.parquet")

    # Platt scaling fitted on validation only (steps 30-34)
    def logit(p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return np.log(p / (1 - p))[:, None]

    platt = LogisticRegression(C=1e6).fit(logit(val["score"].to_numpy()), val[S.LABEL])
    rows = []
    for t, g in pd.concat([val, test]).groupby(S.TIME_STEP):
        y, s = g[S.LABEL].to_numpy(), g["score"].to_numpy()
        cal = calibration_metrics(y, platt.predict_proba(logit(s))[:, 1])
        thr = thresholded_metrics(y, s, meta["threshold"])
        row = {
            S.TIME_STEP: int(t),
            "n": len(y),
            "n_illicit": int(y.sum()),
            "prevalence": float(y.mean()),
            "ap": fast_average_precision(y, s) if y.sum() else np.nan,
            "roc_auc": roc_auc_score(y, s) if 0 < y.sum() < len(y) else np.nan,
            "precision@thr": thr["precision"],
            "recall@thr": thr["recall"],
            "illicit_score_q10": np.quantile(s[y == 1], 0.1) if y.sum() else np.nan,
            "illicit_score_q50": np.quantile(s[y == 1], 0.5) if y.sum() else np.nan,
            "illicit_score_q90": np.quantile(s[y == 1], 0.9) if y.sum() else np.nan,
            "licit_score_q50": np.quantile(s[y == 0], 0.5),
            "licit_score_q99": np.quantile(s[y == 0], 0.99),
            **{f"cal_{k}": v for k, v in cal.items()},
        }
        for k in budgets:
            row[f"p@{k}"] = expected_hits_at_k(y, s, k) / min(k, len(y))
        rows.append(row)
    return pd.DataFrame(rows), {"run": run.name, "threshold": meta["threshold"]}


# --------------------------------------------------------------------------- #
# figures
# --------------------------------------------------------------------------- #
def plot_overview(prior: pd.DataFrame, cov: pd.DataFrame, static: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True, facecolor=SURFACE)
    t = prior.index.to_numpy()
    ax = axes[0]
    _periods_shade(ax)
    ax.plot(t, prior["prevalence"], color=C2, lw=2, marker="o", ms=3)
    _style(ax, "Prior shift: illicit share of labelled transactions", "prevalence")
    ax = axes[1]
    _periods_shade(ax)
    ax.plot(
        cov[S.TIME_STEP],
        cov["domain_auc"],
        color=C1,
        lw=2,
        marker="o",
        ms=3,
        label="all features (saturated by clock-like features)",
    )
    ax.plot(
        cov[S.TIME_STEP],
        cov["domain_auc_no_time_proxy"],
        color=C2,
        lw=2,
        marker="o",
        ms=3,
        label="time-proxy features removed",
    )
    ax.axhline(0.5, color=INK_MUTED, lw=0.8, ls="--")
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK_MUTED)
    _style(
        ax,
        "Covariate shift: domain-classifier AUC, steps 1-29 vs step s (0.5 = no shift)",
        "domain AUC",
    )
    ax = axes[2]
    _periods_shade(ax)
    for col, color, label in (
        ("median_ks_local", C1, "local"),
        ("median_ks_aggregate", C2, "aggregate"),
        ("median_ks_augmented", C3, "augmented"),
    ):
        ax.plot(cov[S.TIME_STEP], cov[col], color=color, lw=2, label=label)
    ax.legend(frameon=False, fontsize=8, ncols=3, loc="upper left", labelcolor=INK_MUTED)
    _style(ax, "Median per-feature KS statistic vs steps 1-29, by feature group", "median KS")
    ax = axes[3]
    _periods_shade(ax)
    ax.plot(
        static[S.TIME_STEP],
        static["roc_auc"],
        color=C1,
        lw=2,
        marker="o",
        ms=3,
        label="ROC-AUC (prevalence-invariant)",
    )
    ax.plot(static[S.TIME_STEP], static["ap"], color=C2, lw=2, marker="o", ms=3, label="AP")
    ax.legend(frameon=False, fontsize=8, loc="lower left", labelcolor=INK_MUTED)
    ax.set_ylim(0, 1.02)
    _style(ax, "Phase 1 static XGBoost (trained on 1-29): val 30-34 and test 35-49", "metric")
    ax.set_xlabel(
        "time step (light band: val 30-34; dark band: test 35-49; dotted: 42|43)",
        color=INK_MUTED,
        fontsize=9,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_scores_and_calibration(static: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), facecolor=SURFACE)
    t = static[S.TIME_STEP]
    ax = axes[0]
    ax.fill_between(
        t, static["illicit_score_q10"], static["illicit_score_q90"], color=C2, alpha=0.2, lw=0
    )
    ax.plot(
        t,
        static["illicit_score_q50"],
        color=C2,
        lw=2,
        marker="o",
        ms=3,
        label="illicit median (band: q10-q90)",
    )
    ax.plot(t, static["licit_score_q99"], color=C1, lw=2, marker="o", ms=3, label="licit q99")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED)
    _style(ax, "Static-model scores by class", "score")
    ax.set_xlabel("time step", color=INK_MUTED, fontsize=9)
    ax = axes[1]
    ax.plot(
        t,
        static["cal_mean_predicted"],
        color=C1,
        lw=2,
        marker="o",
        ms=3,
        label="mean calibrated probability",
    )
    ax.plot(
        t,
        static["cal_observed_rate"],
        color=C2,
        lw=2,
        marker="o",
        ms=3,
        label="observed prevalence",
    )
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED)
    _style(ax, "Calibration-in-the-large (Platt fitted on steps 30-34)", "rate")
    ax.set_xlabel("time step", color=INK_MUTED, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_learnability(learn: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    models = list(dict.fromkeys(learn["model"]))
    periods = list(dict.fromkeys(learn["period"]))
    width = 0.25
    for i, (m, color) in enumerate(zip(models, (C1, C2, C3), strict=False)):
        vals = [
            learn[(learn.model == m) & (learn.period == p)]["pooled_ap"].iloc[0] for p in periods
        ]
        xs = np.arange(len(periods)) + (i - 1) * width
        ax.bar(xs, vals, width=width * 0.92, color=color, label=m)
        for x, v in zip(xs, vals, strict=True):
            ax.text(x, v + 0.01, f"{v:.2f}", ha="center", fontsize=8, color=INK_MUTED)
    prev = [learn[learn.period == p]["prevalence"].iloc[0] for p in periods]
    ax.set_xticks(
        np.arange(len(periods)),
        [f"steps {p}\n(prevalence {q:.3f})" for p, q in zip(periods, prev, strict=True)],
    )
    ax.set_ylim(0, 1.05)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED, loc="upper right")
    _style(ax, "Within-period learnability (diagnostic; LOSO is not deployable)", "pooled AP")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_shift_diagnostics(cfg: Config) -> dict[str, Any]:
    out = cfg.paths.reports_dir / "phase2" / "shift"
    (out / "figures").mkdir(parents=True, exist_ok=True)
    ds = load_processed(cfg.paths.processed_dir)
    tx = fill_degrees_from_edges(ds.transactions, ds.edges)
    budgets = cfg.backtest.budgets if cfg.backtest else [10, 20, 50, 100]
    xgb_params = {k: v for k, v in (cfg.backtest.xgboost if cfg.backtest else {}).items()}

    prior = prior_and_missingness(ds.transactions)
    prior.to_csv(out / "prior_and_missingness_by_step.csv")
    bt = cfg.backtest
    proxies = list(
        time_proxy_features(
            tx,
            S.ALL_FEATURES,
            bt.time_proxy_max_step if bt else 34,
            bt.time_proxy_threshold if bt else 0.9,
        )
    )
    cov = covariate_shift(tx, time_proxies=proxies)
    cov.to_csv(out / "covariate_shift_by_step.csv", index=False)
    cc, top = class_conditional_shift(tx, time_proxies=proxies)
    cc.to_csv(out / "class_conditional_shift.csv", index=False)
    static, static_meta = static_model_diagnostics(cfg.paths.artifacts_dir, budgets)
    static.to_csv(out / "static_model_by_step.csv", index=False)
    learn = within_period_learnability(tx, xgb_params)
    learn.to_csv(out / "within_period_learnability.csv", index=False)

    plot_overview(prior, cov, static, out / "figures" / "shift_overview.png")
    plot_scores_and_calibration(static, out / "figures" / "scores_and_calibration.png")
    plot_learnability(learn, out / "figures" / "within_period_learnability.png")

    def period_mean(df: pd.DataFrame, col: str) -> dict[str, float]:
        return {p: float(df[df[S.TIME_STEP].between(*r)][col].mean()) for p, r in PERIODS.items()}

    summary = {
        "static_model_run": static_meta["run"],
        "prevalence_by_period": {
            p: float(
                prior.loc[r[0] : r[1], "n_illicit"].sum()
                / prior.loc[r[0] : r[1], "n_labeled"].sum()
            )
            for p, r in PERIODS.items()
        },
        "missing_rate_labeled_by_period": period_mean(prior.reset_index(), "missing_rate_labeled"),
        "time_proxy_features": proxies,
        "domain_auc_mean_by_period": period_mean(cov, "domain_auc"),
        "domain_auc_no_time_proxy_mean_by_period": period_mean(cov, "domain_auc_no_time_proxy"),
        "median_ks_by_period": {g: period_mean(cov, f"median_ks_{g}") for g in GROUPS},
        "class_conditional_domain_auc": cc.to_dict(orient="records"),
        "top_features_illicit_train_vs_43_49": top,
        "within_period_learnability": learn.to_dict(orient="records"),
        "static_calibration_by_period": {
            p: {
                c: float(static[static[S.TIME_STEP].between(*r)][c].mean())
                for c in ("cal_mean_predicted", "cal_observed_rate", "cal_brier", "cal_ece")
            }
            for p, r in list(PERIODS.items())[1:]
        },
    }
    write_json(out / "summary.json", summary)
    log.info("shift diagnostics written to %s", out)
    return json.loads(json.dumps(summary, default=float))
