"""Benchmark the paged findings and alert-queue queries (docs/LIVE_MONITOR.md).

Seeds a temporary database with ``--tx`` assessed observations (the collector's retention
cap is 50,000) and ``--alerts`` grouped alerts, then times each API query through
``LiveReader`` (read-only connection, as the edge API uses), first and deep pages, every
filter. Constructed data only (tests/risk_fixtures.py). Prints JSON.

    PYTHONPATH=src:tests python scripts/bench_risk_pages.py --tx 50000 --alerts 50000
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from chainwatch.live.store import LiveReader
from chainwatch.risk.persistence import AlertsQuery, FindingsQuery
from risk_fixtures import seed


def timed(fn, reps: int) -> dict:
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    out.sort()
    return {
        "p50_ms": round(statistics.median(out), 2),
        "p95_ms": round(out[min(len(out) - 1, int(0.95 * len(out)))], 2),
        "max_ms": round(out[-1], 2),
    }


def deep(reader, page_fn, q, pages: int):
    """The cursor ``pages`` pages deep (to time a page far from the top)."""
    cursor = None
    for _ in range(pages):
        p = page_fn(q if cursor is None else _with_cursor(q, cursor), 0.0)
        cursor = p["next_cursor"]
        if cursor is None:
            break
    return cursor


def _with_cursor(q, cursor):
    if isinstance(q, FindingsQuery):
        return FindingsQuery(**{**q.__dict__, "before_id": int(cursor)})
    return AlertsQuery(**{**q.__dict__, "cursor": cursor})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx", type=int, default=50_000)
    ap.add_argument("--alerts", type=int, default=50_000)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        t0 = time.perf_counter()
        store, truth = seed(Path(d) / "live.sqlite", a.tx, a.alerts)
        seeded_s = time.perf_counter() - t0
        store.close()
        reader = LiveReader(Path(d) / "live.sqlite")
        lim = a.limit
        fq = {
            "findings: all, first page": FindingsQuery(limit=lim),
            "findings: source matches": FindingsQuery(type="source_match", limit=lim),
            "findings: anomalies P4": FindingsQuery(type="anomaly", priority=4, limit=lim),
            "findings: authority ofac-sdn": FindingsQuery(authority="ofac-sdn", limit=lim),
            "findings: insufficient evidence": FindingsQuery(
                type="insufficient_evidence", limit=lim
            ),
            "findings: time range 1 h": FindingsQuery(
                since=1_800_000_000 + a.tx / 2, until=1_800_003_600 + a.tx / 2, limit=lim
            ),
            "findings: page size 200": FindingsQuery(limit=200),
        }
        aq = {
            "alerts: triage, first page": AlertsQuery(limit=lim),
            "alerts: open": AlertsQuery(status="open", limit=lim),
            "alerts: retrospective": AlertsQuery(type="retrospective", limit=lim),
            "alerts: authority ofac-sdn": AlertsQuery(authority="ofac-sdn", limit=lim),
            "alerts: P2, last seen in a range": AlertsQuery(
                priority=2, since=1_800_010_000, until=1_800_020_000, limit=lim
            ),
            "alerts: newest": AlertsQuery(sort="newest", limit=lim),
            "alerts: page size 200": AlertsQuery(limit=200),
        }
        results = {}
        for name, q in fq.items():
            results[name] = timed(lambda q=q: reader.findings_page(q, 0.0), a.reps)
        for name, q in aq.items():
            results[name] = timed(lambda q=q: reader.alerts_page(q, 0.0), a.reps)
        c = deep(reader, reader.findings_page, FindingsQuery(limit=lim), 100)
        if c:
            dq = FindingsQuery(limit=lim, before_id=int(c))
            results["findings: page 101"] = timed(lambda: reader.findings_page(dq, 0.0), a.reps)
        c = deep(reader, reader.alerts_page, AlertsQuery(limit=lim), 100)
        if c:
            dq2 = AlertsQuery(limit=lim, cursor=c)
            results["alerts: triage page 101"] = timed(lambda: reader.alerts_page(dq2, 0.0), a.reps)
        with reader._conn() as con:
            plans = {}
            for label, sql in (
                (
                    "findings ids",
                    "SELECT r.tx_id FROM risk r WHERE r.priority BETWEEN 1 AND 4 "
                    "ORDER BY r.tx_id DESC LIMIT 26",
                ),
                ("findings count", "SELECT COUNT(*) FROM risk r WHERE r.priority BETWEEN 1 AND 4"),
                (
                    "alerts count",
                    "SELECT COUNT(*) FROM alerts WHERE status IN ('new', 'under_review')",
                ),
            ):
                plans[label] = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
        print(
            json.dumps(
                {
                    "kind": "LOCAL benchmark on constructed data (not network observations)",
                    "tx": a.tx,
                    "alerts": a.alerts,
                    "findings": sum(
                        f["state"]
                        in ("source_match", "relation", "anomaly_strong", "anomaly_notable")
                        for f in truth.findings
                    ),
                    "seeded_s": round(seeded_s, 1),
                    "reps": a.reps,
                    "results_ms": results,
                    "plans": plans,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
