"""Asynchronous mempool collector: WebSocket txids -> sampled, rate-limited REST details ->
observation-time features -> SQLite. Runs as its own process (``chainwatch live-collect``);
the API only reads the database it writes.

Pipeline and its bounds:

    ws (track-mempool-txids) --validate/dedupe (bounded LRU)--> sample (hash, rate p)
        --> bounded queue (drop newest when full; skip entries older than max_queue_age_s)
        --> N workers --> token bucket (fetch_per_minute; pauses on HTTP 429)
        --> GET {rest_base}/tx/{txid} (timeout) --> features + rule-based flags --> SQLite

Nothing is backfilled: only txids announced while the collector is connected are seen, and
only a sample of those is fetched. Coverage is therefore partial by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import signal
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from chainwatch import __version__
from chainwatch.config import LiveConfig
from chainwatch.live import features as feat
from chainwatch.live.protocol import MAX_MESSAGE_BYTES, SUBSCRIBE, parse_message
from chainwatch.live.store import LiveStore

log = logging.getLogger("chainwatch.live")
USER_AGENT = f"ChainWatch-live-monitor/{__version__} (+https://github.com/Apostal322/ChainWatch)"


@dataclass
class Counters:
    messages: int = 0
    malformed: int = 0
    received: int = 0  # txids announced in "added"
    invalid_txids: int = 0
    duplicates: int = 0
    not_sampled: int = 0
    queued: int = 0
    dropped_backpressure: int = 0
    expired: int = 0
    fetched: int = 0
    failed: int = 0
    not_found: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    malformed_payloads: int = 0
    removed: int = 0
    replaced: int = 0
    connects: int = 0
    reconnects: int = 0
    stale_reconnects: int = 0


class SeenSet:
    """Bounded LRU set of txids (memory-safe deduplication)."""

    def __init__(self, max_items: int) -> None:
        self.max_items = max_items
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, txid: str) -> bool:
        if txid in self._items:
            self._items.move_to_end(txid)
            return False
        self._items[txid] = None
        if len(self._items) > self.max_items:
            self._items.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._items)


def sampled(txid: str, rate: float) -> bool:
    """Deterministic, arrival-order-independent sampling: same txid -> same decision."""
    if rate >= 1.0:
        return True
    h = int.from_bytes(hashlib.sha256(txid.encode()).digest()[:8], "big")
    return h / 2**64 < rate


class TokenBucket:
    """Async token bucket with an explicit pause (used for HTTP 429 / Retry-After)."""

    def __init__(
        self,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(max(1, burst))
        self.tokens = self.burst
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.paused_until = 0.0
        self.lock = asyncio.Lock()

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, self.clock() + seconds)
        self.tokens = 0.0

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = self.clock()
                if now < self.paused_until:
                    await self.sleep(self.paused_until - now)
                    continue
                self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await self.sleep((1.0 - self.tokens) / self.rate)


def retry_after_seconds(value: str | None, default: float = 60.0) -> float:
    try:
        return min(600.0, max(1.0, float(value))) if value else default
    except ValueError:
        return default  # HTTP-date form: fall back to a conservative pause


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Exponential backoff with full jitter."""
    return rng.uniform(0.0, min(cap, base * 2**attempt))


class Collector:
    def __init__(
        self,
        cfg: LiveConfig,
        store: LiveStore,
        *,
        connect: Callable[..., Any] | None = None,
        http: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        self.cfg, self.store, self.connect = cfg, store, connect
        self.http = http
        self._own_http = http is None
        self.rng = rng or random.Random()
        self.clock = clock
        self.c = Counters()
        self.seen = SeenSet(cfg.dedupe_max)
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=cfg.queue_max)
        self.bucket = TokenBucket(cfg.fetch_per_minute, cfg.fetch_burst)
        self.recent_fee_rates: deque[float] = deque(maxlen=500)
        self.stop = asyncio.Event()
        self.state = "starting"
        self.started_at = clock()
        self.connected_since: float | None = None
        self.last_message_at: float | None = None
        self.last_sequence: int | None = None
        self.last_error: str | None = None
        self._flushed = Counters()
        self._max_fetches: int | None = None

    # ---- WebSocket side ------------------------------------------------------------------
    def handle_frame(self, raw: str | bytes) -> None:
        res = parse_message(raw)
        self.c.messages += 1
        self.last_message_at = self.clock()
        if res.malformed:
            self.c.malformed += 1
            return
        d = res.delta
        if d is None:
            return
        if d.sequence is not None:
            self.last_sequence = d.sequence
        self.c.invalid_txids += d.invalid
        self.c.removed += len(d.removed)
        self.c.replaced += len(d.replaced)
        now = self.clock()
        for txid in d.added:
            self.c.received += 1
            if not self.seen.add(txid):
                self.c.duplicates += 1
            elif not sampled(txid, self.cfg.sample_rate):
                self.c.not_sampled += 1
            else:
                try:
                    self.queue.put_nowait((txid, now))
                    self.c.queued += 1
                except asyncio.QueueFull:
                    self.c.dropped_backpressure += 1

    async def _ws_loop(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            self.state = "connecting" if self.c.connects == 0 else "reconnecting"
            try:
                async with self.connect(
                    self.cfg.ws_url,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=MAX_MESSAGE_BYTES,
                    user_agent_header=USER_AGENT,
                ) as ws:
                    await ws.send(SUBSCRIBE)
                    self.c.connects += 1
                    self.state, self.connected_since = "connected", self.clock()
                    log.info(json.dumps({"msg": "connected", "url": self.cfg.ws_url}))
                    while not self.stop.is_set():
                        recv = asyncio.ensure_future(ws.recv())
                        stopper = asyncio.ensure_future(self.stop.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {recv, stopper},
                                timeout=self.cfg.stale_after_s,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:  # also on cancellation: never leave a pending recv behind
                            for fut in (recv, stopper):
                                if not fut.done():
                                    fut.cancel()
                                    with contextlib.suppress(BaseException):
                                        await fut
                        if recv not in done:
                            if not self.stop.is_set():
                                self.c.stale_reconnects += 1
                                self.last_error = "no message within stale_after_s"
                            break
                        self.handle_frame(recv.result())
                        attempt = 0  # a working stream resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake failures, closed connections
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning(json.dumps({"msg": "websocket error", "error": self.last_error}))
            self.connected_since = None
            if self.stop.is_set():
                break
            self.state = "reconnecting"
            self.c.reconnects += 1
            delay = backoff_delay(
                attempt, self.cfg.reconnect_base_s, self.cfg.reconnect_max_s, self.rng
            )
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=delay)

    # ---- REST side -----------------------------------------------------------------------
    async def fetch_one(self, txid: str, first_seen: float) -> None:
        now = self.clock()
        if now - first_seen > self.cfg.max_queue_age_s:
            self.c.expired += 1
            return
        if self.store.has(txid):  # idempotent across restarts
            self.c.duplicates += 1
            return
        await self.bucket.acquire()
        assert self.http is not None
        error: str | None = None
        try:
            resp = await self.http.get(f"{self.cfg.rest_base}/tx/{txid}")
        except httpx.TimeoutException:
            self.c.timeouts += 1
            error = "timeout"
        except httpx.HTTPError as e:
            error = f"network:{type(e).__name__}"
        else:
            if resp.status_code == 429:
                self.c.rate_limited += 1
                self.bucket.pause(retry_after_seconds(resp.headers.get("retry-after")))
                error = "rate_limited"
            elif resp.status_code == 404:
                self.c.not_found += 1  # evicted, replaced or not (yet) indexed
                error = "not_found"
            elif resp.status_code != 200:
                error = f"http_{resp.status_code}"
            else:
                try:
                    f = feat.extract(
                        resp.json(), txid=txid, first_seen=first_seen, fetched_at=self.clock()
                    )
                except (ValueError, feat.FeatureError) as e:
                    self.c.malformed_payloads += 1
                    error = f"malformed_payload:{str(e)[:80]}"
                else:
                    flags = feat.indicators(f, self.recent_fee_rates)
                    self.recent_fee_rates.append(f["fee_rate"])
                    if self.store.add_fetched(f, flags):
                        self.c.fetched += 1
        if error is not None:
            self.c.failed += 1
            self.store.add_failed(txid, first_seen, self.clock(), error)

    async def _worker(self) -> None:
        while not self.stop.is_set():
            txid, first_seen = await self.queue.get()
            try:
                await self.fetch_one(txid, first_seen)
            finally:
                self.queue.task_done()
            if (
                self._max_fetches is not None
                and self.c.fetched + self.c.failed >= self._max_fetches
            ):
                self.stop.set()

    # ---- status --------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "updated_at": self.clock(),
            "started_at": self.started_at,
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "last_sequence": self.last_sequence,
            "reconnects": self.c.reconnects,
            "counters": {
                **asdict(self.c),
                "queue_size": self.queue.qsize(),
                "dedupe_size": len(self.seen),
                "last_error": self.last_error,
            },
            "settings": {
                "source": "mempool.space",
                "ws_url": self.cfg.ws_url,
                "rest_base": self.cfg.rest_base,
                "sample_rate": self.cfg.sample_rate,
                "fetch_per_minute": self.cfg.fetch_per_minute,
                "fetch_concurrency": self.cfg.fetch_concurrency,
                "queue_max": self.cfg.queue_max,
                "retention_rows": self.cfg.retention_rows,
                "collector_version": __version__,
            },
        }

    def flush(self) -> None:
        now, c, f = self.clock(), self.c, self._flushed
        self.store.bump(
            now,
            received=c.received - f.received,
            sampled=c.queued - f.queued,
            fetched=c.fetched - f.fetched,
            failed=c.failed - f.failed,
            dropped=c.dropped_backpressure + c.expired - f.dropped_backpressure - f.expired,
            rate_limited=c.rate_limited - f.rate_limited,
        )
        self._flushed = Counters(**asdict(c))
        self.store.heartbeat(**self.snapshot())

    async def _heartbeat(self) -> None:
        last_prune = 0.0
        while not self.stop.is_set():
            self.flush()
            if self.clock() - last_prune > 60:
                self.store.prune(self.cfg.retention_rows)
                last_prune = self.clock()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.heartbeat_s)

    # ---- lifecycle -----------------------------------------------------------------------
    async def run(self, duration: float | None = None, max_fetches: int | None = None) -> Counters:
        self._max_fetches = max_fetches
        if self.http is None:
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.request_timeout_s),
                headers={"user-agent": USER_AGENT, "accept": "application/json"},
                limits=httpx.Limits(max_connections=self.cfg.fetch_concurrency),
            )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.stop.set)
        tasks = [asyncio.create_task(self._ws_loop(), name="ws")]
        tasks += [asyncio.create_task(self._worker()) for _ in range(self.cfg.fetch_concurrency)]
        tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))
        try:
            if duration is None:
                await self.stop.wait()
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=duration)
        finally:
            self.stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.state = "stopped"
            self.connected_since = None
            self.flush()
            if self._own_http:
                await self.http.aclose()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
        return self.c
