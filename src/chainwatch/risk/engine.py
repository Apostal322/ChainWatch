"""Unified risk assessment: separate layers, explicit priority, failure isolation.

Each layer runs independently; an exception in one is recorded in ``errors`` and its
status becomes ``ERROR`` while the others still report (e.g. screening keeps working if
the anomaly detector fails). Outputs are never blended into a single "fraud probability".

``priority`` is an ordinal *review* priority with fixed semantics (docs/RISK_ENGINE.md):

    1  source-backed screening match (direct: input or output address is listed)
    2  relation to a source-backed match, distance 2, within observed data
    3  strong behavioural anomaly
    4  behavioural anomaly (notable)
    None  no finding (which is not a statement of legitimacy)

Alerts are raised for priorities 1-3 and grouped (``group_key``) so repeats update one
investigation item instead of creating new ones.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from chainwatch.live.features import FEATURE_SCHEMA_VERSION
from chainwatch.risk.anomaly import RobustTailDetector
from chainwatch.risk.screening import (
    INSUFFICIENT_INFORMATION,
    SCREENING_DATA_UNAVAILABLE,
    SCREENING_MATCH,
    Screener,
    graph_relations,
)
from chainwatch.risk.sources import source_title
from chainwatch.risk.supervised import SupervisedLayer

ENGINE_VERSION = "risk-engine/1"
PRIORITY_LABELS = {
    1: "Source-backed screening match",
    2: "Relation to a source-backed match (observed data)",
    3: "Strong behavioural anomaly",
    4: "Behavioural anomaly",
}


class RiskEngine:
    def __init__(
        self,
        screener: Screener,
        detector: RobustTailDetector,
        supervised: SupervisedLayer,
        parent_lookup: Callable[[list[str]], tuple[list[dict], int]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.screener, self.detector, self.supervised = screener, detector, supervised
        self.parent_lookup = parent_lookup
        self.clock = clock

    def assess(
        self, features: dict[str, Any], addrs: dict[str, Any], parent_txids: list[str]
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        evaluated_at = self.clock()
        errors: list[dict[str, str]] = []

        def guarded(layer: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            try:
                return fn()
            except Exception as e:  # isolate the failure to this layer
                errors.append({"layer": layer, "error": f"{type(e).__name__}: {e}"[:200]})
                return {"status": "ERROR"}

        screening = guarded("screening", lambda: self.screener.screen(addrs, evaluated_at))

        def graph() -> dict[str, Any]:
            if self.parent_lookup is None or not parent_txids:
                return {
                    "status": "NOT_EVALUATED",
                    "relations": [],
                    "coverage": {"parents": len(parent_txids), "parents_observed": 0},
                }
            matches, observed = self.parent_lookup(parent_txids)
            return graph_relations(parent_txids, matches, observed)

        relation = guarded("graph", graph)
        anomaly = guarded("anomaly", lambda: self.detector.score(features))
        supervised = guarded("supervised", lambda: self.supervised.predict(features))

        level = None
        if screening.get("status") == SCREENING_MATCH:
            level = 1
        elif relation.get("status") == "RELATION_FOUND":
            level = 2
        elif anomaly.get("status") == "ANOMALY":
            level = 3 if anomaly.get("level") == "strong" else 4
        insufficient = screening.get("status") in (
            SCREENING_DATA_UNAVAILABLE,
            INSUFFICIENT_INFORMATION,
            "ERROR",
        ) and anomaly.get("status") in (
            "INSUFFICIENT_BASELINE",
            "INSUFFICIENT_INFORMATION",
            "ERROR",
        )
        record = {
            "txid": features["txid"],
            "engine_version": ENGINE_VERSION,
            "feature_schema": features.get("schema_version", FEATURE_SCHEMA_VERSION),
            # event times (docs/RISK_ENGINE.md section 1)
            "observed_at": features["first_seen"],
            "fetched_at": features["fetched_at"],
            "evaluated_at": evaluated_at,
            "screening": screening,
            "graph": relation,
            "anomaly": anomaly,
            "supervised": supervised,
            "priority": {
                "level": level,
                "label": PRIORITY_LABELS.get(level, "No finding"),
                "semantics": "ordinal review priority, not a probability of wrongdoing",
            },
            "insufficient_evidence": insufficient,
            "errors": errors,
        }
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        return record


def alert_specs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Grouped alerts for a record (priorities 1-3)."""
    out = []
    txid, at = record["txid"], record["evaluated_at"]
    for m in record["screening"].get("matches", []):
        role = "spends from" if m["role"] == "in" else "pays to"
        out.append(
            {
                "group_key": f"screening:{m['source_id']}:{m['address']}",
                "layer": "screening",
                "kind": "source_match",
                "priority": 1,
                "title": f"Transaction {role} an address listed by "
                f"{source_title(m['source_id'], m['label'])}",
                "evidence": {"match": m, "txid": txid},
            }
        )
    for r in record["graph"].get("relations", []):
        out.append(
            {
                "group_key": f"relation:{r['source_id']}:{r['path'][0]}",
                "layer": "graph",
                "kind": r["type"],
                "priority": 2,
                "title": f"Spends an output of {r['via_parent'][:12]}..., which spent from an "
                f"address listed by {source_title(r['source_id'], None)} (observed data, "
                "distance 2)",
                "evidence": {
                    "relation": r,
                    "txid": txid,
                    "limitation": record["graph"].get("limitation"),
                },
            }
        )
    an = record["anomaly"]
    if an.get("status") == "ANOMALY" and an.get("level") == "strong":
        top = an["indicators"][0]
        hour = int(at // 3600)
        out.append(
            {
                "group_key": f"anomaly:{top['measurement']}:{hour}",
                "layer": "anomaly",
                "kind": top["measurement"],
                "priority": 3,
                "title": f"{top['label']} (strong behavioural anomaly; not evidence of wrongdoing)",
                "evidence": {"indicators": an["indicators"], "txid": txid},
            }
        )
    return out
