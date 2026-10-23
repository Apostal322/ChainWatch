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

Failure policy: per-transaction problems (HTTP errors, bad payloads) become ``failed`` rows;
an unexpected exception in one item is logged and counted (``internal_errors``). If a
pipeline task itself dies (e.g. the database cannot be written), ``run`` stops everything
and ``fatal`` is set, so the process exits non-zero and systemd restarts it instead of
leaving a live-looking process that no longer collects.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import hashlib
import json
import logging
import random
import signal
import sqlite3
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
    duplicates: int = 0  # announced again while still remembered (bounded LRU)
    already_stored: int = 0  # sampled but already in the database (e.g. after a restart)
    gone_before_fetch: int = 0  # removed/mined before its turn: not fetched (saves budget)
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
    upstream_errors: int = 0  # HTTP 5xx (the fetch budget pauses briefly)
    internal_errors: int = 0  # unexpected exceptions while processing one item
    # bounded retries of transient detail-fetch failures (each attempt uses the fetch budget)
    retries_scheduled: int = 0
    retries_succeeded: int = 0  # fetched on a later attempt
    retries_exhausted: int = 0  # still failing after the last attempt, or out of time
    retries_dropped: int = 0  # no room for another pending retry: recorded as failed
    db_errors: int = 0  # lifecycle/gap writes that failed (e.g. the database was locked)
    # mempool's backend repeats its sequence number while it re-syncs (after its restart):
    # those deltas re-announce the whole mempool and are not new arrivals, so they are skipped
    resync_deltas: int = 0
    resync_txids_skipped: int = 0
    # Upstream mempool-txids semantics (mempool/mempool websocket-handler.ts):
    # ``removed`` lists every txid that left the mempool in a poll (replacements, conflicts,
    # evictions, expiry), NOT mined ones; ``mined`` comes with each new block;
    # ``replaced`` pairs (old, by) overlap ``removed`` (the old txid is also removed) and
    # one old txid can appear in several pairs. The counters are therefore not disjoint.
    removed: int = 0
    mined: int = 0
    replacement_pairs: int = 0
    lifecycle_updates: int = 0  # stored transactions later seen mined / removed / replaced
    # risk intelligence (docs/RISK_ENGINE.md)
    risk_assessed: int = 0
    risk_errors: int = 0  # assessment or persistence failed; the observation is kept
    layer_errors: int = 0  # a single layer failed inside an assessment (isolated)
    screening_matches: int = 0
    graph_relations: int = 0
    anomalies_strong: int = 0
    anomalies_notable: int = 0
    alerts_raised: int = 0
    connects: int = 0
    reconnects: int = 0
    stale_reconnects: int = 0
    sequence_gaps: int = 0  # missed mempool updates while connected
    recovered_downtime_s: float = 0.0  # gap since the previous process's last heartbeat
    retrospective_matches: int = 0  # found when the collector loaded a new indicator version


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

    def __contains__(self, txid: object) -> bool:
        return txid in self._items

    def __len__(self) -> int:
        return len(self._items)


class LifecycleMemo:
    """Bounded memory of txids that left the mempool, with their state, so a transaction
    whose removal/mining/replacement arrives while it waits for or during its fetch is not
    fetched (or, if already in flight, gets its state once stored)."""

    def __init__(self, max_items: int) -> None:
        self.max_items = max_items
        self._items: OrderedDict[str, tuple[str, float, str | None]] = OrderedDict()

    def add(self, txid: str, state: str, at: float, replaced_by: str | None = None) -> None:
        prev = self._items.pop(txid, None)
        if prev is not None and prev[0] == "mined":
            state, replaced_by = "mined", None  # mined is final
        elif prev is not None and prev[0] == "replaced" and state == "removed":
            state, replaced_by = "replaced", prev[2]  # as mark_lifecycle: replaced refines removed
        self._items[txid] = (state, at, replaced_by)
        if len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def get(self, txid: str) -> tuple[str, float, str | None] | None:
        return self._items.get(txid)

    def __contains__(self, txid: object) -> bool:
        return txid in self._items

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
        self.last = self.paused_until  # no refill credit for the pause itself (no burst after)

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = self.clock()
                if now < self.paused_until:
                    await self.sleep(self.paused_until - now)
                    continue
                self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0 - 1e-9:  # tolerance: float refill can land at 0.999…
                    self.tokens = max(0.0, self.tokens - 1.0)
                    return
                await self.sleep((1.0 - self.tokens) / self.rate)


def retry_after_seconds(
    value: str | None, default: float = 60.0, now: float | None = None
) -> float:
    """Seconds to pause for a ``Retry-After`` header (delta-seconds or HTTP-date), 1-600 s."""
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return default
        if when is None or when.tzinfo is None:
            return default
        seconds = when.timestamp() - (time.time() if now is None else now)
    if seconds != seconds:  # NaN
        return 1.0
    return min(600.0, max(1.0, seconds))


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Exponential backoff with full jitter. ``attempt`` is clamped, so after any number of
    failures the delay stays finite (2**1024 would overflow a float)."""
    return rng.uniform(0.0, min(cap, base * 2 ** min(attempt, 30)))


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 3)


HEALTHY_AFTER_S = 30.0  # a connection must deliver for this long before backoff resets
UPSTREAM_ERROR_PAUSE_S = 30.0  # fetch pause after an HTTP 5xx from the REST API
MAX_TX_BYTES = 16_000_000  # a REST transaction document larger than this is refused
GONE_MAX = 50_000  # remembered removed/mined txids (bounded)


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
        self.gone = LifecycleMemo(GONE_MAX)  # removed, mined or replaced: not worth fetching
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=cfg.queue_max)
        # txid -> (next attempt number, last error) while a retry is pending or queued
        self._retry: dict[str, tuple[int, str]] = {}
        self._pending_update: dict[str, Any] | None = None  # detector update after commit
        self._retry_timers: dict[str, tuple[asyncio.TimerHandle, float]] = {}
        self.bucket = TokenBucket(cfg.fetch_per_minute, cfg.fetch_burst)
        self.recent_fee_rates: deque[float] = deque(maxlen=500)
        self.stop = asyncio.Event()
        self.state = "starting"
        self.started_at = clock()
        self.connected_since: float | None = None
        self.last_message_at: float | None = None
        # the last frame that carried a mempool delta: pongs or junk keep the socket busy but
        # are no evidence that transactions are being observed (the stale watchdog uses this)
        self._last_delta_at: float | None = None
        self.last_sequence: int | None = None
        self.last_error: str | None = None
        self._flushed = Counters()
        self._outage_id: int | None = None
        self._conn_sequence: int | None = None
        # a re-sync stretch (repeated sequence): its new arrivals cannot be told apart from
        # re-announcements and are skipped, so the stretch is recorded as a coverage gap
        self._resync: tuple[float, int] | None = None  # (started_at, txids skipped)
        self.first_started_at = self.started_at
        self._max_fetches: int | None = None
        self.fatal: str | None = None  # set when a pipeline task died; the CLI exits non-zero
        self.engine = None  # built in run() from the store (risk intelligence)
        self.risk_unavailable: str | None = None
        self._indicator_version = -1
        self._latencies: deque[float] = deque(maxlen=500)

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
        self._last_delta_at = self.last_message_at
        gap: str | None = None
        resync = False
        if d.sequence is not None:
            prev = self._conn_sequence
            if prev is not None and d.sequence > prev + 1:
                self.c.sequence_gaps += 1
                gap = (
                    f"{d.sequence - prev - 1} mempool update(s) missed "
                    f"(sequence {prev} -> {d.sequence})"
                )
            # the backend only advances its sequence while in sync; a repeated number is a
            # re-sync delta that re-announces old transactions (websocket-handler.ts)
            resync = prev is not None and d.sequence == prev
            self._conn_sequence = d.sequence
            self.last_sequence = d.sequence
        self.c.invalid_txids += d.invalid
        self.c.removed += len(d.removed)
        self.c.mined += len(d.mined)
        self.c.replacement_pairs += len(d.replaced)
        at = self.last_message_at
        for txid in d.removed:
            self.gone.add(txid, "removed", at)
        for old, by in d.replaced:  # block conflicts reach ``removed`` only a delta later
            self.gone.add(old, "replaced", at, by)
        for txid in d.mined:
            self.gone.add(txid, "mined", at)
        # new arrivals first, so a database problem below cannot lose them
        now = self.clock()
        resync_gap: tuple[float, int] | None = None
        if resync:
            self.c.resync_deltas += 1
            self.c.resync_txids_skipped += len(d.added)
            started, skipped = self._resync or (at, 0)
            self._resync = (started, skipped + len(d.added))
        else:
            if self._resync is not None and d.sequence is not None:  # in sync again
                resync_gap, self._resync = self._resync, None
            for txid in d.added:
                self.c.received += 1
                if not self.seen.add(txid):
                    self.c.duplicates += 1
                elif not sampled(txid, self.cfg.sample_rate):
                    self.c.not_sampled += 1
                elif txid in self._retry:  # announced again while its retry is pending
                    self.c.duplicates += 1
                else:
                    try:
                        self.queue.put_nowait((txid, now))
                        self.c.queued += 1
                    except asyncio.QueueFull:
                        self.c.dropped_backpressure += 1
        if self.store is None:
            return
        try:
            if gap is not None:
                self.store.record_gap("sequence_gap", at, at, gap)
            if resync_gap is not None:
                self._record_resync_gap(resync_gap, at)
            if d.removed or d.mined or d.replaced:
                # post-observation state only; features and past assessments never change
                from chainwatch.risk.persistence import mark_lifecycle

                n = mark_lifecycle(self.store.db, d.removed, "removed", at)
                for old, by in d.replaced:
                    n += mark_lifecycle(self.store.db, [old], "replaced", at, replaced_by=by)
                n += mark_lifecycle(self.store.db, d.mined, "mined", at)  # last: final state
                self.c.lifecycle_updates += n
        except sqlite3.Error as e:  # e.g. locked by another writer: not a network problem
            self.c.db_errors += 1
            self.last_error = f"database: {type(e).__name__}: {e}"[:200]
            log.warning(json.dumps({"msg": "lifecycle/gap write failed", "error": self.last_error}))

    def _record_resync_gap(self, gap: tuple[float, int], ended_at: float) -> None:
        started, skipped = gap
        self.store.record_gap(
            "sequence_gap", started, ended_at,
            f"mempool backend re-syncing: {skipped} announced txid(s) skipped (new arrivals "
            "cannot be told apart from re-announcements)",
        )  # fmt: skip

    def _end_resync(self, ended_at: float) -> None:
        """A connection or the process ends during a re-sync stretch: record it now."""
        if self._resync is None or self.store is None:
            return
        gap, self._resync = self._resync, None
        try:
            self._record_resync_gap(gap, ended_at)
        except sqlite3.Error as e:
            self.c.db_errors += 1
            self.last_error = f"database: {type(e).__name__}: {e}"[:200]

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
                    self.last_error = None
                    self._conn_sequence = None  # a new connection restarts sequence tracking
                    self._last_delta_at = None
                    if self._outage_id is not None:
                        self.store.close_outage(self._outage_id, self.connected_since)
                        self._outage_id = None
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
                        since_delta = self.clock() - (self._last_delta_at or self.connected_since)
                        if since_delta > self.cfg.stale_after_s:  # frames, but no deltas
                            self.c.stale_reconnects += 1
                            self.last_error = "no mempool update within stale_after_s"
                            break
                        if (
                            self._last_delta_at is not None
                            and self.clock() - self.connected_since >= HEALTHY_AFTER_S
                        ):
                            attempt = 0  # only a stream that keeps working resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake failures, closed connections
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning(json.dumps({"msg": "websocket error", "error": self.last_error}))
            self._end_resync(self.last_message_at or self.clock())
            conn_started, self.connected_since = self.connected_since, None
            if self.stop.is_set():
                break
            if self._outage_id is None:
                # Not receiving since the last message of this connection (a half-open or
                # silent stream is only detected later, by pings or the stale watchdog).
                start = self.clock()
                if conn_started is not None:
                    last = self.last_message_at
                    start = last if last is not None and last >= conn_started else conn_started
                self._outage_id = self.store.open_outage(
                    "disconnected", start, self.last_error or "connection lost"
                )
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
        attempt, previous_error = self._retry.pop(txid, (1, ""))
        try:
            await self._attempt(txid, first_seen, attempt, previous_error)
        except asyncio.CancelledError:
            # shutdown while a retry waits for a token or its GET: its last failure is the
            # outcome (a first attempt was never tried and leaves no row, as before)
            if attempt > 1:
                self.c.failed += 1
                self._record_failed(txid, first_seen, previous_error)
            raise

    async def _attempt(
        self, txid: str, first_seen: float, attempt: int, previous_error: str
    ) -> None:
        now = self.clock()
        if now - first_seen > self.cfg.max_queue_age_s:
            self._expired(txid, first_seen, previous_error)
            return
        if txid in self.gone:  # replaced, evicted or mined while queued: a fetch would 404
            self.c.gone_before_fetch += 1
            return
        if self.store.has(txid):  # idempotent across restarts
            self.c.already_stored += 1
            return
        await self.bucket.acquire()
        if self.clock() - first_seen > self.cfg.max_queue_age_s:  # aged while paused
            self._expired(txid, first_seen, previous_error)
            return
        if txid in self.gone:  # left the mempool while waiting for a token
            self.c.gone_before_fetch += 1
            return
        assert self.http is not None
        error: str | None = None
        status, headers, body = 0, {}, b""
        try:
            # an overall deadline: httpx's timeout is per read, so a body that trickles in
            # could otherwise hold the worker forever while the status still says connected
            async with asyncio.timeout(self.cfg.request_timeout_s):
                status, headers, body = await self._get(f"{self.cfg.rest_base}/tx/{txid}")
        except (httpx.TimeoutException, TimeoutError):
            self.c.timeouts += 1
            error = "timeout"
        except httpx.HTTPError as e:
            error = f"network:{type(e).__name__}"
        except ValueError as e:  # response larger than MAX_TX_BYTES
            self.c.malformed_payloads += 1
            error = f"malformed_payload:{str(e)[:80]}"
        else:
            if status == 429:
                self.c.rate_limited += 1
                self.bucket.pause(retry_after_seconds(headers.get("retry-after")))
                error = "rate_limited"
            elif status == 404:
                self.c.not_found += 1  # evicted, replaced or not (yet) indexed
                error = "not_found"
            elif status >= 500:
                self.c.upstream_errors += 1
                self.bucket.pause(UPSTREAM_ERROR_PAUSE_S)  # do not drain the queue into errors
                error = f"http_{status}"
            elif status != 200:
                error = f"http_{status}"
            else:
                try:
                    payload = json.loads(body)
                    f = feat.extract(
                        payload, txid=txid, first_seen=first_seen, fetched_at=self.clock()
                    )
                except (ValueError, feat.FeatureError) as e:
                    self.c.malformed_payloads += 1
                    error = f"malformed_payload:{str(e)[:80]}"
                else:
                    flags = feat.indicators(f, self.recent_fee_rates)
                    self.recent_fee_rates.append(f["fee_rate"])
                    self._store_and_assess(f, flags, payload)
                    self.c.retries_succeeded += attempt > 1
        if error is not None and not self._schedule_retry(txid, first_seen, attempt, error):
            self.c.failed += 1
            self._record_failed(txid, first_seen, error)  # a locked database is not fatal

    # ---- bounded retries ---------------------------------------------------------------
    @staticmethod
    def transient(error: str, attempt: int) -> bool:
        """Failures worth another attempt. A 404 right after the announcement can be the
        explorer's indexing lag, so it gets one more try; malformed or oversized payloads
        and other 4xx answers will not change."""
        if error == "not_found":
            return attempt == 1
        return error in ("timeout", "rate_limited") or error.startswith(("network:", "http_5"))

    def _schedule_retry(self, txid: str, first_seen: float, attempt: int, error: str) -> bool:
        """Re-queue a transient failure after an exponential, jittered delay. Never beyond
        ``fetch_max_attempts``, never past ``max_queue_age_s`` and never more pending
        retries than half the queue. Returns False when the failure is final."""
        cfg = self.cfg
        if attempt >= cfg.fetch_max_attempts or not self.transient(error, attempt):
            self.c.retries_exhausted += attempt > 1
            return False
        if txid in self._retry_timers:  # already pending (announced twice): one retry only
            return True
        if self.stop.is_set() or len(self._retry_timers) >= max(1, cfg.queue_max // 2):
            self.c.retries_dropped += 1
            return False
        delay = cfg.fetch_retry_base_s * 2 ** (attempt - 1) * (0.5 + 0.5 * self.rng.random())
        if self.clock() + delay - first_seen > cfg.max_queue_age_s:
            self.c.retries_exhausted += 1
            return False
        self._retry[txid] = (attempt + 1, error)
        timer = asyncio.get_running_loop().call_later(delay, self._requeue, txid, first_seen)
        self._retry_timers[txid] = (timer, first_seen)
        self.c.retries_scheduled += 1
        return True

    def _requeue(self, txid: str, first_seen: float) -> None:
        self._retry_timers.pop(txid, None)
        try:
            self.queue.put_nowait((txid, first_seen))
        except asyncio.QueueFull:
            self.c.retries_dropped += 1
            _, error = self._retry.pop(txid, (0, "retry_dropped"))
            self.c.failed += 1
            self._record_failed(txid, first_seen, error)

    def _expired(self, txid: str, first_seen: float, previous_error: str) -> None:
        if not previous_error:
            self.c.expired += 1
        else:  # a retried fetch that ran out of time is a failure (counted once), not a skip
            self.c.retries_exhausted += 1
            self.c.failed += 1
            self._record_failed(txid, first_seen, previous_error)

    def _record_failed(self, txid: str, first_seen: float, error: str) -> None:
        try:
            self.store.add_failed(txid, first_seen, self.clock(), error)
        except sqlite3.Error as e:
            self.c.db_errors += 1
            self.last_error = f"database: {type(e).__name__}: {e}"[:200]

    def _drain_retries(self) -> None:
        """On shutdown, pending retries are recorded with their last error (not lost):
        those still waiting for their timer and those already back in the queue."""
        for txid, (timer, first_seen) in list(self._retry_timers.items()):
            timer.cancel()
            _, error = self._retry.pop(txid, (0, "retry_pending"))
            self.c.failed += 1
            self._record_failed(txid, first_seen, error)
        self._retry_timers.clear()
        while not self.queue.empty():
            txid, first_seen = self.queue.get_nowait()
            if txid in self._retry:
                _, error = self._retry.pop(txid)
                self.c.failed += 1
                self._record_failed(txid, first_seen, error)

    async def _get(self, url: str) -> tuple[int, Any, bytes]:
        """GET with a size cap on the body (status, headers, body)."""
        assert self.http is not None
        async with self.http.stream("GET", url) as resp:
            chunks, size = [], 0
            if resp.status_code == 200:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_TX_BYTES:
                        raise ValueError("transaction document too large")
                    chunks.append(chunk)
            return resp.status_code, resp.headers, b"".join(chunks)

    # ---- risk intelligence --------------------------------------------------------------
    def _build_engine(self) -> None:
        from chainwatch.risk import persistence as rp
        from chainwatch.risk.anomaly import RobustTailDetector
        from chainwatch.risk.engine import RiskEngine
        from chainwatch.risk.supervised import SupervisedLayer

        detector = RobustTailDetector()
        # only rows with today's feature schema: an upgrade that changes a measurement's
        # meaning must not mix old values into the reference window
        detector.warm_start(
            [
                f
                for f in self.store.recent_features(detector.cfg.window)
                if f.get("schema_version") == feat.FEATURE_SCHEMA_VERSION
            ]
        )
        db = self.store.db

        def parents(txids: list[str]) -> tuple[dict[str, list], int, int]:
            checked = txids[: rp.MAX_PARENTS_CHECKED]
            return (
                rp.observed_parent_inputs(db, checked),
                rp.count_observed(db, checked),
                len(checked),
            )

        self.engine = RiskEngine(
            self._load_screener(), detector, SupervisedLayer(None), parents, clock=self.clock
        )
        # A snapshot stored while this process was down (or just before it died) was never
        # reloaded here: re-screen the latest version of each source (idempotent).
        latest = [v for (v,) in db.execute(
            "SELECT MAX(id) FROM indicator_versions GROUP BY source_id"
        )]  # fmt: skip
        self._retro(latest)

    def _retro(self, version_ids: list[int]) -> bool:
        """Retrospective screening for these versions in one transaction. A database error
        (e.g. locked) is counted and reported, never fatal: the caller retries."""
        from chainwatch.risk import persistence as rp

        try:
            with rp.transaction(self.store.db):
                found = sum(rp.retro_screen(self.store.db, v, self.clock()) for v in version_ids)
        except sqlite3.Error as e:
            self.c.db_errors += 1
            self.last_error = f"database: {type(e).__name__}: {e}"[:200]
            log.warning(json.dumps({"msg": "retrospective screening failed",
                                    "error": self.last_error}))  # fmt: skip
            return False
        self.c.retrospective_matches += found
        return True

    def _load_screener(self):
        from chainwatch.risk import persistence as rp
        from chainwatch.risk.screening import Screener

        self._indicator_version = rp.latest_version_id(self.store.db)
        return Screener(
            rp.indicator_rows(self.store.db),
            rp.source_status(self.store.db),
            max_source_age_s=self.cfg.indicator_max_age_days * 86400,
        )

    def _reload_indicators_if_changed(self) -> None:
        from chainwatch.risk import persistence as rp

        if self.engine is None:
            return
        previous = self._indicator_version
        if rp.latest_version_id(self.store.db) != previous:
            self.engine.screener = self._load_screener()
            # Transactions assessed between the snapshot being stored and this reload used
            # the old list: screen them retrospectively now (idempotent with the CLI's pass).
            new = [r[0] for r in self.store.db.execute(
                "SELECT id FROM indicator_versions WHERE id > ? ORDER BY id", (previous,)
            )]  # fmt: skip
            if not self._retro(new):
                self._indicator_version = previous  # try again on the next heartbeat
        else:  # refresh freshness (checked_at) of unchanged sources
            self.engine.screener.sources = rp.source_status(self.store.db)

    def _store_and_assess(self, f: dict[str, Any], flags: list[str], payload: dict) -> None:
        """The observation and its risk record are committed together, so readers (and the
        event stream) never see a fetched row whose assessment is still pending. A failed
        assessment rolls back only its own savepoint; the observation is kept."""
        from chainwatch.risk import persistence as rp

        assessed = None
        try:
            with rp.transaction(self.store.db):
                tx_id = self.store.add_fetched_id(f, flags)
                if tx_id is not None:
                    gone = self.gone.get(f["txid"])
                    if gone is not None:  # it left the mempool while its fetch was in flight
                        rp.mark_lifecycle(self.store.db, [f["txid"]], gone[0], gone[1], gone[2])
                    assessed = self._assess(tx_id, f, payload)
        except BaseException:
            self._pending_update = None  # rolled back: not part of the reference window
            raise
        # counted only once committed (a disk-full rollback must not count as fetched)
        pending, self._pending_update = self._pending_update, None
        if pending is not None and self.engine is not None:
            self.engine.detector.update(pending)
        if tx_id is not None:
            self.c.fetched += 1
            if assessed is not None:
                self._count_assessment(assessed)

    def _assess(self, tx_id: int, f: dict[str, Any], payload: dict[str, Any]) -> dict | None:
        """Assess and store inside the caller's transaction; returns the stored record, or
        None if risk is off or the assessment failed (the observation is kept)."""
        if self.engine is None or not self.cfg.risk_enabled:
            return None
        from chainwatch.risk import persistence as rp
        from chainwatch.risk.engine import alert_specs

        db = self.store.db
        addrs = None
        try:  # in their own savepoint: retrospective screening finds the transaction later
            addrs = feat.extract_addresses(payload)  # even if its assessment fails below
            with rp.savepoint(db):
                rp.save_addresses(db, tx_id, addrs)
        except Exception:
            if not db.in_transaction:
                raise
        try:
            if addrs is None:
                raise ValueError("addresses could not be extracted")
            parents = sorted(
                {
                    i["txid"]
                    for i in payload["vin"]
                    if isinstance(i, dict)
                    and not i.get("is_coinbase")
                    and isinstance(i.get("txid"), str)
                }
            )
            record = self.engine.assess(f, addrs, parents)
            specs = alert_specs(record)
            with rp.savepoint(db):
                rp.save_risk(db, tx_id, record)
                for spec in specs:
                    rp.upsert_alert(db, txid=f["txid"], at=record["evaluated_at"], **spec)
            record["_alerts"] = len(specs)
        except Exception as e:  # keep the observation even if the assessment fails
            if not self.store.db.in_transaction:  # SQLite rolled everything back (disk full)
                raise
            self.c.risk_errors += 1
            self.last_error = f"risk: {type(e).__name__}: {e}"[:200]
            log.exception(json.dumps({"msg": "risk assessment failed", "txid": f["txid"]}))
            from chainwatch.risk.engine import failed_record

            try:  # a durable trace: the row reads "assessment failed", not "risk off"
                with rp.savepoint(db):
                    rp.save_risk(db, tx_id, failed_record(f, f"{type(e).__name__}: {e}",
                                                          self.clock()))  # fmt: skip
            except Exception:
                if not db.in_transaction:
                    raise
            return None
        # the window holds only committed transactions, in order (a rolled-back record must
        # not shape later scores); a restart's warm start reads the stored rows instead
        self._pending_update = f
        return record

    def _count_assessment(self, record: dict[str, Any]) -> None:
        self.c.alerts_raised += record.pop("_alerts", 0)
        self.c.risk_assessed += 1
        self.c.layer_errors += len(record["errors"])
        self._latencies.append(record["latency_ms"])
        level = record["priority"]["level"]
        self.c.screening_matches += level == 1
        self.c.graph_relations += level == 2
        self.c.anomalies_strong += level == 3
        self.c.anomalies_notable += level == 4

    async def _worker(self) -> None:
        while not self.stop.is_set():
            txid, first_seen = await self.queue.get()
            try:
                await self.fetch_one(txid, first_seen)
            except Exception as e:  # one bad item must not kill the worker
                self.c.internal_errors += 1
                self.last_error = f"internal: {type(e).__name__}: {e}"[:200]
                log.exception(json.dumps({"msg": "item failed", "txid": txid}))
                # a failing write here (e.g. locked) is counted; a database that is really
                # unusable stops the heartbeat, which is fatal (exit 1, systemd restarts)
                self._record_failed(txid, first_seen, f"internal:{type(e).__name__}")
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
            "first_started_at": self.first_started_at,
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "last_sequence": self.last_sequence,
            "reconnects": self.c.reconnects,
            "counters": {
                **asdict(self.c),
                "queue_size": self.queue.qsize(),
                "dedupe_size": len(self.seen),
                "last_error": self.last_error,
                "risk_latency_ms_p50": _pct(self._latencies, 0.5),
                "risk_latency_ms_p95": _pct(self._latencies, 0.95),
                "anomaly_reference_size": self.engine.detector.n if self.engine else None,
                "supervised_state": self.engine.supervised.status() if self.engine else None,
                "risk_unavailable": self.risk_unavailable,
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
                "risk_enabled": self.cfg.risk_enabled,
                "indicator_max_age_days": self.cfg.indicator_max_age_days,
                "anomaly": (asdict(self.engine.detector.cfg) if self.engine else None),
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
        last_prune = last_checkpoint = self.clock()
        while not self.stop.is_set():
            self.flush()
            self._reload_indicators_if_changed()
            if self.clock() - last_prune > 60:
                self.store.prune(self.cfg.retention_rows, retention_days=self.cfg.retention_days)
                last_prune = self.clock()
            if self.clock() - last_checkpoint > 3600:
                self.store.checkpoint()
                last_checkpoint = self.clock()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.heartbeat_s)

    # ---- lifecycle -----------------------------------------------------------------------
    def recover(self) -> None:
        """Account for the time the collector was not running (crash, reboot, deploy).

        The interval between the previous process's last heartbeat and this start is a
        collection gap: it is stored as a ``collector_down`` outage. Outages that the
        previous process left open are closed at its last heartbeat.
        """
        prev = self.store.previous_status()
        if prev is None:
            return
        last = prev["updated_at"]
        self.first_started_at = prev.get("first_started_at") or prev.get("started_at") or last
        self.store.close_dangling_outages(last)
        if self.started_at > last:
            how = (
                "clean stop"
                if prev["state"] == "stopped"
                else f"unclean exit in state {prev['state']}"
            )
            self.store.record_gap(
                "collector_down", last, self.started_at, f"collector not running ({how})"
            )
            self.c.recovered_downtime_s = round(self.started_at - last, 1)

    async def run(self, duration: float | None = None, max_fetches: int | None = None) -> Counters:
        self._max_fetches = max_fetches
        self.recover()
        if self.cfg.risk_enabled:
            try:
                self._build_engine()
            except Exception as e:  # observations still matter; the status says why no risk
                self.engine = None
                self.risk_unavailable = f"{type(e).__name__}: {e}"[:200]
                log.exception(json.dumps({"msg": "risk engine unavailable"}))
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
        tasks += [
            asyncio.create_task(self._worker(), name=f"worker-{i}")
            for i in range(self.cfg.fetch_concurrency)
        ]
        tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            # Supervise: return on stop, on the duration, or as soon as any pipeline task
            # ends (they only end by raising). A dead task means the process must restart.
            done, _ = await asyncio.wait(
                {stopper, *tasks}, timeout=duration, return_when=asyncio.FIRST_COMPLETED
            )
            for t in tasks:
                if t in done and not self.stop.is_set():
                    exc = t.exception() if not t.cancelled() else None
                    self.fatal = f"task {t.get_name()} died: {type(exc).__name__}: {exc}"[:300]
                    log.critical(json.dumps({"msg": "pipeline task died", "error": self.fatal}))
                    break
        finally:
            stopper.cancel()
            self.stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._end_resync(self.clock())
            self._drain_retries()
            self.state = "failed" if self.fatal else "stopped"
            self.last_error = self.fatal or self.last_error
            self.connected_since = None
            with contextlib.suppress(Exception):  # the database may be what failed
                if self._outage_id is not None:
                    self.store.close_outage(self._outage_id, self.clock())
                    self._outage_id = None
                self.flush()
                # retention runs every 60 s, so rows can exceed the cap by up to one minute
                # of fetches while running; a stopped collector leaves the file within it
                self.store.prune(self.cfg.retention_rows, retention_days=self.cfg.retention_days)
            if self._own_http:
                await self.http.aclose()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
        return self.c
