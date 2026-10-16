"""Security regression tests from the v0.6.0 audit (docs/AUDIT.md): event-stream slot
accounting, per-client stream caps, Last-Event-ID parsing, the remote backend's request
coalescing / negative cache / upstream budget / bounded reads, and client keys."""

from __future__ import annotations

import contextlib
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from chainwatch.config import LiveConfig
from chainwatch.serving import live_api
from chainwatch.serving.live_api import (
    NoLiveData,
    RemoteBackend,
    UpstreamUnavailable,
    install_live_routes,
)
from chainwatch.serving.security import client_key

TOKEN = "t" * 48


def _live(**kw) -> LiveConfig:
    base = dict(enabled=True, remote_url="http://127.0.0.1:9", remote_cache_s=60.0)
    base.update(kw)
    return LiveConfig(**base)


def _error(status, code, message, request):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class SlowBackend:
    """Stand-in backend whose max_id() blocks until released (the race window)."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def max_id(self) -> int:
        self.release.wait(5)
        return 0

    def recent(self):
        return []

    def status(self, now):
        return {"connection": "connected"}


def _events_app(backend, **kw) -> FastAPI:
    app = FastAPI()
    install_live_routes(app, _live(**kw), _error, backend=backend)
    return app


# ---- event streams ----------------------------------------------------------------------
def test_stream_slots_are_reserved_before_any_await():
    """Previously the cap was checked, then max_id() awaited, then the counter incremented:
    concurrent opens all passed the check (25 of 25 with a cap of 5)."""
    be = SlowBackend()
    app = _events_app(be, sse_max_clients=3, sse_max_per_client=100, sse_max_seconds=0.2)
    codes: list[int] = []

    def open_one():
        with TestClient(app) as c:
            codes.append(c.get("/v1/live/events").status_code)

    threads = [threading.Thread(target=open_one) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.5)  # all eight are inside the handler or rejected by now
    be.release.set()
    for t in threads:
        t.join(10)
    assert codes.count(200) == 3 and codes.count(503) == 5


def test_per_client_stream_cap_and_slots_are_released():
    be = SlowBackend()
    be.release.set()
    app = _events_app(be, sse_max_clients=10, sse_max_per_client=2, sse_max_seconds=0.3)
    with TestClient(app) as c:
        results: list[int] = []

        def open_one():
            results.append(c.get("/v1/live/events").status_code)

        ts = [threading.Thread(target=open_one) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert sorted(results) == [200, 200, 429, 429]
        # streams ended (sse_max_seconds): both slots are free again
        assert c.get("/v1/live/events").status_code == 200


@pytest.mark.parametrize("header", ["²", "9" * 5000, "-1", "1e9", " 12"])
def test_hostile_last_event_id_is_ignored_not_a_500(header):
    be = SlowBackend()
    be.release.set()
    app = _events_app(be, sse_max_seconds=0.1)
    with TestClient(app) as c:
        # latin-1 bytes: '\u00b2' passes str.isdigit() but int() rejects it (was a 500)
        r = c.get("/v1/live/events", headers={"last-event-id": header.encode("latin-1")})
        assert r.status_code == 200


# ---- remote backend -------------------------------------------------------------------
def _edge(handler, delay: float = 0.0):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if delay:
            time.sleep(delay)
        return handler(request)

    return httpx.MockTransport(route), calls


def _rows(request):
    return httpx.Response(200, json={"total": 0, "offset": 0, "rows": []})


def test_concurrent_misses_share_one_upstream_request():
    transport, calls = _edge(_rows, delay=0.2)
    be = RemoteBackend(_live(), TOKEN, transport=transport)
    ts = [threading.Thread(target=be.recent) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert calls["n"] == 1  # was 20 before single-flight


def test_failures_are_cached_so_an_outage_does_not_hammer_the_edge():
    transport, calls = _edge(lambda r: httpx.Response(429, json={"error": {"code": "x"}}))
    be = RemoteBackend(_live(), TOKEN, transport=transport)
    for _ in range(5):
        with pytest.raises(UpstreamUnavailable):
            be.recent()
    assert calls["n"] == 1


def test_varying_parameters_cannot_multiply_upstream_traffic():
    transport, calls = _edge(_rows)
    be = RemoteBackend(_live(), TOKEN, transport=transport)
    failures = 0
    for offset in range(60):
        try:
            be.transactions(limit=10, offset=offset)
        except UpstreamUnavailable:
            failures += 1
    assert calls["n"] <= live_api.UPSTREAM_BURST + 2 and failures > 0


def test_lru_keeps_hot_keys_when_full():
    transport, calls = _edge(_rows)
    be = RemoteBackend(_live(), TOKEN, transport=transport)
    be._tokens = 10_000.0  # isolate the LRU from the upstream budget
    be.recent()
    for offset in range(live_api.CACHE_MAX_KEYS + 50):
        be._tokens = 10_000.0
        be.transactions(limit=10, offset=offset)
        be.recent()  # hot key stays cached
    assert len(be._cache) <= live_api.CACHE_MAX_KEYS
    hot = calls["n"]
    be.recent()
    assert calls["n"] == hot


def test_oversized_or_invalid_upstream_bodies_fail_cleanly():
    big = httpx.Response(200, content=b"[" + b"0," * 1_100_000 + b"0]")
    transport, _ = _edge(lambda r: big)
    with pytest.raises(UpstreamUnavailable, match="too large"):
        RemoteBackend(_live(), TOKEN, transport=transport).recent()
    transport, _ = _edge(lambda r: httpx.Response(200, content=b"{not json"))
    with pytest.raises(UpstreamUnavailable, match="invalid JSON"):
        RemoteBackend(_live(), TOKEN, transport=transport).recent()
    nodata = httpx.Response(503, json={"error": {"code": "live_no_data"}})
    transport, _ = _edge(lambda r: nodata)
    with pytest.raises(NoLiveData):
        RemoteBackend(_live(), TOKEN, transport=transport).recent()


# ---- client keys ------------------------------------------------------------------------
def _scope(peer: str, xff: bytes | None = None):
    headers = [(b"x-forwarded-for", xff)] if xff else []
    return {"client": (peer, 1234), "headers": headers}


def test_ipv6_clients_are_keyed_by_slash_64():
    a = client_key(_scope("2001:db8:1:2::1"))
    b = client_key(_scope("2001:db8:1:2:ffff::9"))
    assert a == b == "2001:db8:1:2::/64"
    assert client_key(_scope("2001:db8:1:3::1")) != a
    assert client_key(_scope("::ffff:203.0.113.7")) == "203.0.113.7"
    assert client_key(_scope("10.0.0.1", b"6.6.6.6, 203.0.113.7"), 1) == "203.0.113.7"


def _any(request):
    p = request.url.path
    if p.endswith("/status"):
        return httpx.Response(200, json={"connection": "connected"})
    if p.endswith("/risk/summary"):
        return httpx.Response(200, json={"mode": "live", "enabled": True})
    if p.startswith("/v1/live/transactions/"):
        return httpx.Response(404, json={"error": {"code": "not_found"}})
    return _rows(request)


def test_anonymous_parameter_cycling_cannot_starve_the_dashboards_own_polling():
    """v0.8 audit (P1): an anonymous visitor varying ``hours`` or txids missed the cache on
    every request, drained the shared upstream budget, and the dashboard's own polling then
    got 502 on every route."""
    transport, calls = _edge(_any)
    be = RemoteBackend(_live(remote_cache_s=0.0), TOKEN, transport=transport)
    for i in range(200):  # the attacker: distinct keys, and the cheap-to-ask txid route
        with contextlib.suppress(UpstreamUnavailable):
            be.risk_summary(1 + i * 0.01)
        with contextlib.suppress(UpstreamUnavailable):
            be.transaction(f"{i:064x}")
    for _ in range(3):  # the dashboard's polling still works
        assert be.status(time.time())["connection"] == "connected"
        assert be.risk_summary(24)["enabled"] is True
        assert be.recent() == []
    # snapping keeps the attacker's hours on a handful of keys, txids hit the budget
    assert calls["n"] <= live_api.UPSTREAM_BURST + 3 * 3 + 5


def test_last_good_answer_is_served_when_the_edge_fails():
    state = {"ok": True}

    def handler(request):
        return _rows(request) if state["ok"] else httpx.Response(500)

    transport, _ = _edge(handler)
    be = RemoteBackend(_live(remote_cache_s=0.0), TOKEN, transport=transport)
    assert be.recent() == []
    state["ok"] = False
    assert be.recent() == []  # stale but useful, not a 502
    be._good.clear()
    with pytest.raises(UpstreamUnavailable):
        be.recent()


def test_refused_keys_cannot_evict_the_polling_answers_from_the_cache():
    """Budget-refused failures were cached in the same LRU as the dashboard's polling
    answers, so a flood of distinct keys evicted them and every poll went upstream."""
    transport, calls = _edge(_any)
    be = RemoteBackend(_live(remote_cache_s=60.0), TOKEN, transport=transport)
    be.recent()
    n = calls["n"]
    for i in range(3 * live_api.CACHE_MAX_KEYS):
        with contextlib.suppress(UpstreamUnavailable):
            be.transaction(f"{i:064x}")
    spent = calls["n"] - n
    assert spent <= live_api.UPSTREAM_BURST + 1  # the rest were refused by the budget
    be.recent()
    assert calls["n"] - n == spent  # still served from the cache


def test_event_stream_and_feed_poll_share_one_query():
    """The stream polled limit=100 and the feed limit=200: two keys cached at different
    moments made the feed look older than the stream, and the page reset itself."""
    transport, calls = _edge(_any)
    be = RemoteBackend(_live(remote_cache_s=60.0), TOKEN, transport=transport)
    be.transactions(limit=200, offset=0, sort="id", descending=True, outcome=None)
    be.recent()
    assert calls["n"] == 1


def test_status_is_never_served_stale():
    """A stale /status said "connected" for a minute after the edge went down."""
    state = {"ok": True}

    def handler(request):
        return _any(request) if state["ok"] else httpx.Response(500)

    transport, _ = _edge(handler)
    be = RemoteBackend(_live(remote_cache_s=0.0), TOKEN, transport=transport)
    assert be.status(time.time())["connection"] == "connected"
    state["ok"] = False
    with pytest.raises(UpstreamUnavailable):
        be.status(time.time())


def test_upstream_has_an_overall_deadline_and_errors_never_strand_waiters():
    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                time.sleep(0.1)
                yield b" "

    transport, _ = _edge(lambda r: httpx.Response(200, stream=Drip()))
    be = RemoteBackend(_live(remote_cache_s=0.0, remote_timeout_s=0.3), TOKEN, transport=transport)
    t0 = time.monotonic()
    with pytest.raises(UpstreamUnavailable):
        be.recent()
    assert time.monotonic() - t0 < 1.5  # was the whole 2 s body (or forever)

    def boom(request):
        raise RecursionError("deep JSON")

    transport, _ = _edge(boom)
    be = RemoteBackend(_live(), TOKEN, transport=transport)
    with pytest.raises(UpstreamUnavailable):
        be.recent()
    assert be._inflight == {}  # waiters released


def test_a_client_that_never_reads_cannot_hold_a_stream_slot():
    """The deadline was only checked between sends, and a send to a non-reading client
    blocks: the slot stayed taken (seen at 400 s with sse_max_seconds=120)."""
    import asyncio

    be = SlowBackend()
    be.release.set()
    app = _events_app(be, sse_max_clients=1, sse_max_per_client=1, sse_max_seconds=0.3)

    async def stalled_client():
        scope = {"type": "http", "method": "GET", "path": "/v1/live/events", "raw_path": b"",
                 "query_string": b"", "headers": [], "client": ("10.0.0.1", 1),
                 "server": ("t", 80), "scheme": "http", "http_version": "1.1",
                 "root_path": "", "app": app}  # fmt: skip
        sent = 0

        async def send(message):
            nonlocal sent
            sent += 1
            if sent > 1:  # headers went out; the client never reads the body
                await asyncio.Event().wait()

        async def receive():
            await asyncio.Event().wait()

        t0 = time.monotonic()
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return time.monotonic() - t0

    assert asyncio.run(stalled_client()) < 2.0
    with TestClient(app) as c:  # the slot is free again
        assert c.get("/v1/live/events").status_code == 200


def test_token_rotation_and_token_format(tmp_path, monkeypatch):
    from chainwatch.live.edge import create_edge_app, edge_tokens, valid_token

    old, new = "a" * 48, "b" * 48
    monkeypatch.setenv("CHAINWATCH_LIVE_TOKEN", old)
    monkeypatch.setenv("CHAINWATCH_LIVE_TOKEN_NEXT", new)
    live = LiveConfig(enabled=True, db_path=tmp_path / "x.sqlite", edge_trusted_proxy_hops=0)
    tokens = edge_tokens(live)
    assert tokens == (old, new)
    with TestClient(create_edge_app(live, tokens)) as c:  # both work during the rotation
        for t in (old, new):
            r = c.get("/v1/live/stats", headers={"authorization": f"Bearer {t}"})
            assert r.status_code in (200, 503), r.status_code  # 503: no database yet
        assert (
            c.get("/v1/live/stats", headers={"authorization": "Bearer " + "c" * 48}).status_code
            == 401
        )
    assert not valid_token(" " * 34) and not valid_token("a" * 31) and valid_token("Ab0-_.~+/=" * 4)
    monkeypatch.setenv("CHAINWATCH_LIVE_TOKEN", " " * 40)
    with pytest.raises(SystemExit) as e:
        edge_tokens(live)
    assert e.value.code == 78


def test_status_never_shows_credentials_from_a_source_url():
    raw = {"ws_url": "wss://user:secret@node.example/ws", "err": ["https://u:p@h.example/tx"]}
    clean = {"ws_url": "wss://node.example/ws", "err": ["https://h.example/tx"]}
    assert live_api._redact(raw) == clean
