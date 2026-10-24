"""LOCAL MOCK of the two mempool.space endpoints the collector uses, for offline development,
the end-to-end test of the live pipeline and fault-injection soak tests. It serves
CONSTRUCTED data, never real network observations; the dashboard labels any
non-mempool.space source accordingly.

    PYTHONPATH=src python scripts/mock_mempool.py --port 8999 --tx-per-second 5
    # collector: live.ws_url=ws://127.0.0.1:8999/api/v1/ws, live.rest_base=http://127.0.0.1:8999/api

Endpoints (documented shapes; see src/chainwatch/live/protocol.py and features.py):
  WS  /api/v1/ws        expects {"track-mempool-txids": true}, then sends "mempool-txids" deltas
                        (sequence +1 per update, like mempool's backend)
  GET /api/tx/{txid}    Esplora-shaped transaction (about 3% answer 404, like evicted txs)

Fault injection (local testing only; scripts/soak_test.py drives it):
  POST /__mock/fault  {"kind": ..., "seconds": s, "value": v}
    rest_429        REST answers 429 with Retry-After: v (default 5) for s seconds
    rest_500        REST answers 500 for s seconds
    rest_malformed  REST answers 200 with a body that is not JSON for s seconds
    rest_schema     REST answers 200 with JSON missing required fields for s seconds
    rest_slow       REST answers after v seconds (default 3) for s seconds
    ws_drop         close every WebSocket now (abnormal close, code 1011)
    ws_silent       stop sending deltas (connection stays open) for s seconds
    ws_burst        add v extra txids (default 5000) to the next delta
    ws_gap          skip v sequence numbers (default 3) once
  GET  /__mock/stats  counters (requests by outcome, txids announced, open sockets)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from live_fixtures import delta, esplora_tx, txid

KNOWN_MAX = 200_000  # bounded memory for multi-hour soak runs
REST_FAULTS = {"rest_429", "rest_500", "rest_malformed", "rest_schema", "rest_slow"}


def build_app(tps: float, seed: int = 0) -> FastAPI:
    app = FastAPI(title="LOCAL MOCK mempool (not real data)")
    rng = random.Random(seed)
    known: OrderedDict[str, dict] = OrderedDict()
    counter = {"n": 0, "seq": 0}  # seq: +1 per mempool update, like mempool's backend
    faults: dict[str, tuple[float, float]] = {}  # kind -> (until, value)
    once: dict[str, float] = {}  # one-shot faults: kind -> value
    stats: Counter[str] = Counter()
    sockets: set[WebSocket] = set()

    def active(kind: str) -> float | None:
        until, value = faults.get(kind, (0.0, 0.0))
        return value if time.monotonic() < until else None

    def make(tid: str) -> dict:
        n_out = rng.choice([1, 2, 2, 2, 3, 5, 60]) if rng.random() < 0.97 else 120
        outs = [rng.randint(300, 5_000_000) for _ in range(n_out)]
        weight = rng.randint(400, 2_400)
        return esplora_tx(
            tid,
            n_in=rng.choice([1, 1, 2, 3, 60]),
            out_values=outs,
            weight=weight,
            size=weight // 3,
            fee=int(weight / 4 * rng.choice([1.0, 2.0, 3.5, 6.0, 12.0, 40.0, 150.0])),
            sequence=rng.choice([0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD]),
            locktime=rng.choice([0, 0, 0, 899_000]),
            op_return=rng.random() < 0.05,
        )

    def announce() -> str:
        counter["n"] += 1
        tid = txid(f"mock-{seed}-{counter['n']}")
        known[tid] = make(tid)
        while len(known) > KNOWN_MAX:
            known.popitem(last=False)
        stats["announced"] += 1
        return tid

    @app.websocket("/api/v1/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        stats["ws_connections"] += 1
        try:
            msg = json.loads(await socket.receive_text())
            if msg.get("track-mempool-txids") is not True:
                await socket.close(code=1008)
                return
            sockets.add(socket)
            while True:
                await asyncio.sleep(2.0)
                if active("ws_silent") is not None:
                    continue
                n = max(1, int(rng.gauss(tps * 2, tps * 0.5)))
                n += int(once.pop("ws_burst", 0))
                batch = [announce() for _ in range(n)]
                counter["seq"] += 1 + int(once.pop("ws_gap", 0))
                await socket.send_text(delta(batch, seq=counter["seq"]))
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            sockets.discard(socket)

    @app.get("/api/tx/{tid}")
    async def tx(tid: str) -> Response:
        stats["rest_requests"] += 1
        if (v := active("rest_slow")) is not None:
            await asyncio.sleep(v)
        if (v := active("rest_429")) is not None:
            stats["rest_429"] += 1
            return Response("Too Many Requests", 429, headers={"retry-after": str(int(v))})
        if active("rest_500") is not None:
            stats["rest_500"] += 1
            return Response("upstream error", 500)
        if active("rest_malformed") is not None:
            stats["rest_malformed"] += 1
            return Response('{"txid": "trunc', 200, media_type="application/json")
        if active("rest_schema") is not None:
            stats["rest_schema"] += 1
            return Response(json.dumps({"txid": tid}), 200, media_type="application/json")
        if tid not in known or rng.random() < 0.03:
            stats["rest_404"] += 1
            raise HTTPException(404, "Transaction not found")
        stats["rest_200"] += 1
        return Response(json.dumps(known[tid]), 200, media_type="application/json")

    @app.post("/__mock/fault")
    async def fault(body: dict) -> dict:
        kind, seconds = body.get("kind"), float(body.get("seconds", 30))
        value = body.get("value")
        stats[f"fault_{kind}"] += 1
        if kind in REST_FAULTS or kind == "ws_silent":
            default = {"rest_429": 5, "rest_slow": 3}.get(kind, 1)
            faults[kind] = (time.monotonic() + seconds, float(value or default))
        elif kind == "ws_drop":
            for s in list(sockets):
                await s.close(code=1011)
        elif kind == "ws_burst":
            once["ws_burst"] = float(value or 5000)
        elif kind == "ws_gap":
            once["ws_gap"] = float(value or 3)
        else:
            raise HTTPException(400, f"unknown fault {kind!r}")
        return {"ok": True, "kind": kind}

    @app.get("/__mock/stats")
    def get_stats() -> dict:
        return {**stats, "open_sockets": len(sockets), "known": len(known)}

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8999)
    ap.add_argument("--tx-per-second", type=float, default=5.0)
    a = ap.parse_args()
    uvicorn.run(build_app(a.tx_per_second), host="127.0.0.1", port=a.port, log_level="warning")
