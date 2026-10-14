"""Resource and API-usage measurement of the live collector against a LOCAL mock.

    PYTHONPATH=src python scripts/live_load_test.py --seconds 120 --tx-per-second 20

A local WebSocket server announces constructed txids in the documented ``mempool-txids``
format; a mock REST transport answers detail requests. The collector runs with the limits
of configs/live.yaml. Reported: outgoing request rate (must stay within fetch_per_minute
plus the burst), queue/drop behaviour, peak RSS and database size. This measures the
collector itself; it says nothing about mempool.space's real traffic or availability.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from chainwatch.config import REPO_ROOT, load_config
from chainwatch.live.collector import Collector
from chainwatch.live.store import LiveReader, LiveStore
from live_fixtures import delta, esplora_tx, txid


async def main(seconds: float, tps: float) -> dict:
    counter = {"n": 0}
    request_times: list[float] = []

    async def handler(ws):
        await ws.recv()  # subscription
        with contextlib.suppress(ConnectionClosed):  # the collector disconnects at the end
            while True:
                batch = [txid(f"load{counter['n'] + k}") for k in range(int(tps * 2))]
                counter["n"] += len(batch)
                await ws.send(delta(batch, seq=counter["n"]))
                await asyncio.sleep(2.0)  # mempool.space pushes deltas on mempool updates

    def rest(request: httpx.Request) -> httpx.Response:
        request_times.append(time.monotonic())
        return httpx.Response(200, json=esplora_tx(request.url.path.rsplit("/", 1)[-1]))

    tmp = Path(tempfile.mkdtemp())
    async with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        live = dataclasses.replace(
            load_config(REPO_ROOT / "configs" / "live.yaml").live,
            ws_url=f"ws://127.0.0.1:{port}",
            db_path=tmp / "live.sqlite",
        )
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.monotonic()
        c = await Collector(
            live,
            LiveStore(live.db_path),
            http=httpx.AsyncClient(transport=httpx.MockTransport(rest)),
        ).run(duration=seconds)
        elapsed = time.monotonic() - t0
    # worst 60-second window of outgoing requests
    worst = max(
        (sum(1 for u in request_times if t <= u < t + 60) for t in request_times), default=0
    )
    return {
        "seconds": round(elapsed, 1),
        "announced_tx_per_second": tps,
        "limits": {
            "sample_rate": live.sample_rate,
            "fetch_per_minute": live.fetch_per_minute,
            "fetch_burst": live.fetch_burst,
            "queue_max": live.queue_max,
        },
        "counters": dataclasses.asdict(c),
        "rest_requests": len(request_times),
        "max_requests_in_any_60s": worst,
        "request_cap_60s": live.fetch_per_minute + live.fetch_burst,
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "rss_before_run_mib": round(rss0 / 1024, 1),
        "db_bytes": live.db_path.stat().st_size
        + sum(p.stat().st_size for p in tmp.glob("live.sqlite-*")),
        "stored_rows": LiveReader(live.db_path).list(limit=1, outcome=None)[0],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--tx-per-second", type=float, default=20)
    a = ap.parse_args()
    print(json.dumps(asyncio.run(main(a.seconds, a.tx_per_second)), indent=2))
