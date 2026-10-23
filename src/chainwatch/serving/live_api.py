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
import dataclasses
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from chainwatch.config import LiveConfig
from chainwatch.live.features import INDICATORS
from chainwatch.live.protocol import valid_txid
from chainwatch.live.store import SORTABLE, LiveReader, NoLiveData
from chainwatch.risk.anomaly import DetectorConfig
from chainwatch.serving.security import client_key

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
log = logging.getLogger("chainwatch.serving.live")
MAX_REMOTE_BYTES = 2_000_000
RECENT_WINDOW = 100  # rows polled for the event stream (shared by all clients)
CACHE_MAX_KEYS = 256  # RemoteBackend LRU size
UPSTREAM_PER_S = 3.0  # RemoteBackend cache misses per second (edge allows 240/min per client)
UPSTREAM_BURST = 20
RISK_SUMMARY_TTL_S = 15.0
MAX_ROW_ID = 2**63 - 1  # SQLite INTEGER: larger ids raised OverflowError (HTTP 500)
STALE_OK_S = 60.0  # RemoteBackend serves a key's last good answer this long on failure
HOURS_STEPS = (1, 6, 24, 168, 720)  # remote queries are snapped to these (bounded key space)
WINDOW_STEPS = (60, 240, 1440)


_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


def _redact(value: Any) -> Any:
    """Drop ``user:password@`` from URLs (in settings and error texts) before they reach the
    anonymous status route: a custom source URL may carry credentials."""
    if isinstance(value, str):
        return _USERINFO.sub("", value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _snap(value: float, steps: tuple[int, ...]) -> int:
    return next((s for s in steps if value <= s), steps[-1])


EVENT_ID_RE = re.compile(r"[0-9]{1,18}")
RISK_LAYERS = {
    "screening": "Source-backed screening: exact matches of input/output addresses against "
    "versioned indicator sources (e.g. OFAC SDN). A match means the source lists the "
    "address; it is not proof of fraud. No match means only that the enabled sources did "
    "not list the screened addresses.",
    "graph": "Relations within this collector's observed data (distance 2). Proximity is "
    "not ownership or culpability; unobserved parents are not checked.",
    "anomaly": "Behavioural anomalies: measurements in the tail of recently sampled "
    "traffic. Unusual is not evidence of wrongdoing: batch payouts, consolidations and "
    "CoinJoins are unusual and legitimate.",
    "supervised": "No supervised model is validated for live transactions; this layer abstains.",
}


class LiveDisabled(Exception):
    pass


class LiveUnauthorized(Exception):
    pass


class UpstreamUnavailable(Exception):
    """The remote edge API could not be reached or answered unexpectedly."""


class BudgetExhausted(UpstreamUnavailable):
    """Refused locally by the upstream request budget: never cached, so a flood of refused
    keys cannot evict the dashboard's own polling answers from the LRU cache."""


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
        self._summary_cache: dict[float, tuple[float, dict[str, Any]]] = {}
        self._summary_lock = threading.Lock()

    def status(self, now: float) -> dict[str, Any]:
        live, st = self.live, _redact(self.reader.status())
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

    def risk_summary(self, hours: float) -> dict[str, Any]:
        # aggregate over up to the whole retention window (~0.5 s at 50k rows): computed at
        # most once per RISK_SUMMARY_TTL_S per window, however many callers ask
        with self._summary_lock:
            hit = self._summary_cache.get(hours)
            if hit and time.monotonic() - hit[0] < RISK_SUMMARY_TTL_S:
                return hit[1]
            value = self._risk_summary(hours)
            self._summary_cache[hours] = (time.monotonic(), value)
            while len(self._summary_cache) > 16:
                self._summary_cache.pop(next(iter(self._summary_cache)))
            return value

    def _risk_summary(self, hours: float) -> dict[str, Any]:
        now = time.time()
        summary = self.reader.risk_summary(now - hours * 3600)
        st = self.reader.status() or {}
        counters = st.get("counters") or {}
        settings = st.get("settings") or {}
        max_age_days = settings.get("indicator_max_age_days", LiveConfig().indicator_max_age_days)
        anomaly_cfg = settings.get("anomaly") or dataclasses.asdict(DetectorConfig())
        if summary is not None:
            for src in summary["sources"]:  # same staleness rule as the screening layer
                checked = src.get("checked_at") or src.get("retrieved_at")
                src["age_s"] = round(now - checked, 1) if checked else None
                # a copy superseded by a newer snapshot of the same list is not a stale list
                src["stale"] = not src.get("superseded_by") and (
                    checked is None or now - checked > max_age_days * 86400
                )
        return {
            "enabled": summary is not None,
            "window_hours": hours,
            "summary": summary,
            "engine": {
                "anomaly_reference_size": counters.get("anomaly_reference_size"),
                "risk_latency_ms_p50": counters.get("risk_latency_ms_p50"),
                "risk_latency_ms_p95": counters.get("risk_latency_ms_p95"),
                "risk_errors": counters.get("risk_errors"),
                "layer_errors": counters.get("layer_errors"),
                "supervised": counters.get("supervised_state"),
                "indicator_max_age_days": max_age_days,
                "anomaly_min_reference": anomaly_cfg.get("min_reference"),
                "anomaly_window": anomaly_cfg.get("window"),
                "anomaly_alpha": anomaly_cfg.get("alpha"),
            },
            "layers": RISK_LAYERS,
        }

    def findings(self, max_priority: int, limit: int, before_id: int | None) -> list[dict]:
        return self.reader.findings(max_priority, limit, before_id)

    def alerts(self, status: str | None, limit: int) -> list[dict]:
        return self.reader.alerts(status, limit)

    def max_id(self) -> int:
        return self.reader.max_id()


class RemoteBackend:
    """HTTPS client for the collector's edge API, with a short shared cache.

    Load on the collector VM is bounded however many browsers are connected:
    * each distinct query is fetched at most once per ``remote_cache_s`` (LRU of
      ``CACHE_MAX_KEYS``); concurrent misses for the same key share one request
      (single flight);
    * failures are cached too, so an outage does not turn every page view into a slow
      upstream timeout that ties up the dashboard's worker threads;
    * all cache misses of arbitrary queries share a token bucket (``UPSTREAM_PER_S``), so
      varying query parameters cannot multiply upstream traffic; over budget they fail fast.
      The dashboard's own polling queries (a fixed, small set: ``priority``) are not drawn
      from that budget, so an anonymous visitor cycling parameters cannot starve them;
    * on failure a key's last good answer (at most ``STALE_OK_S`` old) is served instead of
      an error, and every request has an overall deadline (a dripping edge cannot hold it).
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
        # key -> (stored_at, value or exception); LRU order
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._good: OrderedDict[str, tuple[float, Any]] = OrderedDict()  # last good answers
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._tokens, self._last = float(UPSTREAM_BURST), time.monotonic()
        self.upstream_requests = 0  # observability and tests

    def _cached(self, key: str, now: float) -> tuple[bool, Any]:
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.live.remote_cache_s:
            self._cache.move_to_end(key)
            return True, hit[1]
        return False, None

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        priority: bool = False,
        stale_ok: bool = True,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        while True:
            with self._lock:
                found, value = self._cached(key, time.monotonic())
                if not found:
                    event = self._inflight.get(key)
                    if event is None:  # this thread fetches; others wait for it
                        self._inflight[key] = threading.Event()
                        break
            if found:
                return self._result(key, value, stale_ok)
            event.wait(self.live.remote_timeout_s + 1.0)
        value: Any = UpstreamUnavailable("internal error")
        try:
            value = self._fetch(path, params, priority)
        except (UpstreamUnavailable, NoLiveData) as e:
            value = e
        except Exception as e:  # never leave waiters hanging or leak internals to the browser
            log.warning("remote collector request failed: %s", type(e).__name__)
            value = UpstreamUnavailable(type(e).__name__)
        finally:
            with self._lock:
                event = self._inflight.pop(key)
                now = time.monotonic()
                if not isinstance(value, BudgetExhausted):
                    self._cache[key] = (now, value)
                    self._cache.move_to_end(key)
                    while len(self._cache) > CACHE_MAX_KEYS:
                        self._cache.popitem(last=False)
                if not isinstance(value, Exception):
                    self._good[key] = (now, value)
                    self._good.move_to_end(key)
                    while len(self._good) > CACHE_MAX_KEYS:
                        self._good.popitem(last=False)
            event.set()
        return self._result(key, value, stale_ok)

    def _result(self, key: str, value: Any, stale_ok: bool = True) -> Any:
        if stale_ok and isinstance(value, UpstreamUnavailable):
            with self._lock:
                good = self._good.get(key)
            if good is not None and time.monotonic() - good[0] < STALE_OK_S:
                return good[1]  # a slightly old answer beats an error page
        if isinstance(value, Exception):
            raise value
        return value

    def _take_token(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(UPSTREAM_BURST, self._tokens + (now - self._last) * UPSTREAM_PER_S)
            self._last = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _fetch(self, path: str, params: dict[str, Any], priority: bool = False) -> Any:
        if not priority and not self._take_token():
            raise BudgetExhausted("upstream request budget exhausted")
        self.upstream_requests += 1
        t0 = time.monotonic()
        try:
            with self.client.stream("GET", self.base + path, params=params) as r:
                chunks, size = [], 0
                for chunk in r.iter_bytes():  # bounded read: never buffer a huge body
                    size += len(chunk)
                    if size > MAX_REMOTE_BYTES:
                        raise UpstreamUnavailable("response too large")
                    if time.monotonic() - t0 > self.live.remote_timeout_s:
                        # httpx's timeout is per read: a dripping body needs a total deadline
                        raise UpstreamUnavailable("edge API too slow")
                    chunks.append(chunk)
        except httpx.HTTPError as e:
            raise UpstreamUnavailable(type(e).__name__) from e
        content = b"".join(chunks)
        try:
            body = json.loads(content) if content else None
        except ValueError:
            body = None
            if r.status_code == 200:
                raise UpstreamUnavailable("edge API sent invalid JSON") from None
        if r.status_code == 503 and _code(body) == "live_no_data":
            raise NoLiveData(self.base)
        if r.status_code == 404 and path.startswith("/v1/live/transactions/"):
            return None
        if r.status_code == 401:  # e.g. the edge token was rotated but not this side
            log.warning("edge API rejected the dashboard's token (rotated? check %s)",
                        self.live.api_token_env)  # fmt: skip
            raise UpstreamUnavailable("edge API rejected the token (401)")
        if r.status_code != 200 or body is None:
            raise UpstreamUnavailable(f"edge API answered {r.status_code} ({_code(body)})")
        return body

    def status(self, now: float) -> dict[str, Any]:
        # never a stale answer: the status is what tells visitors the edge is unreachable
        body = dict(self._get("/v1/live/status", priority=True, stale_ok=False))
        body["via"] = "remote collector (edge API)"
        body["server_time"] = now
        return body

    def transactions(self, **q: Any) -> dict[str, Any]:
        params = dict(q)
        params["order"] = "desc" if params.pop("descending", True) else "asc"
        params["outcome"] = params.get("outcome") or "all"
        feed = {"limit": 200, "offset": 0, "sort": "id", "order": "desc", "outcome": "all"}
        polled = {k: v for k, v in params.items() if v is not None} == feed  # dashboard feed
        body = self._get("/v1/live/transactions", params, priority=polled)
        return {k: body[k] for k in ("total", "offset", "rows")}

    def transaction(self, txid: str) -> dict[str, Any] | None:
        body = self._get(f"/v1/live/transactions/{txid}")
        return None if body is None else {k: v for k, v in body.items() if k != "mode"}

    def stats(self, window_minutes: int) -> dict[str, Any]:
        window = _snap(window_minutes, WINDOW_STEPS)
        body = self._get("/v1/live/stats", {"window_minutes": window}, priority=window == 60)
        return {k: v for k, v in body.items() if k != "mode"}

    def outages(self, hours: float, limit: int) -> list[dict[str, Any]]:
        q = {"hours": _snap(hours, HOURS_STEPS), "limit": limit}
        polled = q == {"hours": 24, "limit": 50}
        return self._get("/v1/live/outages", q, priority=polled)["outages"]

    def recent(self) -> list[dict[str, Any]]:
        # the same query (cache key) as the dashboard's feed poll: two keys cached at
        # different moments made the feed look older than the stream and reset it
        q = {"limit": 200, "offset": 0, "sort": "id", "descending": True, "outcome": None}
        return self.transactions(**q)["rows"]

    def max_id(self) -> int:
        rows = self.recent()
        return max((r["id"] for r in rows), default=0)

    def risk_summary(self, hours: float) -> dict[str, Any]:
        h = _snap(hours, HOURS_STEPS)
        body = self._get("/v1/live/risk/summary", {"hours": h}, priority=h == 24)
        return {k: v for k, v in body.items() if k != "mode"}

    def findings(self, max_priority: int, limit: int, before_id: int | None) -> list[dict]:
        q = {"max_priority": max_priority, "limit": limit, "before_id": before_id}
        polled = (max_priority, limit, before_id) == (4, 50, None)
        return self._get("/v1/live/findings", q, priority=polled)["findings"]

    def alerts(self, status: str | None, limit: int) -> list[dict]:
        q = {"status": status, "limit": limit}
        return self._get("/v1/live/alerts", q, priority=limit == 200)["alerts"]


def _code(body: Any) -> str:
    try:
        return str(body["error"]["code"])
    except (KeyError, TypeError):
        return "unknown"


def make_backend(live: LiveConfig) -> LocalBackend | RemoteBackend:
    if not live.remote_url:
        return LocalBackend(live)
    from chainwatch.live.edge import valid_token  # the edge's own rule (import cycle)

    token = os.environ.get(live.api_token_env, "").strip()
    if not valid_token(token):
        raise ValueError(
            f"live.remote_url is set but ${live.api_token_env} is missing, shorter than 32 "
            "characters or not URL-safe"
        )
    return RemoteBackend(live, token)


# --------------------------------------------------------------------------- routes
def install_live_routes(
    app: FastAPI,
    live: LiveConfig,
    error,
    *,
    token: str | tuple[str, ...] | None = None,
    backend: LocalBackend | RemoteBackend | None = None,
    trusted_proxy_hops: int = 0,
) -> None:
    """Register /v1/live/*. ``token`` makes every route require a bearer token (edge API).
    ``trusted_proxy_hops`` identifies clients for the per-client event-stream cap."""
    be = backend or (make_backend(live) if live.enabled else LocalBackend(live))
    tokens = (token,) if isinstance(token, str) else token  # several during a rotation
    streams: dict[str, int] = {}  # open event streams per client; total = sum
    streams_lock = threading.Lock()

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
        if tokens is not None:
            header = request.headers.get("authorization", "")
            given = (header[7:] if header.lower().startswith("bearer ") else "").encode()
            # compare against every accepted token (no early exit: constant work)
            ok = [hmac.compare_digest(given, t.encode()) for t in tokens]
            if not any(ok):
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
        after_id: int | None = Query(None, ge=0, le=MAX_ROW_ID),
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

    @r.get("/risk/summary")
    def live_risk_summary(hours: float = Query(24, gt=0, le=720)) -> dict[str, Any]:
        """Risk intelligence health: layer outcomes, latency, sources and their freshness."""
        return {"mode": "live", **be.risk_summary(hours)}

    @r.get("/findings")
    def live_findings(
        max_priority: int = Query(4, ge=1, le=4),
        limit: int = Query(50, ge=1, le=200),
        before_id: int | None = Query(None, ge=0, le=MAX_ROW_ID),
    ) -> dict[str, Any]:
        """Assessed transactions with a finding, newest first (priority 1 = source match)."""
        return {"mode": "live", "findings": be.findings(max_priority, limit, before_id)}

    @r.get("/alerts")
    def live_alerts(
        status: str | None = Query(
            None, pattern="^(new|under_review|dismissed|resolved|insufficient_evidence)$"
        ),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Investigation queue (grouped, deduplicated). Read-only over HTTP; statuses are
        changed with `chainwatch-live alerts set-status` on the collector host."""
        return {"mode": "live", "alerts": be.alerts(status, limit)}

    @r.get("/outages")
    def live_outages(
        hours: float = Query(24, gt=0, le=720), limit: int = Query(50, ge=1, le=200)
    ) -> dict[str, Any]:
        """Collection gaps: collector down, WebSocket disconnected, missed mempool updates."""
        return {"mode": "live", "outages": be.outages(hours, limit)}

    @r.get("/events")
    async def live_events(request: Request, since: int | None = Query(None, ge=0)):
        """Server-sent events: ``tx`` for each newly stored observation, ``status`` every 5 s."""
        who = client_key(request.scope, trusted_proxy_hops)
        with streams_lock:  # reserve the slot before any await (no check-then-act race)
            if sum(streams.values()) >= live.sse_max_clients:
                raise HTTPException(503, "too many live event streams")
            if streams.get(who, 0) >= live.sse_max_per_client:
                raise HTTPException(429, "too many live event streams from this client")
            streams[who] = streams.get(who, 0) + 1

        def release() -> None:
            with streams_lock:
                streams[who] -= 1
                if streams[who] <= 0:
                    del streams[who]

        try:
            header = request.headers.get("last-event-id", "")
            start = int(header) if EVENT_ID_RE.fullmatch(header) else since
            if start is None:
                start = await asyncio.to_thread(be.max_id)  # only new observations
        except BaseException:
            release()
            raise

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

        class _Stream(StreamingResponse):
            async def __call__(self, scope, receive, send):  # release even if never iterated
                try:
                    # a client that never reads blocks send(): the in-loop deadline is then
                    # never checked, so the slot is also bounded from outside
                    with anyio.move_on_after(
                        live.sse_max_seconds + min(10.0, live.sse_max_seconds)
                    ):
                        await super().__call__(scope, receive, send)
                finally:
                    release()

        return _Stream(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    app.include_router(r)
