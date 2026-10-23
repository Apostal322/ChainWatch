"""Regression tests for faults found in the v0.6.0 engineering audit (docs/AUDIT.md):
each test reproduces a failure mode that previously left the collector stalled, crashed,
over-counted or under-reported. Local mocks only; deterministic, no wall-clock sleeps
beyond a few hundred milliseconds."""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import time

import httpx
import pytest

from chainwatch.config import REPO_ROOT, LiveConfig
from chainwatch.live import cli as live_cli
from chainwatch.live import collector as coll
from chainwatch.live import features as feat
from chainwatch.live.collector import Collector, TokenBucket, backoff_delay
from chainwatch.live.store import LiveReader, LiveStore
from live_fixtures import delta, esplora_tx, txid


def _cfg(tmp_path, **kw) -> LiveConfig:
    base = dict(
        enabled=True,
        db_path=tmp_path / "live.sqlite",
        sample_rate=1.0,
        fetch_per_minute=6000,
        fetch_burst=100,
        fetch_concurrency=2,
        queue_max=50,
        reconnect_base_s=0.01,
        reconnect_max_s=0.05,
        heartbeat_s=0.05,
        stale_after_s=5.0,
    )
    base.update(kw)
    return LiveConfig(**base)


def _rest(handler=None, log=None):
    def default(request: httpx.Request) -> httpx.Response:
        tid = request.url.path.rsplit("/", 1)[-1]
        if log is not None:
            log.append(tid)
        return (handler or (lambda t: httpx.Response(200, json=esplora_tx(t))))(tid)

    return httpx.AsyncClient(transport=httpx.MockTransport(default))


# ---- backoff ------------------------------------------------------------------------------
def test_backoff_never_overflows_after_many_failures():
    """Previously 2**1024 raised OverflowError after ~8.5 h of failures and killed the
    WebSocket task for good while the process stayed up (systemd never restarted it)."""
    rng = random.Random(0)
    for attempt in (1023, 1024, 5000, 10**6):
        assert 0.0 <= backoff_delay(attempt, 1.0, 60.0, rng) <= 60.0


# ---- one bad item must not kill a worker; a dead task must stop the process ---------------
def test_worker_survives_unexpected_exception_in_one_item(tmp_path, monkeypatch):
    bad = txid("boom")
    real_extract = feat.extract

    def extract(tx, **kw):
        if kw["txid"] == bad:
            raise RuntimeError("unexpected")
        return real_extract(tx, **kw)

    monkeypatch.setattr(coll.feat, "extract", extract)
    cfg = _cfg(tmp_path, fetch_concurrency=1)
    store = LiveStore(cfg.db_path)
    col = Collector(cfg, store, http=_rest())

    async def go():
        now = time.time()
        for t in (bad, txid("ok1"), txid("ok2")):
            col.queue.put_nowait((t, now))
        worker = asyncio.create_task(col._worker())
        await asyncio.wait_for(col.queue.join(), 5)
        assert not worker.done()  # still alive after the bad item
        worker.cancel()

    asyncio.run(go())
    assert col.c.internal_errors == 1 and col.c.fetched == 2
    row = LiveReader(cfg.db_path).get(bad)
    assert row["outcome"] == "failed" and row["error"] == "internal:RuntimeError"


def test_dead_pipeline_task_stops_the_process_with_a_fatal_state(tmp_path):
    """A heartbeat that cannot write (disk full, locked database) used to die silently:
    the process kept running without collecting. Now run() returns at once with
    ``fatal`` set, and the CLI exits non-zero so systemd restarts it."""

    class BrokenStore(LiveStore):
        calls = 0

        def heartbeat(self, **status):
            BrokenStore.calls += 1
            if BrokenStore.calls > 1:
                raise sqlite3.OperationalError("database or disk is full")
            super().heartbeat(**status)

    async def never_connect(*a, **kw):
        await asyncio.sleep(3600)

    cfg = _cfg(tmp_path)
    store = BrokenStore(cfg.db_path)
    col = Collector(cfg, store, http=_rest(), connect=lambda *a, **k: _Hang())
    t0 = time.monotonic()
    asyncio.run(col.run(duration=30))
    assert time.monotonic() - t0 < 5  # did not wait for the 30 s duration
    assert col.fatal and "heartbeat" in col.fatal and "OperationalError" in col.fatal
    assert col.state == "failed"


class _Hang:
    """A WebSocket connect() context that never completes the handshake."""

    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *exc):
        return False


def test_cli_collect_exits_nonzero_on_fatal(tmp_path, monkeypatch):
    async def fake_run(self, duration=None, max_fetches=None):
        self.fatal = "task worker-0 died: OperationalError: disk I/O error"
        return self.c

    monkeypatch.setattr(Collector, "run", fake_run)
    cfg = tmp_path / "c.yaml"
    base = REPO_ROOT / "configs" / "live.yaml"
    cfg.write_text(f"extends: {base}\nlive:\n  enabled: true\n  db_path: {tmp_path / 'x.sqlite'}\n")
    assert live_cli.main(["--config", str(cfg), "collect", "--duration", "1"]) == 1


# ---- payload ranges: integers that SQLite cannot store crashed the worker -----------------
@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["vout"][0].update(value=2**70),
        lambda t: t["vout"][0].update(value=-41_000),
        lambda t: t["vin"][0]["prevout"].update(value=-5),
        lambda t: t.update(fee=2**64),
        lambda t: t.update(weight=10**9),
        lambda t: t.update(locktime=2**40),
    ],
)
def test_out_of_range_amounts_are_malformed_payloads(mutate):
    tx = esplora_tx(txid("range"))
    mutate(tx)
    with pytest.raises(feat.FeatureError):
        feat.extract(tx, txid=txid("range"), first_seen=1.0, fetched_at=2.0)


def test_out_of_range_payload_is_a_failed_row_not_a_crash(tmp_path):
    def handler(tid):
        tx = esplora_tx(tid)
        tx["fee"] = 2**70
        return httpx.Response(200, json=tx)

    cfg = _cfg(tmp_path)
    col = Collector(cfg, LiveStore(cfg.db_path), http=_rest(handler))
    asyncio.run(col.fetch_one(txid("huge"), time.time()))
    assert col.c.malformed_payloads == 1 and col.c.internal_errors == 0


def test_missing_status_is_unknown_not_unconfirmed(tmp_path):
    tx = esplora_tx(txid("nostatus"))
    tx.pop("status", None)
    f = feat.extract(tx, txid=txid("nostatus"), first_seen=1.0, fetched_at=2.0)
    assert f["confirmed_at_fetch"] is None
    store = LiveStore(tmp_path / "s.sqlite")
    assert store.add_fetched(f, [])
    assert LiveReader(store.path).get(txid("nostatus"))["confirmed_at_fetch"] is None


def test_timelocked_only_when_locktime_is_enforced():
    final = esplora_tx(txid("final"), locktime=899_000, sequence=0xFFFFFFFF)
    enforced = esplora_tx(txid("enf"), locktime=899_000, sequence=0xFFFFFFFE)
    f1 = feat.extract(final, txid=txid("final"), first_seen=1, fetched_at=2)
    f2 = feat.extract(enforced, txid=txid("enf"), first_seen=1, fetched_at=2)
    assert not f1["locktime_enforced"] and "timelocked" not in feat.indicators(f1)
    assert f2["locktime_enforced"] and "timelocked" in feat.indicators(f2)


# ---- removed/mined txids are not fetched (they would 404 and waste the REST budget) -------
def test_removed_or_mined_txids_are_skipped_before_fetch(tmp_path):
    cfg = _cfg(tmp_path)
    log: list[str] = []
    col = Collector(cfg, LiveStore(cfg.db_path), http=_rest(log=log))
    a, b, c = txid("rep-a"), txid("mined-b"), txid("keep-c")
    col.handle_frame(delta([a, b, c], seq=1))
    col.handle_frame(
        json.dumps(
            {
                "mempool-txids": {
                    "sequence": 2,
                    "added": [txid("rep-a2")],
                    "removed": [a],
                    "mined": [b],
                    "replaced": [{"replaced": a, "by": txid("rep-a2")}],
                }
            }
        )
    )
    assert (col.c.removed, col.c.mined, col.c.replacement_pairs) == (1, 1, 1)

    async def drain():
        while not col.queue.empty():
            t, seen = col.queue.get_nowait()
            await col.fetch_one(t, seen)

    asyncio.run(drain())
    assert col.c.gone_before_fetch == 2 and a not in log and b not in log
    assert c in log and col.c.fetched == 2  # c and the replacement


# ---- REST budget behaviour --------------------------------------------------------------
def test_token_bucket_does_not_spin_on_float_rounding():
    """With a fake clock at ~1e3 s, refills landed at 0.99999… tokens and the requested
    sleep was below the clock's resolution: acquire() looped forever."""
    t = [1030.0]

    async def fake_sleep(s):
        t[0] += s

    b = TokenBucket(6000, burst=100, clock=lambda: t[0], sleep=fake_sleep)
    b.pause(0)

    async def go():
        for _ in range(5):
            await asyncio.wait_for(b.acquire(), 2)

    asyncio.run(go())


def test_no_burst_right_after_a_429_pause():
    t = [0.0]

    async def fake_sleep(s):
        t[0] += s

    b = TokenBucket(60, burst=5, clock=lambda: t[0], sleep=fake_sleep)

    async def go():
        b.pause(60)
        stamps = []
        for _ in range(3):
            await b.acquire()
            stamps.append(t[0])
        return stamps

    stamps = asyncio.run(go())
    # resumes at t=60 and then paces at 1 request/s instead of bursting 5 at once
    assert stamps[0] == pytest.approx(61.0) and stamps[2] - stamps[0] == pytest.approx(2.0)


def test_5xx_pauses_fetching_and_items_expire_after_the_pause(tmp_path):
    cfg = _cfg(tmp_path, max_queue_age_s=10)
    log: list[str] = []
    col = Collector(cfg, LiveStore(cfg.db_path), http=_rest(lambda t: httpx.Response(503), log=log))
    now = [1000.0]
    col.clock = lambda: now[0]
    col.bucket = TokenBucket(6000, 100, clock=lambda: now[0], sleep=_advance(now))

    async def go():
        await col.fetch_one(txid("e1"), now[0])  # 503 -> pause 30 s
        await col.fetch_one(txid("e2"), now[0] - 1)  # waits out the pause, now too old
        return col

    asyncio.run(go())
    assert col.c.upstream_errors == 1 and col.c.expired == 1 and log == [txid("e1")]


def _advance(now):
    async def sleep(s):
        now[0] += s

    return sleep


# ---- outage timing ------------------------------------------------------------------------
def test_disconnect_outage_starts_at_the_last_message(tmp_path):
    """A silent stream is detected only by the stale watchdog; the outage must start at the
    last message, not at detection time (that under-reported up to stale_after_s)."""
    from websockets.asyncio.server import serve

    async def handler(ws):
        await ws.recv()
        await ws.send(delta([txid("s1")], seq=1))
        await ws.wait_closed()  # silent until the client's 1 s watchdog gives up

    async def go():
        async with serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _cfg(tmp_path, ws_url=f"ws://127.0.0.1:{port}", stale_after_s=1.0)
            col = Collector(cfg, LiveStore(cfg.db_path), http=_rest())
            # long enough for a slow runner to connect, receive, and let the watchdog fire
            # (1.6 s failed on Windows CI); reconnects add outages, each starting at its last
            # message
            await col.run(duration=4.0)
            return cfg

    cfg = asyncio.run(go())
    disc = [o for o in LiveReader(cfg.db_path).outages(0) if o["kind"] == "disconnected"]
    assert disc and all(o["ended_at"] - o["started_at"] >= 0.9 for o in disc)


# ---- backup / restore -------------------------------------------------------------------
def test_backup_refuses_missing_database_and_keep_zero(tmp_path):
    with pytest.raises(FileNotFoundError):
        live_cli.backup(tmp_path / "typo.sqlite", tmp_path / "b", keep=7)
    assert not (tmp_path / "typo.sqlite").exists()  # no empty database was created
    store = LiveStore(tmp_path / "live.sqlite")
    store.close()
    with pytest.raises(ValueError):
        live_cli.backup(tmp_path / "live.sqlite", tmp_path / "b", keep=0)


def test_restore_round_trip_and_refuses_while_collector_runs(tmp_path):
    db = tmp_path / "live.sqlite"
    store = LiveStore(db)
    f = feat.extract(esplora_tx(txid("r1")), txid=txid("r1"), first_seen=1, fetched_at=2)
    store.add_fetched(f, [])
    store.heartbeat(state="stopped", updated_at=time.time(), started_at=1.0)
    backup = live_cli.backup(db, tmp_path / "b", keep=3)
    store.db.execute("DELETE FROM tx")  # data loss after the backup
    store.heartbeat(state="connected", updated_at=time.time(), started_at=1.0)
    store.close()

    with pytest.raises(RuntimeError, match="collector is running"):
        live_cli.restore(backup, db, heartbeat_s=5.0)
    con = sqlite3.connect(db)
    con.execute("UPDATE collector SET state = 'stopped'")
    con.commit()
    con.close()
    live_cli.restore(backup, db, heartbeat_s=5.0)
    assert LiveReader(db).get(txid("r1")) is not None
    assert list(tmp_path.glob("live.sqlite.pre-restore-*"))  # the replaced file is kept

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        live_cli.restore(corrupt, db, heartbeat_s=5.0)


def test_stats_totals_survive_lost_minute_counters(tmp_path):
    """A SIGKILL loses up to heartbeat_s of per-minute counters; durable rows must win."""
    store = LiveStore(tmp_path / "s.sqlite")
    now = time.time()
    for i in range(5):
        t = txid(f"st{i}")
        f = feat.extract(esplora_tx(t), txid=t, first_seen=now - 10, fetched_at=now - 5)
        store.add_fetched(f, [])
    store.add_failed(txid("stf"), now - 10, now - 5, "not_found")
    store.bump(now - 5, received=40, sampled=6, fetched=2)  # 3 fetches were never flushed
    totals = LiveReader(store.path).stats(60, now)["totals"]
    assert totals["fetched"] == 5 and totals["failed"] == 1 and totals["received"] == 40


@pytest.mark.parametrize(
    "content",
    [None, "live:\n  enabled: [unterminated\n", "live:\n  sample_rate: 7\n", "live:\n  bogus: 1\n"],
)
def test_bad_config_is_a_clean_ex_config_exit(tmp_path, capsys, content):
    """Missing, unparsable or invalid configs exit 78 with one line (systemd does not
    restart on 78: RestartPreventExitStatus), instead of a traceback and a restart loop."""
    cfg = tmp_path / "c.yaml"
    if content is not None:
        base = REPO_ROOT / "configs" / "live.yaml"
        cfg.write_text(f"extends: {base}\n{content}" if "enabled" not in content else content)
    with pytest.raises(SystemExit) as exc:
        live_cli.main(["--config", str(cfg), "collect", "--duration", "1"])
    assert exc.value.code == live_cli.EX_CONFIG
    assert "config error" in capsys.readouterr().err


def test_clean_stop_leaves_the_database_within_the_row_cap(tmp_path):
    """Retention runs every 60 s, so a running collector can exceed retention_rows by up
    to a minute of fetches (the 110-min soak saw 3,026 / 3,000); a clean stop prunes."""
    cfg = _cfg(tmp_path, retention_rows=10)
    store = LiveStore(cfg.db_path)
    now = time.time()
    for i in range(25):
        t = txid(f"cap{i}")
        store.add_fetched(feat.extract(esplora_tx(t), txid=t, first_seen=now, fetched_at=now), [])
    col = Collector(cfg, store, http=_rest(), connect=lambda *a, **k: _Hang())
    asyncio.run(col.run(duration=0.3))
    assert LiveReader(cfg.db_path).list(limit=1, outcome=None)[0] == 10


# ---- v0.8 audit (docs/AUDIT.md, C-findings) --------------------------------------------------
def _frame(seq, added=(), removed=(), mined=(), replaced=()):
    return json.dumps(
        {
            "mempool-txids": {
                "sequence": seq,
                "added": list(added),
                "removed": list(removed),
                "mined": list(mined),
                "replaced": [{"replaced": a, "by": b} for a, b in replaced],
            }
        }
    )


def _mock(fn):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: fn(r.url.path.rsplit("/", 1)[-1]))
    )


def test_mined_overrides_replaced(tmp_path):
    """mempool.space can report A replaced by B, then A mined (the conflict lost)."""
    c = _cfg(tmp_path)
    a, b = txid("A"), txid("B")
    col = Collector(
        c, LiveStore(c.db_path), http=_mock(lambda t: httpx.Response(200, json=esplora_tx(t)))
    )
    col.handle_frame(_frame(1, added=[a]))
    asyncio.run(col.fetch_one(*col.queue.get_nowait()))
    col.handle_frame(_frame(2, added=[b], removed=[a], replaced=[(a, b)]))
    col.handle_frame(_frame(3, mined=[a], replaced=[(b, a)]))
    lc = LiveReader(c.db_path).get(a)["lifecycle"]
    assert lc["state"] == "mined" and lc["replaced_by"] is None


def test_memo_keeps_replaced_when_removed_follows():
    """mempool.space reports a block conflict as replaced, then removed in the next delta;
    the memo stored the in-flight transaction as removed and lost replaced_by, unlike
    mark_lifecycle."""
    memo = coll.LifecycleMemo(10)
    memo.add("t", "replaced", 1.0, "by")
    memo.add("t", "removed", 2.0)
    assert memo.get("t") == ("replaced", 2.0, "by")


def test_lifecycle_event_during_fetch_is_not_lost(tmp_path):
    c = _cfg(tmp_path)
    a = txid("inflight")
    box = {}

    def handler(t):  # the transaction is mined while its GET is in flight
        box["col"].handle_frame(_frame(2, mined=[a]))
        return httpx.Response(200, json=esplora_tx(t, confirmed=True))

    col = box["col"] = Collector(c, LiveStore(c.db_path), http=_mock(handler))
    col.handle_frame(_frame(1, added=[a]))
    asyncio.run(col.fetch_one(*col.queue.get_nowait()))
    assert LiveReader(c.db_path).get(a)["lifecycle"]["state"] == "mined"


def test_gone_is_rechecked_after_waiting_for_a_token(tmp_path):
    c = _cfg(tmp_path)
    a, now, calls = txid("waiting"), [1e6], []
    col = Collector(
        c,
        LiveStore(c.db_path),
        clock=lambda: now[0],
        http=_mock(lambda t: calls.append(t) or httpx.Response(200, json=esplora_tx(t))),
    )

    async def sleep(s):  # the txid leaves the mempool while the worker waits for a token
        col.gone.add(a, "removed", now[0])
        now[0] += s

    col.bucket = TokenBucket(6000, 100, clock=lambda: now[0], sleep=sleep)
    col.bucket.pause(60)
    asyncio.run(col.fetch_one(a, now[0]))
    assert calls == [] and col.c.gone_before_fetch == 1


def test_block_conflicts_are_not_fetched(tmp_path):
    """A block message lists conflicts only in ``replaced``; ``removed`` comes a delta later."""
    c = _cfg(tmp_path)
    x, y = txid("conflict"), txid("winner")
    col = Collector(c, LiveStore(c.db_path), http=_rest())
    col.handle_frame(_frame(1, added=[x]))
    col.handle_frame(_frame(2, mined=[y], replaced=[(x, y)]))
    asyncio.run(col.fetch_one(*col.queue.get_nowait()))
    assert col.c.gone_before_fetch == 1 and col.c.fetched == 0


def test_disk_full_in_the_risk_write_does_not_count_a_phantom_fetch(tmp_path):
    """SQLITE_FULL rolls back the whole transaction (not just the savepoint): nothing may be
    counted as fetched, and the error must surface (the worker records a failed row)."""
    c = _cfg(tmp_path)
    store = LiveStore(c.db_path)
    t = txid("full")
    p = esplora_tx(t, out_values=[600 + i for i in range(500)], weight=86_000, size=21_500)
    col = Collector(c, store, http=None)
    col._build_engine()
    pages = store.db.execute("PRAGMA page_count").fetchone()[0]
    store.db.execute(f"PRAGMA max_page_count = {pages + 4}")
    f = feat.extract(p, txid=t, first_seen=time.time(), fetched_at=time.time())
    with pytest.raises(sqlite3.Error):
        col._store_and_assess(f, [], p)
    assert col.c.fetched == 0 and col.c.risk_assessed == 0
    assert not store.db.in_transaction


def test_db_error_in_frame_does_not_drop_new_txids_or_the_connection(tmp_path):
    c = _cfg(tmp_path)
    store = LiveStore(c.db_path)
    store.db.execute("PRAGMA busy_timeout = 50")
    col = Collector(c, store, http=None)
    other = sqlite3.connect(c.db_path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # e.g. the indicators CLI holding the write lock
    try:
        col.handle_frame(_frame(1, added=[txid("n1"), txid("n2")], removed=[txid("old")]))
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert col.c.queued == 2 and col.c.db_errors == 1  # handled here, not as a network error


def test_sum_of_outputs_above_max_money_is_malformed():
    t = txid("sum")
    tx = esplora_tx(t, out_values=[feat.MAX_MONEY_SAT] * 2, fee=0)
    for i in tx["vin"]:
        i["prevout"] = None
    with pytest.raises(feat.FeatureError, match="sum of outputs"):
        feat.extract(tx, txid=t, first_seen=1, fetched_at=2)


def test_fee_must_equal_inputs_minus_outputs_when_all_prevouts_are_known():
    t = txid("fee")
    tx = esplora_tx(t, fee=1_000)
    tx["fee"] = 50_000
    with pytest.raises(feat.FeatureError, match="fee"):
        feat.extract(tx, txid=t, first_seen=1, fetched_at=2)
    tx = esplora_tx(t, weight=100, size=222)  # weight below size: impossible
    with pytest.raises(feat.FeatureError, match="weight"):
        feat.extract(tx, txid=t, first_seen=1, fetched_at=2)


def test_huge_sequence_does_not_break_the_heartbeat(tmp_path):
    c = _cfg(tmp_path)
    col = Collector(c, LiveStore(c.db_path), http=None)
    col.handle_frame(_frame(2**63, added=[]))
    col.flush()  # used to raise OverflowError in the heartbeat task -> fatal exit loop
    assert col.last_sequence is None


def test_reader_get_survives_a_concurrent_prune(tmp_path):
    store = LiveStore(tmp_path / "s.sqlite")
    t = txid("victim")
    store.add_fetched(feat.extract(esplora_tx(t), txid=t, first_seen=1, fetched_at=2), [])
    real = sqlite3.Connection.execute
    n = [0]

    class Conn(sqlite3.Connection):
        def execute(self, sql, *a):
            n[0] += 1
            if n[0] == 3:  # retention deletes the row between the reader's queries
                store.db.execute("DELETE FROM tx WHERE txid = ?", (t,))
            return real(self, sql, *a)

    orig = sqlite3.connect
    sqlite3.connect = lambda *a, **k: orig(*a, factory=Conn, **k)
    try:
        got = LiveReader(store.path).get(t)  # a consistent row (snapshot), never a TypeError
    finally:
        sqlite3.connect = orig
    assert got is None or got["txid"] == t


def test_rest_read_has_an_overall_deadline(tmp_path):
    """httpx's timeout is per read: a body that trickles in kept a worker busy forever."""

    async def drip(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 100000\r\n\r\n")
        try:
            while True:
                writer.write(b" ")
                await writer.drain()
                await asyncio.sleep(0.2)
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def go():
        srv = await asyncio.start_server(drip, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = _cfg(tmp_path, rest_base=f"http://127.0.0.1:{port}/api", request_timeout_s=0.5,
                 fetch_max_attempts=1)  # fmt: skip
        col = Collector(c, LiveStore(c.db_path), http=httpx.AsyncClient(timeout=0.5))
        try:
            await asyncio.wait_for(col.fetch_one(txid("drip"), time.time()), 3.0)
        finally:
            srv.close()
        return col.c

    c = asyncio.run(go())
    assert c.timeouts == 1 and c.failed == 1


def test_concurrent_migrations_do_not_collide(tmp_path):
    """Two processes opening an old database at once used to race on ALTER TABLE: both read
    the columns, both add them, the second failed ("duplicate column name"). Here a second
    migration starts just before our first ALTER; it must wait for our transaction."""
    import threading

    from chainwatch.live.store import SCHEMA
    from chainwatch.risk import persistence as rp

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)  # a v0.6 database: no risk tables, no lifecycle columns
    con.close()
    errors: list[Exception] = []

    def other_process():
        other = sqlite3.connect(path, isolation_level=None, timeout=10)
        try:
            rp.migrate(other)
        except Exception as e:  # pragma: no cover - the failure being guarded against
            errors.append(e)
        finally:
            other.close()

    class Interleave:  # connection proxy: the other migration starts before our first ALTER
        def __init__(self, db):
            self.db, self.thread = db, None

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *a):
            if sql.startswith("ALTER TABLE tx") and self.thread is None:
                self.thread = threading.Thread(target=other_process)
                self.thread.start()
                self.thread.join(timeout=1.0)  # old code: it finishes first (no lock held)
            return self.db.execute(sql, *a)

    mine = Interleave(sqlite3.connect(path, isolation_level=None, timeout=10))
    rp.migrate(mine)
    mine.thread.join()
    mine.db.close()
    assert errors == []
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(tx)")}
    assert {"lifecycle", "lifecycle_at", "replaced_by"} <= cols


# ---- bounded retries of transient fetch failures --------------------------------------------
def _retry_run(tmp_path, answers, **kw):
    """Drive one txid through fetch_one and its retries; ``answers`` is consumed per request."""
    calls, acquired, delays = [], [0], []
    t = txid("retry")

    def handler(tid):
        calls.append(tid)
        a = answers[min(len(calls), len(answers)) - 1]
        return a(tid) if callable(a) else a

    async def go():
        c = _cfg(tmp_path, **{"fetch_retry_base_s": 0.02, **kw})
        col = Collector(c, LiveStore(c.db_path), http=_mock(handler))
        col.bucket.pause = lambda s: None
        real_acquire = col.bucket.acquire

        async def acquire():
            acquired[0] += 1
            await real_acquire()

        col.bucket.acquire = acquire
        loop = asyncio.get_running_loop()
        real_later = loop.call_later

        def later(delay, callback, *a, **kw):
            if callback == col._requeue:  # not asyncio's own timers (wait_for on 3.11)
                delays.append(delay)
            return real_later(delay, callback, *a, **kw)

        loop.call_later = later
        try:
            await col.fetch_one(t, col.clock())
            while col._retry_timers or not col.queue.empty():
                item = await asyncio.wait_for(col.queue.get(), 2.0)
                await col.fetch_one(*item)
        finally:
            loop.call_later = real_later
        return col

    col = asyncio.run(go())
    return col, LiveReader(col.cfg.db_path).get(t), calls, acquired[0], delays


def test_transient_failures_are_retried_and_recovered(tmp_path):
    ok = lambda tid: httpx.Response(200, json=esplora_tx(tid))  # noqa: E731
    col, row, calls, acquired, delays = _retry_run(
        tmp_path, [httpx.Response(503), httpx.Response(429), ok]
    )
    c = col.c
    assert row["outcome"] == "fetched" and len(calls) == 3 == acquired  # budget per attempt
    assert (c.retries_scheduled, c.retries_succeeded, c.failed, c.fetched) == (2, 1, 0, 1)
    # exponential with jitter: [0.5, 1] x base x 2^(attempt-1)
    assert 0.01 <= delays[0] <= 0.02 and 0.02 <= delays[1] <= 0.04


def test_retries_are_bounded_by_the_attempt_cap(tmp_path):
    col, row, calls, _, _ = _retry_run(tmp_path, [httpx.Response(503)], fetch_max_attempts=3)
    c = col.c
    assert len(calls) == 3 and row["outcome"] == "failed" and row["error"] == "http_503"
    assert (c.retries_scheduled, c.retries_exhausted, c.failed) == (2, 1, 1)


def test_permanent_failures_are_not_retried(tmp_path):
    col, row, calls, _, _ = _retry_run(tmp_path, [httpx.Response(200, json={"txid": "x"})])
    assert len(calls) == 1 and col.c.retries_scheduled == 0
    assert row["error"].startswith("malformed_payload")
    _, row, calls, _, _ = _retry_run(tmp_path / "b", [httpx.Response(400)])
    assert len(calls) == 1 and row["error"] == "http_400"


def test_not_found_gets_one_retry_for_indexing_lag(tmp_path):
    col, row, calls, _, _ = _retry_run(tmp_path, [httpx.Response(404)], fetch_max_attempts=5)
    assert len(calls) == 2 and row["error"] == "not_found" and col.c.retries_scheduled == 1


def test_no_retry_outlives_the_queue_age_limit(tmp_path):
    col, row, calls, _, _ = _retry_run(
        tmp_path, [httpx.Response(503)], fetch_retry_base_s=60.0, max_queue_age_s=30.0
    )
    assert len(calls) == 1 and row["outcome"] == "failed" and col.c.retries_exhausted == 1


def test_pending_retries_are_recorded_on_shutdown(tmp_path):
    t = txid("pending")

    async def go():
        c = _cfg(tmp_path, fetch_retry_base_s=30.0)
        col = Collector(c, LiveStore(c.db_path), http=_mock(lambda tid: httpx.Response(503)))
        col.bucket.pause = lambda s: None
        await col.fetch_one(t, col.clock())
        assert col._retry_timers  # waiting 15-30 s
        col._drain_retries()
        return col

    col = asyncio.run(go())
    row = LiveReader(col.cfg.db_path).get(t)
    assert row["outcome"] == "failed" and row["error"] == "http_503" and not col._retry_timers


# ---- v0.8.1 audit: restore race and backup hygiene --------------------------------------------
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")


def _db_with_backup(tmp_path):
    db = tmp_path / "live.sqlite"
    store = LiveStore(db)
    f = feat.extract(esplora_tx(txid("keep")), txid=txid("keep"), first_seen=1, fetched_at=2)
    store.add_fetched(f, [])
    store.heartbeat(state="stopped", updated_at=1.0, started_at=1.0)
    store.close()
    return db, live_cli.backup(db, tmp_path / "b", keep=3)


@posix_only
def test_restore_is_refused_while_any_writer_has_the_database_open(tmp_path):
    """The heartbeat check alone let a restore run while a just-started collector (state
    still 'stopped') or a timer-run indicator refresh had the database open: their writes
    went to the renamed files, or their fresh WAL was paired with the restored file."""
    db, backup = _db_with_backup(tmp_path)
    writer = LiveStore(db)  # e.g. a collector restarted by systemd, before its heartbeat
    with pytest.raises(RuntimeError, match="in use"):
        live_cli.restore(backup, db, heartbeat_s=5.0)
    writer.close()
    live_cli.restore(backup, db, heartbeat_s=5.0)


@posix_only
def test_no_writer_can_open_the_database_during_a_restore(tmp_path):
    from chainwatch.live.store import DatabaseBusy, acquire_lock

    db, _ = _db_with_backup(tmp_path)
    lock = acquire_lock(db, exclusive=True)  # what restore holds
    with pytest.raises(DatabaseBusy):
        LiveStore(db)
    lock.close()
    LiveStore(db).close()


def test_restore_keeps_uncheckpointed_data_in_the_pre_restore_copy(tmp_path):
    """The WAL (up to an hour after a crash) was renamed to a name SQLite never pairs with
    the renamed main file: the pre-restore copy silently lacked those rows."""
    db, backup = _db_with_backup(tmp_path)
    other = sqlite3.connect(db)  # e.g. what a crashed process left in the WAL
    other.execute("PRAGMA wal_autocheckpoint=0")
    other.execute("CREATE TABLE late (x INTEGER)")
    other.executemany("INSERT INTO late VALUES (?)", [(i,) for i in range(100)])
    other.commit()
    live_cli.restore(backup, db, heartbeat_s=5.0)
    other.close()
    (pre,) = tmp_path.glob("live.sqlite.pre-restore-*")
    assert sqlite3.connect(pre).execute("SELECT COUNT(*) FROM late").fetchone()[0] == 100
    restored = sqlite3.connect(db)
    assert restored.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == 1
    assert "late" not in {r[0] for r in restored.execute("SELECT name FROM sqlite_master")}


def test_backup_rotation_counts_only_verified_copies(tmp_path, monkeypatch):
    """A failed integrity check left the file under its final name (it then counted as one
    of the kept copies); interrupted .partial files were never removed; the rotation glob
    deleted an operator's own live-manual-*.sqlite."""
    db, _ = _db_with_backup(tmp_path)
    dest = tmp_path / "b"
    manual = dest / "live-manual-before-upgrade.sqlite"
    manual.write_bytes(b"x")
    stale = dest / "live-20260101T000000Z.sqlite.partial"
    stale.write_bytes(b"x")
    for i in range(4):
        live_cli.backup(db, dest, keep=2, now=1_800_000_000 + i)
    ours = sorted(p.name for p in dest.glob("live-2*.sqlite"))
    assert len(ours) == 2 and manual.exists() and not stale.exists()
    for name in ours:
        digest, fname = (dest / f"{name}.sha256").read_text().split()
        assert fname == name and len(digest) == 64

    def corrupt_check(self, dest_path):  # the copy fails its integrity check
        raise RuntimeError(f"backup {dest_path} failed its integrity check")

    monkeypatch.setattr(LiveStore, "backup", corrupt_check)
    with pytest.raises(RuntimeError, match="integrity"):
        live_cli.backup(db, dest, keep=2, now=1_900_000_000)
    assert sorted(p.name for p in dest.glob("live-2*.sqlite")) == ours


def _collect_argv(tmp_path, db):
    from chainwatch.config import REPO_ROOT

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"extends: {REPO_ROOT / 'configs' / 'live.yaml'}\nlive:\n  db_path: {db}\n")
    return ["--config", str(cfg), "collect", "--duration", "0.1"]


def test_collector_refuses_a_corrupted_database_without_a_restart_loop(tmp_path, capsys):
    """A corrupted file was opened and written to; only the nightly backup noticed."""
    db = tmp_path / "live.sqlite"
    store = LiveStore(db)
    for i in range(400):
        t = txid(f"c{i}")
        store.add_fetched(feat.extract(esplora_tx(t), txid=t, first_seen=1, fetched_at=2), [])
    store.checkpoint()
    store.close()
    size = db.stat().st_size
    with open(db, "r+b") as fh:  # damage pages in the middle, keep the schema page
        fh.seek(size // 2)
        fh.write(b"\xff" * 8192)
    assert live_cli.main(_collect_argv(tmp_path, db)) == 78
    assert "integrity" in capsys.readouterr().err


@posix_only
def test_collector_start_during_a_restore_is_a_temporary_failure(tmp_path, capsys):
    from chainwatch.live.store import acquire_lock

    db, _ = _db_with_backup(tmp_path)
    lock = acquire_lock(db, exclusive=True)
    try:
        assert live_cli.main(_collect_argv(tmp_path, db)) == 75  # systemd retries later
    finally:
        lock.close()
    assert "being restored" in capsys.readouterr().err
