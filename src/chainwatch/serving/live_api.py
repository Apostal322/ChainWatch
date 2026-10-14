"""Read-only /v1/live API over the collector's SQLite file (Phase 5, docs/LIVE_MONITOR.md).

Isolation from the research service:
* This module never contacts mempool.space; it only reads ``live.db_path`` (``mode=ro``).
  Collection is a separate, explicitly started process (``chainwatch live-collect``).
* Every route answers 404 ``live_disabled`` unless ``live.enabled`` is true, which is the
  default in every config except ``configs/live.yaml``.
* Responses are labelled as REAL network observations; replay data stays under /v1/replay.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from chainwatch.config import LiveConfig
from chainwatch.live.features import INDICATORS
from chainwatch.live.protocol import valid_txid
from chainwatch.live.store import SORTABLE, LiveReader, NoLiveData

LIVE_NOTICE = (
    "Real Bitcoin mempool observations received from mempool.space by this collector. "
    "Coverage is partial: only transactions announced while the collector runs are seen, "
    "and details are fetched for a sample of them under a request limit. Indicators are "
    "rule-based flags for observable properties, not fraud predictions or probabilities. "
    "No Elliptic++ model is applied to live data."
)


MOCK_NOTICE = (
    "The collector is connected to a NON-PRODUCTION source (for example scripts/mock_mempool.py), "
    "not the public mempool.space service. Everything shown here is constructed or unverified "
    "data, not real Bitcoin network observations."
)


class LiveDisabled(Exception):
    pass


def is_public_mempool(url: str | None) -> bool:
    """True only if the collector's source is the public mempool.space service."""
    host = urlparse(url or "").hostname or ""
    return host == "mempool.space" or host.endswith(".mempool.space")


def connection_state(status: dict[str, Any] | None, now: float, heartbeat_s: float) -> str:
    """connected | reconnecting | disconnected (collector not running or never started)."""
    if status is None or now - status["updated_at"] > max(15.0, 3 * heartbeat_s):
        return "disconnected"
    return {
        "connected": "connected",
        "connecting": "reconnecting",
        "reconnecting": "reconnecting",
    }.get(status["state"], "disconnected")


def install_live_routes(app: FastAPI, live: LiveConfig, error) -> None:
    reader = LiveReader(live.db_path)
    streams = {"open": 0}

    @app.exception_handler(LiveDisabled)
    async def _disabled(request: Request, exc: LiveDisabled):
        return error(
            404,
            "live_disabled",
            "live monitoring is disabled in this deployment (opt-in: configs/live.yaml)",
            request,
        )

    @app.exception_handler(NoLiveData)
    async def _nodata(request: Request, exc: NoLiveData):
        return error(
            503, "live_no_data", "no live data yet: start `chainwatch live-collect`", request
        )

    def _require() -> None:
        if not live.enabled:
            raise LiveDisabled

    def _status_body(now: float) -> dict[str, Any]:
        st = reader.status()
        counters = (st or {}).get("counters", {})
        received = counters.get("received", 0)
        source = ((st or {}).get("settings") or {}).get("ws_url") or live.ws_url
        real = is_public_mempool(source)
        return {
            "mode": "live",
            "source_url": source,
            "source_is_public_mempool": real,
            "data_origin": "real Bitcoin network (mempool.space public API)"
            if real
            else "NON-PRODUCTION SOURCE (local mock or custom endpoint): not verified network data",
            "connection": connection_state(st, now, live.heartbeat_s),
            "collector": st,
            "coverage": {
                "sample_rate": live.sample_rate,
                "fetch_per_minute": live.fetch_per_minute,
                "fetched_share_of_announced": (
                    round(counters.get("fetched", 0) / received, 4) if received else None
                ),
            },
            "explorer_tx_url": live.explorer_tx_url,
            "indicators": INDICATORS,
            "notice": LIVE_NOTICE if real else MOCK_NOTICE,
            "server_time": now,
        }

    @app.get("/v1/live/status", tags=["live"])
    def live_status() -> dict[str, Any]:
        _require()
        return _status_body(time.time())

    @app.get("/v1/live/transactions", tags=["live"])
    def live_transactions(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=100_000),
        sort: str = Query("id"),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        min_fee_rate: float | None = Query(None, ge=0),
        max_fee_rate: float | None = Query(None, ge=0),
        flag: str | None = Query(None),
        outcome: str = Query("fetched", pattern="^(fetched|failed|all)$"),
    ) -> dict[str, Any]:
        _require()
        if sort not in SORTABLE:
            raise HTTPException(400, f"sort must be one of {sorted(SORTABLE)}")
        if flag is not None and flag not in INDICATORS:
            raise HTTPException(400, f"unknown flag {flag!r}")
        total, rows = reader.list(
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order == "desc",
            min_fee_rate=min_fee_rate,
            max_fee_rate=max_fee_rate,
            flag=flag,
            outcome=None if outcome == "all" else outcome,
        )
        return {"mode": "live", "total": total, "offset": offset, "rows": rows}

    @app.get("/v1/live/transactions/{txid}", tags=["live"])
    def live_transaction(txid: str) -> dict[str, Any]:
        _require()
        if not valid_txid(txid):
            raise HTTPException(400, "txid must be 64 lowercase hex characters")
        tx = reader.get(txid)
        if tx is None:
            raise HTTPException(404, "transaction not observed by this collector")
        tx["explorer_url"] = live.explorer_tx_url + txid
        tx["flag_descriptions"] = {f: INDICATORS[f] for f in tx["flags"] if f in INDICATORS}
        return {"mode": "live", **tx}

    @app.get("/v1/live/stats", tags=["live"])
    def live_stats(window_minutes: int = Query(60, ge=1, le=1440)) -> dict[str, Any]:
        _require()
        return {"mode": "live", **reader.stats(window_minutes)}

    @app.get("/v1/live/events", tags=["live"])
    async def live_events(request: Request, since: int | None = Query(None, ge=0)):
        """Server-sent events: ``tx`` for each newly stored observation, ``status`` every 5 s."""
        _require()
        if streams["open"] >= live.sse_max_clients:
            raise HTTPException(503, "too many live event streams")
        header = request.headers.get("last-event-id", "")
        start = int(header) if header.isdigit() else since
        if start is None:
            start = await asyncio.to_thread(reader.max_id)  # only new observations
        streams["open"] += 1

        async def gen():
            last, last_status = start, 0.0
            deadline = time.monotonic() + live.sse_max_seconds
            try:
                yield "retry: 3000\n\n"
                while time.monotonic() < deadline:
                    if await request.is_disconnected():
                        break
                    _, rows = await asyncio.to_thread(
                        reader.list, limit=100, sort="id", descending=False, after_id=last,
                        outcome=None,
                    )  # fmt: skip
                    for r in rows:
                        last = r["id"]
                        yield f"id: {last}\nevent: tx\ndata: {json.dumps(r)}\n\n"
                    now = time.time()
                    if now - last_status >= 5:
                        body = await asyncio.to_thread(_status_body, now)
                        yield f"event: status\ndata: {json.dumps(body['connection'])}\n\n"
                        last_status = now
                    await asyncio.sleep(1.0)
            except NoLiveData:
                yield 'event: status\ndata: "disconnected"\n\n'
            finally:
                streams["open"] -= 1

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
