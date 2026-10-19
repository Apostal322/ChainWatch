"""Live monitor: protocol, features, storage, rate limiting, backpressure, reconnection and
the collector's end-to-end path, all against deterministic local mocks (no internet)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import random

import httpx
import pytest

from chainwatch.config import LiveConfig
from chainwatch.live import features as feat
from chainwatch.live.collector import (
    Collector,
    SeenSet,
    TokenBucket,
    backoff_delay,
    retry_after_seconds,
    sampled,
)
from chainwatch.live.protocol import SUBSCRIBE, parse_message, valid_txid
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


def _rest(responses: dict[str, httpx.Response] | None = None, log: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        tid = request.url.path.rsplit("/", 1)[-1]
        if log is not None:
            log.append(tid)
        if responses and tid in responses:
            return responses[tid]
        return httpx.Response(200, json=esplora_tx(tid))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- protocol ------------------------------------------------------------------------------
def test_parse_documented_delta():
    a, b, c = txid(1), txid(2), txid(3)
    r = parse_message(delta([a, b], seq=79919, removed=[c], replaced=[(c, a)]))
    assert not r.malformed and r.delta is not None
    assert r.delta.sequence == 79919
    assert r.delta.added == (a, b) and r.delta.removed == (c,) and r.delta.replaced == ((c, a),)
    assert r.delta.invalid == 0
    assert json.loads(SUBSCRIBE) == {"track-mempool-txids": True}


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe", "not json", "[1,2]", '"str"', '{"mempool-txids": [1]}', "{" * 5000],
)
def test_malformed_frames_never_raise(raw):
    assert parse_message(raw).malformed


def test_invalid_txids_are_dropped_and_counted():
    good = txid(1)
    bad = [good.upper(), good[:-1], 123, None, "zz" * 32, good + "0"]
    msg = json.dumps(
        {
            "mempool-txids": {
                "sequence": "x",
                "added": [good, *bad],
                "replaced": [{"replaced": good}],
            }
        }
    )
    r = parse_message(msg)
    assert r.delta.added == (good,) and r.delta.sequence is None
    assert r.delta.invalid == len(bad) + 1
    assert not valid_txid(good.upper())


def test_other_keys_and_pong_are_ignored():
    r = parse_message(json.dumps({"pong": True, "conversions": {}}))
    assert r.pong and r.delta is None and not r.malformed and r.ignored_keys == ["conversions"]


# ---- features ------------------------------------------------------------------------------
def test_feature_extraction_semantics():
    tid = txid("f")
    tx = esplora_tx(
        tid, n_in=2, out_values=(70_000, 29_000, 300), fee=1_234, weight=561, op_return=True
    )
    f = feat.extract(tx, txid=tid, first_seen=100.0, fetched_at=101.5)
    assert f["vsize"] == 141  # ceil(561 / 4)
    assert f["fee_rate"] == round(1234 / 141, 3)
    assert (f["n_inputs"], f["n_outputs"], f["n_parent_txs"]) == (2, 4, 2)
    assert f["output_value_total"] == 99_300  # includes change: never called an amount
    assert "amount" not in json.dumps(list(f)) and "payment" not in f
    assert f["input_value_total"] == 99_300 + 1_234  # inputs = outputs + fee
    assert f["n_dust_outputs"] == 1 and f["n_op_return"] == 1  # OP_RETURN is not dust
    assert f["confirmed_at_fetch"] is False and f["first_seen"] == 100.0
    assert f["input_refs"][0].endswith(":0") and len(f["input_refs"]) == 2
    assert not f["rbf_signal"] and f["segwit"] and f["locktime_kind"] == "none"


def test_rbf_locktime_and_indicators():
    tid = txid("r")
    f = feat.extract(
        esplora_tx(tid, sequence=0xFFFFFFFD, locktime=899_999, version=7),
        txid=tid,
        first_seen=0,
        fetched_at=0,
    )
    assert f["rbf_signal"] and f["locktime_kind"] == "height"
    flags = feat.indicators(f, recent_fee_rates=[0.1] * 30)
    assert {"rbf_signal", "timelocked", "nonstandard_version", "fee_rate_outlier"} <= set(flags)
    assert set(flags) <= set(feat.INDICATORS)
    assert "fee_rate_outlier" not in feat.indicators(f, recent_fee_rates=[0.1] * 5)  # too few


def test_fan_out_and_consolidation_flags():
    a, b = txid("fan"), txid("con")
    fan = feat.extract(esplora_tx(a, out_values=[10_000] * 60), txid=a, first_seen=0, fetched_at=0)
    con = feat.extract(
        esplora_tx(b, n_in=60, out_values=(500_000,)), txid=b, first_seen=0, fetched_at=0
    )
    assert "high_fan_out" in feat.indicators(fan) and len(fan["output_values"]) == feat.MAX_LISTED
    assert "consolidation" in feat.indicators(con) and len(con["input_refs"]) == feat.MAX_LISTED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.update(txid=txid("other")),
        lambda t: t.update(vin=[]),
        lambda t: t.update(weight="x"),
        lambda t: t.update(fee=-1),
        lambda t: t["vout"].append({"value": "1"}),
    ],
)
def test_malformed_payloads_rejected(mutate):
    tid = txid("m")
    tx = esplora_tx(tid)
    mutate(tx)
    with pytest.raises(feat.FeatureError):
        feat.extract(tx, txid=tid, first_seen=0, fetched_at=0)


def test_missing_prevout_value_gives_unknown_input_total():
    tid = txid("p")
    tx = esplora_tx(tid)
    tx["vin"][0]["prevout"] = None
    assert feat.extract(tx, txid=tid, first_seen=0, fetched_at=0)["input_value_total"] is None


# ---- dedupe, sampling, backoff, rate limiting ------------------------------------------------
def test_seen_set_is_bounded_lru():
    s = SeenSet(3)
    assert all(s.add(x) for x in "abc") and not s.add("a")
    s.add("d")  # evicts "b" (least recently seen)
    assert len(s) == 3 and s.add("b") and not s.add("a")


def test_sampling_is_deterministic_and_close_to_rate():
    ids = [txid(i) for i in range(4000)]
    picks = [sampled(t, 0.1) for t in ids]
    assert picks == [sampled(t, 0.1) for t in ids]
    assert 0.08 < sum(picks) / len(ids) < 0.12
    assert all(sampled(t, 1.0) for t in ids[:10])


def test_backoff_is_exponential_jittered_and_capped():
    rng = random.Random(0)
    for attempt in range(12):
        d = backoff_delay(attempt, 1.0, 60.0, rng)
        assert 0.0 <= d <= min(60.0, 2**attempt)
    assert retry_after_seconds("7") == 7.0 and retry_after_seconds(None) == 60.0
    # HTTP-date form: seconds until then (a date in the past means "now": the 1 s minimum)
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 1.0
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 120) == 120.0
    assert retry_after_seconds("not a date") == 60.0
    assert retry_after_seconds("100000") == 600.0


def test_token_bucket_limits_rate_and_honours_pause():
    t = [0.0]
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
        t[0] += s

    async def go():
        b = TokenBucket(60, burst=2, clock=lambda: t[0], sleep=fake_sleep)
        for _ in range(5):
            await b.acquire()
        before = t[0]
        b.pause(30)
        await b.acquire()
        return before

    before = asyncio.run(go())
    assert before == pytest.approx(3.0)  # 2 burst tokens, then one per second
    assert t[0] >= before + 30  # the 429 pause is respected


# ---- collector pieces (no network) -----------------------------------------------------------
def test_backpressure_drops_when_queue_full(tmp_path):
    async def go():
        store = LiveStore(tmp_path / "db.sqlite")
        col = Collector(_cfg(tmp_path, queue_max=3), store, http=_rest())
        col.handle_frame(delta([txid(i) for i in range(10)], seq=1))
        col.handle_frame(delta([txid(0), txid(1)], seq=2))  # duplicates
        col.handle_frame("garbage")
        # mempool's backend repeats its sequence while it re-syncs: such a delta
        # re-announces old transactions, which are not new arrivals
        col.handle_frame(delta([txid(i) for i in range(100, 150)], seq=2))
        return col.c, col.queue.qsize()

    c, size = asyncio.run(go())
    assert size == 3 and c.queued == 3 and c.dropped_backpressure == 7
    assert c.duplicates == 2 and c.received == 12 and c.malformed == 1
    assert c.resync_deltas == 1 and c.resync_txids_skipped == 50


def test_fetch_outcomes_429_404_timeout_and_idempotency(tmp_path):
    ok, missing, limited, slow, bad = (txid(f"o{i}") for i in range(5))
    responses = {
        missing: httpx.Response(404),
        limited: httpx.Response(429, headers={"retry-after": "3"}),
        bad: httpx.Response(200, json={"txid": "nope"}),
    }

    def handler(request):
        tid = request.url.path.rsplit("/", 1)[-1]
        if tid == slow:
            raise httpx.ReadTimeout("slow", request=request)
        return responses.get(tid) or httpx.Response(200, json=esplora_tx(tid))

    async def go():
        store = LiveStore(tmp_path / "db.sqlite")
        col = Collector(  # one attempt: outcome classification only (retries: test_live_faults)
            _cfg(tmp_path, fetch_max_attempts=1),
            store,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        col.bucket.pause = lambda s: setattr(col, "_paused", s)  # observe, don't wait
        now = col.clock()
        for t in (ok, missing, limited, slow, bad, ok):
            await col.fetch_one(t, now)
        await col.fetch_one(txid("old"), now - 10_000)  # older than max_queue_age_s
        return col, store

    col, store = asyncio.run(go())
    c = col.c
    assert (c.fetched, c.failed, c.not_found, c.rate_limited, c.timeouts) == (1, 4, 1, 1, 1)
    assert c.malformed_payloads == 1 and c.already_stored == 1 and c.expired == 1
    assert col._paused == 3.0  # Retry-After honoured
    reader = LiveReader(store.path)
    assert (
        reader.get(missing)["outcome"] == "failed" and reader.get(missing)["error"] == "not_found"
    )
    assert reader.get(ok)["features"]["txid"] == ok


def test_store_idempotent_retention_and_queries(tmp_path):
    store = LiveStore(tmp_path / "db.sqlite")
    for i in range(30):
        t = txid(f"s{i}")
        f = feat.extract(esplora_tx(t, fee=1_000 + 100 * i), txid=t, first_seen=i, fetched_at=i)
        assert store.add_fetched(f, ["rbf_signal"] if i % 2 else [])
    t0 = txid("s0")
    f0 = feat.extract(esplora_tx(t0), txid=t0, first_seen=0, fetched_at=0)
    assert not store.add_fetched(f0, [])  # idempotent
    assert store.prune(retention_rows=20, now=100) == 10
    r = LiveReader(store.path)
    total, rows = r.list(limit=5, sort="fee_rate", descending=True)
    assert total == 20 and rows[0]["txid"] == txid("s29")
    assert [x["fee_rate"] for x in rows] == sorted((x["fee_rate"] for x in rows), reverse=True)
    total_f, flagged = r.list(limit=100, flag="rbf_signal")
    assert total_f == 10 and all("rbf_signal" in x["flags"] for x in flagged)
    assert r.list(min_fee_rate=17.0, limit=100)[0] < 20
    with pytest.raises(ValueError):
        r.list(sort="txid; DROP TABLE tx")


def test_reader_is_read_only(tmp_path):
    import sqlite3

    LiveStore(tmp_path / "db.sqlite").close()
    with LiveReader(tmp_path / "db.sqlite")._conn() as c, pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM tx")


# ---- reconnection and the full collector path against a LOCAL WebSocket server -----------------
def _serve(script):
    """Local mempool-like WS server; ``script(ws, connection_index)`` drives each connection."""
    from websockets.asyncio.server import serve

    state = {"connections": 0, "subscriptions": []}

    async def handler(ws):
        i = state["connections"]
        state["connections"] += 1
        state["subscriptions"].append(json.loads(await ws.recv()))
        await script(ws, i)

    return serve(handler, "127.0.0.1", 0), state


def test_reconnects_after_disconnect_and_ingests_end_to_end(tmp_path):
    first, second = [txid(f"a{i}") for i in range(3)], [txid(f"b{i}") for i in range(3)]
    fetched: list[str] = []

    async def script(ws, i):
        if i == 0:
            await ws.send(delta(first, seq=1))
            await ws.send("not json")  # malformed frame is counted and skipped
            await ws.close()  # server drops the connection
        else:
            await ws.send(delta([*second, first[0]], seq=2))  # includes a duplicate
            await asyncio.sleep(10)

    async def go():
        server, state = _serve(script)
        async with server as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _cfg(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
            store = LiveStore(cfg.db_path)
            col = Collector(cfg, store, http=_rest(log=fetched), rng=random.Random(1))
            c = await col.run(duration=10, max_fetches=6)
            return c, state, cfg

    c, state, cfg = asyncio.run(go())
    assert state["connections"] >= 2 and c.reconnects >= 1
    assert all(s == {"track-mempool-txids": True} for s in state["subscriptions"])
    assert c.received == 7 and c.duplicates == 1 and c.malformed == 1
    assert c.fetched == 6 and sorted(fetched) == sorted(first + second)
    r = LiveReader(cfg.db_path)
    total, rows = r.list(limit=10)
    assert total == 6 and {x["txid"] for x in rows} == set(first + second)
    st = r.status()
    assert st["state"] == "stopped" and st["counters"]["fetched"] == 6
    assert r.stats(window_minutes=5)["totals"]["fetched"] == 6


def test_stale_stream_triggers_reconnect(tmp_path):
    async def script(ws, i):
        await asyncio.sleep(10)  # connected but silent

    async def go():
        server, state = _serve(script)
        async with server as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _cfg(tmp_path, ws_url=f"ws://127.0.0.1:{port}", stale_after_s=0.2)
            col = Collector(cfg, LiveStore(cfg.db_path), http=_rest())
            c = await col.run(duration=1.0)
            return c, state

    c, state = asyncio.run(go())
    assert c.stale_reconnects >= 2 and state["connections"] >= 2


def test_unreachable_server_backs_off_and_stops_gracefully(tmp_path):
    cfg = _cfg(tmp_path, ws_url="ws://127.0.0.1:9/")  # nothing listens on port 9

    async def go():
        col = Collector(cfg, LiveStore(cfg.db_path), http=_rest())
        c = await col.run(duration=6.0)  # refused connects can take ~2 s on Windows
        return c, col

    c, col = asyncio.run(go())
    assert c.connects == 0 and c.reconnects >= 1 and col.last_error
    assert LiveReader(cfg.db_path).status()["state"] == "stopped"


def test_live_config_validation():
    with pytest.raises(ValueError):
        LiveConfig.from_dict({"sample_rate": 0})
    with pytest.raises(ValueError):
        LiveConfig.from_dict({"fetch_per_minute": 0})
    assert dataclasses.replace(LiveConfig(), sample_rate=0.5).sample_rate == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        {"retention_rows": 0},  # prune(0) deleted every stored row
        {"retention_rows": -5},
        {"retention_days": 0},
        {"heartbeat_s": 0},  # the heartbeat loop would spin
        {"stale_after_s": 0},
        {"sse_max_clients": -1},  # 0 is allowed: event stream off
        {"indicator_max_age_days": -1},
        {"reconnect_base_s": 5, "reconnect_max_s": 0.1},
        {"ws_url": "http://mempool.space/api/v1/ws"},
        {"rest_base": "ftp://mempool.space/api"},
        {"request_timeout_s": 0},
        {"max_queue_age_s": -1},
        {"edge_trusted_proxy_hops": -1},
    ],
)
def test_live_config_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        LiveConfig.from_dict(bad)
    with pytest.raises(ValueError):  # also when constructed directly or replaced
        dataclasses.replace(LiveConfig(), **bad)
