"""/v1/live API: mode isolation, read-only access, filters, detail, stats and SSE streaming.
The live database is filled through the real collector code path with local mocks only."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from chainwatch.config import REPO_ROOT, LiveConfig, load_config
from chainwatch.data.loader import prepare
from chainwatch.data.synthetic import make_raw_frames
from chainwatch.live.collector import Collector
from chainwatch.live.store import LiveStore
from chainwatch.serving.app import ServiceState, create_app
from chainwatch.serving.export import export_registry
from chainwatch.serving.live_api import connection_state, is_public_mempool
from chainwatch.training.pipeline import evaluate, train
from conftest import make_test_config, write_raw
from live_fixtures import esplora_tx, txid

IDS = [txid(f"api{i}") for i in range(12)]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live_api")
    raw_dir = write_raw(tmp / "raw", make_raw_frames(12, 60, 0))
    cfg = make_test_config(tmp, raw_dir)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    evaluate(cfg, train(cfg, "xgboost"))
    export_registry(cfg)
    return cfg, ServiceState(cfg), tmp


@pytest.fixture(scope="module")
def live_db(env):
    """Fill a live DB through Collector.fetch_one with mocked REST responses."""
    _, _, tmp = env
    db = tmp / "live.sqlite"

    def handler(request):
        tid = request.url.path.rsplit("/", 1)[-1]
        i = IDS.index(tid)
        if i == 11:
            return httpx.Response(404)
        kw = {"fee": 500 + 400 * i, "sequence": 0xFFFFFFFD if i % 3 == 0 else 0xFFFFFFFF}
        return httpx.Response(200, json=esplora_tx(tid, **kw))

    async def fill():
        cfg = LiveConfig(enabled=True, db_path=db, fetch_per_minute=6000, fetch_burst=100)
        col = Collector(
            cfg, LiveStore(db), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        for t in IDS:
            await col.fetch_one(t, col.clock())
        col.state = "connected"
        col.flush()

    asyncio.run(fill())
    return db


def _client(env, **live):
    cfg, state, _ = env
    lc = dataclasses.replace(LiveConfig(), **live)
    return TestClient(create_app(dataclasses.replace(cfg, live=lc), state=state))


# ---- mode isolation -------------------------------------------------------------------------
def test_live_disabled_by_default_everywhere(env):
    for name in ("default.yaml", "demo.yaml", "public-demo.yaml"):
        assert load_config(REPO_ROOT / "configs" / name).live.enabled is False
    assert load_config(REPO_ROOT / "configs" / "live.yaml").live.enabled is True
    with _client(env) as c:
        assert c.get("/v1/info").json()["live_enabled"] is False
        for path in ("/v1/live/status", "/v1/live/transactions", "/v1/live/stats"):
            r = c.get(path)
            assert r.status_code == 404 and r.json()["error"]["code"] == "live_disabled", path
        r = c.get("/v1/live/events")
        assert r.status_code == 404 and r.json()["error"]["code"] == "live_disabled"
        assert c.get("/v1/replay/steps").status_code == 200  # replay unaffected


def test_creating_the_app_never_starts_a_collector(env, monkeypatch):
    import chainwatch.live.collector as collector_mod

    def boom(*a, **k):
        raise AssertionError("the API must not start the collector")

    monkeypatch.setattr(collector_mod.Collector, "__init__", boom)
    with _client(env, enabled=True, db_path=env[2] / "absent.sqlite") as c:
        r = c.get("/v1/live/status")
        assert r.status_code == 503 and r.json()["error"]["code"] == "live_no_data"


# ---- endpoints --------------------------------------------------------------------------------
def test_status_transactions_detail_and_stats(env, live_db):
    with _client(env, enabled=True, db_path=live_db) as c:
        assert c.get("/v1/info").json()["live_enabled"] is True
        st = c.get("/v1/live/status").json()
        assert st["mode"] == "live" and "real Bitcoin" in st["data_origin"]
        assert st["connection"] == "connected" and st["collector"]["counters"]["fetched"] == 11
        assert "not fraud predictions" in st["notice"] and st["source_is_public_mempool"]

        page = c.get("/v1/live/transactions", params={"limit": 5, "sort": "fee_rate"}).json()
        assert page["total"] == 11 and len(page["rows"]) == 5
        rates = [r["fee_rate"] for r in page["rows"]]
        assert rates == sorted(rates, reverse=True)
        flagged = c.get("/v1/live/transactions", params={"flag": "rbf_signal", "limit": 50}).json()
        assert flagged["total"] == 4 and all(r["rbf_signal"] for r in flagged["rows"])
        failed = c.get("/v1/live/transactions", params={"outcome": "failed"}).json()
        assert failed["total"] == 1 and failed["rows"][0]["error"] == "not_found"

        d = c.get(f"/v1/live/transactions/{IDS[0]}").json()
        assert d["explorer_url"] == "https://mempool.space/tx/" + IDS[0]
        assert d["features"]["vsize"] == 141 and "rbf_signal" in d["flag_descriptions"]

        s = c.get("/v1/live/stats", params={"window_minutes": 10}).json()
        assert s["totals"]["fetched"] == 11 and s["n_fetched"] == 11
        assert sum(b["count"] for b in s["fee_rate_histogram"]) == 11
        assert s["failures"] == {"not_found": 1}


@pytest.mark.parametrize(
    ("path", "params", "code"),
    [
        ("/v1/live/transactions/" + "Z" * 64, None, 400),
        ("/v1/live/transactions/" + "0" * 64, None, 404),
        ("/v1/live/transactions", {"sort": "txid"}, 400),
        ("/v1/live/transactions", {"flag": "fraud"}, 400),
        ("/v1/live/transactions", {"limit": 1000}, 422),
        ("/v1/live/stats", {"window_minutes": 0}, 422),
    ],
)
def test_input_validation(env, live_db, path, params, code):
    with _client(env, enabled=True, db_path=live_db) as c:
        assert c.get(path, params=params).status_code == code


def test_api_cannot_modify_live_data(env, live_db):
    with _client(env, enabled=True, db_path=live_db) as c:
        assert c.post("/v1/live/transactions").status_code == 405
        assert c.delete(f"/v1/live/transactions/{IDS[0]}").status_code == 405


def test_sse_streams_new_observations_and_status(env, live_db):
    app = _client(env, enabled=True, db_path=live_db, sse_max_seconds=1.5)
    with app as c, c.stream("GET", "/v1/live/events", params={"since": 0}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [e for e in body.split("\n\n") if e.strip()]
    tx_events = [e for e in events if "event: tx" in e]
    assert body.startswith("retry: 3000")
    assert len(tx_events) == 12  # 11 fetched + 1 failed observation, in id order
    first = json.loads(tx_events[0].split("data: ", 1)[1])
    assert first["txid"] == IDS[0] and tx_events[0].startswith("id: 1\n")
    assert any('event: status\ndata: "connected"' in e for e in events)

    # resume with Last-Event-ID: only later events are sent
    app = _client(env, enabled=True, db_path=live_db, sse_max_seconds=1.2)
    with app as c, c.stream("GET", "/v1/live/events", headers={"last-event-id": "10"}) as r:
        body = "".join(r.iter_text())
    assert body.count("event: tx") == 2


def test_sse_client_limit(env, live_db):
    with _client(env, enabled=True, db_path=live_db, sse_max_clients=0) as c:
        r = c.get("/v1/live/events")
        assert r.status_code == 503


def test_connection_state_derivation():
    now = time.time()
    stale = now - 60
    assert connection_state(None, now, 5) == "disconnected"
    assert connection_state({"state": "connected", "updated_at": now}, now, 5) == "connected"
    assert connection_state({"state": "reconnecting", "updated_at": now}, now, 5) == "reconnecting"
    assert connection_state({"state": "connected", "updated_at": stale}, now, 5) == "disconnected"
    assert connection_state({"state": "stopped", "updated_at": now}, now, 5) == "disconnected"


def test_source_labelling_is_explicit():
    assert is_public_mempool("wss://mempool.space/api/v1/ws")
    assert not is_public_mempool("ws://127.0.0.1:9000")
    assert not is_public_mempool("wss://mempool.space.evil.example/ws")
    assert not is_public_mempool(None)
