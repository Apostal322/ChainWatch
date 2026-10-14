"""Soak and fault-injection test of the always-on live stack, using REAL processes against
the LOCAL mock source (scripts/mock_mempool.py). Linux only (reads /proc). Disposable data:
everything lives in a temporary directory that is deleted afterwards.

    PYTHONPATH=src python scripts/soak_test.py --minutes 120 --out soak.json

Topology (the docs/LIVE_CLOUD.md architecture, minus Caddy/TLS):

    mock (WS + REST, fault control) -> collector -> SQLite -> edge API (token)
        -> dashboard in remote mode (RemoteBackend) -> probes / SSE clients

Faults are injected round-robin every --fault-every seconds: REST 429 / 500 / malformed
JSON / schema drift / slow responses, WebSocket drop / silence (stale watchdog) / burst /
sequence gap, collector SIGKILL (restarted like systemd would), edge API restart, dashboard
SSE clients that connect and drop. Resources, API availability and latency are sampled
throughout; pass criteria are evaluated at the end and the process exits 1 on failure.

This measures the software under controlled faults. It is NOT a public-network observation:
the source is constructed data, and it says nothing about mempool.space or a cloud VM.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TICK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
FAULTS = [
    ("rest_429", {"seconds": 20, "value": 5}),
    ("ws_drop", {}),
    ("rest_500", {"seconds": 20}),
    ("ws_burst", {"value": 5000}),
    ("rest_malformed", {"seconds": 20}),
    ("kill_collector", {}),
    ("rest_schema", {"seconds": 20}),
    ("ws_gap", {"value": 3}),
    ("rest_slow", {"seconds": 30, "value": 4}),
    ("restart_edge", {}),
    ("ws_silent", {"seconds": 40}),  # longer than stale_after_s: the watchdog must reconnect
    ("sse_clients", {}),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def proc_stats(pid: int) -> tuple[float, int, int] | None:
    """(cpu seconds, rss kB, peak rss kB) from /proc, or None if the process is gone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        cpu = (int(fields[11]) + int(fields[12])) / TICK
        status = Path(f"/proc/{pid}/status").read_text()
        kv = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        return cpu, int(kv["VmRSS"].split()[0]), int(kv["VmHWM"].split()[0])
    except (FileNotFoundError, ProcessLookupError, KeyError, IndexError):
        return None


class Stack:
    def __init__(self, work: Path, tps: float, args: argparse.Namespace) -> None:
        self.work, self.args = work, args
        self.ports = {k: free_port() for k in ("mock", "edge", "dash")}
        self.token = secrets.token_hex(32)
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "CHAINWATCH_LIVE_TOKEN": self.token,
        }
        self.cfg = work / "live.yaml"
        self.cfg.write_text(
            f"extends: {ROOT / 'configs' / 'live-cloud.yaml'}\n"
            "live:\n"
            f"  db_path: {work / 'live.sqlite'}\n"
            f"  ws_url: ws://127.0.0.1:{self.ports['mock']}/api/v1/ws\n"
            f"  rest_base: http://127.0.0.1:{self.ports['mock']}/api\n"
            f"  sample_rate: {args.sample_rate}\n"
            f"  fetch_per_minute: {args.fetch_per_minute}\n"
            "  fetch_burst: 5\n"
            "  fetch_concurrency: 2\n"
            f"  retention_rows: {args.retention_rows}\n"
            "  stale_after_s: 20\n"
            "  reconnect_max_s: 15\n"
            "  request_timeout_s: 5\n"
            "  edge_trusted_proxy_hops: 0\n"
        )
        self.dash_cfg = work / "dash.yaml"
        self.dash_cfg.write_text(
            f"extends: {ROOT / 'configs' / 'public-demo-live.yaml'}\n"
            "live:\n  remote_url: env:CHAINWATCH_LIVE_URL\n"
        )
        self.tps = tps
        self.procs: dict[str, subprocess.Popen] = {}
        self.cpu_base: dict[str, float] = {}
        self.restarts = {"collector": 0, "edge": 0}

    def _spawn(self, name: str, cmd: list[str], env: dict | None = None) -> None:
        log = open(self.work / f"{name}.log", "ab")  # noqa: SIM115 - closed with the process
        self.procs[name] = subprocess.Popen(
            cmd, cwd=ROOT, env=env or self.env, stdout=log, stderr=subprocess.STDOUT
        )

    def start_mock(self) -> None:
        self._spawn("mock", [PY, "scripts/mock_mempool.py", "--port", str(self.ports["mock"]),
                             "--tx-per-second", str(self.tps)])  # fmt: skip

    def start_collector(self) -> None:
        self._spawn(
            "collector", [PY, "-m", "chainwatch.live.cli", "--config", str(self.cfg), "collect"]
        )

    def start_edge(self) -> None:
        self._spawn("edge", [PY, "-m", "chainwatch.live.cli", "--config", str(self.cfg),
                             "serve-edge", "--port", str(self.ports["edge"])])  # fmt: skip

    def start_dash(self) -> None:
        env = {**self.env, "CHAINWATCH_CONFIG": str(self.dash_cfg),
               "CHAINWATCH_LIVE_URL": f"http://127.0.0.1:{self.ports['edge']}"}  # fmt: skip
        self._spawn(
            "dash", [PY, "-m", "chainwatch.cli", "serve", "--port", str(self.ports["dash"])], env
        )

    def wait_http(self, url: str, timeout: float = 60) -> None:
        end = time.time() + timeout
        while time.time() < end:
            with_suppress = None
            try:
                if httpx.get(url, timeout=2).status_code < 500:
                    return
            except httpx.HTTPError as e:
                with_suppress = e
            time.sleep(0.5)
        raise RuntimeError(f"{url} did not come up: {with_suppress}")

    def stop(self, name: str, sig: int = signal.SIGTERM, timeout: float = 15) -> float | None:
        p = self.procs.get(name)
        if p is None or p.poll() is not None:
            return None
        t0 = time.time()
        p.send_signal(sig)
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return None
        return time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--tx-per-second", type=float, default=6.0)
    ap.add_argument("--sample-rate", type=float, default=0.1)
    ap.add_argument("--fetch-per-minute", type=int, default=120)
    ap.add_argument("--retention-rows", type=int, default=3000)
    ap.add_argument("--fault-every", type=float, default=90)
    ap.add_argument("--sample-every", type=float, default=10)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="chainwatch-soak-"))
    st = Stack(work, args.tx_per_second, args)
    auth = {"authorization": f"Bearer {st.token}"}
    edge = f"http://127.0.0.1:{st.ports['edge']}"
    dash = f"http://127.0.0.1:{st.ports['dash']}"
    mock = f"http://127.0.0.1:{st.ports['mock']}"
    samples: list[dict] = []
    probes = {"edge_ok": 0, "edge_fail": 0, "dash_ok": 0, "dash_fail": 0}
    edge_lat: list[float] = []
    dash_lat: list[float] = []
    faults_done: list[dict] = []
    recoveries: list[dict] = []
    sse_events = {"clients": 0, "events": 0, "errors": 0}
    stop_evt = threading.Event()

    st.start_mock()
    st.wait_http(f"{mock}/__mock/stats")
    t_start = time.time()
    st.start_collector()
    st.start_edge()
    st.wait_http(f"{edge}/health/live")
    st.start_dash()
    st.wait_http(f"{dash}/health/live", timeout=120)

    def status() -> dict | None:
        try:
            r = httpx.get(f"{edge}/v1/live/status", headers=auth, timeout=5)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def wait_connected(since: float, timeout: float = 90) -> float | None:
        end = time.time() + timeout
        while time.time() < end:
            s = status()
            col = (s or {}).get("collector") or {}
            if (
                s
                and s.get("connection") == "connected"
                and (col.get("connected_since") or 0) >= since
            ):
                return time.time() - since
            time.sleep(0.5)
        return None

    def sse_client(seconds: float) -> None:
        sse_events["clients"] += 1
        try:
            with httpx.stream("GET", f"{dash}/v1/live/events", timeout=seconds + 10) as r:
                end = time.time() + seconds
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        sse_events["events"] += 1
                    if time.time() > end:
                        break  # drop the connection mid-stream
        except httpx.HTTPError:
            sse_events["errors"] += 1

    def prober() -> None:
        while not stop_evt.is_set():
            for name, url, lat, h in (
                ("edge", f"{edge}/v1/live/transactions?limit=50", edge_lat, auth),
                ("dash", f"{dash}/v1/live/status", dash_lat, {}),
            ):
                t0 = time.perf_counter()
                try:
                    ok = httpx.get(url, headers=h, timeout=5).status_code == 200
                except httpx.HTTPError:
                    ok = False
                probes[f"{name}_{'ok' if ok else 'fail'}"] += 1
                if ok:
                    lat.append((time.perf_counter() - t0) * 1000)
            stop_evt.wait(2)

    threading.Thread(target=prober, daemon=True).start()
    wait_connected(t_start)
    for name in ("collector", "edge"):
        s = proc_stats(st.procs[name].pid)
        st.cpu_base[name] = s[0] if s else 0.0

    deadline = t_start + args.minutes * 60
    next_fault, next_sample, i = time.time() + args.fault_every, time.time(), 0
    cpu_acc = {"collector": 0.0, "edge": 0.0}  # cpu of processes that were replaced
    while time.time() < deadline:
        now = time.time()
        if now >= next_sample:
            row = {"t": round(now - t_start, 1)}
            for name in ("collector", "edge", "mock", "dash"):
                s = proc_stats(st.procs[name].pid)
                if s:
                    row[f"{name}_rss_kb"] = s[1]
                    if name in cpu_acc:
                        row[f"{name}_cpu_s"] = round(cpu_acc[name] + s[0] - st.cpu_base[name], 2)
            s = status()
            if s:
                c = (s.get("collector") or {}).get("counters") or {}
                row.update(connection=s.get("connection"), queue=c.get("queue_size"),
                           dedupe=c.get("dedupe_size"), received=c.get("received"))  # fmt: skip
            db = work / "live.sqlite"
            row["db_bytes"] = (
                sum(p.stat().st_size for p in work.glob("live.sqlite*")) if db.exists() else 0
            )
            samples.append(row)
            next_sample = now + args.sample_every
        if now >= next_fault:
            kind, params = FAULTS[i % len(FAULTS)]
            i += 1
            rec = {"t": round(now - t_start, 1), "kind": kind}
            if kind == "kill_collector":
                s = proc_stats(st.procs["collector"].pid)
                cpu_acc["collector"] += (s[0] - st.cpu_base["collector"]) if s else 0
                st.stop("collector", signal.SIGKILL)
                time.sleep(5)  # systemd RestartSec is 10 s; shorter keeps the run dense
                t0 = time.time()
                st.start_collector()
                st.restarts["collector"] += 1
                time.sleep(0.5)
                st.cpu_base["collector"] = (proc_stats(st.procs["collector"].pid) or (0,))[0]
                rec["recovered_s"] = wait_connected(t0)
            elif kind == "restart_edge":
                s = proc_stats(st.procs["edge"].pid)
                cpu_acc["edge"] += (s[0] - st.cpu_base["edge"]) if s else 0
                rec["shutdown_s"] = st.stop("edge")
                t0 = time.time()
                st.start_edge()
                st.restarts["edge"] += 1
                try:
                    st.wait_http(f"{edge}/health/live", 30)
                    rec["recovered_s"] = round(time.time() - t0, 2)
                except RuntimeError:
                    rec["recovered_s"] = None
                st.cpu_base["edge"] = (proc_stats(st.procs["edge"].pid) or (0,))[0]
            elif kind == "sse_clients":
                ts = [threading.Thread(target=sse_client, args=(5 + k,)) for k in range(5)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(30)
                rec["sse"] = dict(sse_events)
            else:
                httpx.post(f"{mock}/__mock/fault", json={"kind": kind, **params}, timeout=5)
                if kind in ("ws_drop", "ws_silent"):
                    rec["recovered_s"] = wait_connected(time.time(), timeout=120)
            faults_done.append(rec)
            if "recovered_s" in rec:
                recoveries.append(rec)
            next_fault = time.time() + args.fault_every
        time.sleep(0.5)

    stop_evt.set()
    final_status = status()
    shutdown = {name: st.stop(name) for name in ("dash", "edge", "collector")}
    mock_stats = httpx.get(f"{mock}/__mock/stats", timeout=5).json()
    st.stop("mock")

    con = sqlite3.connect(work / "live.sqlite")
    rows = con.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    outages = con.execute(
        "SELECT kind, COUNT(*), ROUND(SUM(COALESCE(ended_at, started_at) - started_at), 1) "
        "FROM outages GROUP BY kind"
    ).fetchall()
    errors = con.execute("SELECT error, COUNT(*) FROM tx WHERE outcome='failed' "
                         "GROUP BY error ORDER BY 2 DESC LIMIT 10").fetchall()  # fmt: skip
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    tracebacks = {n: (work / f"{n}.log").read_text(errors="replace").count("Traceback")
                  for n in ("collector", "edge", "dash")}  # fmt: skip

    elapsed = time.time() - t_start
    half = len(samples) // 2

    def peak(key: str, part: list[dict]) -> int:
        return max((r.get(key, 0) for r in part), default=0)

    def pct(xs: list[float], q: float) -> float | None:
        return round(statistics.quantiles(xs, n=100)[q - 1], 1) if len(xs) >= 2 else None

    cpu_c = samples[-1].get("collector_cpu_s", 0) if samples else 0
    cpu_e = samples[-1].get("edge_cpu_s", 0) if samples else 0
    total = {k: probes[f"{k}_ok"] + probes[f"{k}_fail"] for k in ("edge", "dash")}
    avail = {k: round(probes[f"{k}_ok"] / max(1, n), 4) for k, n in total.items()}
    pair = ("collector", "edge")
    rss_first = {n: round(peak(f"{n}_rss_kb", samples[:half]) / 1024, 1) for n in pair}
    rss_second = {n: round(peak(f"{n}_rss_kb", samples[half:]) / 1024, 1) for n in pair}
    recovery: dict[str, list] = {}
    for r in recoveries:
        recovery.setdefault(r["kind"], []).append(r["recovered_s"])
    latency = {
        f"{name}_p{q}": pct(xs, q)
        for name, xs in (("edge", edge_lat), ("dash", dash_lat))
        for q in (50, 95, 99)
    }
    report = {
        "kind": "LOCAL soak + fault injection against a MOCK source (not network data)",
        "elapsed_s": round(elapsed),
        "settings": {k: v for k, v in vars(args).items() if k not in ("out", "keep")},
        "faults_injected": len(faults_done),
        "faults": faults_done,
        "recovery_s": recovery,
        "collector_restarts": st.restarts["collector"],
        "edge_restarts": st.restarts["edge"],
        "cpu_percent_one_core": {
            "collector": round(100 * cpu_c / elapsed, 2),
            "edge": round(100 * cpu_e / elapsed, 2),
        },
        "rss_peak_mb": {
            n: round(peak(f"{n}_rss_kb", samples) / 1024, 1) for n in ("collector", "edge", "dash")
        },
        "rss_peak_mb_first_half": rss_first,
        "rss_peak_mb_second_half": rss_second,
        "queue_max_seen": max((r.get("queue") or 0 for r in samples), default=0),
        "db_bytes_max": peak("db_bytes", samples),
        "rows_final": rows,
        "outages_by_kind": {k: {"count": n, "seconds": s} for k, n, s in outages},
        "failed_by_error": dict(errors),
        "integrity_check": integrity,
        "api_availability": avail,
        "probes": total,
        "latency_ms": latency,
        "sse": sse_events,
        "shutdown_s": shutdown,
        "tracebacks_in_logs": tracebacks,
        "final_counters": ((final_status or {}).get("collector") or {}).get("counters"),
        "mock": mock_stats,
        "samples": samples,
    }
    rec_times = [r["recovered_s"] for r in recoveries]
    down = {k: n for k, n, _ in outages}.get("collector_down", 0)
    checks = {
        "every recovery happened (<= 90 s)": all(x is not None and x <= 90 for x in rec_times),
        "rows within retention cap": rows <= args.retention_rows,
        "collector memory bounded (2nd-half peak <= 1st-half peak + 25 MB)": (
            rss_second["collector"] <= rss_first["collector"] + 25
        ),
        "edge memory bounded (2nd-half peak <= 1st-half peak + 25 MB)": (
            rss_second["edge"] <= rss_first["edge"] + 25
        ),
        "queue never above queue_max (100)": report["queue_max_seen"] <= 100,
        "SQLite integrity ok": integrity == "ok",
        "dashboard availability >= 98 %": avail["dash"] >= 0.98,
        "edge availability >= 97 % (includes restarts)": avail["edge"] >= 0.97,
        "no tracebacks in logs": sum(tracebacks.values()) == 0,
        "every process stopped cleanly on SIGTERM": all(v is not None for v in shutdown.values()),
        "collector_down outage recorded for each kill": down >= st.restarts["collector"],
    }
    report["checks"] = checks
    report["passed"] = all(checks.values())
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text)
    summary = {k: v for k, v in report.items() if k not in ("samples", "faults")}
    print(json.dumps(summary, indent=2, default=str))
    if args.keep:
        print(f"kept {work}")
    else:
        for p in sorted(work.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        work.rmdir()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
