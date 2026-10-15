"""SQLite persistence for risk intelligence (same file as the live store; WAL).

Tables (docs/RISK_ENGINE.md section 5):

* ``tx_address``: screened addresses per observed transaction (role in/out, index). Enables
  retrospective screening when a source adds an address later, and observed-graph lookups.
* ``indicator_versions`` / ``indicators``: every source snapshot (sha256 version, retrieval
  and publication time) and every listed address with its *availability interval*
  ``[available_at, removed_at)``: the time ChainWatch learned of the listing and, if it was
  delisted, the time it learned of that. Screening at time T uses only indicators available
  at T (point-in-time correctness).
* ``risk``: one assessment per transaction (all layers, versions, latency).
* ``alerts`` / ``alert_events``: the investigation queue with an audit log.
* ``tx.lifecycle``: what happened to the transaction *after* observation (mined, removed,
  replaced). It is state, never a feature, so it cannot leak into a past assessment.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from chainwatch.risk.indicators import Snapshot
from chainwatch.risk.sources import authority_of, source_title

_VERSIONS_TABLE = """indicator_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    licence TEXT NOT NULL,
    retrieved_at REAL NOT NULL,
    published_at REAL,
    checked_at REAL NOT NULL,
    n_records INTEGER NOT NULL,
    n_rejected INTEGER NOT NULL,
    rejected TEXT NOT NULL DEFAULT '[]'
)"""
RISK_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS tx_address (
    tx_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('in', 'out')),
    idx INTEGER NOT NULL,
    address TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tx_address_addr ON tx_address(address);
CREATE INDEX IF NOT EXISTS tx_address_tx ON tx_address(tx_id);
CREATE TABLE IF NOT EXISTS """
    + _VERSIONS_TABLE
    + """;
CREATE INDEX IF NOT EXISTS indicator_versions_source ON indicator_versions(source_id, id);
CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    address TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    reference TEXT NOT NULL,
    listed_on TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    available_at REAL NOT NULL,
    removed_at REAL,
    version_added INTEGER NOT NULL,
    version_last INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS indicators_addr ON indicators(address);
CREATE INDEX IF NOT EXISTS indicators_active ON indicators(source_id, removed_at);
CREATE TABLE IF NOT EXISTS risk (
    tx_id INTEGER PRIMARY KEY,
    txid TEXT NOT NULL UNIQUE,
    evaluated_at REAL NOT NULL,
    priority INTEGER,
    screening TEXT NOT NULL,
    graph TEXT NOT NULL,
    anomaly TEXT NOT NULL,
    supervised TEXT NOT NULL,
    record TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    n_errors INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS risk_priority ON risk(priority, tx_id);
CREATE INDEX IF NOT EXISTS risk_evaluated ON risk(evaluated_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL UNIQUE,
    layer TEXT NOT NULL,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('new', 'under_review', 'dismissed', 'resolved', 'insufficient_evidence')),
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    suppressed INTEGER NOT NULL DEFAULT 0,
    retrospective INTEGER NOT NULL DEFAULT 0,
    txids TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    note TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status, priority, last_seen_at);
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    at REAL NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS alert_events_alert ON alert_events(alert_id);
CREATE TABLE IF NOT EXISTS alert_txids (
    alert_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    PRIMARY KEY (alert_id, txid)
) WITHOUT ROWID;
"""
)
ALERT_STATUSES = ("new", "under_review", "dismissed", "resolved", "insufficient_evidence")
MAX_ALERT_TXIDS = 50
LIFECYCLE_COLUMNS = (("lifecycle", "TEXT"), ("lifecycle_at", "REAL"), ("replaced_by", "TEXT"))


MASS_REMOVAL_SHARE = 0.25  # a snapshot may not delist more than this share of active rows
MASS_REMOVAL_MIN = 5  # ...unless it is only a handful (small lists)


class SnapshotRejected(ValueError):
    """A snapshot that would empty or mostly empty the active list (a broken download, a
    changed schema, an error page): refused unless explicitly allowed."""


def migrate(db: sqlite3.Connection) -> None:
    """Create or upgrade the risk tables. Idempotent, and run inside one IMMEDIATE
    transaction so two processes starting together cannot both apply a step (they used to
    race on ``ALTER TABLE``: "duplicate column name")."""
    with _tx(db):
        had_alert_txids = _has_table(db, "alert_txids")
        for stmt in RISK_SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
        cols = {r[1] for r in db.execute("PRAGMA table_info(tx)")}
        for name, typ in LIFECYCLE_COLUMNS:
            if name not in cols:
                db.execute(f"ALTER TABLE tx ADD COLUMN {name} {typ}")
        if "n_errors" not in {r[1] for r in db.execute("PRAGMA table_info(risk)")}:
            db.execute("ALTER TABLE risk ADD COLUMN n_errors INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE risk SET n_errors = 1 WHERE record LIKE '%\"errors\":[{%'")
        if not had_alert_txids:  # alerts from before v0.8: their (capped) txid lists
            for aid, txids in db.execute("SELECT id, txids FROM alerts").fetchall():
                db.executemany(
                    "INSERT OR IGNORE INTO alert_txids (alert_id, txid) VALUES (?, ?)",
                    [(aid, t) for t in json.loads(txids or "[]")],
                )
        _drop_version_uniqueness(db)


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    q = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return db.execute(q, (name,)).fetchone() is not None


def _drop_version_uniqueness(db: sqlite3.Connection) -> None:
    """Before v0.8 a (source, sha256) pair could exist once, so content that reverted to an
    earlier version was taken as "unchanged" and its re-listed addresses stayed inactive."""
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'indicator_versions'"
    ).fetchone()[0]
    if "UNIQUE" not in sql:
        return
    cols = ("id, source_id, sha256, method, url, licence, retrieved_at, published_at, "
            "checked_at, n_records, n_rejected, rejected")  # fmt: skip
    db.execute("ALTER TABLE indicator_versions RENAME TO indicator_versions_v07")
    db.execute("CREATE TABLE " + _VERSIONS_TABLE)
    db.execute(f"INSERT INTO indicator_versions ({cols}) SELECT {cols} FROM indicator_versions_v07")
    db.execute("DROP TABLE indicator_versions_v07")
    db.execute(
        "CREATE INDEX IF NOT EXISTS indicator_versions_source ON indicator_versions(source_id, id)"
    )


def _tx(db: sqlite3.Connection):
    """Explicit transaction on an autocommit connection (isolation_level=None)."""

    class _T:
        def __enter__(self):
            db.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type, *_):
            if not db.in_transaction:  # SQLite already rolled back (e.g. SQLITE_FULL)
                if exc_type is None:
                    raise sqlite3.OperationalError("transaction was rolled back by SQLite")
                return False
            db.execute("ROLLBACK" if exc_type else "COMMIT")
            return False

    return _T()


transaction = _tx


def _savepoint(db: sqlite3.Connection, name: str = "risk"):
    """Nested scope inside an open transaction: a failure rolls back only this part."""

    class _S:
        def __enter__(self):
            db.execute(f"SAVEPOINT {name}")

        def __exit__(self, exc_type, *_):
            if not db.in_transaction:  # the whole transaction was rolled back (disk full)
                return False
            if exc_type:
                db.execute(f"ROLLBACK TO {name}")
            db.execute(f"RELEASE {name}")
            return False

    return _S()


savepoint = _savepoint


# ---------------------------------------------------------------- indicators
def ingest_snapshot(
    db: sqlite3.Connection, snap: Snapshot, now: float, *, allow_mass_removal: bool = False
) -> dict[str, Any]:
    """Record a source snapshot and make the active listings match it.

    * Newly listed addresses become available at ``snap.retrieved_at``; addresses missing from
      the snapshot are closed (``removed_at``). This is a diff against what is *active*, so
      content that reverts to an earlier version re-lists what that version listed.
    * Sources of the same authority (the official SDN file and its mirror) describe one
      list: addresses another such source still lists but this newer snapshot does not are
      closed there too, so a superseded copy cannot keep a delisted address alive.
    * An empty snapshot, or one that would delist more than ``MASS_REMOVAL_SHARE`` of the
      active listings, is refused (:class:`SnapshotRejected`) unless ``allow_mass_removal``:
      a truncated download, an error page or a schema change must not silently empty the list.
    * Nothing to change and the same content as the source's latest version: only
      ``checked_at`` (freshness) is refreshed.
    """
    current = {r.address for r in snap.records}
    authority = authority_of(snap.source_id)
    with _tx(db):
        latest = db.execute(
            "SELECT id, sha256 FROM indicator_versions WHERE source_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (snap.source_id,),
        ).fetchone()
        active = {
            a: i
            for i, a in db.execute(
                "SELECT id, address FROM indicators WHERE source_id = ? AND removed_at IS NULL",
                (snap.source_id,),
            )
        }
        others = [
            (i, a)
            for i, a, src in db.execute(
                "SELECT id, address, source_id FROM indicators "
                "WHERE source_id != ? AND removed_at IS NULL",
                (snap.source_id,),
            )
            if authority_of(src) == authority
        ]
        to_add = [r for r in snap.records if r.address not in active]
        close_own = [i for a, i in active.items() if a not in current]
        close_other = [i for i, a in others if a not in current]
        if latest and latest[1] == snap.sha256 and not (to_add or close_own or close_other):
            db.execute(
                "UPDATE indicator_versions SET checked_at = ? WHERE id = ?", (now, latest[0])
            )
            return {"status": "unchanged", "version_id": latest[0], **snap.summary()}
        if not allow_mass_removal:
            if not snap.records:
                raise SnapshotRejected(
                    f"{snap.source_id}: the snapshot has no valid address "
                    f"({len(snap.rejected)} rejected); refusing to empty the list"
                )
            base = len(active) + len(others)
            removing = len(close_own) + len(close_other)
            if removing > max(MASS_REMOVAL_MIN, MASS_REMOVAL_SHARE * base):
                raise SnapshotRejected(
                    f"{snap.source_id}: the snapshot would delist {removing} of {base} active "
                    "listings; refusing (pass --allow-mass-removal if this is genuine)"
                )
        cur = db.execute(
            "INSERT INTO indicator_versions (source_id, sha256, method, url, licence, "
            "retrieved_at, published_at, checked_at, n_records, n_rejected, rejected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap.source_id, snap.sha256, snap.method, snap.url, snap.licence,
                snap.retrieved_at, snap.published_at, now, len(snap.records),
                len(snap.rejected), json.dumps(snap.rejected[:200]),
            ),
        )  # fmt: skip
        vid = int(cur.lastrowid)
        for rec in snap.records:
            if rec.address in active:  # still listed: refresh the source's metadata
                db.execute(
                    "UPDATE indicators SET version_last = ?, label = ?, reference = ?, "
                    "listed_on = ?, details = ? WHERE id = ?",
                    (vid, rec.label, rec.reference, rec.listed_on, json.dumps(rec.details),
                     active[rec.address]),
                )  # fmt: skip
        db.executemany(
            "INSERT INTO indicators (source_id, address, category, label, reference, "
            "listed_on, details, available_at, version_added, version_last) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(snap.source_id, r.address, r.category, r.label, r.reference, r.listed_on,
              json.dumps(r.details), snap.retrieved_at, vid, vid) for r in to_add],
        )  # fmt: skip
        db.executemany(
            "UPDATE indicators SET removed_at = ? WHERE id = ?",
            [(snap.retrieved_at, i) for i in close_own + close_other],
        )
    return {"status": "new_version", "version_id": vid, "added": len(to_add),
            "removed": len(close_own), "superseded_elsewhere": len(close_other),
            **snap.summary()}  # fmt: skip


def indicator_rows(db: sqlite3.Connection) -> list[tuple]:
    """Every listing with the version that added it and the version whose metadata (party
    names, programmes) it carries now."""
    return db.execute(
        "SELECT i.id, i.source_id, i.address, i.category, i.label, i.reference, i.listed_on, "
        "i.details, i.available_at, i.removed_at, i.version_added, v.sha256, w.sha256 "
        "FROM indicators i JOIN indicator_versions v ON v.id = i.version_added "
        "JOIN indicator_versions w ON w.id = i.version_last"
    ).fetchall()


def source_status(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT v.source_id, v.id, v.sha256, v.method, v.url, v.licence, v.retrieved_at, "
        "v.published_at, MAX(v.checked_at), v.n_records, v.n_rejected FROM indicator_versions v "
        "WHERE v.id = (SELECT MAX(id) FROM indicator_versions w WHERE w.source_id = v.source_id) "
        "GROUP BY v.source_id"
    ).fetchall()
    out = []
    for r in rows:
        last_checked = db.execute(
            "SELECT MAX(checked_at) FROM indicator_versions WHERE source_id = ?", (r[0],)
        ).fetchone()[0]
        active = db.execute(
            "SELECT COUNT(*) FROM indicators WHERE source_id = ? AND removed_at IS NULL", (r[0],)
        ).fetchone()[0]
        first = db.execute(
            "SELECT MIN(retrieved_at) FROM indicator_versions WHERE source_id = ?", (r[0],)
        ).fetchone()[0]
        out.append(
            {
                "source_id": r[0], "version_id": r[1], "sha256": r[2], "method": r[3],
                "url": r[4], "licence": r[5], "retrieved_at": r[6], "published_at": r[7],
                "checked_at": last_checked, "n_records": r[9], "n_rejected": r[10],
                "active_indicators": active,
                "first_retrieved_at": first,  # since when the source exists for screening
                "authority": authority_of(r[0]),
                "superseded_by": None,
            }
        )  # fmt: skip
    # a source whose list was refreshed more recently through another channel of the same
    # authority (the official file after a mirror fallback, or vice versa) is superseded:
    # its age is not a staleness problem
    for s in out:
        newer = [o for o in out if o is not s and o["authority"] == s["authority"]
                 and (o["checked_at"] or 0) > (s["checked_at"] or 0)]  # fmt: skip
        if newer:
            s["superseded_by"] = max(newer, key=lambda o: o["checked_at"] or 0)["source_id"]
    return out


def latest_version_id(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COALESCE(MAX(id), 0) FROM indicator_versions").fetchone()[0]


# ---------------------------------------------------------------- per-transaction records
def save_addresses(db: sqlite3.Connection, tx_id: int, addrs: dict[str, Any]) -> None:
    rows = [(tx_id, "in", i, a) for i, a in addrs["inputs"]]
    rows += [(tx_id, "out", i, a) for i, a in addrs["outputs"]]
    db.executemany("INSERT INTO tx_address (tx_id, role, idx, address) VALUES (?, ?, ?, ?)", rows)


def tx_id_of(db: sqlite3.Connection, txid: str) -> int | None:
    row = db.execute("SELECT id FROM tx WHERE txid = ?", (txid,)).fetchone()
    return row[0] if row else None


def save_risk(db: sqlite3.Connection, tx_id: int, record: dict[str, Any]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO risk (tx_id, txid, evaluated_at, priority, screening, graph, "
        "anomaly, supervised, record, latency_ms, n_errors) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tx_id, record["txid"], record["evaluated_at"], record["priority"]["level"],
            record["screening"]["status"], record["graph"]["status"],
            record["anomaly"]["status"], record["supervised"]["status"],
            json.dumps(record, separators=(",", ":")), record["latency_ms"],
            len(record.get("errors") or []),
        ),
    )  # fmt: skip


MAX_PARENTS_CHECKED = 200


def observed_parent_inputs(
    db: sqlite3.Connection, parent_txids: Iterable[str]
) -> dict[str, list[tuple[int, str]]]:
    """Input addresses of parents this collector observed and assessed (``tx_address``).
    The engine screens them at the *child's* evaluation time, so a later listing or
    delisting is reflected (a parent's own frozen screening result is not reused)."""
    ids = list(parent_txids)[:MAX_PARENTS_CHECKED]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[str, list[tuple[int, str]]] = {}
    for txid, idx, address in db.execute(
        f"SELECT t.txid, a.idx, a.address FROM tx t JOIN tx_address a ON a.tx_id = t.id "
        f"WHERE t.txid IN ({marks}) AND a.role = 'in'",
        ids,
    ):
        out.setdefault(txid, []).append((idx, address))
    return out


def count_observed(db: sqlite3.Connection, txids: Iterable[str]) -> int:
    ids = list(txids)[:MAX_PARENTS_CHECKED]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    return db.execute(f"SELECT COUNT(*) FROM risk WHERE txid IN ({marks})", ids).fetchone()[0]


def mark_lifecycle(
    db: sqlite3.Connection, txids: Iterable[str], state: str, at: float, replaced_by=None
) -> int:
    """Post-observation state (mined / removed / replaced). Never touches features.

    ``mined`` is final and overrides an earlier ``removed``/``replaced`` (mempool.space can
    report a replacement whose original is then mined: the conflict lost); ``replaced``
    refines ``removed``; nothing overrides ``mined``."""
    if state == "mined":
        sql = ("UPDATE tx SET lifecycle = 'mined', lifecycle_at = ?, replaced_by = NULL "
               "WHERE txid = ? AND (lifecycle IS NULL OR lifecycle != 'mined')")  # fmt: skip
        return sum(db.execute(sql, (at, t)).rowcount for t in txids)
    n = 0
    for t in txids:
        n += db.execute(
            "UPDATE tx SET lifecycle = ?, lifecycle_at = ?, replaced_by = COALESCE(?, replaced_by) "
            "WHERE txid = ? AND (lifecycle IS NULL OR lifecycle = 'removed')",
            (state, at, replaced_by, t),
        ).rowcount
    return n


def prune_orphans(db: sqlite3.Connection, now: float, alert_days: float = 90.0) -> None:
    # Row ids grow with fetch order and retention removes the oldest rows, so an id range
    # (indexed) finds the orphans without scanning the table every minute.
    min_id = db.execute("SELECT COALESCE(MIN(id), 0) FROM tx").fetchone()[0]
    if min_id:
        db.execute("DELETE FROM tx_address WHERE tx_id < ?", (min_id,))
        db.execute("DELETE FROM risk WHERE tx_id < ?", (min_id,))
    old = now - alert_days * 86400
    for table in ("alert_events", "alert_txids"):
        db.execute(
            f"DELETE FROM {table} WHERE alert_id IN (SELECT id FROM alerts WHERE last_seen_at < ?)",
            (old,),
        )
    db.execute("DELETE FROM alerts WHERE last_seen_at < ?", (old,))


# ---------------------------------------------------------------- alerts
def upsert_alert(
    db: sqlite3.Connection,
    *,
    group_key: str,
    layer: str,
    kind: str,
    priority: int,
    title: str,
    txid: str,
    evidence: dict[str, Any],
    at: float,
    retrospective: bool = False,
) -> int:
    """Create or update a grouped alert. Repeats of a dismissed alert are counted as
    suppressed (no re-notification); a resolved alert that recurs is reopened as new.
    Idempotent per transaction: ``alert_txids`` remembers every transaction an alert has
    counted (the ``txids`` column only shows the latest ``MAX_ALERT_TXIDS``)."""
    row = db.execute(
        "SELECT id, status, txids, occurrences FROM alerts WHERE group_key = ?", (group_key,)
    ).fetchone()
    if row is None:
        cur = db.execute(
            "INSERT INTO alerts (group_key, layer, kind, priority, status, title, created_at, "
            "updated_at, last_seen_at, retrospective, txids, evidence) "
            "VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?)",
            (group_key, layer, kind, priority, title, at, at, at, int(retrospective),
             json.dumps([txid]), json.dumps(evidence)),
        )  # fmt: skip
        aid = int(cur.lastrowid)
        db.execute("INSERT INTO alert_txids (alert_id, txid) VALUES (?, ?)", (aid, txid))
        _event(db, aid, at, "created", f"{layer}/{kind} for {txid}")
        return aid
    aid, status, txids_json, occ = row
    if not db.execute(
        "INSERT OR IGNORE INTO alert_txids (alert_id, txid) VALUES (?, ?)", (aid, txid)
    ).rowcount:
        return aid  # the same transaction never counts twice
    txids = ([*json.loads(txids_json), txid])[-MAX_ALERT_TXIDS:]
    if status == "dismissed":
        db.execute(
            "UPDATE alerts SET suppressed = suppressed + 1, last_seen_at = ?, occurrences = ?, "
            "txids = ? WHERE id = ?",
            (at, occ + 1, json.dumps(txids), aid),
        )
        _event(db, aid, at, "suppressed", txid)
        return aid
    new_status = "new" if status == "resolved" else status
    db.execute(
        "UPDATE alerts SET status = ?, priority = MIN(priority, ?), last_seen_at = ?, "
        "updated_at = ?, occurrences = ?, txids = ?, evidence = ? WHERE id = ?",
        (new_status, priority, at, at, occ + 1, json.dumps(txids), json.dumps(evidence), aid),
    )
    _event(db, aid, at, "reopened" if status == "resolved" else "occurrence", txid)
    return aid


def set_alert_status(
    db: sqlite3.Connection, alert_id: int, status: str, at: float, note: str | None = None
) -> None:
    if status not in ALERT_STATUSES:
        raise ValueError(f"status must be one of {ALERT_STATUSES}")
    cur = db.execute(
        "UPDATE alerts SET status = ?, updated_at = ?, note = COALESCE(?, note) WHERE id = ?",
        (status, at, (note or None) and note[:1000], alert_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"no alert {alert_id}")
    _event(db, alert_id, at, f"status:{status}", (note or "")[:1000])


def _event(db: sqlite3.Connection, aid: int, at: float, event: str, detail: str) -> None:
    db.execute(
        "INSERT INTO alert_events (alert_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (aid, at, event, detail[:1000]),
    )


# ---------------------------------------------------------------- read side (API)
def risk_record(db: sqlite3.Connection, txid: str) -> dict[str, Any] | None:
    row = db.execute("SELECT record FROM risk WHERE txid = ?", (txid,)).fetchone()
    return json.loads(row[0]) if row else None


def findings(
    db: sqlite3.Connection, *, max_priority: int = 4, limit: int = 50, before_id: int | None = None
) -> list[dict[str, Any]]:
    """Assessed transactions with a finding (priority 1..max_priority), newest first."""
    args: list[Any] = [max_priority]
    where = "r.priority IS NOT NULL AND r.priority <= ?"
    if before_id is not None:
        where += " AND r.tx_id < ?"
        args.append(before_id)
    rows = db.execute(
        "SELECT r.tx_id, r.record, t.lifecycle, t.lifecycle_at, t.replaced_by FROM risk r "
        f"JOIN tx t ON t.id = r.tx_id WHERE {where} ORDER BY r.tx_id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    out = []
    for tx_id, rec, lifecycle, lifecycle_at, replaced_by in rows:
        r = json.loads(rec)
        out.append(
            {
                "id": tx_id,
                "txid": r["txid"],
                "evaluated_at": r["evaluated_at"],
                "observed_at": r["observed_at"],
                "priority": r["priority"],
                "screening": {
                    "status": r["screening"].get("status"),
                    "matches": r["screening"].get("matches", []),
                },
                "graph": {
                    "status": r["graph"].get("status"),
                    "relations": r["graph"].get("relations", []),
                },
                "anomaly": {
                    "status": r["anomaly"].get("status"),
                    "level": r["anomaly"].get("level"),
                    "indicators": r["anomaly"].get("indicators", []),
                },
                "insufficient_evidence": r.get("insufficient_evidence", False),
                "errors": r.get("errors", []),
                "lifecycle": {"state": lifecycle, "at": lifecycle_at, "replaced_by": replaced_by},
            }
        )
    return out


def alerts(
    db: sqlite3.Connection, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    args: list[Any] = []
    where = ""
    if status:
        where = "WHERE status = ?"
        args.append(status)
    rows = db.execute(
        "SELECT id, group_key, layer, kind, priority, status, title, created_at, updated_at, "
        f"last_seen_at, occurrences, suppressed, retrospective, txids, evidence, note FROM alerts "
        f"{where} ORDER BY (status IN ('new', 'under_review')) DESC, priority ASC, "
        "last_seen_at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    keys = ("id", "group_key", "layer", "kind", "priority", "status", "title", "created_at",
            "updated_at", "last_seen_at", "occurrences", "suppressed", "retrospective", "txids",
            "evidence", "note")  # fmt: skip
    out = []
    for r in rows:
        d = dict(zip(keys, r, strict=True))
        d["txids"] = json.loads(d["txids"])
        d["evidence"] = json.loads(d["evidence"])
        d["retrospective"] = bool(d["retrospective"])
        out.append(d)
    return out


def alert_history(db: sqlite3.Connection, alert_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT at, event, detail FROM alert_events WHERE alert_id = ? ORDER BY id", (alert_id,)
    ).fetchall()
    return [{"at": a, "event": e, "detail": d} for a, e, d in rows]


def risk_summary(db: sqlite3.Connection, since: float) -> dict[str, Any]:
    def counts(col: str) -> dict[str, int]:
        return dict(
            db.execute(
                f"SELECT {col}, COUNT(*) FROM risk WHERE evaluated_at >= ? GROUP BY {col}",
                (since,),
            ).fetchall()
        )

    by_priority = {
        str(k) if k is not None else "none": v
        for k, v in db.execute(
            "SELECT priority, COUNT(*) FROM risk WHERE evaluated_at >= ? GROUP BY priority",
            (since,),
        )
    }
    lat = [
        r[0]
        for r in db.execute(
            "SELECT latency_ms FROM risk WHERE evaluated_at >= ? ORDER BY latency_ms", (since,)
        )
    ]
    errors = db.execute(
        "SELECT COUNT(*) FROM risk WHERE evaluated_at >= ? AND n_errors > 0", (since,)
    ).fetchone()[0]
    assessed = sum(by_priority.values())
    fetched = db.execute(
        "SELECT COUNT(*) FROM tx WHERE outcome = 'fetched' AND fetched_at >= ?", (since,)
    ).fetchone()[0]
    lifecycle = dict(
        db.execute(
            "SELECT COALESCE(lifecycle, 'unknown'), COUNT(*) FROM tx WHERE outcome = 'fetched' "
            "AND fetched_at >= ? GROUP BY 1",
            (since,),
        ).fetchall()
    )
    alert_counts = dict(
        db.execute("SELECT status, COUNT(*) FROM alerts GROUP BY status").fetchall()
    )

    def pct(q: float) -> float | None:
        return round(lat[min(len(lat) - 1, int(q * (len(lat) - 1) + 0.5))], 3) if lat else None

    return {
        "since": since,
        "fetched": fetched,
        "assessed": assessed,
        "not_assessed": max(0, fetched - assessed),
        "by_priority": by_priority,
        "screening": counts("screening"),
        "graph": counts("graph"),
        "anomaly": counts("anomaly"),
        "supervised": counts("supervised"),
        "assessments_with_layer_errors": errors,
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": lat[-1] if lat else None},
        "lifecycle": lifecycle,
        "alerts_by_status": alert_counts,
        "alerts_by_status_scope": "all_time",  # the queue is not windowed
        "sources": source_status(db),
    }


def retro_screen(db: sqlite3.Connection, version_id: int, at: float) -> int:
    """Stored transactions that involve an address this version newly made available, and
    whose own assessment did not match it under the same authority.

    That covers transactions observed before ChainWatch had the listing, and those assessed
    in the moments between a snapshot being stored and the collector loading it. Their
    point-in-time assessments are left unchanged; each match is a separate alert marked
    ``retrospective`` (idempotent: running it again counts nothing twice). Returns the
    number of such matches."""
    rows = db.execute(
        "SELECT t.id, t.txid, a.role, a.idx, i.address, i.source_id, i.category, i.label, "
        "i.reference, i.listed_on, i.available_at, i.details, v.sha256 FROM indicators i "
        "JOIN indicator_versions v ON v.id = i.version_added "
        "JOIN tx_address a ON a.address = i.address JOIN tx t ON t.id = a.tx_id "
        "WHERE i.version_added = ?",
        (version_id,),
    ).fetchall()
    found = 0
    for row in rows:
        tx_id, txid, role, idx, address, source_id, category, label, reference = row[:9]
        listed_on, available_at, sha = row[9], row[10], row[12]
        details = json.loads(row[11] or "{}")
        authority = authority_of(source_id)
        rec = db.execute("SELECT record FROM risk WHERE tx_id = ?", (tx_id,)).fetchone()
        matched = json.loads(rec[0])["screening"].get("matches", []) if rec else []
        if any(m["address"] == address and authority_of(m["source_id"]) == authority
               for m in matched):  # fmt: skip
            continue  # its own assessment already had this listing
        found += 1
        verb = "spent from" if role == "in" else "paid to"
        upsert_alert(
            db,
            group_key=f"retro:{authority}:{role}:{address}",
            layer="screening",
            kind="retrospective_source_match",
            priority=1,
            title=f"Transaction assessed without this listing {verb} an address now listed by "
            f"{source_title(source_id, label)}",
            txid=txid,
            evidence={  # the same fields as a live match, plus the snapshot that added it
                "address": address,
                "role": role,
                "index": idx,
                "source_id": source_id,
                "authority": authority,
                "category": category,
                "label": label,
                "reference": reference,
                "listed_on": listed_on,
                "available_at": available_at,
                "source_version_id": version_id,
                "source_version": sha[:12],
                "programs": details.get("programs"),
                "party_designated_on": details.get("party_designated_on"),
                "assessed": rec is not None,
                "retrospective": True,
            },
            at=at,
            retrospective=True,
        )
    return found
