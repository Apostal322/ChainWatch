"""Inference latency benchmark (``chainwatch benchmark``).

Two layers are measured separately:

* **in-process** ``predict_proba`` of one snapshot pipeline (preprocessing + XGBoost),
  single rows and batches;
* **end-to-end HTTP** against a real ``uvicorn`` server (one worker, loopback), which
  includes JSON (de)serialisation, validation, feature lookup and logging.

Numbers depend on the machine; the JSON records the hardware and conditions.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from chainwatch.config import Config
from chainwatch.serving.app import ServiceState
from chainwatch.utils import environment_info, get_logger, write_json

log = get_logger(__name__)


def _pct(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs)
    return {
        "p50_ms": float(np.percentile(a, 50)),
        "p95_ms": float(np.percentile(a, 95)),
        "p99_ms": float(np.percentile(a, 99)),
        "mean_ms": float(a.mean()),
        "n": len(a),
    }


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def in_process(svc: ServiceState, repeats: int = 300) -> dict[str, Any]:
    eng = svc.engine
    step = eng.replay_steps()[-1]
    version = eng.model_for(step, "expanding")
    ids = svc.store.step_frame(step)["tx_id"].tolist()
    rng = np.random.default_rng(0)
    eng.score(version, ids[:1])  # warm-up
    single = []
    for i in rng.integers(0, len(ids), repeats):
        t0 = time.perf_counter()
        eng.score(version, [ids[int(i)]])
        single.append((time.perf_counter() - t0) * 1e3)
    batches = {}
    for n in (100, 1000):
        sample = [ids[int(i)] for i in rng.integers(0, len(ids), n)]
        sample = list(dict.fromkeys(sample))
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            eng.score(version, sample)
            times.append((time.perf_counter() - t0) * 1e3)
        batches[str(len(sample))] = _pct(times) | {
            "us_per_tx": float(np.median(times)) * 1e3 / len(sample)
        }
    return {"model_version": version, "single_row": _pct(single), "batch": batches}


def http(cfg: Config, svc: ServiceState, repeats: int = 200) -> dict[str, Any]:
    port = _free_port()
    env = dict(os.environ, CHAINWATCH_CONFIG=str(cfg.source_path))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "chainwatch.serving.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 300
        with httpx.Client(base_url=base, timeout=60) as c:
            while True:
                try:
                    if c.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.time() > deadline or proc.poll() is not None:
                    raise RuntimeError("server did not become ready")
                time.sleep(0.5)
            step = svc.engine.replay_steps()[-1]
            ids = svc.store.step_frame(step)["tx_id"].tolist()
            rng = np.random.default_rng(1)
            results: dict[str, Any] = {}

            def timed(fn, n):
                fn()  # warm-up (fills the per-step cache)
                ts = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    r = fn()
                    ts.append((time.perf_counter() - t0) * 1e3)
                    r.raise_for_status()
                return _pct(ts)

            results["GET /health/live"] = timed(lambda: c.get("/health/live"), repeats)
            results["POST /v1/predict/batch (1 id)"] = timed(
                lambda: c.post(
                    "/v1/predict/batch", json={"tx_ids": [ids[int(rng.integers(len(ids)))]]}
                ),
                repeats,
            )
            for n in (100, 1000):
                sample = list(dict.fromkeys(ids[int(i)] for i in rng.integers(0, len(ids), n)))
                results[f"POST /v1/predict/batch ({len(sample)} ids)"] = timed(
                    lambda s=sample: c.post("/v1/predict/batch", json={"tx_ids": s}), 30
                )
            results["GET /v1/transactions/{id} (incl. TreeSHAP)"] = timed(
                lambda: c.get(f"/v1/transactions/{ids[int(rng.integers(len(ids)))]}"), 100
            )
            results["GET /v1/replay/steps/{s}/ranking (cached step)"] = timed(
                lambda: c.get(f"/v1/replay/steps/{step}/ranking?limit=50"), 100
            )
            return results
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def run_benchmark(cfg: Config, out: Path | None = None) -> Path:
    svc = ServiceState(cfg)
    result = {
        "conditions": {
            "cpu": _cpu_model(),
            "logical_cpus": os.cpu_count(),
            "platform": platform.platform(),
            "data_mode": svc.registry_meta["data_mode"],
            "n_transactions": len(svc.store.model_frame),
            "n_models": len(svc.bundles),
            "server": "uvicorn, 1 worker, loopback HTTP, JSON logging to /dev/null",
            "service_load_seconds": round(svc.load_seconds, 2),
            "environment": environment_info(),
        },
        "in_process": in_process(svc),
        "http": http(cfg, svc),
    }
    mode = svc.registry_meta["data_mode"].replace("+", "p")
    out = out or cfg.paths.reports_dir / "phase3" / f"benchmark_{mode}.json"
    write_json(out, result)
    log.info("benchmark written to %s", out)
    return out
