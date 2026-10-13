"""Generated Phase 2 backtest report (tables, bootstrap CIs, verdicts, figures)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from chainwatch.backtest.analysis import bootstrap_comparison, cost_table, per_step_table, summarize
from chainwatch.backtest.pipeline import find_stage_runs, load_stage
from chainwatch.config import Config
from chainwatch.data import schema as S
from chainwatch.utils import get_logger, read_json, write_json

log = get_logger(__name__)

INK, INK_MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # validated slots 1-5
STATIC_COLOR = "#52514e"
BASELINE = "static"


def _style(ax: plt.Axes, title: str, ylabel: str, xlabel: str = "time step") -> None:
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=9)
    ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=9)
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=8)


def _fmt_ci(est: float, lo: float, hi: float, digits: int = 3) -> str:
    return f"{est:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"


def _boot(preds, steps, cfg, block=None) -> pd.DataFrame:
    bt = cfg.backtest
    sub = preds[preds[S.TIME_STEP].isin(steps)]
    return bootstrap_comparison(
        sub,
        BASELINE,
        steps,
        bt.primary_budget,
        block_length=bt.bootstrap.block_length if block is None else block,
        n_resamples=bt.bootstrap.n_resamples,
        seed=bt.bootstrap.seed,
    )


def stage_tables(
    preds: pd.DataFrame, fits: pd.DataFrame, steps: list[int], cfg: Config, selected: dict[str, str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    bt = cfg.backtest
    k = bt.primary_budget
    _, agg = summarize(preds, bt.budgets, bt.reference_prevalence, steps)
    boot = _boot(preds, steps, cfg)
    costs = cost_table(fits)
    sel = set(selected.values()) | {BASELINE}
    rows = []
    for _, r in agg.iterrows():
        st = r["strategy"]
        b_ap = boot[(boot.strategy == st) & (boot.metric == "pooled_ap")].iloc[0]
        b_h = boot[(boot.strategy == st) & (boot.metric == f"hits@{k}")].iloc[0]
        c = costs[costs.strategy == st].iloc[0]
        rows.append(
            {
                "strategy": st,
                "selected": "yes" if st in sel else "",
                "pooled AP (seed mean±sd)": f"{r['pooled_ap_mean']:.3f} ± {r['pooled_ap_std']:.3f}",
                "pooled AP 95% CI": f"[{b_ap.ci_low:.3f}, {b_ap.ci_high:.3f}]",
                "ΔAP vs static [95% CI]": _fmt_ci(
                    b_ap.diff_vs_baseline, b_ap.diff_ci_low, b_ap.diff_ci_high
                ),
                f"hits@{k}/step (sum)": (
                    f"{r[f'hits@{k}_mean']:.1f} / {r[f'max_hits@{k}_mean']:.0f}"
                ),
                f"Δhits@{k} [95% CI]": _fmt_ci(
                    b_h.diff_vs_baseline, b_h.diff_ci_low, b_h.diff_ci_high, 1
                ),
                "ROC-AUC": f"{r['roc_auc_mean']:.3f}",
                "std. AP": f"{r['std_ap_mean']:.3f}",
                "mean step AP": f"{r['mean_step_ap_mean']:.3f}",
                f"hits_all@{k}": f"{r[f'hits_all@{k}_mean']:.1f}",
                "fits": int(c.n_fits),
                "fit s": f"{c.fit_seconds:.0f}",
            }
        )
    table = pd.DataFrame(rows)
    order = {BASELINE: 0}
    table["_o"] = table["strategy"].map(lambda s: order.get(s, 1))
    return table.sort_values(["_o", "strategy"]).drop(columns="_o"), boot


def budget_table(
    preds: pd.DataFrame, steps: list[int], cfg: Config, strategies: list[str]
) -> pd.DataFrame:
    bt = cfg.backtest
    ps = per_step_table(
        preds[preds["strategy"].isin(strategies) & preds[S.TIME_STEP].isin(steps)], bt.budgets
    )
    rows = []
    for st, g in ps.groupby("strategy"):
        by_seed = g.groupby("seed")
        row: dict[str, Any] = {"strategy": st}
        for k in bt.budgets:
            hits = by_seed[f"hits@{k}"].sum()
            row[f"K={k}: found"] = f"{hits.mean():.1f}"
            row[f"K={k}: P@K"] = (
                f"{(hits / by_seed['n'].apply(lambda n, k=k: np.minimum(n, k).sum())).mean():.3f}"
            )
            row[f"K={k}: found (all-tx LB)"] = f"{by_seed[f'hits_all@{k}'].sum().mean():.1f}"
        rows.append(row)
    maxrow = {"strategy": "max possible"}
    one = ps[(ps.strategy == strategies[0]) & (ps.seed == ps.seed.min())]
    for k in cfg.backtest.budgets:
        maxrow[f"K={k}: found"] = f"{np.minimum(one['n_illicit'], k).sum():.0f}"
    rows.append(maxrow)
    return pd.DataFrame(rows).fillna("")


def plot_per_step(
    ps: pd.DataFrame, strategies: list[str], metric: str, title: str, path: Path, ylabel: str
) -> None:
    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    for i, st in enumerate(strategies):
        g = ps[ps.strategy == st].groupby(S.TIME_STEP)[metric]
        mean, lo, hi = g.mean(), g.min(), g.max()
        color = STATIC_COLOR if st == BASELINE else SERIES[(i - 1) % len(SERIES)]
        ls = "--" if st == BASELINE else "-"
        ax.fill_between(mean.index, lo, hi, color=color, alpha=0.15, lw=0)
        ax.plot(mean.index, mean, color=color, lw=2, ls=ls, marker="o", ms=3.5, label=st)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED, ncols=3, loc="upper right")
    _style(ax, title, ylabel, xlabel="")
    counts = ps[(ps.strategy == strategies[0]) & (ps.seed == ps.seed.min())]
    for _, r in counts.iterrows():
        ax.text(
            r[S.TIME_STEP],
            -0.13,
            str(int(r["n_illicit"])),
            ha="center",
            fontsize=7,
            color=INK_MUTED,
            transform=ax.get_xaxis_transform(),
        )
    ax.text(
        0, -0.21, "illicit labelled per step", fontsize=7, color=INK_MUTED, transform=ax.transAxes
    )
    ax.set_xticks(sorted(counts[S.TIME_STEP]))
    if metric == "ap":
        ax.set_ylim(0, 1.05)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.24)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_cumulative_hits(
    ps: pd.DataFrame, strategies: list[str], k: int, title: str, path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    base = ps[(ps.strategy == strategies[0]) & (ps.seed == ps.seed.min())].sort_values(S.TIME_STEP)
    ax.plot(
        base[S.TIME_STEP],
        np.minimum(base["n_illicit"], k).cumsum(),
        color=GRID,
        lw=6,
        label="maximum possible",
        solid_capstyle="round",
    )
    for i, st in enumerate(strategies):
        g = ps[ps.strategy == st].sort_values(S.TIME_STEP)
        cum = g.groupby("seed")[f"hits@{k}"].cumsum()
        mean = cum.groupby(g[S.TIME_STEP]).mean()
        color = STATIC_COLOR if st == BASELINE else SERIES[(i - 1) % len(SERIES)]
        ax.plot(mean.index, mean, color=color, lw=2, ls="--" if st == BASELINE else "-", label=st)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED, loc="upper left")
    _style(ax, title, "cumulative illicit found")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_forest(boots: dict[str, pd.DataFrame], metric: str, path: Path, xlabel: str) -> None:
    fig, axes = plt.subplots(
        1, len(boots), figsize=(5 * len(boots), 4.6), facecolor=SURFACE, sharey=True
    )
    axes = np.atleast_1d(axes)
    for ax, (name, b) in zip(axes, boots.items(), strict=True):
        d = b[(b.metric == metric) & (b.strategy != BASELINE)].sort_values("strategy")
        y = np.arange(len(d))
        ax.axvline(0, color=INK_MUTED, lw=1)
        ax.hlines(y, d.diff_ci_low, d.diff_ci_high, color=SERIES[0], lw=2)
        ax.plot(d.diff_vs_baseline, y, "o", color=SERIES[0], ms=6)
        ax.set_yticks(y, d.strategy)
        _style(ax, name, "", xlabel)
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_cost_benefit(tables: dict[str, tuple[pd.DataFrame, pd.DataFrame]], path: Path) -> None:
    """Pooled AP (seed mean) vs number of fits per strategy, one panel per stage."""
    fig, axes = plt.subplots(1, len(tables), figsize=(5.5 * len(tables), 4), facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, (name, (summary, costs)) in zip(axes, tables.items(), strict=True):
        df = summary.merge(costs, on="strategy")
        families = sorted({_family(s) for s in df.strategy})
        for i, fam in enumerate(families):
            d = df[df.strategy.map(_family) == fam].sort_values("n_fits")
            color = STATIC_COLOR if fam == BASELINE else SERIES[i % len(SERIES)]
            single_marker = "s" if fam == "expanding" else "o"
            ax.plot(
                d.n_fits,
                d.pooled_ap_mean,
                f"{single_marker}-" if len(d) > 1 else single_marker,
                color=color,
                lw=1.5,
                ms=9 if fam == "expanding" else 6,
                label=fam,
                zorder=3 if fam == "expanding" else 2,
                mfc="none" if fam == "expanding" else color,
                mew=2,
            )
        ax.set_xscale("log", base=2)
        ax.legend(frameon=False, fontsize=8, labelcolor=INK_MUTED, loc="lower right")
        _style(ax, name, "pooled AP (seed mean)", "model fits per backtest (log2)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _family(strategy: str) -> str:
    return strategy.split("_")[0]


def ablation_section(cfg: Config, out: Path) -> list[str]:
    """Exploratory: same strategies with vs without clock-like features (paired bootstrap)."""
    bt = cfg.backtest
    lines: list[str] = []
    rows = []
    for stage in ("dev", "confirm"):
        base_runs = find_stage_runs(cfg.paths.backtests_dir, stage, bt.label_latency)
        abl_runs = find_stage_runs(cfg.paths.backtests_dir, f"{stage}-notp", bt.label_latency)
        if not base_runs or not abl_runs:
            continue
        bp, _, bm = load_stage(base_runs[-1])
        ap_, _, am = load_stage(abl_runs[-1])
        steps = bm["scored_steps"]
        periods = {"all": steps}
        if stage == "confirm":
            cut = bt.confirm_split_after
            periods |= {
                f"{steps[0]}-{cut}": [t for t in steps if t <= cut],
                f"{cut + 1}-{steps[-1]}": [t for t in steps if t > cut],
            }
        for st in sorted(set(ap_["strategy"].astype(str))):
            a = bp[bp.strategy.astype(str) == st].assign(strategy="with proxies")
            b = ap_[ap_.strategy.astype(str) == st].assign(strategy="without proxies")
            both = pd.concat([a, b[b.seed.isin(a.seed.unique())]], ignore_index=True)
            for pname, psteps in periods.items():
                sub = both[both[S.TIME_STEP].isin(psteps)]
                boot = bootstrap_comparison(
                    sub,
                    "with proxies",
                    psteps,
                    bt.primary_budget,
                    min(bt.bootstrap.block_length, len(psteps)),
                    bt.bootstrap.n_resamples,
                    bt.bootstrap.seed,
                )
                r = boot[(boot.strategy == "without proxies") & (boot.metric == "pooled_ap")].iloc[
                    0
                ]
                base = boot[(boot.strategy == "with proxies") & (boot.metric == "pooled_ap")].iloc[
                    0
                ]
                rows.append(
                    {
                        "stage": stage,
                        "steps": pname,
                        "strategy": st,
                        "AP with proxies": round(base.estimate, 3),
                        "AP without proxies": round(r.estimate, 3),
                        "Δ (without - with) [95% CI]": _fmt_ci(
                            r.diff_vs_baseline, r.diff_ci_low, r.diff_ci_high
                        ),
                    }
                )
        lines += [f"Dropped in `{abl_runs[-1].name}`: `{list(am['dropped_time_proxies'])}`", ""]
    if not rows:
        return []
    table = pd.DataFrame(rows)
    table.to_csv(out / "time_proxy_ablation.csv", index=False)
    return [
        "## Exploratory: time-proxy ablation (deviation 1, not pre-registered)",
        "",
        "Same strategies and seeds, with the clock-like features removed. Paired circular "
        "block bootstrap over steps of the difference in pooled AP.",
        "",
        *lines,
        table.to_markdown(index=False),
        "",
    ]


def verdicts(boots: dict[str, pd.DataFrame], selected: dict[str, str], k: int) -> dict[str, Any]:
    """Protocol §5: robust iff CI excludes 0 with the same sign in dev and confirm, and the
    point estimate has that sign in both confirm sub-periods."""
    out = {}
    for family, st in selected.items():
        if st == BASELINE:
            continue
        fam = {}
        for metric in ("pooled_ap", f"hits@{k}"):

            def row(name, metric=metric, st=st):
                b = boots[name]
                return b[(b.strategy == st) & (b.metric == metric)].iloc[0]

            dev, conf = row("dev"), row("confirm")
            a, b_ = row("confirm early"), row("confirm late")
            sign = np.sign(dev.diff_vs_baseline)
            excl = lambda r: r.diff_ci_low > 0 or r.diff_ci_high < 0  # noqa: E731
            robust = bool(
                excl(dev)
                and excl(conf)
                and np.sign(conf.diff_vs_baseline) == sign
                and np.sign(a.diff_vs_baseline) == sign
                and np.sign(b_.diff_vs_baseline) == sign
            )
            fam[metric] = {
                "dev_diff": dev.diff_vs_baseline,
                "dev_ci": [dev.diff_ci_low, dev.diff_ci_high],
                "confirm_diff": conf.diff_vs_baseline,
                "confirm_ci": [conf.diff_ci_low, conf.diff_ci_high],
                "confirm_early_diff": a.diff_vs_baseline,
                "confirm_late_diff": b_.diff_vs_baseline,
                "robust": robust,
                "direction": "improvement" if sign > 0 else "degradation" if sign < 0 else "none",
            }
        out[f"{family} ({st})"] = fam
    return out


def write_backtest_report(cfg: Config) -> Path:
    bt = cfg.backtest
    k = bt.primary_budget
    out = cfg.paths.reports_dir / "phase2" / "backtest"
    (out / "figures").mkdir(parents=True, exist_ok=True)
    L = bt.label_latency

    dev_runs = find_stage_runs(cfg.paths.backtests_dir, "dev", L)
    conf_runs = find_stage_runs(cfg.paths.backtests_dir, "confirm", L)
    if not dev_runs or not conf_runs:
        raise FileNotFoundError("need dev and confirm backtests at the primary latency")
    dev_dir, conf_dir = dev_runs[-1], conf_runs[-1]
    selected = read_json(dev_dir / "selection.json")["selected"]
    dev_p, dev_f, dev_m = load_stage(dev_dir)
    conf_p, conf_f, conf_m = load_stage(conf_dir)
    if conf_m["selection_source"] != dev_dir.name:
        log.warning(
            "confirm run used selection %s, latest dev run is %s",
            conf_m["selection_source"],
            dev_dir.name,
        )
    shown = [BASELINE, *[selected[f] for f in sorted(selected) if f != BASELINE]]

    dev_steps, conf_steps = dev_m["scored_steps"], conf_m["scored_steps"]
    cut = bt.confirm_split_after
    early, late = [s for s in conf_steps if s <= cut], [s for s in conf_steps if s > cut]
    if not early or not late:
        raise ValueError(f"confirm_split_after={cut} leaves an empty sub-period of {conf_steps}")
    _p_early, _p_late = f"{early[0]}-{early[-1]}", f"{late[0]}-{late[-1]}"
    dev_table, dev_boot = stage_tables(dev_p, dev_f, dev_steps, cfg, selected)
    conf_table, conf_boot = stage_tables(conf_p, conf_f, conf_steps, cfg, selected)
    early_table, early_boot = stage_tables(conf_p, conf_f, early, cfg, selected)
    late_table, late_boot = stage_tables(conf_p, conf_f, late, cfg, selected)
    block1 = _boot(conf_p, conf_steps, cfg, block=1)
    boots = {
        "dev": dev_boot,
        "confirm": conf_boot,
        "confirm early": early_boot,
        "confirm late": late_boot,
    }
    for name, b in {**boots, "confirm block1": block1}.items():
        b.to_csv(out / f"bootstrap_{name.replace(' ', '_')}.csv", index=False)
    v = verdicts(boots, selected, k)
    write_json(out / "verdicts.json", v)

    dev_ps = per_step_table(dev_p, bt.budgets)
    conf_ps = per_step_table(conf_p, bt.budgets)
    dev_ps.to_csv(out / "per_step_dev.csv", index=False)
    conf_ps.to_csv(out / "per_step_confirm.csv", index=False)
    costs = pd.concat(
        [cost_table(dev_f).assign(stage="dev"), cost_table(conf_f).assign(stage="confirm")]
    )
    costs.to_csv(out / "costs.csv", index=False)

    plot_per_step(
        conf_ps,
        shown,
        "ap",
        "Confirm backtest (EXPLORATORY, steps 35-49): AP per step",
        out / "figures" / "per_step_ap_confirm.png",
        "average precision",
    )
    plot_per_step(
        dev_ps,
        shown,
        "ap",
        "Development backtest (steps 20-34): AP per step",
        out / "figures" / "per_step_ap_dev.png",
        "average precision",
    )
    plot_per_step(
        conf_ps,
        shown,
        "roc_auc",
        "Confirm backtest: ROC-AUC per step",
        out / "figures" / "per_step_auc_confirm.png",
        "ROC-AUC",
    )
    plot_cumulative_hits(
        conf_ps,
        shown,
        k,
        f"Confirm: cumulative illicit found, budget {k}/step",
        out / "figures" / "cumulative_hits_confirm.png",
    )
    plot_cumulative_hits(
        dev_ps,
        shown,
        k,
        f"Dev: cumulative illicit found, budget {k}/step",
        out / "figures" / "cumulative_hits_dev.png",
    )
    plot_forest(
        {"dev (20-34)": dev_boot, "confirm (35-49)": conf_boot},
        "pooled_ap",
        out / "figures" / "forest_delta_ap.png",
        "Δ pooled AP vs static (95% CI)",
    )
    plot_forest(
        {"dev (20-34)": dev_boot, "confirm (35-49)": conf_boot},
        f"hits@{k}",
        out / "figures" / "forest_delta_hits.png",
        f"Δ illicit found @{k}/step vs static",
    )

    plot_cost_benefit(
        {
            "dev (20-34)": (
                summarize(dev_p, bt.budgets, bt.reference_prevalence, dev_steps)[1],
                cost_table(dev_f),
            ),
            "confirm (35-49, exploratory)": (
                summarize(conf_p, bt.budgets, bt.reference_prevalence, conf_steps)[1],
                cost_table(conf_f),
            ),
        },
        out / "figures" / "cost_benefit.png",
    )
    abl_md = ablation_section(cfg, out)

    sens_md: list[str] = []
    for lat in bt.latency_sensitivity:
        runs = find_stage_runs(cfg.paths.backtests_dir, "confirm", lat)
        if not runs:
            continue
        sp, sf, sm = load_stage(runs[-1])
        st_table, _ = stage_tables(sp, sf, sm["scored_steps"], cfg, selected)
        st_table.to_csv(out / f"confirm_L{lat}_table.csv", index=False)
        sens_md += [
            f"### Label latency L = {lat} (confirm, selection frozen at L = {L})",
            "",
            st_table.to_markdown(index=False),
            "",
        ]

    def md(df):
        return df.to_markdown(index=False)

    lines = [
        "# Phase 2 backtest results (generated)",
        "",
        f"Generated by `chainwatch backtest-report` from `{dev_dir.name}` (dev) and "
        f"`{conf_dir.name}` (confirm). Primary label latency L = {L}. Protocol: "
        "docs/PHASE2_PROTOCOL.md.",
        "",
        "> **The confirm period (35-49) was inspected in Phase 1. Confirm results are "
        "exploratory, not an untouched holdout.**",
        "",
        f"Selected on dev (frozen before confirm ran): `{selected}`",
        "",
        "CIs: paired circular block bootstrap over time steps "
        f"(block {bt.bootstrap.block_length}, {bt.bootstrap.n_resamples} resamples); metrics "
        "averaged over training seeds inside each replicate. `hits@K` = expected illicit "
        f"transactions found reviewing the top {k} labelled transactions per step "
        "(tie-aware), summed over steps; `hits_all` ranks all transactions and counts "
        "unknown ones as misses (a lower bound). `std. AP` = AP standardised to prevalence "
        f"{bt.reference_prevalence}.",
        "",
        f"## Development backtest (steps {dev_steps[0]}-{dev_steps[-1]}, seeds {dev_m['seeds']})",
        "",
        md(dev_table),
        "",
        f"## Confirm backtest (steps {conf_steps[0]}-{conf_steps[-1]}, seeds {conf_m['seeds']}, "
        "EXPLORATORY)",
        "",
        md(conf_table),
        "",
        "### Confirm sub-period 35-42",
        "",
        md(early_table),
        "",
        "### Confirm sub-period 43-49",
        "",
        md(late_table),
        "",
        "## Investigation budgets (confirm, static vs selected; seed means)",
        "",
        md(budget_table(conf_p, conf_steps, cfg, shown)),
        "",
        "## Robustness verdicts (protocol §5)",
        "",
        "```json",
        pd.Series(v).to_json(indent=2),
        "```",
        "",
        "## Sensitivity",
        "",
        "Block length 1 (step-cluster bootstrap), confirm, Δ pooled AP vs static:",
        "",
        md(
            block1[block1.metric == "pooled_ap"][
                ["strategy", "diff_vs_baseline", "diff_ci_low", "diff_ci_high"]
            ].round(3)
        ),
        "",
        *sens_md,
        *abl_md,
        "## Figures",
        "",
        *[
            f"![{n}](figures/{n}.png)\n"
            for n in (
                "per_step_ap_confirm",
                "per_step_auc_confirm",
                "cumulative_hits_confirm",
                "per_step_ap_dev",
                "cumulative_hits_dev",
                "forest_delta_ap",
                "forest_delta_hits",
                "cost_benefit",
            )
        ],
    ]
    path = out / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Backtest report written to %s", path)
    return path
