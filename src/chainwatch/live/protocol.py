"""mempool.space WebSocket protocol for the ``track-mempool-txids`` subscription.

Contract (mempool/mempool backend, ``websocket-handler.ts`` and ``mempool.interfaces.ts``,
checked at upstream commit 4cf96d2, 2026-10-13):

    client -> {"track-mempool-txids": true}
    server -> {"mempool-txids": {"sequence": int, "added": [txid], "removed": [txid],
                                 "mined": [txid], "replaced": [{"replaced": txid, "by": txid}]}}

Other keys can share a message (e.g. ``pong``); they are ignored. Every field is validated:
a malformed message yields ``None`` and invalid txids are dropped, so nothing from the
network reaches storage unchecked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SUBSCRIBE = json.dumps({"track-mempool-txids": True})
PING = json.dumps({"action": "ping"})
TXID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MESSAGE_BYTES = 4_000_000  # far above a normal delta; bigger frames are refused


def valid_txid(value: object) -> bool:
    return isinstance(value, str) and TXID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class MempoolDelta:
    sequence: int | None
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()
    mined: tuple[str, ...] = ()
    replaced: tuple[tuple[str, str], ...] = ()  # (replaced txid, replacing txid)
    invalid: int = 0  # entries discarded because they were not valid txids


@dataclass
class ParseResult:
    delta: MempoolDelta | None = None
    pong: bool = False
    malformed: bool = False
    ignored_keys: list[str] = field(default_factory=list)


def _txids(values: object) -> tuple[tuple[str, ...], int]:
    if not isinstance(values, list):
        return (), (0 if values is None else 1)
    good = tuple(v for v in values if valid_txid(v))
    return good, len(values) - len(good)


def parse_message(raw: str | bytes) -> ParseResult:
    """Parse one WebSocket frame. Never raises on bad input."""
    if isinstance(raw, bytes | bytearray):
        if len(raw) > MAX_MESSAGE_BYTES:
            return ParseResult(malformed=True)
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ParseResult(malformed=True)
    if len(raw) > MAX_MESSAGE_BYTES:
        return ParseResult(malformed=True)
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError):
        return ParseResult(malformed=True)
    if not isinstance(msg, dict):
        return ParseResult(malformed=True)

    out = ParseResult(pong="pong" in msg)
    out.ignored_keys = sorted(k for k in msg if k not in ("mempool-txids", "pong"))
    body = msg.get("mempool-txids")
    if body is None:
        return out
    if not isinstance(body, dict):
        out.malformed = True
        return out

    seq = body.get("sequence")
    sequence = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
    added, bad_a = _txids(body.get("added"))
    removed, bad_r = _txids(body.get("removed"))
    mined, bad_m = _txids(body.get("mined"))
    replaced: list[tuple[str, str]] = []
    bad_x = 0
    rep = body.get("replaced")
    if isinstance(rep, list):
        for item in rep:
            if (
                isinstance(item, dict)
                and valid_txid(item.get("replaced"))
                and valid_txid(item.get("by"))
            ):
                replaced.append((item["replaced"], item["by"]))
            else:
                bad_x += 1
    elif rep is not None:
        bad_x += 1
    out.delta = MempoolDelta(
        sequence=sequence,
        added=added,
        removed=removed,
        mined=mined,
        replaced=tuple(replaced),
        invalid=bad_a + bad_r + bad_m + bad_x,
    )
    return out
