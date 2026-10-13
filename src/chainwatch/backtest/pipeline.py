"""Run backtest stages and persist self-describing artifacts.

    artifacts/backtests/<stage>_L<latency>_<timestamp>/
        predictions.parquet   every scored transaction (incl. unknown) per strategy & seed
        fits.csv              one row per (strategy, seed, deployment): window, size, cost
        summary.csv           pooled metrics per strategy & seed
        metadata.json         config snapshot, stage, latency, seeds, specs, data hashes, env
        selection.json        (dev only) the frozen per-family choice

The confirm stage refuses to run unless a dev selection for the same label latency
exists, so strategy parameters cannot be chosen after looking at confirm results.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from chainwatch.backtest.analysis import cost_table, select_per_family, summarize
from chainwatch.backtest.runner import run_backtest
from chainwatch.backtest.schedule import expand_strategies
from chainwatch.config import REPO_ROOT, Config
from chainwatch.data import schema as S
from chainwatch.data.loader import load_processed
from chainwatch.features.preprocessing import fill_degrees_from_edges, time_proxy_features
from chainwatch.training.pipeline import _safe_manifest
from chainwatch.training.tracking import log_run
from chainwatch.utils import environment_info, get_logger, git_commit, read_json, write_json

log = get_logger(__name__)

STAGES = ("dev", "confirm")


def find_stage_runs(backtests_dir: Path, stage: str, latency: int) -> list[Path]:
    if not backtests_dir.exists():
        return []
    prefix = f"{stage}_L{latency}_"
    return sorted(
        p
        for p in backtests_dir.iterdir()
        if p.name.startswith(prefix) and (p / "metadata.json").exists()
    )


def load_selection(backtests_dir: Path, latency: int) -> tuple[dict[str, str], Path]:
    runs = [
        r for r in find_stage_runs(backtests_dir, "dev", latency) if (r / "selection.json").exists()
    ]
    if not runs:
        raise FileNotFoundError(
            f"No dev selection for label latency {latency}. Run "
            f"`chainwatch backtest --stage dev --latency {latency}` first: strategy parameters "
            "must be frozen on the development backtest before the confirm stage runs."
        )
    return read_json(runs[-1] / "selection.json")["selected"], runs[-1]


def stage_label(stage: str, drop_time_proxies: bool) -> str:
    return f"{stage}-notp" if drop_time_proxies else stage


def run_stage(
    cfg: Config,
    stage: str,
    latency: int | None = None,
    selection_latency: int | None = None,
    drop_time_proxies: bool = False,
    only: list[str] | None = None,
) -> Path:
    """Run one backtest stage.

    ``drop_time_proxies`` removes clock-like features (exploratory ablation, see
    docs/PHASE2_PROTOCOL.md "Deviations"); ``only`` restricts the strategies, and
    ``"selected"`` expands to static plus the frozen dev selection.
    """
    bt = cfg.backtest
    if bt is None:
        raise ValueError("config has no `backtest` section")
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    latency = bt.label_latency if latency is None else latency
    st = bt.stages[stage]
    specs = expand_strategies(bt.strategies)
    families = {s.name: s.family for s in specs}
    if only:
        wanted = set(only)
        if "selected" in wanted:
            sel_now, _ = load_selection(cfg.paths.backtests_dir, bt.label_latency)
            wanted = (wanted - {"selected"}) | set(sel_now.values()) | {"static"}
        unknown = wanted - set(families)
        if unknown:
            raise ValueError(f"unknown strategies {sorted(unknown)}")
        specs = [s for s in specs if s.name in wanted]

    selection, selection_source = None, None
    if stage == "confirm":
        sel_latency = bt.label_latency if selection_latency is None else selection_latency
        selection, selection_source = load_selection(cfg.paths.backtests_dir, sel_latency)
        log.info("Using frozen dev selection from %s: %s", selection_source.name, selection)

    ds = load_processed(cfg.paths.processed_dir)
    tx = fill_degrees_from_edges(ds.transactions, ds.edges)
    tx = tx[tx[S.TIME_STEP] <= st.end]  # nothing after the stage horizon is ever loaded
    feature_cols = S.feature_columns(bt.feature_set)
    proxies: dict[str, float] = {}
    if drop_time_proxies:
        proxies = time_proxy_features(
            tx, feature_cols, bt.time_proxy_max_step, bt.time_proxy_threshold
        )
        feature_cols = [c for c in feature_cols if c not in proxies]
        log.info("Dropping %d time-proxy features: %s", len(proxies), list(proxies))

    log.info(
        "Backtest %s: origin=%d scored=%d..%d latency=%d seeds=%s strategies=%d",
        stage,
        st.origin,
        st.origin + 1,
        st.end,
        latency,
        st.seeds,
        len(specs),
    )
    result = run_backtest(
        tx,
        specs,
        st.origin,
        st.end,
        latency,
        st.seeds,
        feature_cols,
        bt.xgboost,
        log1p_augmented=cfg.preprocessing.log1p_augmented,
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (
        cfg.paths.backtests_dir / f"{stage_label(stage, drop_time_proxies)}_L{latency}_{stamp}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result.predictions.to_parquet(run_dir / "predictions.parquet", index=False)
    result.fits.to_csv(run_dir / "fits.csv", index=False)
    per_seed, agg = summarize(
        result.predictions, bt.budgets, bt.reference_prevalence, st.scored_steps
    )
    per_seed.to_csv(run_dir / "summary.csv", index=False)

    meta: dict[str, Any] = {
        "stage": stage,
        "label_latency": latency,
        "origin": st.origin,
        "end": st.end,
        "scored_steps": st.scored_steps,
        "seeds": st.seeds,
        "strategies": {
            s.name: {"family": s.family, "kind": s.kind, "params": dict(s.params)} for s in specs
        },
        "exploratory": stage == "confirm" or drop_time_proxies,
        "feature_columns": feature_cols,
        "dropped_time_proxies": proxies,
        "selection_source": selection_source.name if selection_source else None,
        "selection_used": selection,
        "config": cfg.to_dict(),
        "data_manifest": _safe_manifest(cfg.paths.processed_dir),
        "git_commit": git_commit(REPO_ROOT),
        "environment": environment_info(),
    }
    write_json(run_dir / "metadata.json", meta)

    if stage == "dev" and not drop_time_proxies and not only:
        selected = select_per_family(agg, result.fits, families, bt.selection_tolerance)
        write_json(
            run_dir / "selection.json",
            {
                "rule": "max seed-mean pooled AP on dev; near-ties (<= tolerance) -> fewer fits, "
                "then larger window / longer half-life / lower frequency",
                "tolerance": bt.selection_tolerance,
                "selected": selected,
                "dev_pooled_ap_mean": agg.set_index("strategy")["pooled_ap_mean"].to_dict(),
            },
        )
        log.info("Dev selection: %s", selected)

    costs = cost_table(result.fits).set_index("strategy")
    for _, row in agg.iterrows():
        name = row["strategy"]
        log_run(
            cfg.tracking,
            run_name=f"{run_dir.name}/{name}",
            params={
                "stage": stage,
                "latency": latency,
                "strategy": name,
                "family": families[name],
                "seeds": st.seeds,
            },
            metrics={k: v for k, v in row.items() if k != "strategy"}
            | {
                "n_fits": float(costs.loc[name, "n_fits"]),
                "fit_seconds": float(costs.loc[name, "fit_seconds"]),
            },
            run_dir=run_dir,
            tags={"phase": "2", "stage": stage},
            log_artifacts=False,
        )
    log.info("Backtest written to %s", run_dir)
    show = agg[["strategy", "pooled_ap_mean", "pooled_ap_std", f"hits@{bt.primary_budget}_mean"]]
    log.info("\n%s", show.sort_values("pooled_ap_mean", ascending=False).to_string(index=False))
    return run_dir


def load_stage(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    return (
        pd.read_parquet(run_dir / "predictions.parquet"),
        pd.read_csv(run_dir / "fits.csv"),
        read_json(run_dir / "metadata.json"),
    )
