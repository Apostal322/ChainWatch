"""Synthetic risk rows for the paged findings / alert-queue tests and the benchmark
(scripts/bench_risk_pages.py). Rows are written straight into the collector's tables, in the
shapes the engine stores (src/chainwatch/risk/engine.py), with known ground truth per row.
Constructed data only: no network, no real transaction."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass, field

from chainwatch.live.store import LiveStore
from chainwatch.risk import persistence as rp

# (state, weight): the mix of assessment outcomes generated
STATES = (
    ("none", 0.80),
    ("source_match", 0.03),
    ("relation", 0.02),
    ("anomaly_strong", 0.02),
    ("anomaly_notable", 0.06),
    ("insufficient", 0.03),
    ("screening_unavailable", 0.02),
    ("failed", 0.02),
)
PRIORITY = {"source_match": 1, "relation": 2, "anomaly_strong": 3, "anomaly_notable": 4}
SOURCES = ("ofac-sdn", "ofac-sdn-mirror")


def hexid(i: int, salt: str = "t") -> str:
    return hashlib.sha256(f"{salt}{i}".encode()).hexdigest()


def address(i: int) -> str:
    return "bc1q" + hashlib.sha256(f"a{i}".encode()).hexdigest()[:38]


@dataclass
class Truth:
    findings: list[dict] = field(default_factory=list)  # {"id", "state", "source", "at"}
    alerts: list[dict] = field(default_factory=list)  # the alert row as stored


def record(txid: str, state: str, at: float, source: str | None, rng: random.Random) -> dict:
    screening = {"status": "NO_KNOWN_SCREENING_MATCH", "matches": []}
    graph = {"status": "NO_KNOWN_RELATION", "relations": []}
    anomaly = {"status": "NO_ANOMALY", "level": None, "indicators": []}
    errors: list[dict] = []
    if state == "source_match":
        screening = {
            "status": "SCREENING_MATCH",
            "matches": [
                {
                    "role": "out",
                    "index": 0,
                    "address": address(rng.randrange(500)),
                    "source_id": source,
                    "authority": "ofac-sdn",
                    "label": "TEST",
                    "category": "sanctions",
                    "reference": "x",
                    "available_at": at - 10,
                }
            ],
        }
    elif state == "relation":
        graph = {
            "status": "RELATION_FOUND",
            "relations": [
                {
                    "type": "spends_output_of_matched_tx",
                    "distance": 2,
                    "path": [address(rng.randrange(500)), hexid(rng.randrange(9999), "p")],
                    "via_parent": hexid(rng.randrange(9999), "p"),
                    "source_id": source,
                    "label": "TEST",
                    "reference": "x",
                }
            ],
        }
    elif state.startswith("anomaly"):
        level = "strong" if state == "anomaly_strong" else "notable"
        anomaly = {
            "status": "ANOMALY",
            "level": level,
            "indicators": [
                {
                    "measurement": "vsize",
                    "label": "Unusually large",
                    "group": "size",
                    "side": "high",
                    "value": 99999,
                    "tail_share": 0.0005,
                }
            ],
        }
    elif state == "insufficient":
        screening = {"status": "SCREENING_DATA_UNAVAILABLE", "matches": []}
        anomaly = {"status": "INSUFFICIENT_BASELINE", "level": None, "indicators": []}
    elif state == "screening_unavailable":
        screening = {"status": "SCREENING_DATA_UNAVAILABLE", "matches": []}
    elif state == "failed":
        screening, graph = {"status": "ERROR"}, {"status": "ERROR"}
        anomaly = {"status": "ERROR"}
        errors = [{"layer": "assessment", "error": "OperationalError: database is locked"}]
    level = PRIORITY.get(state)
    return {
        "txid": txid,
        "engine_version": "test",
        "feature_schema": "test",
        "observed_at": at - 1,
        "fetched_at": at - 0.5,
        "evaluated_at": at,
        "screening": screening,
        "graph": graph,
        "anomaly": anomaly,
        "supervised": {"status": "NO_VALIDATED_MODEL"},
        "priority": {
            "level": level,
            "label": "x",
            "semantics": "ordinal review priority, not a probability of wrongdoing",
        },
        "insufficient_evidence": state in ("insufficient", "failed"),
        "errors": errors,
        "latency_ms": 1.0,
    }


def seed(path, n_tx: int, n_alerts: int, *, start: float = 1_800_000_000.0, seed: int = 1):
    """``n_tx`` assessed observations one second apart and ``n_alerts`` grouped alerts.
    Returns (LiveStore, Truth)."""
    rng = random.Random(seed)
    store = LiveStore(path)
    db: sqlite3.Connection = store.db
    truth = Truth()
    states = [s for s, _ in STATES]
    weights = [w for _, w in STATES]
    with rp.transaction(db):
        for i in range(n_tx):
            txid = hexid(i)
            at = start + i
            state = rng.choices(states, weights)[0]
            source = rng.choice(SOURCES) if state in ("source_match", "relation") else None
            cur = db.execute(
                "INSERT INTO tx (txid, first_seen, fetched_at, outcome, fee_rate, vsize, features) "
                "VALUES (?, ?, ?, 'fetched', 2.0, 200, '{}')",
                (txid, at - 1, at - 0.5),
            )
            rp.save_risk(db, cur.lastrowid, record(txid, state, at, source, rng))
            truth.findings.append({"id": cur.lastrowid, "state": state, "source": source, "at": at})
        kinds = (
            ("screening", "source_match", 1, "screening:ofac-sdn:out:{}"),
            ("screening", "retrospective_source_match", 1, "retro:ofac-sdn:in:{}"),
            ("graph", "spends_output_of_matched_tx", 2, "relation:ofac-sdn:{}"),
            ("anomaly", "vsize", 3, "anomaly:vsize:{}"),
            # a key written before v0.8 named the source id, not the authority
            ("screening", "source_match", 1, "screening:ofac-sdn-mirror:out:{}"),
        )
        for j in range(n_alerts):
            layer, kind, prio, key = kinds[j % len(kinds)]
            created = start + rng.uniform(0, max(1, n_tx))
            seen = created + rng.uniform(0, 3600)
            status = rng.choice(rp.ALERT_STATUSES)
            txids = [hexid(rng.randrange(max(1, n_tx))) for _ in range(rng.randint(1, 50))]
            row = {
                "group_key": key.format(j),
                "layer": layer,
                "kind": kind,
                "priority": prio,
                "status": status,
                "title": f"{kind} {j}",
                "created_at": created,
                "updated_at": seen,
                "last_seen_at": seen,
                "retrospective": int(kind.startswith("retro")),
                "txids": json.dumps(txids),
                "evidence": json.dumps({"txid": txids[-1]}),
            }
            cur = db.execute(
                f"INSERT INTO alerts ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
            truth.alerts.append({**row, "id": cur.lastrowid})
    return store, truth


def triage_key(a: dict) -> tuple:
    return (
        0 if a["status"] in ("new", "under_review") else 1,
        a["priority"],
        -a["last_seen_at"],
        -a["id"],
    )
