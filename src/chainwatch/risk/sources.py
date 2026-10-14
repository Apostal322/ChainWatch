"""Registry of indicator sources ChainWatch knows how to ingest (docs/RISK_DATA_FEASIBILITY.md).

Only sources whose terms allow this use are listed. Fetching is bounded (size, timeout,
HTTPS only) and the content is parsed as data; see ``indicators.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from chainwatch.risk.indicators import (
    MAX_SOURCE_BYTES,
    Snapshot,
    parse_address_list,
    parse_ofac_sdn_advanced,
)


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    kind: str
    url: str
    licence: str
    category: str
    label: str
    note: str


SOURCES: dict[str, SourceSpec] = {
    "ofac-sdn": SourceSpec(
        "ofac-sdn",
        "ofac-sdn-advanced-xml",
        "https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml",
        "US government work (public domain)",
        "sanctions",
        "",
        "Official OFAC SDN list (XML). Per-address party name, profile id, programmes and "
        "entry date. Recommended on the VM.",
    ),
    "ofac-sdn-mirror": SourceSpec(
        "ofac-sdn-mirror",
        "address-list-txt",
        "https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/"
        "lists/sanctioned_addresses_XBT.txt",
        "Data: OFAC SDN (US government work, public domain); extraction tool: MIT (0xB10C)",
        "sanctions",
        "OFAC SDN (third-party nightly extraction)",
        "Nightly extraction of the XBT addresses from sdn_advanced.xml by a third party. Same "
        "addresses, but no per-address party names or dates. Fallback when the official "
        "file cannot be fetched.",
    ),
}


def fetch(url: str, timeout: float = 120.0, dest: Path | None = None) -> bytes | Path:
    """Download over HTTPS with a size cap. With ``dest`` the body is streamed to that file
    (large sources such as the ~100 MB SDN XML never sit in memory) and the path returned."""
    if urlparse(url).scheme != "https":
        raise ValueError("indicator sources must be fetched over https")
    # OFAC's sanctions list service answers 403 to requests without a User-Agent
    headers = {
        "user-agent": "ChainWatch-indicator-refresh (+https://github.com/Apostal322/ChainWatch)"
    }
    with (
        httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client,
        client.stream("GET", url) as r,
    ):
        r.raise_for_status()
        if urlparse(str(r.url)).scheme != "https":
            raise ValueError("redirected to a non-https URL")
        size = 0
        chunks: list[bytes] = []
        sink = dest.open("wb") if dest is not None else None
        try:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise ValueError("indicator source too large")
                if sink is not None:
                    sink.write(chunk)
                else:
                    chunks.append(chunk)
        finally:
            if sink is not None:
                sink.close()
    return dest if dest is not None else b"".join(chunks)


def load(spec: SourceSpec, content: bytes | Path, url: str | None = None) -> Snapshot:
    now = time.time()
    if isinstance(content, Path) and spec.kind != "ofac-sdn-advanced-xml":
        content = content.read_bytes()  # small text lists
    if spec.kind == "ofac-sdn-advanced-xml":
        return parse_ofac_sdn_advanced(content, url=url or spec.url, retrieved_at=now,
                                       source_id=spec.source_id)  # fmt: skip
    if spec.kind == "address-list-txt":
        return parse_address_list(
            content, source_id=spec.source_id, url=url or spec.url, licence=spec.licence,
            category=spec.category, label=spec.label, retrieved_at=now,
        )  # fmt: skip
    raise ValueError(f"unknown source kind {spec.kind!r}")
