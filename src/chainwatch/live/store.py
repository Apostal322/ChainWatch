"""SQLite storage shared by the collector (writer) and the API (read-only reader).

* WAL mode: one writer process and concurrent readers.
* Idempotent: one row per txid (``INSERT OR IGNORE``); re-delivered txids are no-ops.
* Bounded: ``prune`` keeps the newest ``retention_rows`` transactions and 24 h of
  per-minute counters, so the file cannot grow without limit.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txid TEXT NOT NULL UNIQUE,
    first_seen REAL NOT NULL,
    fetched_at REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('fetched', 'failed')),
    error TEXT,
    fee_rate REAL,
    fee INTEGER,
    vsize INTEGER,
    n_inputs INTEGER,
    n_outputs INTEGER,
    output_value_total INTEGER,
    rbf_signal INTEGER,
    confirmed_at_fetch INTEGER,
    flags TEXT NOT NULL DEFAULT '[]',
    features TEXT
);
CREATE INDEX IF NOT EXISTS tx_fee_rate ON tx(fee_rate);
CREATE INDEX IF NOT EXISTS tx_first_seen ON tx(first_seen);
CREATE TABLE IF NOT EXISTS minute_stats (
    minute INTEGER PRIMARY KEY,
    received INTEGER NOT NULL DEFAULT 0,
    sampled INTEGER NOT NULL DEFAULT 0,
    fetched INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0,
    rate_limited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collector (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    connected_since REAL,
    last_message_at REAL,
    last_sequence INTEGER,
    reconnects INTEGER NOT NULL DEFAULT 0,
    counters TEXT NOT NULL DEFAULT '{}',
    settings TEXT NOT NULL DEFAULT '{}'
);
"""
MINUTE_FIELDS = ("received", "sampled", "fetched", "failed", "dropped", "rate_limited")
SORTABLE = {"first_seen", "fee_rate", "fee", "vsize", "n_inputs", "n_outputs", "id"}
SUMMARY_COLS = (
    "id, txid, first_seen, fetched_at, outcome, error, fee_rate, fee, vsize, n_inputs, "
    "n_outputs, output_value_total, rbf_signal, confirmed_at_fetch, flags"
)


class NoLiveData(Exception):
    """The live database does not exist yet (the collector has never run)."""


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["flags"] = json.loads(d["flags"] or "[]")
    d["rbf_signal"] = None if d["rbf_signal"] is None else bool(d["rbf_signal"])
    d["confirmed_at_fetch"] = (
        None if d["confirmed_at_fetch"] is None else bool(d["confirmed_at_fetch"])
    )
    return d


class LiveStore:
    """Writer side, used only by the collector process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def has(self, txid: str) -> bool:
        return self.db.execute("SELECT 1 FROM tx WHERE txid = ?", (txid,)).fetchone() is not None

    def add_fetched(self, f: dict[str, Any], flags: list[str]) -> bool:
        """Insert a fetched transaction; returns False if the txid was already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tx (txid, first_seen, fetched_at, outcome, fee_rate, fee, "
            "vsize, n_inputs, n_outputs, output_value_total, rbf_signal, confirmed_at_fetch, "
            "flags, features) VALUES (?, ?, ?, 'fetched', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f["txid"], f["first_seen"], f["fetched_at"], f["fee_rate"], f["fee"],
                f["vsize"], f["n_inputs"], f["n_outputs"], f["output_value_total"],
                int(f["rbf_signal"]), int(f["confirmed_at_fetch"]), json.dumps(flags),
                json.dumps(f, separators=(",", ":")),
            ),
        )  # fmt: skip
        return cur.rowcount == 1

    def add_failed(self, txid: str, first_seen: float, fetched_at: float, error: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tx (txid, first_seen, fetched_at, outcome, error) "
            "VALUES (?, ?, ?, 'failed', ?)",
            (txid, first_seen, fetched_at, error[:200]),
        )
        return cur.rowcount == 1

    def bump(self, now: float, **counts: int) -> None:
        counts = {k: v for k, v in counts.items() if v}
        if not counts:
            return
        bad = set(counts) - set(MINUTE_FIELDS)
        if bad:
            raise ValueError(f"unknown counters {bad}")
        minute = int(now // 60)
        cols = ", ".join(counts)
        sets = ", ".join(f"{k} = {k} + excluded.{k}" for k in counts)
        self.db.execute(
            f"INSERT INTO minute_stats (minute, {cols}) VALUES (?, {', '.join('?' * len(counts))}) "
            f"ON CONFLICT(minute) DO UPDATE SET {sets}",
            (minute, *counts.values()),
        )

    def heartbeat(self, **status: Any) -> None:
        status = dict(status)
        status["counters"] = json.dumps(status.get("counters", {}))
        status["settings"] = json.dumps(status.get("settings", {}))
        cols = ", ".join(status)
        self.db.execute(
            f"INSERT INTO collector (id, {cols}) VALUES (1, {', '.join('?' * len(status))}) "
            f"ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{k} = excluded.{k}" for k in status),
            tuple(status.values()),
        )

    def prune(self, retention_rows: int, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cur = self.db.execute(
            "DELETE FROM tx WHERE id <= (SELECT MAX(id) FROM tx) - ?", (retention_rows,)
        )
        self.db.execute("DELETE FROM minute_stats WHERE minute < ?", (int(now // 60) - 24 * 60,))
        return cur.rowcount


class LiveReader:
    """Read-only access for the API process; opens the database with ``mode=ro``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise NoLiveData(str(self.path))
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def status(self) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM collector WHERE id = 1").fetchone()
        if row is None:
            return None
        d = dict(row)
        d["counters"] = json.loads(d["counters"])
        d["settings"] = json.loads(d["settings"])
        return d

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "id",
        descending: bool = True,
        min_fee_rate: float | None = None,
        max_fee_rate: float | None = None,
        flag: str | None = None,
        outcome: str | None = "fetched",
        after_id: int | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        if sort not in SORTABLE:
            raise ValueError(f"sort must be one of {sorted(SORTABLE)}")
        where, args = [], []
        if outcome:
            where.append("outcome = ?")
            args.append(outcome)
        if min_fee_rate is not None:
            where.append("fee_rate >= ?")
            args.append(min_fee_rate)
        if max_fee_rate is not None:
            where.append("fee_rate <= ?")
            args.append(max_fee_rate)
        if flag:
            where.append("EXISTS (SELECT 1 FROM json_each(tx.flags) WHERE value = ?)")
            args.append(flag)
        if after_id is not None:
            where.append("id > ?")
            args.append(after_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = f"{sort} {'DESC' if descending else 'ASC'}, id DESC"
        with self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM tx {clause}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT {SUMMARY_COLS} FROM tx {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return total, [_summary(r) for r in rows]

    def get(self, txid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(f"SELECT {SUMMARY_COLS}, features FROM tx WHERE txid = ?", (txid,))
            row = row.fetchone()
        if row is None:
            return None
        d = _summary(row)
        d["features"] = json.loads(row["features"]) if row["features"] else None
        return d

    def max_id(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM tx").fetchone()[0]

    def stats(self, window_minutes: int, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        since_min = int(now // 60) - window_minutes + 1
        since = now - window_minutes * 60
        with self._conn() as c:
            minutes = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM minute_stats WHERE minute >= ? ORDER BY minute", (since_min,)
                )
            ]
            fetched = c.execute(
                "SELECT fee_rate, vsize, flags FROM tx WHERE outcome = 'fetched' AND "
                "fetched_at >= ? ORDER BY fee_rate",
                (since,),
            ).fetchall()
            failed = c.execute(
                "SELECT error, COUNT(*) FROM tx WHERE outcome = 'failed' AND fetched_at >= ? "
                "GROUP BY error",
                (since,),
            ).fetchall()
        totals = {k: sum(m[k] for m in minutes) for k in MINUTE_FIELDS}
        rates = [r["fee_rate"] for r in fetched]
        vsizes = sorted(r["vsize"] for r in fetched)
        flag_counts: dict[str, int] = {}
        for r in fetched:
            for fl in json.loads(r["flags"]):
                flag_counts[fl] = flag_counts.get(fl, 0) + 1
        return {
            "window_minutes": window_minutes,
            "totals": totals,
            "per_minute": [
                {"t": m["minute"] * 60, **{k: m[k] for k in MINUTE_FIELDS}} for m in minutes
            ],
            "n_fetched": len(rates),
            "fee_rate_quantiles": _quantiles(rates),
            "vsize_quantiles": _quantiles(vsizes),
            "fee_rate_histogram": _histogram(rates),
            "flag_counts": flag_counts,
            "failures": {e or "unknown": n for e, n in failed},
        }


def _quantiles(sorted_values: list[float]) -> dict[str, float] | None:
    if not sorted_values:
        return None
    n = len(sorted_values)
    pick = lambda q: sorted_values[min(n - 1, int(q * (n - 1) + 0.5))]  # noqa: E731
    return {"p10": pick(0.1), "p50": pick(0.5), "p90": pick(0.9), "max": sorted_values[-1]}


FEE_BINS = (0, 1, 2, 3, 5, 8, 12, 20, 35, 60, 100, 200)  # sat/vB lower edges


def _histogram(rates: list[float]) -> list[dict[str, Any]]:
    counts = [0] * len(FEE_BINS)
    for r in rates:
        i = max(j for j, edge in enumerate(FEE_BINS) if r >= edge) if r >= 0 else 0
        counts[i] += 1
    return [
        {"from": lo, "to": FEE_BINS[i + 1] if i + 1 < len(FEE_BINS) else None, "count": c}
        for i, (lo, c) in enumerate(zip(FEE_BINS, counts, strict=True))
    ]
