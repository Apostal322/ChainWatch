"""SQLite storage shared by the collector (writer) and the API (read-only reader).

* WAL mode: one writer process and concurrent readers.
* Idempotent: one row per txid (``INSERT OR IGNORE``); re-delivered txids are no-ops.
* Bounded: ``prune`` keeps at most ``retention_rows`` transactions no older than
  ``retention_days``, 24 h of per-minute counters and 30 days of outage records, and
  ``checkpoint`` truncates the WAL, so the files cannot grow without limit.
* Gaps are explicit: the ``outages`` table records every interval in which the collector
  was not receiving (process down, WebSocket disconnected) and every missed mempool update
  (``sequence_gap``), so coverage can be reported honestly.
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
CREATE INDEX IF NOT EXISTS tx_fetched_at ON tx(fetched_at);
-- covering index for the API's filtered counts/pages (COUNT by outcome was a full scan)
CREATE INDEX IF NOT EXISTS tx_outcome_fee ON tx(outcome, fee_rate);
CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('collector_down', 'disconnected', 'sequence_gap')),
    started_at REAL NOT NULL,
    ended_at REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS outages_started ON outages(started_at);
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
    first_started_at REAL,
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


def _opt_int(v: bool | None) -> int | None:
    return None if v is None else int(v)


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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(collector)")}
        if "first_started_at" not in cols:  # migrate databases created by v0.5.0
            self.db.execute("ALTER TABLE collector ADD COLUMN first_started_at REAL")
        from chainwatch.risk.persistence import migrate

        migrate(self.db)  # risk tables and tx lifecycle columns (v0.7.0)

    def close(self) -> None:
        self.db.close()

    def has(self, txid: str) -> bool:
        return self.db.execute("SELECT 1 FROM tx WHERE txid = ?", (txid,)).fetchone() is not None

    def add_fetched(self, f: dict[str, Any], flags: list[str]) -> bool:
        """Insert a fetched transaction; returns False if the txid was already stored."""
        return self.add_fetched_id(f, flags) is not None

    def add_fetched_id(self, f: dict[str, Any], flags: list[str]) -> int | None:
        """Insert a fetched transaction and return its row id (None if already stored)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tx (txid, first_seen, fetched_at, outcome, fee_rate, fee, "
            "vsize, n_inputs, n_outputs, output_value_total, rbf_signal, confirmed_at_fetch, "
            "flags, features) VALUES (?, ?, ?, 'fetched', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f["txid"], f["first_seen"], f["fetched_at"], f["fee_rate"], f["fee"],
                f["vsize"], f["n_inputs"], f["n_outputs"], f["output_value_total"],
                int(f["rbf_signal"]), _opt_int(f["confirmed_at_fetch"]), json.dumps(flags),
                json.dumps(f, separators=(",", ":")),
            ),
        )  # fmt: skip
        return int(cur.lastrowid) if cur.rowcount == 1 else None

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

    def previous_status(self) -> dict[str, Any] | None:
        self.db.row_factory = sqlite3.Row
        try:
            row = self.db.execute("SELECT * FROM collector WHERE id = 1").fetchone()
        finally:
            self.db.row_factory = None
        return dict(row) if row else None

    def open_outage(self, kind: str, started_at: float, detail: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO outages (kind, started_at, detail) VALUES (?, ?, ?)",
            (kind, started_at, detail[:200]),
        )
        return int(cur.lastrowid)

    def close_outage(self, outage_id: int, ended_at: float) -> None:
        self.db.execute(
            "UPDATE outages SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, outage_id),
        )

    def record_gap(self, kind: str, started_at: float, ended_at: float, detail: str) -> None:
        self.db.execute(
            "INSERT INTO outages (kind, started_at, ended_at, detail) VALUES (?, ?, ?, ?)",
            (kind, started_at, ended_at, detail[:200]),
        )

    def close_dangling_outages(self, ended_at: float) -> int:
        """Outages left open by a crashed process end where its last heartbeat was."""
        cur = self.db.execute(
            "UPDATE outages SET ended_at = MAX(started_at, ?) WHERE ended_at IS NULL", (ended_at,)
        )
        return cur.rowcount

    def prune(
        self, retention_rows: int, now: float | None = None, retention_days: float | None = None
    ) -> int:
        now = time.time() if now is None else now
        cur = self.db.execute(
            "DELETE FROM tx WHERE id <= (SELECT MAX(id) FROM tx) - ?", (retention_rows,)
        )
        removed = cur.rowcount
        if retention_days is not None:
            removed += self.db.execute(
                "DELETE FROM tx WHERE fetched_at < ?", (now - retention_days * 86400,)
            ).rowcount
        self.db.execute("DELETE FROM minute_stats WHERE minute < ?", (int(now // 60) - 24 * 60,))
        self.db.execute(
            "DELETE FROM outages WHERE ended_at IS NOT NULL AND ended_at < ?", (now - 30 * 86400,)
        )
        from chainwatch.risk.persistence import prune_orphans

        prune_orphans(self.db, now)  # addresses and risk records of pruned rows; old alerts
        return removed

    def recent_features(self, limit: int) -> list[dict[str, Any]]:
        """Features of the last ``limit`` fetched transactions, oldest first (detector warm
        start after a restart: the same reference window the previous process had)."""
        rows = self.db.execute(
            "SELECT features FROM tx WHERE outcome = 'fetched' AND features IS NOT NULL "
            "ORDER BY fetched_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def checkpoint(self) -> None:
        """Fold the WAL into the database and truncate it (bounds the -wal file)."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def backup(self, dest: Path) -> Path:
        """Consistent online copy (SQLite backup API); safe while the collector writes."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        target = sqlite3.connect(tmp)
        try:
            self.db.backup(target)
        finally:
            target.close()
        tmp.replace(dest)
        return dest


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
        # Index use (measured at the 50k-row cap, docs/LIVE_CLOUD.md section 8): COUNT uses the
        # covering (outcome, fee_rate) index; a page sorted by fee uses it too; a page sorted
        # by anything else is fastest as a reverse rowid scan that stops after LIMIT rows,
        # so there the unary "+" keeps SQLite from choosing the index plus a full sort.
        hint = "" if sort == "fee_rate" else "+"
        where, page, args = [], [], []
        if outcome:
            # with a flag filter every row is read anyway (json_each), so a plain table
            # scan beats an index lookup per row for the COUNT as well
            where.append(f"{'+' if flag else ''}outcome = ?")
            page.append(f"{hint}outcome = ?")
            args.append(outcome)
        if min_fee_rate is not None:
            where.append("fee_rate >= ?")
            page.append(f"{hint}fee_rate >= ?")
            args.append(min_fee_rate)
        if max_fee_rate is not None:
            where.append("fee_rate <= ?")
            page.append(f"{hint}fee_rate <= ?")
            args.append(max_fee_rate)
        if flag:
            cond = "EXISTS (SELECT 1 FROM json_each(tx.flags) WHERE value = ?)"
            where.append(cond)
            page.append(cond)
            args.append(flag)
        if after_id is not None:
            where.append("id > ?")
            page.append("id > ?")
            args.append(after_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        page_clause = f"WHERE {' AND '.join(page)}" if page else ""
        order = f"{sort} {'DESC' if descending else 'ASC'}, id DESC"
        with self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM tx {clause}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT {SUMMARY_COLS} FROM tx {page_clause} ORDER BY {order} LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            out = [_summary(r) for r in rows]
            if out:  # review priority of each row (risk intelligence), when assessed
                marks = ",".join("?" * len(out))
                try:
                    prio = dict(
                        c.execute(
                            f"SELECT tx_id, priority FROM risk WHERE tx_id IN ({marks})",
                            [r["id"] for r in out],
                        ).fetchall()
                    )
                except sqlite3.OperationalError:  # database from before v0.7.0
                    prio = {}
                for r in out:
                    r["risk_priority"] = prio.get(r["id"])
        return total, out

    def get(self, txid: str) -> dict[str, Any] | None:
        from chainwatch.risk import persistence as rp

        with self._conn() as c:
            row = c.execute(f"SELECT {SUMMARY_COLS}, features FROM tx WHERE txid = ?", (txid,))
            row = row.fetchone()
            if row is None:
                return None
            d = _summary(row)
            d["features"] = json.loads(row["features"]) if row["features"] else None
            try:
                d["risk"] = rp.risk_record(c, txid)
                lc = c.execute(
                    "SELECT lifecycle, lifecycle_at, replaced_by FROM tx WHERE txid = ?", (txid,)
                ).fetchone()
                d["lifecycle"] = {"state": lc[0], "at": lc[1], "replaced_by": lc[2]}
            except sqlite3.OperationalError:  # database from before v0.7.0
                d["risk"], d["lifecycle"] = None, None
        return d

    def _risk(self, fn, default):
        with self._conn() as c:
            try:
                return fn(c)
            except sqlite3.OperationalError as e:  # tables not created yet (older collector)
                if "no such table" in str(e) or "no such column" in str(e):
                    return default
                raise

    def findings(self, max_priority: int, limit: int, before_id: int | None) -> list[dict]:
        from chainwatch.risk import persistence as rp

        return self._risk(
            lambda c: rp.findings(c, max_priority=max_priority, limit=limit, before_id=before_id),
            [],
        )

    def alerts(self, status: str | None, limit: int) -> list[dict]:
        from chainwatch.risk import persistence as rp

        return self._risk(lambda c: rp.alerts(c, status=status, limit=limit), [])

    def risk_summary(self, since: float) -> dict[str, Any] | None:
        from chainwatch.risk import persistence as rp

        return self._risk(lambda c: rp.risk_summary(c, since), None)

    def last_observation_at(self) -> float | None:
        with self._conn() as c:
            # "+outcome": walk the fetched_at index backwards (the outcome index made this a
            # 73 ms scan at 50k rows)
            q = "SELECT MAX(fetched_at) FROM tx WHERE +outcome = 'fetched'"
            return c.execute(q).fetchone()[0]

    def outages(self, since: float, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM outages WHERE COALESCE(ended_at, 1e18) >= ? "
                "ORDER BY started_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def availability(self, window_s: float, now: float | None = None) -> dict[str, Any]:
        """Share of the window in which the collector was receiving (outages subtracted).

        The window starts no earlier than the collector's first start, so a fresh install
        is not penalised for time before it existed. Sequence gaps are counted separately.
        """
        now = time.time() if now is None else now
        st = self.status()
        if st is None or st.get("started_at") is None:
            return {"window_s": 0.0, "observed_share": None, "down_s": 0.0, "sequence_gaps": 0}
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, started_at, COALESCE(ended_at, ?) AS e, detail FROM outages "
                "WHERE COALESCE(ended_at, ?) >= ?",
                (now, now, now - window_s),
            ).fetchall()
        begin = max(now - window_s, st.get("first_started_at") or st["started_at"])
        span = max(now - begin, 1e-9)
        intervals = sorted(
            (max(r["started_at"], begin), min(r["e"], now))
            for r in rows
            if r["kind"] != "sequence_gap" and r["e"] > begin
        )
        down, cur_s, cur_e = 0.0, None, None
        for s0, e0 in intervals:  # merge overlapping intervals
            if cur_e is None or s0 > cur_e:
                if cur_e is not None:
                    down += cur_e - cur_s
                cur_s, cur_e = s0, e0
            else:
                cur_e = max(cur_e, e0)
        if cur_e is not None:
            down += cur_e - cur_s
        gaps = sum(1 for r in rows if r["kind"] == "sequence_gap")
        return {
            "window_s": round(span, 1),
            "observed_share": round(max(0.0, 1 - down / span), 4),
            "down_s": round(down, 1),
            "sequence_gaps": gaps,
        }

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
            # no ORDER BY: it made SQLite walk the fee_rate index over the whole table
            # (83 ms at 50k rows) instead of the fetched_at range (3 ms); sorted below
            fetched = c.execute(
                "SELECT fee_rate, vsize, flags FROM tx WHERE +outcome = 'fetched' AND "
                "fetched_at >= ?",
                (since,),
            ).fetchall()
            failed = c.execute(
                "SELECT error, COUNT(*) FROM tx WHERE +outcome = 'failed' AND fetched_at >= ? "
                "GROUP BY error",
                (since,),
            ).fetchall()
        totals = {k: sum(m[k] for m in minutes) for k in MINUTE_FIELDS}
        rates = sorted(r["fee_rate"] for r in fetched)
        # Both sources are lower bounds: per-minute counters are flushed every heartbeat, so
        # a hard kill loses up to heartbeat_s of counts, while stored rows can be pruned by
        # retention. Rows are durable, so stored fetched/failed counts are used when larger.
        totals["fetched"] = max(totals["fetched"], len(rates))
        totals["failed"] = max(totals["failed"], sum(n for _, n in failed))
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
