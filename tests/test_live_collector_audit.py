"""Collector regressions from the v0.8.1 independent audit (each fails on d8fcb94)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from chainwatch.config import LiveConfig
from chainwatch.live import features as feat
from chainwatch.live.collector import Collector
from chainwatch.live.store import LiveReader, LiveStore
from live_fixtures import esplora_tx, txid


def _cfg(tmp_path, **kw) -> LiveConfig:
    base = dict(enabled=True, db_path=tmp_path / "live.sqlite", sample_rate=1.0,
                fetch_per_minute=6000, fetch_burst=100, fetch_concurrency=1, queue_max=50,
                reconnect_base_s=0.01, reconnect_max_s=0.05, heartbeat_s=0.05,
                stale_after_s=5.0, risk_enabled=False)  # fmt: skip
    base.update(kw)
    return LiveConfig(**base)


def _mock(fn, log=None):
    def handler(request):
        tid = request.url.path.rsplit("/", 1)[-1]
        if log is not None:
            log.append(tid)
        return fn(tid)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _frame(seq, added=()):
    return json.dumps({"mempool-txids": {"sequence": seq, "added": list(added),
                                         "removed": [], "mined": [], "replaced": []}})  # fmt: skip


class FakeWS:
    def __init__(self, frames, hold=3600.0):
        self.frames, self.hold = list(frames), hold

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        pass

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(self.hold)


def test_a_retry_waiting_in_the_queue_is_recorded_on_shutdown(tmp_path):
    """After a 5xx the token bucket pauses 30 s, longer than the retry delay, so the retry
    waits in the queue or for a token, outside the timers: shutdown lost it without a row."""
    t = txid("A")
    c = _cfg(tmp_path, fetch_retry_base_s=0.05)
    col = Collector(c, LiveStore(c.db_path), http=_mock(lambda tid: httpx.Response(503)),
                    connect=lambda *a, **k: FakeWS([_frame(1, [t])]))  # fmt: skip
    asyncio.run(col.run(duration=1.0))
    row = LiveReader(c.db_path).get(t)
    assert col.c.retries_scheduled == 1
    assert row is not None and row["outcome"] == "failed" and row["error"] == "http_503"
    assert col.c.failed == 1 and not col._retry


def test_a_txid_announced_again_while_its_retry_is_pending_is_not_queued_twice(tmp_path):
    t = txid("B")
    calls: list[str] = []

    async def go():
        c = _cfg(tmp_path, fetch_retry_base_s=0.2, dedupe_max=1, fetch_max_attempts=5)
        col = Collector(c, LiveStore(c.db_path),
                        http=_mock(lambda tid: httpx.Response(503), calls))  # fmt: skip
        col.bucket.pause = lambda s: None
        col.handle_frame(_frame(1, [t]))
        await col.fetch_one(*col.queue.get_nowait())  # attempt 1 -> retry timer
        col.handle_frame(_frame(2, [txid("other")]))  # evicts t from the 1-entry LRU
        col.queue.get_nowait()
        col.handle_frame(_frame(3, [t]))  # announced again while its retry is pending
        queued_again = col.queue.qsize()
        col._drain_retries()
        await asyncio.sleep(0.5)  # no timer may fire after the drain
        return col, queued_again

    col, queued_again = asyncio.run(go())
    assert queued_again == 0 and col.queue.qsize() == 0 and len(calls) == 1
    assert col.c.failed == 1  # the pending retry is recorded once


def test_resync_stretches_are_recorded_as_coverage_gaps(tmp_path):
    """mempool repeats its sequence for the whole out-of-sync stretch (clear protection);
    the skipped arrivals left no trace, so availability looked complete."""
    c = _cfg(tmp_path)
    store = LiveStore(c.db_path)
    col = Collector(c, store, http=_mock(lambda t: httpx.Response(503)))
    col.handle_frame(_frame(7, [txid("d0")]))
    for i in range(100):
        col.handle_frame(_frame(7, [txid(f"d{i + 1}")]))
    col.handle_frame(_frame(8, [txid("in-sync")]))  # the backend is in sync again
    rows = store.db.execute("SELECT kind, detail FROM outages").fetchall()
    assert col.c.resync_txids_skipped == 100
    assert len(rows) == 1 and rows[0][0] == "sequence_gap" and "100 announced" in rows[0][1]


@pytest.mark.parametrize("bad", [True, -5, 2**40, "1"])
def test_input_sequence_must_be_a_32_bit_unsigned_integer(bad):
    """A bool or negative sequence switched on rbf_signal and locktime_enforced."""
    t = txid("F")
    p = esplora_tx(t)
    p["vin"][0]["sequence"] = bad
    with pytest.raises(feat.FeatureError, match="sequence"):
        feat.extract(p, txid=t, first_seen=0, fetched_at=0)


def test_fee_is_bounded_when_prevouts_are_missing():
    t = txid("J")
    p = esplora_tx(t)
    p["vin"][0]["prevout"] = None
    p["fee"] = 21_000_000 * 10**8  # MAX_MONEY: fee + outputs exceed the supply
    with pytest.raises(feat.FeatureError, match="fee plus outputs"):
        feat.extract(p, txid=t, first_seen=0, fetched_at=0)


def test_a_locked_database_in_the_failure_path_is_not_fatal(tmp_path):
    """add_failed was called directly: a writer lock > busy timeout killed the worker."""
    t = txid("G")
    c = _cfg(tmp_path)
    store = LiveStore(c.db_path)
    col = Collector(c, store, http=_mock(lambda tid: httpx.Response(400)),
                    connect=lambda *a, **k: FakeWS([_frame(1, [t])]))  # fmt: skip

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    store.add_failed = locked
    asyncio.run(col.run(duration=1.0))
    assert col.fatal is None and col.c.db_errors >= 1 and col.c.failed == 1


class PongWS(FakeWS):
    async def recv(self):
        await asyncio.sleep(0.05)
        return '{"pong": true}'


def test_frames_without_mempool_updates_do_not_satisfy_the_stale_watchdog(tmp_path):
    """Any frame reset the watchdog: pongs kept the state 'connected' with no updates."""
    c = _cfg(tmp_path, stale_after_s=0.3)
    col = Collector(c, LiveStore(c.db_path), http=_mock(lambda t: httpx.Response(503)),
                    connect=lambda *a, **k: PongWS([]))  # fmt: skip
    asyncio.run(col.run(duration=1.5))
    assert col.c.stale_reconnects >= 2 and col.c.received == 0


def test_an_expired_retry_is_one_outcome(tmp_path):
    """It counted as expired (minute_stats.dropped) and failed: two outcomes for one txid."""
    t = txid("I")
    now = [1000.0]

    async def go():
        c = _cfg(tmp_path, max_queue_age_s=10.0, fetch_retry_base_s=1.0)
        store = LiveStore(c.db_path)
        col = Collector(c, store, http=_mock(lambda tid: httpx.Response(503)),
                        clock=lambda: now[0])  # fmt: skip
        col.bucket.pause = lambda s: None
        col.handle_frame(_frame(1, [t]))
        item = col.queue.get_nowait()
        await col.fetch_one(*item)  # 503 -> retry scheduled
        col._retry_timers[t][0].cancel()
        col._requeue(t, item[1])
        now[0] += 60  # aged out while waiting
        await col.fetch_one(*col.queue.get_nowait())
        col.flush()
        return store.db.execute(
            "SELECT sampled, fetched, failed, dropped FROM minute_stats"
        ).fetchall()

    (sampled, fetched, failed, dropped), *_ = asyncio.run(go())
    assert (sampled, fetched, failed, dropped) == (1, 0, 1, 0)
