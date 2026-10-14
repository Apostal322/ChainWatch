"""Risk-indicator sources: versioned, provenance-carrying snapshots of listed addresses.

An indicator says only that a *named source* lists an address. Being listed by OFAC is a
legal sanctions designation, not proof that a given transaction is fraudulent; a public
abuse report is an allegation. Every record keeps where it came from and when ChainWatch
learned it, so screening can be point-in-time correct (docs/RISK_ENGINE.md section 2).

External data is untrusted input: it is parsed (never evaluated), every address is
validated with ``chainwatch.risk.addresses``, invalid entries are rejected with a reason,
and sizes are bounded. No field from a source can change configuration or code paths.
"""

from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from chainwatch.risk.addresses import InvalidAddress, parse_address

MAX_SOURCE_BYTES = 400_000_000  # sdn_advanced.xml is ~100 MB in 2026; refuse absurd inputs
MAX_RECORDS = 1_000_000
OFAC_FEATURE_TYPE = "Digital Currency Address - XBT"
OFAC_NS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML"


@dataclass(frozen=True)
class IndicatorRecord:
    address: str  # canonical (see addresses.canonical)
    category: str  # e.g. "sanctions"
    label: str  # human-readable listing, e.g. the SDN party name; "" if the source has none
    reference: str  # source-specific identifier, e.g. "SDN profile 12345"
    listed_on: str | None = None  # the source's own listing date (ISO), when it states one
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    source_id: str
    method: str  # how it was obtained/parsed, e.g. "ofac-sdn-advanced-xml"
    url: str
    licence: str
    retrieved_at: float  # when ChainWatch obtained this content (availability time)
    published_at: float | None  # the source's own date of issue, if it states one
    sha256: str  # of the raw content: the snapshot's version identifier
    records: list[IndicatorRecord]
    rejected: list[tuple[str, str]]  # (raw value, reason)

    def summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "method": self.method,
            "url": self.url,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "records": len(self.records),
            "rejected": len(self.rejected),
        }


def _validated(raw: str, rejected: list[tuple[str, str]]) -> str | None:
    try:
        return parse_address(raw).value
    except InvalidAddress as e:
        rejected.append((raw[:120], str(e)))
        return None


def parse_address_list(
    content: bytes,
    *,
    source_id: str,
    url: str,
    licence: str,
    category: str,
    label: str,
    retrieved_at: float | None = None,
    method: str = "address-list-txt",
) -> Snapshot:
    """One address per line (``#`` comments allowed), e.g. the nightly OFAC XBT extraction
    published by github.com/0xB10C/ofac-sanctioned-digital-currency-addresses (a mirror of
    the official SDN list: no per-address party names or dates)."""
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("indicator source too large")
    text = content.decode("utf-8", errors="strict")
    records: dict[str, IndicatorRecord] = {}
    rejected: list[tuple[str, str]] = []
    for line in text.splitlines():
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        addr = _validated(raw, rejected)
        if addr and addr not in records:
            records[addr] = IndicatorRecord(addr, category, label, f"{source_id}:{addr}")
        if len(records) > MAX_RECORDS:
            raise ValueError("too many records")
    return Snapshot(
        source_id=source_id,
        method=method,
        url=url,
        licence=licence,
        retrieved_at=time.time() if retrieved_at is None else retrieved_at,
        published_at=None,
        sha256=hashlib.sha256(content).hexdigest(),
        records=sorted(records.values(), key=lambda r: r.address),
        rejected=rejected,
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    parts = {_local(c.tag): (c.text or "").strip() for c in el}
    try:
        y, m, d = int(parts["Year"]), int(parts.get("Month", 1)), int(parts.get("Day", 1))
    except (KeyError, ValueError):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_ofac_sdn_advanced(
    content: bytes,
    *,
    url: str = "https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml",
    retrieved_at: float | None = None,
    source_id: str = "ofac-sdn",
) -> Snapshot:
    """Bitcoin addresses from OFAC's official ``sdn_advanced.xml`` (US government work, public
    domain), with the listed party's primary name, profile id, programmes and entry date.

    Structure (ADVANCED_XML namespace): ``ReferenceValueSets/FeatureTypeValues/FeatureType``
    names the "Digital Currency Address - XBT" feature id; each
    ``DistinctParties/DistinctParty/Profile/Feature[@FeatureTypeID]`` carries the address in
    ``FeatureVersion/VersionDetail``; ``SanctionsEntries/SanctionsEntry[@ProfileID]`` holds the
    entry date and programme comments. Parsing is streaming-free but bounded by
    ``MAX_SOURCE_BYTES``; ElementTree does not resolve external entities.
    """
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("indicator source too large")
    if b"<!DOCTYPE" in content[:4096] or b"<!ENTITY" in content[:65536]:
        raise ValueError("DTDs/entities are not accepted in indicator sources")
    root = ET.fromstring(content)
    if _local(root.tag) != "Sanctions":
        raise ValueError(f"unexpected root element {_local(root.tag)!r}")

    issued = _date(next((c for c in root if _local(c.tag) == "DateOfIssue"), None))
    feature_ids = {
        el.get("ID")
        for el in root.iter()
        if _local(el.tag) == "FeatureType" and (el.text or "").strip() == OFAC_FEATURE_TYPE
    }
    if not feature_ids:
        raise ValueError(f"no FeatureType {OFAC_FEATURE_TYPE!r} in this file")

    entries: dict[str, dict[str, Any]] = {}  # profile id -> entry facts
    for el in root.iter():
        if _local(el.tag) != "SanctionsEntry":
            continue
        dates = [
            d
            for ev in el
            if _local(ev.tag) == "EntryEvent"
            for d in [_date(_child(ev, "Date"))]
            if d
        ]
        programs = sorted(
            {
                (c.text or "").strip()
                for m in el
                if _local(m.tag) == "SanctionsMeasure"
                for c in m
                if _local(c.tag) == "Comment" and (c.text or "").strip()
            }
        )
        entries[el.get("ProfileID", "")] = {
            "entry_date": min(dates) if dates else None,
            "programs": programs,
        }

    records: dict[str, IndicatorRecord] = {}
    rejected: list[tuple[str, str]] = []
    for party in root.iter():
        if _local(party.tag) != "DistinctParty":
            continue
        for profile in party:
            if _local(profile.tag) != "Profile":
                continue
            pid = profile.get("ID") or party.get("FixedRef") or ""
            name = _primary_name(profile)
            entry = entries.get(pid, {})
            for feat in profile:
                if _local(feat.tag) != "Feature" or feat.get("FeatureTypeID") not in feature_ids:
                    continue
                for detail in feat.iter():
                    if _local(detail.tag) != "VersionDetail" or not (detail.text or "").strip():
                        continue
                    addr = _validated(detail.text.strip(), rejected)
                    if addr and addr not in records:
                        records[addr] = IndicatorRecord(
                            address=addr,
                            category="sanctions",
                            label=name,
                            reference=f"OFAC SDN profile {pid}",
                            listed_on=entry.get("entry_date"),
                            details={"programs": entry.get("programs", []), "profile_id": pid},
                        )
    return Snapshot(
        source_id=source_id,
        method="ofac-sdn-advanced-xml",
        url=url,
        licence="US government work (public domain)",
        retrieved_at=time.time() if retrieved_at is None else retrieved_at,
        published_at=_iso_to_ts(issued),
        sha256=hashlib.sha256(content).hexdigest(),
        records=sorted(records.values(), key=lambda r: r.address),
        rejected=rejected,
    )


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _primary_name(profile: ET.Element) -> str:
    for identity in profile:
        if _local(identity.tag) != "Identity":
            continue
        aliases = [a for a in identity if _local(a.tag) == "Alias"]
        aliases.sort(key=lambda a: a.get("Primary") != "true")
        for alias in aliases:
            parts = [
                (v.text or "").strip()
                for v in alias.iter()
                if _local(v.tag) == "NamePartValue" and (v.text or "").strip()
            ]
            if parts:
                return " ".join(parts)[:200]
    return ""


def _iso_to_ts(day: str | None) -> float | None:
    if not day:
        return None
    import datetime as dt

    return dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp()
