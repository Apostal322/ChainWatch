"""LOCAL MOCK of the two mempool.space endpoints the collector uses, for offline development
and the end-to-end test of the live pipeline. It serves CONSTRUCTED data, never real
network observations; the dashboard labels any non-mempool.space source accordingly.

    PYTHONPATH=src python scripts/mock_mempool.py --port 8999 --tx-per-second 5
    # collector: live.ws_url=ws://127.0.0.1:8999/api/v1/ws, live.rest_base=http://127.0.0.1:8999/api

Endpoints (documented shapes; see src/chainwatch/live/protocol.py and features.py):
  WS  /api/v1/ws        expects {"track-mempool-txids": true}, then sends "mempool-txids" deltas
  GET /api/tx/{txid}    Esplora-shaped transaction (about 3% answer 404, like evicted txs)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from live_fixtures import delta, esplora_tx, txid


def build_app(tps: float, seed: int = 0) -> FastAPI:
    app = FastAPI(title="LOCAL MOCK mempool (not real data)")
    rng = random.Random(seed)
    known: dict[str, dict] = {}
    counter = {"n": 0, "seq": 0}  # seq: +1 per mempool update, like mempool's backend

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

    @app.websocket("/api/v1/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        try:
            msg = json.loads(await socket.receive_text())
            if msg.get("track-mempool-txids") is not True:
                await socket.close(code=1008)
                return
            while True:
                batch = []
                for _ in range(max(1, int(rng.gauss(tps * 2, tps * 0.5)))):
                    counter["n"] += 1
                    tid = txid(f"mock-{seed}-{counter['n']}")
                    known[tid] = make(tid)
                    batch.append(tid)
                counter["seq"] += 1
                await socket.send_text(delta(batch, seq=counter["seq"]))
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            return

    @app.get("/api/tx/{tid}")
    def tx(tid: str) -> dict:
        if tid not in known or rng.random() < 0.03:
            raise HTTPException(404, "Transaction not found")
        return known[tid]

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8999)
    ap.add_argument("--tx-per-second", type=float, default=5.0)
    a = ap.parse_args()
    uvicorn.run(build_app(a.tx_per_second), host="127.0.0.1", port=a.port, log_level="warning")
