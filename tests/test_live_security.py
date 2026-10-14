"""Security regression tests from the v0.6.0 audit (docs/AUDIT.md): event-stream slot
accounting, per-client stream caps, Last-Event-ID parsing, the remote backend's request
coalescing / negative cache / upstream budget / bounded reads, and client keys."""

from __future__ import annotations

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
