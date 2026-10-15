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
import io
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chainwatch.risk.addresses import InvalidAddress, parse_address

MAX_SOURCE_BYTES = 400_000_000  # sdn_advanced.xml is ~100 MB in 2026; refuse absurd inputs
MAX_RECORDS = 1_000_000
MAX_ADDRESSES_PER_PARTY = 10_000  # the largest real party has a few hundred
OFAC_FEATURE_TYPE = "Digital Currency Address - XBT"
OFAC_NS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML"


@dataclass(frozen=True)
class IndicatorRecord:
    address: str  # canonical (see addresses.canonical)
    category: str  # e.g. "sanctions"
    label: str  # human-readable listing, e.g. the SDN party name; "" if the source has none
    reference: str  # source-specific identifier, e.g. "SDN profile 12345"
    # The source's own date for THIS address listing (ISO), only when the source states one
    # per address. OFAC does not: its entry date belongs to the party (an address can be
    # added to a party years later), so it goes to details["party_designated_on"] instead.
    # Point-in-time screening uses ChainWatch's first snapshot containing the address.
    listed_on: str | None = None
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
    text = content.decode("utf-8-sig", errors="strict")  # tolerate a UTF-8 byte-order mark
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
    content: bytes | Path,
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
    entry date and programme comments.

    The file is ~100 MB, so it is parsed as a stream (``iterparse``) and each party is freed
    once read: memory stays far below the indicator service's 400 MB limit. DTDs and entity
    declarations are refused; ElementTree never fetches external resources.
    """
    if isinstance(content, Path):
        size = content.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise ValueError("indicator source too large")
        with content.open("rb") as fh:
            _require_utf8(fh.read(1024))
        digest, has_dtd = _scan_file(content)
        stream: Any = content.open("rb")
    else:
        size = len(content)
        if size > MAX_SOURCE_BYTES:
            raise ValueError("indicator source too large")
        _require_utf8(content[:1024])
        digest = hashlib.sha256(content).hexdigest()
        has_dtd = any(m in content for m in _DTD_MARKERS)
        stream = io.BytesIO(content)
    if has_dtd:
        # the whole file is checked, not just its head: a long comment may precede a DOCTYPE
        raise ValueError("DTDs/entities are not accepted in indicator sources")

    issued: str | None = None
    feature_ids: set[str] = set()
    parties: list[tuple[str, str, list[str]]] = []  # (profile id, name, raw addresses)
    pending: dict[str, list[str]] = {}  # raw addresses per profile of the open party
    entries: dict[str, dict[str, Any]] = {}
    stack: list[str] = []  # open element names: Sanctions / section / record / ...
    parents: list[ET.Element] = []  # the open elements themselves
    try:
        for event, el in ET.iterparse(stream, events=("start", "end")):
            tag = _local(el.tag)
            if event == "start":
                if not stack and tag != "Sanctions":
                    raise ValueError(f"unexpected root element {tag!r}")
                stack.append(tag)
                parents.append(el)
                continue
            stack.pop()
            parents.pop()
            if tag == "DateOfIssue":
                issued = _date(el)
            elif tag == "FeatureType":
                if (el.text or "").strip() == OFAC_FEATURE_TYPE:
                    feature_ids.add(el.get("ID", ""))
            elif tag == "Feature" and len(parents) >= 1:
                # read (Bitcoin) or drop (anything else) each feature as it ends, so even a
                # party with a million features never sits in memory
                if el.get("FeatureTypeID") in feature_ids:
                    got = pending.setdefault(parents[-1].get("ID") or "", [])
                    got += [
                        (d.text or "").strip()
                        for d in el.iter()
                        if _local(d.tag) == "VersionDetail" and (d.text or "").strip()
                    ]
                    if len(got) > MAX_ADDRESSES_PER_PARTY:
                        raise ValueError("implausible number of addresses for one party")
                if len(parents[-1]) and parents[-1][-1] is el:
                    del parents[-1][-1]
                el.clear()
            elif tag == "DistinctParty":
                for profile in el:
                    if _local(profile.tag) != "Profile":
                        continue
                    raw = pending.pop(profile.get("ID") or "", [])
                    if raw:
                        pid = profile.get("ID") or el.get("FixedRef") or ""
                        parties.append((pid, _primary_name(profile), raw))
                pending.clear()
            elif tag == "SanctionsEntry":
                dates = [d for ev in el if _local(ev.tag) == "EntryEvent"
                         for d in [_date(_child(ev, "Date"))] if d]  # fmt: skip
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
            if len(stack) == 2 and stack[1] not in _KEEP_SECTIONS:
                # a whole record of a top-level section (a Location, IDRegDocument,
                # DistinctParty, SanctionsEntry, or a section this parser does not know) has
                # been read: free it, or unused sections pile up (the real 122 MB file peaked
                # at 206 MB of Python memory before this, 10 MB after).
                el.clear()
    finally:
        stream.close()
    if not feature_ids:
        raise ValueError(f"no FeatureType {OFAC_FEATURE_TYPE!r} in this file")

    # One address can be listed under several parties (e.g. 1H939dom... under profiles 38419
    # and 38420 in the 2026-01-30 file): keep every party, not just the first.
    by_addr: dict[str, list[dict[str, Any]]] = {}
    rejected: list[tuple[str, str]] = []
    for pid, name, raw_addresses in parties:
        entry = entries.get(pid, {})
        for raw in raw_addresses:
            addr = _validated(raw, rejected)
            if addr is None:
                continue
            party = {"profile_id": pid, "name": name,
                     "party_designated_on": entry.get("entry_date"),
                     "programs": entry.get("programs", [])}  # fmt: skip
            if party not in by_addr.setdefault(addr, []):
                by_addr[addr].append(party)
            if len(by_addr) > MAX_RECORDS:
                raise ValueError("too many records")
    records = {
        addr: IndicatorRecord(
            address=addr,
            category="sanctions",
            label="; ".join(pt["name"] for pt in ps if pt["name"])[:300],
            reference="; ".join(f"OFAC SDN profile {pt['profile_id']}" for pt in ps)[:300],
            listed_on=None,
            details={
                "parties": ps,
                "programs": sorted({p for pt in ps for p in pt["programs"]}),
                "party_designated_on": min(
                    (pt["party_designated_on"] for pt in ps if pt["party_designated_on"]),
                    default=None,
                ),
            },
        )
        for addr, ps in by_addr.items()
    }
    return Snapshot(
        source_id=source_id,
        method="ofac-sdn-advanced-xml",
        url=url,
        licence="US government work (public domain)",
        retrieved_at=time.time() if retrieved_at is None else retrieved_at,
        published_at=_iso_to_ts(issued),
        sha256=digest,
        records=sorted(records.values(), key=lambda r: r.address),
        rejected=rejected,
    )


# top-level sections whose children are read when the section itself ends (not records)
_KEEP_SECTIONS = {"DateOfIssue"}
_DECLARED_ENCODING = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


def _require_utf8(head: bytes) -> None:
    """Only UTF-8 is accepted: the DTD/entity scan works on bytes, so a UTF-16 document could
    hide a DOCTYPE from it."""
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head:
        raise ValueError("indicator sources must be UTF-8 (UTF-16/32 refused)")
    m = _DECLARED_ENCODING.search(head)
    if m and m.group(1).lower() not in (b"utf-8", b"utf8"):
        raise ValueError(f"indicator sources must be UTF-8, not {m.group(1).decode()!r}")


_DTD_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


def _scan_file(path: Path) -> tuple[str, bool]:
    """SHA-256 of the file and whether it contains a DTD/entity declaration anywhere
    (chunks overlap so a marker split across a boundary is still found)."""
    h = hashlib.sha256()
    found, tail = False, b""
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            window = tail + chunk
            found = found or any(m in window for m in _DTD_MARKERS)
            tail = chunk[-16:]
    return h.hexdigest(), found


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
