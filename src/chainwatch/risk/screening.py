"""Layer A: source-backed screening (exact address matches, point-in-time).

Outcomes (docs/RISK_ENGINE.md section 4):

* ``SCREENING_MATCH``: an address the transaction spends from (input) or pays to (output)
  is listed by an enabled source, and that listing was available to ChainWatch at the
  evaluation time.
* ``NO_KNOWN_SCREENING_MATCH``: the enabled sources produced no match for the addresses
  that could be screened. It is NOT a statement that the transaction is legitimate.
* ``SCREENING_DATA_UNAVAILABLE``: no source has been loaded, so nothing was screened.
* ``INSUFFICIENT_INFORMATION``: no address of the transaction could be screened (scripts
  without addresses, missing prevouts).

Relations (``graph``) are reported separately and only within data this collector
observed: a parent transaction that spent from a listed address. Proximity is not
ownership or culpability, and coverage is limited by sampling.
"""

from __future__ import annotations

import json
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

SCREENING_MATCH = "SCREENING_MATCH"
NO_KNOWN_SCREENING_MATCH = "NO_KNOWN_SCREENING_MATCH"
SCREENING_DATA_UNAVAILABLE = "SCREENING_DATA_UNAVAILABLE"
INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
DEFAULT_MAX_SOURCE_AGE_S = 7 * 86400  # a source not refreshed for a week is reported stale


@dataclass(frozen=True)
class Listing:
    indicator_id: int
    source_id: str
    category: str
    label: str
    reference: str
    listed_on: str | None
    details: dict[str, Any]
    available_at: float
    removed_at: float | None
    version_sha256: str

    def active_at(self, t: float) -> bool:
        return self.available_at <= t and (self.removed_at is None or t < self.removed_at)


class Screener:
    """In-memory index of indicator listings with availability intervals."""

    def __init__(
        self,
        rows: list[tuple] | None = None,
        sources: list[dict[str, Any]] | None = None,
        max_source_age_s: float = DEFAULT_MAX_SOURCE_AGE_S,
    ) -> None:
        self.index: dict[str, list[Listing]] = defaultdict(list)
        for r in rows or []:
            (iid, source_id, address, category, label, reference, listed_on, details,
             available_at, removed_at, _v, sha) = r  # fmt: skip
            self.index[address].append(
                Listing(iid, source_id, category, label, reference, listed_on,
                        json.loads(details or "{}"), available_at, removed_at, sha)
            )  # fmt: skip
        self.sources = sources or []
        self.max_source_age_s = max_source_age_s
        # earliest availability per source: before it, the source did not exist for us
        self._loaded_from = sorted(
            s.get("first_retrieved_at") or s["retrieved_at"]
            for s in self.sources
            if s.get("first_retrieved_at") or s.get("retrieved_at")
        )

    def sources_at(self, t: float) -> list[dict[str, Any]]:
        out = []
        for s in self.sources:
            checked = s.get("checked_at") or s.get("retrieved_at") or 0
            out.append(
                {
                    "source_id": s["source_id"],
                    "version": s["sha256"][:12],
                    "method": s["method"],
                    "retrieved_at": s["retrieved_at"],
                    "checked_at": checked,
                    "age_s": round(max(0.0, t - checked), 1),
                    "stale": t - checked > self.max_source_age_s,
                    "active_indicators": s.get("active_indicators"),
                }
            )
        return out

    def screen(self, addrs: dict[str, Any], at: float) -> dict[str, Any]:
        coverage = {
            "addresses_screened": len(addrs["inputs"]) + len(addrs["outputs"]),
            "inputs_without_address": addrs["inputs_without_address"],
            "inputs_missing_prevout": addrs["inputs_missing_prevout"],
            "outputs_without_address": addrs["outputs_without_address"],
            "truncated": addrs["truncated"],
        }
        sources = self.sources_at(at)
        if not self._loaded_from or bisect_right(self._loaded_from, at) == 0:
            return {"status": SCREENING_DATA_UNAVAILABLE, "matches": [], "coverage": coverage,
                    "sources": sources, "evaluated_at": at}  # fmt: skip
        matches = []
        for role, items in (("in", addrs["inputs"]), ("out", addrs["outputs"])):
            for idx, address in items:
                for lst in self.index.get(address, ()):
                    if lst.active_at(at):
                        matches.append(
                            {
                                "role": role,
                                "index": idx,
                                "address": address,
                                "source_id": lst.source_id,
                                "source_version": lst.version_sha256[:12],
                                "indicator_id": lst.indicator_id,
                                "category": lst.category,
                                "label": lst.label,
                                "reference": lst.reference,
                                "listed_on": lst.listed_on,
                                "available_at": lst.available_at,
                                "programs": lst.details.get("programs", []),
                                "party_designated_on": lst.details.get("party_designated_on"),
                            }
                        )
        if matches:
            status = SCREENING_MATCH
        elif coverage["addresses_screened"] == 0:
            status = INSUFFICIENT_INFORMATION
        else:
            status = NO_KNOWN_SCREENING_MATCH
        return {"status": status, "matches": matches, "coverage": coverage, "sources": sources,
                "evaluated_at": at}  # fmt: skip


def graph_relations(
    parent_txids: list[str], parent_matches: list[dict[str, Any]], observed_parents: int
) -> dict[str, Any]:
    """Distance-2 relations within observed data: listed address -> parent tx -> this tx."""
    limitation = (
        "Within this collector's sampled observations only: an unobserved parent is not "
        "checked. Spending an output of a transaction that spent from a listed address does "
        "not show common control, ownership or wrongdoing."
    )
    relations = [
        {
            "type": "spends_output_of_tx_funded_by_listed_address",
            "distance": 2,
            "path": [m["address"], p["parent_txid"], "this transaction"],
            "via_parent": p["parent_txid"],
            "source_id": m["source_id"],
            "label": m["label"],
            "reference": m["reference"],
        }
        for p in parent_matches
        for m in p["matches"]
    ]
    return {
        "status": "RELATION_FOUND" if relations else "NO_KNOWN_RELATION",
        "relations": relations[:20],
        "coverage": {"parents": len(parent_txids), "parents_observed": observed_parents},
        "limitation": limitation,
    }


def now() -> float:
    return time.time()
