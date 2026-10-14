"""Regression tests for faults found in the v0.6.0 engineering audit (docs/AUDIT.md):
each test reproduces a failure mode that previously left the collector stalled, crashed,
over-counted or under-reported. Local mocks only; deterministic, no wall-clock sleeps
beyond a few hundred milliseconds."""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
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
            await col.run(duration=1.6)
            return cfg

    cfg = asyncio.run(go())
    disc = [o for o in LiveReader(cfg.db_path).outages(0) if o["kind"] == "disconnected"]
    assert disc and disc[0]["ended_at"] - disc[0]["started_at"] >= 0.9


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
