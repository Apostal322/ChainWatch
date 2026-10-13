"""Command-line entry point: ``chainwatch <command>`` or ``python -m chainwatch <command>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chainwatch.config import load_config
from chainwatch.models.baselines import MODEL_NAMES
from chainwatch.utils import get_logger, setup_logging, write_json

log = get_logger("chainwatch")


def _cmd_download(args, cfg) -> None:
    from chainwatch.data.download import download_dataset

    download_dataset(cfg.paths.raw_dir, force=args.force)


def _cmd_prepare(args, cfg) -> None:
    from chainwatch.data.loader import prepare

    report = prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    log.info(
        "Validation: %s", {k: v for k, v in report.to_dict().items() if k != "missing_by_column"}
    )


def _cmd_eda(args, cfg) -> None:
    from chainwatch.analysis.eda import run_eda

    run_eda(cfg)


def _cmd_train(args, cfg) -> None:
    from chainwatch.training.pipeline import evaluate, train

    models = MODEL_NAMES if args.model == "all" else (args.model,)
    for name in models:
        run_dir = train(cfg, name)
        print(f"run_dir: {run_dir}")
        if args.evaluate:
            evaluate(cfg, run_dir)


def _cmd_evaluate(args, cfg) -> None:
    from chainwatch.training.pipeline import evaluate

    evaluate(cfg, Path(args.run_dir).resolve(), force=args.force)


def _cmd_report(args, cfg) -> None:
    from chainwatch.models.artifacts import list_runs
    from chainwatch.training.pipeline import runs_table

    table = runs_table(list_runs(cfg.paths.artifacts_dir))
    if table.empty:
        log.warning("No evaluated runs found in %s", cfg.paths.artifacts_dir)
        return
    if args.latest:
        table = table.sort_values("run").groupby(["model", "features"], as_index=False).tail(1)
    table = table.sort_values(["features", "model"]).reset_index(drop=True)
    out = cfg.paths.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "baseline_results.csv", index=False)
    write_json(out / "baseline_results.json", table.to_dict(orient="records"))
    from chainwatch.evaluation.report import write_report

    md_path = write_report(table, cfg.paths.artifacts_dir, out)
    print(table.drop(columns=["run"]).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"report: {md_path}")


def _cmd_audit(args, cfg) -> None:
    from chainwatch.analysis.audit import audit_phase1

    report = audit_phase1(cfg)
    for name, r in report["runs"].items():
        ok = (
            r["test_steps_ok"]
            and r["val_steps_ok"]
            and r["no_unknown_labels"]
            and r["n_test_matches_data"]
            and r["threshold_is_val_max_f1"]
            and r["max_abs_rescore_diff"] < 1e-6
            and r["max_abs_metric_diff"] < 1e-9
        )
        print(
            f"{'OK  ' if ok else 'FAIL'} {name}: AP 35-42={r['ap_35_42']:.4f} "
            f"43-49={r['ap_43_49']:.4f}"
        )


def _cmd_shift(args, cfg) -> None:
    from chainwatch.analysis.shift import run_shift_diagnostics

    run_shift_diagnostics(cfg)


def _cmd_backtest(args, cfg) -> None:
    from chainwatch.backtest.pipeline import run_stage

    only = [x.strip() for x in args.strategies.split(",")] if args.strategies else None
    run_dir = run_stage(
        cfg, args.stage, latency=args.latency, drop_time_proxies=args.drop_time_proxies, only=only
    )
    print(f"run_dir: {run_dir}")


def _cmd_backtest_report(args, cfg) -> None:
    from chainwatch.backtest.report import write_backtest_report

    print(f"report: {write_backtest_report(cfg)}")


def _cmd_demo_data(args, cfg) -> None:
    from chainwatch.data.synthetic import write_synthetic_raw

    out = write_synthetic_raw(cfg.paths.raw_dir, n_steps=args.steps, per_step=args.per_step)
    print(f"synthetic raw data: {out}")


def _cmd_export_snapshots(args, cfg) -> None:
    from chainwatch.serving.export import export_registry

    print(f"registry: {export_registry(cfg, overwrite=args.overwrite)}")


def _cmd_demo_setup(args, cfg) -> None:
    """Synthetic data -> prepare -> Phase 1 reference model -> snapshot registry."""
    from chainwatch.data.loader import prepare
    from chainwatch.data.synthetic import SYNTHETIC_MARKER, write_synthetic_raw
    from chainwatch.serving.export import export_registry
    from chainwatch.training.pipeline import evaluate, train

    if "demo" not in str(cfg.paths.raw_dir):
        raise SystemExit("demo-setup only writes to demo paths; use --config configs/demo.yaml")
    if not (cfg.paths.raw_dir / SYNTHETIC_MARKER).exists():
        write_synthetic_raw(cfg.paths.raw_dir)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    run_dir = train(cfg, "xgboost")
    evaluate(cfg, run_dir)
    print(f"registry: {export_registry(cfg, overwrite=True)}")


def _cmd_serve(args, cfg) -> None:
    import os

    import uvicorn

    from chainwatch.serving.app import configure_json_logging

    if args.config:
        os.environ["CHAINWATCH_CONFIG"] = str(args.config.resolve())
    from chainwatch.config import load_config

    configure_json_logging()
    served = load_config(os.environ.get("CHAINWATCH_CONFIG"))
    uvicorn.run(
        "chainwatch.serving.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=1,  # state (data, models, caches) lives in process memory
        log_config=None,
        access_log=False,
        limit_concurrency=served.serving.limit_concurrency if served.serving else 64,
        timeout_keep_alive=5,
    )


def _cmd_benchmark(args, cfg) -> None:
    from chainwatch.serving.benchmark import run_benchmark

    run_benchmark(cfg, args.out)


def _cmd_verify_replay(args, cfg) -> None:
    from chainwatch.serving.verify import verify_replay

    r = verify_replay(cfg)
    print(
        f"checked {r['n_predictions_checked']} predictions over {r['n_steps_checked']} "
        f"step/policy pairs; max |diff| = {r['max_abs_diff']}; ok = {r['all_within_tolerance']}"
    )
    if not r["all_within_tolerance"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chainwatch", description=__doc__)
    p.add_argument(
        "--config", type=Path, default=None, help="YAML config (default: configs/default.yaml)"
    )
    p.add_argument("--feature-set", choices=["local", "local_aggregate", "all"], default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument(
        "--mlflow", action="store_true", help="enable MLflow logging for this invocation"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("download", help="download raw Elliptic++ transaction files (verified)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=_cmd_download)

    s = sub.add_parser("prepare", help="validate raw files and build the Parquet cache")
    s.set_defaults(func=_cmd_prepare)

    s = sub.add_parser("eda", help="write summary statistics and figures to reports/eda")
    s.set_defaults(func=_cmd_eda)

    s = sub.add_parser("train", help="fit, select and threshold on train/val only")
    s.add_argument("--model", choices=[*MODEL_NAMES, "all"], required=True)
    s.add_argument(
        "--evaluate", action="store_true", help="run the one-time test evaluation afterwards"
    )
    s.set_defaults(func=_cmd_train)

    s = sub.add_parser("evaluate", help="one-time test-set evaluation of a trained run")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--force", action="store_true", help="overwrite an existing test evaluation")
    s.set_defaults(func=_cmd_evaluate)

    s = sub.add_parser("report", help="tabulate test metrics of all evaluated runs")
    s.add_argument(
        "--latest", action="store_true", help="keep only the newest run per model/feature set"
    )
    s.set_defaults(func=_cmd_report)

    # ---- Phase 2 ----------------------------------------------------------------
    s = sub.add_parser("audit", help="re-verify Phase 1 artifacts and metrics independently")
    s.set_defaults(func=_cmd_audit)

    s = sub.add_parser("shift", help="distribution-shift diagnostics -> reports/phase2/shift")
    s.set_defaults(func=_cmd_shift)

    s = sub.add_parser("backtest", help="rolling-origin backtest of retraining strategies")
    s.add_argument("--stage", choices=["dev", "confirm"], required=True)
    s.add_argument("--latency", type=int, default=None, help="label latency in steps")
    s.add_argument(
        "--drop-time-proxies",
        action="store_true",
        help="exploratory ablation: remove clock-like features",
    )
    s.add_argument(
        "--strategies",
        default=None,
        help="comma-separated strategy names, or 'selected' (static + dev selection)",
    )
    s.set_defaults(func=_cmd_backtest)

    # ---- Phase 3 ----------------------------------------------------------------
    s = sub.add_parser("demo-data", help="write SYNTHETIC Elliptic++-schema raw data")
    s.add_argument("--steps", type=int, default=49)
    s.add_argument("--per-step", type=int, default=120)
    s.set_defaults(func=_cmd_demo_data)

    s = sub.add_parser("demo-setup", help="build the full synthetic demo (use configs/demo.yaml)")
    s.set_defaults(func=_cmd_demo_setup)

    s = sub.add_parser("export-snapshots", help="build the serving model registry")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=_cmd_export_snapshots)

    s = sub.add_parser("verify-replay", help="replay scores == saved Phase 2 backtest scores")
    s.set_defaults(func=_cmd_verify_replay)

    s = sub.add_parser("serve", help="run the API (and dashboard if built)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_cmd_serve)

    s = sub.add_parser("benchmark", help="inference latency benchmark -> JSON")
    s.add_argument("--out", type=Path, default=None)
    s.set_defaults(func=_cmd_benchmark)

    s = sub.add_parser("backtest-report", help="tables, CIs and figures -> reports/phase2/backtest")
    s.set_defaults(func=_cmd_backtest_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    cfg = load_config(
        args.config,
        overrides={"feature_set": args.feature_set, "seed": args.seed, "mlflow": args.mlflow},
    )
    args.func(args, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
