"""``chainwatch-live``: the collector VM's command line. Imports only the live package,
FastAPI and uvicorn, so the VM never needs pandas, scikit-learn or XGBoost.

    chainwatch-live --config configs/live-cloud.yaml collect        # systemd: chainwatch-collector
    chainwatch-live --config configs/live-cloud.yaml serve-edge     # systemd: chainwatch-live-api
    chainwatch-live --config configs/live-cloud.yaml backup --dest /var/backups/chainwatch
    chainwatch-live --config configs/live-cloud.yaml report --hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

from chainwatch.config import LiveConfig, load_config


def _logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format='{"ts": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", '
        '"msg": %(message)r}',
    )


def _live(args) -> LiveConfig:
    live = load_config(args.config).live
    if getattr(args, "db", None):
        live = dataclasses.replace(live, db_path=args.db.resolve())
    return live


def cmd_collect(args) -> int:
    from chainwatch.live.collector import Collector
    from chainwatch.live.store import LiveStore

    live = _live(args)
    if getattr(args, "sample_rate", None) is not None:
        live = dataclasses.replace(live, sample_rate=args.sample_rate)
    if not live.enabled and not args.force:
        raise SystemExit(
            "live monitoring is disabled in this config; use configs/live.yaml or "
            "configs/live-cloud.yaml (or pass --force for a one-off bounded run)"
        )
    store = LiveStore(live.db_path)
    try:
        c = asyncio.run(Collector(live, store).run(args.duration, args.max_fetches))
    finally:
        store.close()
    print(json.dumps({"db": str(live.db_path), **dataclasses.asdict(c)}, indent=2))
    return 0


def cmd_serve_edge(args) -> int:
    import uvicorn

    from chainwatch.live.edge import create_edge_app, edge_token

    live = _live(args)
    app = create_edge_app(live, edge_token(live))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        log_config=None,
        limit_concurrency=32,
        timeout_keep_alive=5,
    )
    return 0


def backup(db: Path, dest_dir: Path, keep: int, now: float | None = None) -> Path:
    """Online backup with integrity check; keeps the newest ``keep`` copies."""
    from chainwatch.live.store import LiveStore

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    store = LiveStore(db)
    try:
        out = store.backup(Path(dest_dir) / f"live-{stamp}.sqlite")
    finally:
        store.close()
    check = sqlite3.connect(out)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"backup {out} failed its integrity check")
    finally:
        check.close()
    for old in sorted(Path(dest_dir).glob("live-*.sqlite"))[:-keep]:
        old.unlink()
    return out


def cmd_backup(args) -> int:
    out = backup(_live(args).db_path, args.dest, args.keep)
    print(json.dumps({"backup": str(out), "bytes": out.stat().st_size}))
    return 0


def reliability_report(live: LiveConfig, hours: float, now: float | None = None) -> dict:
    """Facts for the 24-hour reliability test (docs/LIVE_CLOUD.md section 9)."""
    from chainwatch.live.store import LiveReader

    now = time.time() if now is None else now
    r = LiveReader(live.db_path)
    st = r.status() or {}
    outages = r.outages(now - hours * 3600, limit=200)
    stats = r.stats(int(min(hours, 24) * 60), now)
    last = r.last_observation_at()
    files = [live.db_path, *live.db_path.parent.glob(live.db_path.name + "-*")]
    return {
        "window_hours": hours,
        "generated_at": now,
        "collector_state": st.get("state"),
        "heartbeat_age_s": round(now - st["updated_at"], 1) if st else None,
        "last_observation_age_s": round(now - last, 1) if last else None,
        "availability": r.availability(hours * 3600, now),
        "outages_by_kind": {
            k: sum(1 for o in outages if o["kind"] == k)
            for k in ("collector_down", "disconnected", "sequence_gap")
        },
        "outages": outages[:50],
        "totals_last_24h_max": stats["totals"],
        "failures": stats["failures"],
        "stored_rows": r.list(limit=1, outcome=None)[0],
        "db_bytes": sum(f.stat().st_size for f in files if f.exists()),
        "counters_since_process_start": st.get("counters"),
    }


def cmd_report(args) -> int:
    print(json.dumps(reliability_report(_live(args), args.hours), indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chainwatch-live", description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("collect", help="run the mempool collector (talks to mempool.space)")
    s.add_argument("--duration", type=float, default=None)
    s.add_argument("--max-fetches", type=int, default=None)
    s.add_argument("--db", type=Path, default=None)
    s.add_argument("--sample-rate", type=float, default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_collect)
    s = sub.add_parser("serve-edge", help="authenticated read-only API (behind a TLS proxy)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--db", type=Path, default=None)
    s.set_defaults(func=cmd_serve_edge)
    s = sub.add_parser("backup", help="consistent online SQLite backup with rotation")
    s.add_argument("--dest", type=Path, required=True)
    s.add_argument("--keep", type=int, default=7)
    s.add_argument("--db", type=Path, default=None)
    s.set_defaults(func=cmd_backup)
    s = sub.add_parser("report", help="reliability report (availability, outages, totals)")
    s.add_argument("--hours", type=float, default=24.0)
    s.add_argument("--db", type=Path, default=None)
    s.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    _logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
