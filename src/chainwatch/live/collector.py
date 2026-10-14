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
        self.gone = SeenSet(GONE_MAX)  # removed or mined: no longer worth fetching
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
        self._outage_id: int | None = None
        self._conn_sequence: int | None = None
        self.first_started_at = self.started_at
        self._max_fetches: int | None = None
        self.fatal: str | None = None  # set when a pipeline task died; the CLI exits non-zero
        self.engine = None  # built in run() from the store (risk intelligence)
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
        if d.sequence is not None:
            prev = self._conn_sequence
            if prev is not None and d.sequence > prev + 1:
                missed = d.sequence - prev - 1
                self.c.sequence_gaps += 1
                self.store.record_gap(
                    "sequence_gap", self.last_message_at, self.last_message_at,
                    f"{missed} mempool update(s) missed (sequence {prev} -> {d.sequence})",
                )  # fmt: skip
            self._conn_sequence = d.sequence
            self.last_sequence = d.sequence
        self.c.invalid_txids += d.invalid
        self.c.removed += len(d.removed)
        self.c.mined += len(d.mined)
        self.c.replacement_pairs += len(d.replaced)
        for txid in (*d.removed, *d.mined):
            self.gone.add(txid)
        if self.store is not None and (d.removed or d.mined or d.replaced):
            # post-observation state only; features and past assessments are never changed
            from chainwatch.risk.persistence import mark_lifecycle

            at = self.last_message_at
            n = mark_lifecycle(self.store.db, d.mined, "mined", at)
            n += mark_lifecycle(self.store.db, d.removed, "removed", at)
            for old, by in d.replaced:
                n += mark_lifecycle(self.store.db, [old], "replaced", at, replaced_by=by)
            self.c.lifecycle_updates += n
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
                    self.last_error = None
                    self._conn_sequence = None  # a new connection restarts sequence tracking
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
                        if self.clock() - self.connected_since >= HEALTHY_AFTER_S:
                            attempt = 0  # only a stream that keeps working resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake failures, closed connections
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning(json.dumps({"msg": "websocket error", "error": self.last_error}))
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
        now = self.clock()
        if now - first_seen > self.cfg.max_queue_age_s:
            self.c.expired += 1
            return
        if txid in self.gone:  # replaced, evicted or mined while queued: a fetch would 404
            self.c.gone_before_fetch += 1
            return
        if self.store.has(txid):  # idempotent across restarts
            self.c.already_stored += 1
            return
        await self.bucket.acquire()
        if self.clock() - first_seen > self.cfg.max_queue_age_s:  # aged while paused
            self.c.expired += 1
            return
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
            elif resp.status_code >= 500:
                self.c.upstream_errors += 1
                self.bucket.pause(UPSTREAM_ERROR_PAUSE_S)  # do not drain the queue into errors
                error = f"http_{resp.status_code}"
            elif resp.status_code != 200:
                error = f"http_{resp.status_code}"
            else:
                try:
                    payload = resp.json()
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
        if error is not None:
            self.c.failed += 1
            self.store.add_failed(txid, first_seen, self.clock(), error)

    # ---- risk intelligence --------------------------------------------------------------
    def _build_engine(self) -> None:
        from chainwatch.risk import persistence as rp
        from chainwatch.risk.anomaly import RobustTailDetector
        from chainwatch.risk.engine import RiskEngine
        from chainwatch.risk.supervised import SupervisedLayer

        detector = RobustTailDetector()
        detector.warm_start(self.store.recent_features(detector.cfg.window))
        db = self.store.db

        def parents(txids: list[str]) -> tuple[list[dict], int]:
            return rp.observed_parent_matches(db, txids), rp.count_observed(db, txids)

        self.engine = RiskEngine(
            self._load_screener(), detector, SupervisedLayer(None), parents, clock=self.clock
        )

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

        if (
            self.engine is not None
            and rp.latest_version_id(self.store.db) != self._indicator_version
        ):
            self.engine.screener = self._load_screener()
        elif self.engine is not None:  # refresh freshness (checked_at) of unchanged sources
            self.engine.screener.sources = rp.source_status(self.store.db)

    def _store_and_assess(self, f: dict[str, Any], flags: list[str], payload: dict) -> None:
        """The observation and its risk record are committed together, so readers (and the
        event stream) never see a fetched row whose assessment is still pending. A failed
        assessment rolls back only its own savepoint; the observation is kept."""
        from chainwatch.risk import persistence as rp

        with rp.transaction(self.store.db):
            tx_id = self.store.add_fetched_id(f, flags)
            if tx_id is not None:
                self.c.fetched += 1
                self._assess(tx_id, f, payload)

    def _assess(self, tx_id: int, f: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.engine is None or not self.cfg.risk_enabled:
            return
        from chainwatch.risk import persistence as rp
        from chainwatch.risk.engine import alert_specs

        try:
            addrs = feat.extract_addresses(payload)
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
            self.engine.detector.update(f)  # the window holds only transactions before the next
            db = self.store.db
            specs = alert_specs(record)
            with rp.savepoint(db):
                rp.save_addresses(db, tx_id, addrs)
                rp.save_risk(db, tx_id, record)
                for spec in specs:
                    rp.upsert_alert(db, txid=f["txid"], at=record["evaluated_at"], **spec)
            self.c.alerts_raised += len(specs)
        except Exception as e:  # keep the observation even if the assessment fails
            self.c.risk_errors += 1
            self.last_error = f"risk: {type(e).__name__}: {e}"[:200]
            log.exception(json.dumps({"msg": "risk assessment failed", "txid": f["txid"]}))
            return
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
                # if even this write fails (database unusable) the task dies -> fatal exit
                self.store.add_failed(
                    txid, first_seen, self.clock(), f"internal:{type(e).__name__}"
                )
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
            self._build_engine()
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
