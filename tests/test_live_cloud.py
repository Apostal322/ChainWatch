"""Always-on collector operation (docs/LIVE_CLOUD.md): restart recovery, outage and gap
tracking, retention, backups, the authenticated edge API, the dashboard's remote mode,
dependency isolation and the systemd units. Local mocks only; no internet."""

from __future__ import annotations

import asyncio
import configparser
import dataclasses
import random
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from chainwatch.config import REPO_ROOT, LiveConfig, load_config
from chainwatch.data.loader import prepare
from chainwatch.data.synthetic import make_raw_frames
from chainwatch.live import cli as live_cli
from chainwatch.live import features as feat
from chainwatch.live.collector import Collector
from chainwatch.live.edge import create_edge_app, edge_token
from chainwatch.live.store import LiveReader, LiveStore
from chainwatch.serving.app import ServiceState, create_app
from chainwatch.serving.export import export_registry
from chainwatch.serving.live_api import RemoteBackend, make_backend
from chainwatch.training.pipeline import evaluate, train
from conftest import make_test_config, write_raw
from live_fixtures import delta, esplora_tx, txid

TOKEN = "t" * 48
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _cfg(tmp_path, **kw) -> LiveConfig:
    base = dict(
        enabled=True,
        db_path=tmp_path / "live.sqlite",
        sample_rate=1.0,
        fetch_per_minute=6000,
        fetch_burst=100,
        queue_max=50,
        reconnect_base_s=0.01,
        reconnect_max_s=0.05,
        heartbeat_s=0.05,
    )
    base.update(kw)
    return LiveConfig(**base)


def _rest():
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=esplora_tx(r.url.path.rsplit("/", 1)[-1]))
        )
    )


def _fill(cfg: LiveConfig, n: int = 8) -> None:
    async def go():
        col = Collector(cfg, LiveStore(cfg.db_path), http=_rest())
        for i in range(n):
            await col.fetch_one(txid(f"cloud{i}"), col.clock())
        col.state = "connected"
        col.flush()

    asyncio.run(go())


# ---- restart recovery and gap tracking --------------------------------------------------------
def test_crash_restart_records_collector_down_gap(tmp_path):
    cfg = _cfg(tmp_path)
    store = LiveStore(cfg.db_path)
    t0 = 1_000_000.0
    store.heartbeat(
        state="connected", updated_at=t0, started_at=t0 - 500, first_started_at=t0 - 900
    )
    left_open = store.open_outage("disconnected", t0 - 10, "was reconnecting when it died")
    col = Collector(cfg, store, clock=lambda: t0 + 120)
    col.recover()
    rows = LiveReader(cfg.db_path).outages(0)
    down = [o for o in rows if o["kind"] == "collector_down"]
    assert len(down) == 1 and down[0]["started_at"] == t0 and down[0]["ended_at"] == t0 + 120
    assert "unclean exit in state connected" in down[0]["detail"]
    dangling = next(o for o in rows if o["id"] == left_open)
    assert dangling["ended_at"] == t0  # closed at the dead process's last heartbeat
    assert col.c.recovered_downtime_s == 120 and col.first_started_at == t0 - 900


def test_clean_restart_is_labelled_and_idempotent_data_survives(tmp_path):
    cfg = _cfg(tmp_path)
    _fill(cfg, 5)
    store = LiveStore(cfg.db_path)
    st = store.previous_status()
    store.heartbeat(**{**{k: st[k] for k in ("started_at", "updated_at")}, "state": "stopped"})
    col = Collector(cfg, store, http=_rest(), clock=lambda: st["updated_at"] + 30)
    col.recover()
    down = LiveReader(cfg.db_path).outages(0)
    assert down[0]["kind"] == "collector_down" and "clean stop" in down[0]["detail"]

    async def again():  # the restarted process does not refetch what is already stored
        for i in range(5):
            await col.fetch_one(txid(f"cloud{i}"), col.clock())

    asyncio.run(again())
    assert col.c.fetched == 0 and col.c.already_stored == 5
    assert LiveReader(cfg.db_path).list(limit=50)[0] == 5


def _serve(script):
    from websockets.asyncio.server import serve

    state = {"connections": 0}

    async def handler(ws):
        i = state["connections"]
        state["connections"] += 1
        await ws.recv()
        await script(ws, i)

    return serve(handler, "127.0.0.1", 0), state


def test_disconnect_outage_and_sequence_gap_are_recorded(tmp_path):
    async def script(ws, i):
        if i == 0:
            await ws.send(delta([txid("g1")], seq=10))
            await ws.send(delta([txid("g2")], seq=14))  # 3 updates missed while connected
            await ws.close()
        else:
            await ws.send(delta([txid("g3")], seq=40))  # jump after reconnect is NOT a gap
            await asyncio.sleep(10)

    async def go():
        server, state = _serve(script)
        async with server as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _cfg(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
            col = Collector(cfg, LiveStore(cfg.db_path), http=_rest(), rng=random.Random(0))
            await col.run(duration=10, max_fetches=3)
            return cfg, col, state

    cfg, col, state = asyncio.run(go())
    rows = LiveReader(cfg.db_path).outages(0)
    kinds = [o["kind"] for o in rows]
    assert col.c.sequence_gaps == 1 and kinds.count("sequence_gap") == 1
    gap = next(o for o in rows if o["kind"] == "sequence_gap")
    assert "3 mempool update(s) missed" in gap["detail"]
    disc = [o for o in rows if o["kind"] == "disconnected"]
    assert len(disc) == 1 and disc[0]["ended_at"] >= disc[0]["started_at"]
    assert state["connections"] >= 2


def test_availability_merges_overlapping_outages(tmp_path):
    store = LiveStore(tmp_path / "a.sqlite")
    now = 2_000_000.0
    store.heartbeat(
        state="connected", updated_at=now, started_at=now - 3000, first_started_at=now - 10_000
    )
    store.record_gap("collector_down", now - 3600, now - 3000, "down")
    store.record_gap("disconnected", now - 3100, now - 2900, "overlaps the previous one")
    store.record_gap("sequence_gap", now - 100, now - 100, "counted separately")
    a = LiveReader(store.path).availability(3600 * 24, now)
    assert a["down_s"] == pytest.approx(700) and a["window_s"] == pytest.approx(10_000)
    assert a["observed_share"] == pytest.approx(1 - 700 / 10_000, abs=1e-4)
    assert a["sequence_gaps"] == 1


def test_v050_database_is_migrated(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE collector (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, "
        "updated_at REAL NOT NULL, started_at REAL, connected_since REAL, last_message_at REAL, "
        "last_sequence INTEGER, reconnects INTEGER NOT NULL DEFAULT 0, "
        "counters TEXT NOT NULL DEFAULT '{}', settings TEXT NOT NULL DEFAULT '{}')"
    )
    con.commit()
    con.close()
    LiveStore(db).heartbeat(state="stopped", updated_at=1.0, first_started_at=0.5)
    assert LiveReader(db).status()["first_started_at"] == 0.5


# ---- retention, WAL, backups ------------------------------------------------------------------
def test_age_and_row_retention_and_checkpoint(tmp_path):
    store = LiveStore(tmp_path / "r.sqlite")
    now = 10_000_000.0
    for i in range(30):
        t = txid(f"ret{i}")
        f = feat.extract(
            esplora_tx(t), txid=t, first_seen=now - i * 86400, fetched_at=now - i * 86400
        )
        store.add_fetched(f, [])
    removed = store.prune(retention_rows=100, now=now, retention_days=7)
    total, _ = LiveReader(store.path).list(limit=100)
    assert removed == 22 and total == 8  # fetched within the last 7 days (inclusive edge)
    store.checkpoint()
    wal = store.path.with_name(store.path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_backup_is_consistent_and_rotated(tmp_path):
    cfg = _cfg(tmp_path)
    _fill(cfg, 6)
    dest = tmp_path / "backups"
    outs = [live_cli.backup(cfg.db_path, dest, keep=2, now=1_800_000_000 + i) for i in range(3)]
    kept = sorted(dest.glob("live-*.sqlite"))
    assert [p.name for p in kept] == [outs[1].name, outs[2].name]
    assert LiveReader(outs[2]).list(limit=50)[0] == 6  # restorable: a valid live database


def test_reliability_report(tmp_path):
    cfg = _cfg(tmp_path)
    _fill(cfg, 4)
    rep = live_cli.reliability_report(cfg, hours=24)
    assert rep["stored_rows"] == 4 and rep["collector_state"] == "connected"
    assert rep["availability"]["observed_share"] == 1.0
    assert set(rep["outages_by_kind"]) == {"collector_down", "disconnected", "sequence_gap"}
    assert rep["last_observation_age_s"] is not None and rep["db_bytes"] > 0


# ---- edge API: authentication and exposure ------------------------------------------------------
@pytest.fixture
def edge(tmp_path):
    cfg = _cfg(tmp_path, edge_rate_limit_per_minute=600)
    _fill(cfg)
    with TestClient(create_edge_app(cfg, TOKEN)) as c:
        yield c, cfg


def test_edge_requires_bearer_token(edge):
    c, _ = edge
    for path in ("/v1/live/status", "/v1/live/transactions", "/v1/live/stats", "/v1/live/outages"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", path
        assert r.headers["www-authenticate"] == "Bearer"
        assert c.get(path, headers={"authorization": "Bearer " + "x" * 48}).status_code == 401
        assert c.get(path, headers={"authorization": TOKEN}).status_code == 401  # no scheme
        assert c.get(path, headers=AUTH).status_code == 200, path
    assert c.get("/v1/live/events", headers={"authorization": "Bearer nope"}).status_code == 401


def test_edge_health_docs_methods_and_limits(edge):
    c, _ = edge
    assert c.get("/health/live").json() == {"status": "ok"}
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}  # heartbeat is fresh
    for p in ("/docs", "/redoc", "/openapi.json", "/live.sqlite"):
        assert c.get(p).status_code == 404, p
    assert c.post("/v1/live/transactions", headers=AUTH).status_code == 405
    assert c.get("/v1/live/transactions?limit=1000", headers=AUTH).status_code == 422
    assert c.get("/v1/live/status").headers["x-content-type-options"] == "nosniff"
    assert c.get("/v1/live/status", headers=AUTH).headers["cache-control"] == "no-store"


def test_edge_rate_limit(tmp_path):
    cfg = _cfg(tmp_path, edge_rate_limit_per_minute=8, edge_trusted_proxy_hops=0)
    _fill(cfg, 1)
    with TestClient(create_edge_app(cfg, TOKEN)) as c:
        bad = {"authorization": "Bearer " + "x" * 48}
        codes = [c.get("/v1/live/status", headers=bad).status_code for _ in range(6)]
        assert codes[0] == 401 and 429 in codes  # unauthenticated floods are limited
        limited = c.get("/v1/live/status", headers=bad)
        assert limited.status_code == 429 and limited.headers["cache-control"] == "no-store"
        assert limited.headers["x-content-type-options"] == "nosniff"  # headers on 429 too
        # ...but they cannot lock the token holder (the dashboard) out of the same IP
        assert all(c.get("/v1/live/status", headers=AUTH).status_code == 200 for _ in range(12))
        assert c.get("/health/live").status_code == 200


def test_edge_refuses_weak_or_missing_token(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        create_edge_app(cfg, "short")
    monkeypatch.delenv(cfg.api_token_env, raising=False)
    with pytest.raises(SystemExit):
        edge_token(cfg)
    monkeypatch.setenv(cfg.api_token_env, "z" * 64)
    assert edge_token(cfg) == "z" * 64


def test_edge_ready_is_503_without_a_running_collector(tmp_path):
    cfg = _cfg(tmp_path)
    with TestClient(create_edge_app(cfg, TOKEN)) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503 and r.json() == {"status": "disconnected"}


# ---- dashboard in remote mode -> edge API (end to end, in process) ---------------------------
@pytest.fixture(scope="module")
def research_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cloud_dash")
    raw_dir = write_raw(tmp / "raw", make_raw_frames(12, 60, 0))
    cfg = make_test_config(tmp, raw_dir)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    evaluate(cfg, train(cfg, "xgboost"))
    export_registry(cfg)
    return cfg, ServiceState(cfg)


def _dashboard(research_env, edge_client, token=TOKEN, **live):
    cfg, state = research_env
    lc = LiveConfig(enabled=True, remote_url="http://testserver", remote_cache_s=60, **live)
    backend = RemoteBackend(lc, token, transport=edge_client._transport)
    return TestClient(
        create_app(dataclasses.replace(cfg, live=lc), state=state, live_backend=backend)
    )


def test_dashboard_proxies_remote_collector(research_env, edge):
    edge_client, _ = edge
    calls = []
    original = edge_client._transport.handle_request

    def counting(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"  # server-to-server auth
        return original(request)

    edge_client._transport.handle_request = counting
    with _dashboard(research_env, edge_client) as dash:
        st = dash.get("/v1/live/status").json()
        assert st["via"] == "remote collector (edge API)" and st["connection"] == "connected"
        assert st["source_is_public_mempool"] is True and st["last_observation_at"]
        assert "availability_24h" in st
        dash.get("/v1/live/status")  # served from the shared cache
        assert calls.count("/v1/live/status") == 1
        page = dash.get("/v1/live/transactions", params={"limit": 3, "sort": "fee_rate"}).json()
        assert page["total"] == 8 and len(page["rows"]) == 3
        d = dash.get(f"/v1/live/transactions/{txid('cloud0')}").json()
        assert d["features"]["txid"] == txid("cloud0")
        assert dash.get(f"/v1/live/transactions/{'0' * 64}").status_code == 404
        assert dash.get("/v1/live/outages").json()["outages"] == []
        assert dash.get("/v1/live/stats").json()["n_fetched"] == 8
        assert dash.get("/v1/replay/steps").status_code == 200  # research replay unaffected


def test_dashboard_sse_from_remote(research_env, edge):
    edge_client, _ = edge
    app = _dashboard(research_env, edge_client, sse_max_seconds=1.2)
    with app as dash, dash.stream("GET", "/v1/live/events", params={"since": 0}) as r:
        body = "".join(r.iter_text())
    assert body.count("event: tx") == 8 and 'event: status\ndata: "connected"' in body


def test_dashboard_degrades_cleanly_when_edge_rejects_or_is_down(research_env, edge):
    edge_client, _ = edge
    with _dashboard(research_env, edge_client, token="w" * 48) as dash:
        r = dash.get("/v1/live/status")
        assert r.status_code == 502 and r.json()["error"]["code"] == "live_upstream_unavailable"

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    cfg, state = research_env
    lc = LiveConfig(enabled=True, remote_url="http://127.0.0.1:9")
    backend = RemoteBackend(lc, TOKEN, transport=httpx.MockTransport(down))
    with TestClient(
        create_app(dataclasses.replace(cfg, live=lc), state=state, live_backend=backend)
    ) as dash:
        assert dash.get("/v1/live/transactions").status_code == 502
        assert dash.get("/v1/replay/steps").status_code == 200


def test_remote_mode_requires_https_and_a_token(monkeypatch):
    with pytest.raises(ValueError):
        RemoteBackend(LiveConfig(enabled=True, remote_url="http://collector.example"), TOKEN)
    RemoteBackend(LiveConfig(enabled=True, remote_url="https://collector.example"), TOKEN)
    lc = LiveConfig(enabled=True, remote_url="https://collector.example")
    monkeypatch.delenv(lc.api_token_env, raising=False)
    with pytest.raises(ValueError):
        make_backend(lc)
    for bad in ("é" * 40, "a b" * 20):  # the edge would refuse it; fail at start-up instead
        monkeypatch.setenv(lc.api_token_env, bad)
        with pytest.raises(ValueError):
            make_backend(lc)


# ---- configs, isolation, units ---------------------------------------------------------------
def test_cloud_configs():
    cloud = load_config(REPO_ROOT / "configs" / "live-cloud.yaml").live
    # as_posix + endswith: on Windows the absolute VM path resolves to <drive>:/var/lib/...
    assert cloud.enabled
    assert cloud.db_path.as_posix().endswith("/var/lib/chainwatch/live.sqlite")
    assert cloud.fetch_per_minute <= 12 and cloud.sample_rate <= 0.05 and cloud.retention_days <= 7
    assert load_config(REPO_ROOT / "configs" / "public-demo.yaml").live.enabled is False


def test_vm_entry_points_never_import_the_ml_stack():
    code = (
        "import sys; import chainwatch.live.cli, chainwatch.live.edge, chainwatch.live.collector;"
        "heavy=[m for m in ('pandas','numpy','sklearn','xgboost','matplotlib','pyarrow','joblib',"
        "'chainwatch.training','chainwatch.backtest','chainwatch.serving.app')"
        " if m in sys.modules];"
        "print(heavy)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_live_lock_has_no_ml_dependencies():
    lock = (REPO_ROOT / "requirements" / "live.lock.txt").read_text().lower()
    for pkg in ("pandas", "numpy", "scikit-learn", "xgboost", "matplotlib", "pyarrow"):
        assert f"\n{pkg}==" not in lock, pkg
    assert "--hash=sha256:" in lock


def _unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(REPO_ROOT / "deploy" / "oracle" / name)
    return cp


@pytest.mark.parametrize("name", ["chainwatch-collector.service", "chainwatch-live-api.service"])
def test_systemd_units_restart_and_are_sandboxed(name):
    svc = _unit(name)["Service"]
    assert svc["Restart"] == "always" and svc["User"] == "chainwatch"
    assert svc["ProtectSystem"] == "strict" and svc["NoNewPrivileges"] == "yes"
    assert svc["ReadWritePaths"] == "/var/lib/chainwatch" and "MemoryMax" in svc
    assert "chainwatch-live" in svc["ExecStart"] and "train" not in svc["ExecStart"]
    assert _unit(name)["Install"]["WantedBy"] == "multi-user.target"  # starts on boot
    assert _unit(name)["Unit"]["StartLimitIntervalSec"] == "0"  # never stops retrying


def test_edge_api_is_bound_to_localhost_and_token_is_not_in_git():
    svc = _unit("chainwatch-live-api.service")["Service"]
    assert "--host 127.0.0.1" in svc["ExecStart"]
    assert svc["EnvironmentFile"] == "/etc/chainwatch/live.env"
    example = (REPO_ROOT / "deploy" / "oracle" / "live.env.example").read_text()
    assert "<64 hex characters" in example
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert not any(p.endswith(("live.env", ".sqlite")) for p in tracked)


def test_entrypoint_timer_and_caddy_template():
    timer = _unit("chainwatch-backup.timer")["Timer"]
    assert timer["Persistent"] == "true"
    caddy = (REPO_ROOT / "deploy" / "oracle" / "Caddyfile.template").read_text()
    assert "reverse_proxy 127.0.0.1:8100" in caddy and "respond 404" in caddy
