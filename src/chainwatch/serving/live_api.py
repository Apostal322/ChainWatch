"""Read-only /v1/live API (Phase 5, docs/LIVE_MONITOR.md; cloud: docs/LIVE_CLOUD.md).

Two backends serve the same routes:
* ``LocalBackend`` reads the collector's SQLite file read-only (``mode=ro``). Used by the
  edge API on the collector VM and for local development.
* ``RemoteBackend`` proxies to that edge API over HTTPS with a bearer token. Used by the
  public dashboard, which therefore never touches the database or mempool.space.

Isolation from the research service:
* Every route answers 404 ``live_disabled`` unless ``live.enabled`` is true.
* With ``token`` set (the edge API), every /v1/live route requires
  ``Authorization: Bearer <token>`` (constant-time comparison).
* Responses say where the data came from; non-mempool.space sources are labelled as mock.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
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
MAX_REMOTE_BYTES = 2_000_000
RECENT_WINDOW = 100  # rows polled for the event stream (shared by all clients)


class LiveDisabled(Exception):
    pass


class LiveUnauthorized(Exception):
    pass


class UpstreamUnavailable(Exception):
    """The remote edge API could not be reached or answered unexpectedly."""


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


# --------------------------------------------------------------------------- backends
class LocalBackend:
    kind = "local"

    def __init__(self, live: LiveConfig) -> None:
        self.live = live
        self.reader = LiveReader(live.db_path)

    def status(self, now: float) -> dict[str, Any]:
        live, st = self.live, self.reader.status()
        counters = (st or {}).get("counters", {})
        received = counters.get("received", 0)
        settings = (st or {}).get("settings") or {}
        source = settings.get("ws_url") or live.ws_url
        real = is_public_mempool(source)
        return {
            "mode": "live",
            "via": "local database",
            "source_url": source,
            "source_is_public_mempool": real,
            "data_origin": "real Bitcoin network (mempool.space public API)"
            if real
            else "NON-PRODUCTION SOURCE (local mock or custom endpoint): not verified network data",
            "connection": connection_state(st, now, live.heartbeat_s),
            "collector": st,
            "last_observation_at": self.reader.last_observation_at(),
            "availability_24h": self.reader.availability(86_400, now),
            "coverage": {
                "sample_rate": settings.get("sample_rate", live.sample_rate),
                "fetch_per_minute": settings.get("fetch_per_minute", live.fetch_per_minute),
                "fetched_share_of_announced": (
                    round(counters.get("fetched", 0) / received, 4) if received else None
                ),
            },
            "explorer_tx_url": live.explorer_tx_url,
            "indicators": INDICATORS,
            "notice": LIVE_NOTICE if real else MOCK_NOTICE,
            "server_time": now,
        }

    def transactions(self, **q: Any) -> dict[str, Any]:
        total, rows = self.reader.list(**q)
        return {"total": total, "offset": q.get("offset", 0), "rows": rows}

    def transaction(self, txid: str) -> dict[str, Any] | None:
        tx = self.reader.get(txid)
        if tx is not None:
            tx["explorer_url"] = self.live.explorer_tx_url + txid
            tx["flag_descriptions"] = {f: INDICATORS[f] for f in tx["flags"] if f in INDICATORS}
        return tx

    def stats(self, window_minutes: int) -> dict[str, Any]:
        return self.reader.stats(window_minutes)

    def outages(self, hours: float, limit: int) -> list[dict[str, Any]]:
        return self.reader.outages(time.time() - hours * 3600, limit)

    def recent(self) -> list[dict[str, Any]]:
        return self.reader.list(limit=RECENT_WINDOW, sort="id", descending=True, outcome=None)[1]

    def max_id(self) -> int:
        return self.reader.max_id()


class RemoteBackend:
    """HTTPS client for the collector's edge API, with a short shared cache.

    Every distinct query is fetched at most once per ``remote_cache_s`` however many
    browsers are connected, so the load on the collector VM is bounded.
    """

    kind = "remote"

    def __init__(
        self, live: LiveConfig, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        url = urlparse(live.remote_url or "")
        local = url.hostname in ("127.0.0.1", "localhost", "testserver")
        if url.scheme != "https" and not local:
            raise ValueError("live.remote_url must use https:// (plain http only for localhost)")
        self.live = live
        self.base = live.remote_url.rstrip("/")
        self.client = httpx.Client(
            timeout=httpx.Timeout(live.remote_timeout_s),
            headers={"authorization": f"Bearer {token}", "accept": "application/json"},
            transport=transport,
            follow_redirects=False,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.live.remote_cache_s:
                return hit[1]
        try:
            r = self.client.get(self.base + path, params=params)
        except httpx.HTTPError as e:
            raise UpstreamUnavailable(type(e).__name__) from e
        if len(r.content) > MAX_REMOTE_BYTES:
            raise UpstreamUnavailable("response too large")
        if r.status_code == 503 and _code(r) == "live_no_data":
            raise NoLiveData(self.base)
        if r.status_code == 404 and path.startswith("/v1/live/transactions/"):
            body = None
        elif r.status_code != 200:
            raise UpstreamUnavailable(f"edge API answered {r.status_code} ({_code(r)})")
        else:
            body = r.json()
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = (now, body)
        return body

    def status(self, now: float) -> dict[str, Any]:
        body = dict(self._get("/v1/live/status"))
        body["via"] = "remote collector (edge API)"
        body["server_time"] = now
        return body

    def transactions(self, **q: Any) -> dict[str, Any]:
        params = dict(q)
        params["order"] = "desc" if params.pop("descending", True) else "asc"
        params["outcome"] = params.get("outcome") or "all"
        body = self._get("/v1/live/transactions", params)
        return {k: body[k] for k in ("total", "offset", "rows")}

    def transaction(self, txid: str) -> dict[str, Any] | None:
        body = self._get(f"/v1/live/transactions/{txid}")
        return None if body is None else {k: v for k, v in body.items() if k != "mode"}

    def stats(self, window_minutes: int) -> dict[str, Any]:
        body = self._get("/v1/live/stats", {"window_minutes": window_minutes})
        return {k: v for k, v in body.items() if k != "mode"}

    def outages(self, hours: float, limit: int) -> list[dict[str, Any]]:
        return self._get("/v1/live/outages", {"hours": hours, "limit": limit})["outages"]

    def recent(self) -> list[dict[str, Any]]:
        q = {"limit": RECENT_WINDOW, "sort": "id", "order": "desc", "outcome": "all"}
        return self._get("/v1/live/transactions", q)["rows"]

    def max_id(self) -> int:
        rows = self.recent()
        return max((r["id"] for r in rows), default=0)


def _code(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return "unknown"


def make_backend(live: LiveConfig) -> LocalBackend | RemoteBackend:
    if not live.remote_url:
        return LocalBackend(live)
    token = os.environ.get(live.api_token_env, "")
    if len(token) < 32:
        raise ValueError(
            f"live.remote_url is set but ${live.api_token_env} is missing or shorter than "
            "32 characters"
        )
    return RemoteBackend(live, token)


# --------------------------------------------------------------------------- routes
def install_live_routes(
    app: FastAPI,
    live: LiveConfig,
    error,
    *,
    token: str | None = None,
    backend: LocalBackend | RemoteBackend | None = None,
) -> None:
    """Register /v1/live/*. ``token`` makes every route require a bearer token (edge API)."""
    be = backend or (make_backend(live) if live.enabled else LocalBackend(live))
    streams = {"open": 0}

    @app.exception_handler(LiveDisabled)
    async def _disabled(request: Request, exc: LiveDisabled):
        return error(
            404,
            "live_disabled",
            "live monitoring is disabled in this deployment (opt-in: configs/live.yaml)",
            request,
        )

    @app.exception_handler(LiveUnauthorized)
    async def _unauthorized(request: Request, exc: LiveUnauthorized):
        resp = error(401, "unauthorized", "missing or invalid bearer token", request)
        resp.headers["www-authenticate"] = "Bearer"
        return resp

    @app.exception_handler(NoLiveData)
    async def _nodata(request: Request, exc: NoLiveData):
        return error(
            503, "live_no_data", "no live data yet: start the collector (live-collect)", request
        )

    @app.exception_handler(UpstreamUnavailable)
    async def _upstream(request: Request, exc: UpstreamUnavailable):
        return error(502, "live_upstream_unavailable", "remote collector unavailable", request)

    def guard(request: Request) -> None:
        if not live.enabled:
            raise LiveDisabled
        if token is not None:
            header = request.headers.get("authorization", "")
            given = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(given.encode(), token.encode()):
                raise LiveUnauthorized

    r = APIRouter(prefix="/v1/live", tags=["live"], dependencies=[Depends(guard)])

    @r.get("/status")
    def live_status() -> dict[str, Any]:
        return be.status(time.time())

    @r.get("/transactions")
    def live_transactions(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=100_000),
        sort: str = Query("id"),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        min_fee_rate: float | None = Query(None, ge=0),
        max_fee_rate: float | None = Query(None, ge=0),
        flag: str | None = Query(None),
        outcome: str = Query("fetched", pattern="^(fetched|failed|all)$"),
        after_id: int | None = Query(None, ge=0),
    ) -> dict[str, Any]:
        if sort not in SORTABLE:
            raise HTTPException(400, f"sort must be one of {sorted(SORTABLE)}")
        if flag is not None and flag not in INDICATORS:
            raise HTTPException(400, f"unknown flag {flag!r}")
        body = be.transactions(
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order == "desc",
            min_fee_rate=min_fee_rate,
            max_fee_rate=max_fee_rate,
            flag=flag,
            outcome=None if outcome == "all" else outcome,
            after_id=after_id,
        )
        return {"mode": "live", **body}

    @r.get("/transactions/{txid}")
    def live_transaction(txid: str) -> dict[str, Any]:
        if not valid_txid(txid):
            raise HTTPException(400, "txid must be 64 lowercase hex characters")
        tx = be.transaction(txid)
        if tx is None:
            raise HTTPException(404, "transaction not observed by this collector")
        return {"mode": "live", **tx}

    @r.get("/stats")
    def live_stats(window_minutes: int = Query(60, ge=1, le=1440)) -> dict[str, Any]:
        return {"mode": "live", **be.stats(window_minutes)}

    @r.get("/outages")
    def live_outages(
        hours: float = Query(24, gt=0, le=720), limit: int = Query(50, ge=1, le=200)
    ) -> dict[str, Any]:
        """Collection gaps: collector down, WebSocket disconnected, missed mempool updates."""
        return {"mode": "live", "outages": be.outages(hours, limit)}

    @r.get("/events")
    async def live_events(request: Request, since: int | None = Query(None, ge=0)):
        """Server-sent events: ``tx`` for each newly stored observation, ``status`` every 5 s."""
        if streams["open"] >= live.sse_max_clients:
            raise HTTPException(503, "too many live event streams")
        header = request.headers.get("last-event-id", "")
        start = int(header) if header.isdigit() else since
        if start is None:
            start = await asyncio.to_thread(be.max_id)  # only new observations
        streams["open"] += 1

        async def gen():
            last, last_status = start, 0.0
            deadline = time.monotonic() + live.sse_max_seconds
            try:
                yield "retry: 3000\n\n"
                while time.monotonic() < deadline:
                    if await request.is_disconnected():
                        break
                    rows = await asyncio.to_thread(be.recent)
                    for row in sorted((x for x in rows if x["id"] > last), key=lambda x: x["id"]):
                        last = row["id"]
                        yield f"id: {last}\nevent: tx\ndata: {json.dumps(row)}\n\n"
                    now = time.time()
                    if now - last_status >= 5:
                        body = await asyncio.to_thread(be.status, now)
                        yield f"event: status\ndata: {json.dumps(body['connection'])}\n\n"
                        last_status = now
                    await asyncio.sleep(1.0)
            except (NoLiveData, UpstreamUnavailable):
                yield 'event: status\ndata: "disconnected"\n\n'
            finally:
                streams["open"] -= 1

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    app.include_router(r)
