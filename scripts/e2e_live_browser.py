"""Full-system integration test in a real browser (LOCAL mock source, disposable data):

    mock WS/REST -> collector -> SQLite -> edge API (bearer token) -> dashboard in remote
    mode (RemoteBackend) -> SSE -> React "Live monitor" tab

Checks that a new transaction arrives live and its detail panel matches the API, that risk
intelligence works end to end (a local TEST indicator list naming some mock output addresses
-> source matches in the API, the investigation queue and the per-transaction assessment;
every fetched row is assessed in the same commit), that a collector outage and its recovery
are shown, and that the replay tab stays synthetic.
Needs Playwright with Chromium in the running interpreter; the services run with
CHAINWATCH_PY (default: this interpreter), which needs the project installed. Built
frontend required (cd frontend && npm run build).

    python scripts/e2e_live_browser.py /tmp/e2e-shots
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_fixtures import p2wpkh_address, txid  # noqa: E402  (the mock's address scheme)

PY = os.environ.get("CHAINWATCH_PY", sys.executable)
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
work = Path(tempfile.mkdtemp(prefix="cw-e2e-"))
tok = secrets.token_hex(32)
env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CHAINWATCH_LIVE_TOKEN": tok}
(work / "live.yaml").write_text(f"""extends: {ROOT}/configs/live-cloud.yaml
live:
  db_path: {work}/live.sqlite
  ws_url: ws://127.0.0.1:8971/api/v1/ws
  rest_base: http://127.0.0.1:8971/api
  sample_rate: 0.3
  fetch_per_minute: 120
  edge_trusted_proxy_hops: 0
""")
procs = {}


def spawn(name, cmd, e=env):
    procs[name] = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=e,
        stdout=open(work / f"{name}.log", "ab"),  # noqa: SIM115 - lives as long as the process
        stderr=subprocess.STDOUT,
    )


def wait(url, t=90):
    end = time.time() + t
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} not up")


results = {}
NNBSP = chr(0x202F)  # narrow no-break space (locale number grouping)


def until(fn, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.5)
    raise SystemExit("timed out waiting")


def check(name, ok, detail=""):
    results[name] = bool(ok)
    print(("PASS " if ok else "FAIL ") + name, detail, flush=True)


try:
    # TEST indicator list (not real sanctions data): output 0 of every 5th mock transaction
    listed = [p2wpkh_address(f"{txid(f'mock-0-{n}')}-out-0") for n in range(5, 3000, 5)]
    (work / "test-list.txt").write_text("\n".join(listed) + "\n")
    subprocess.run(
        [PY, "-m", "chainwatch.live.cli", "--config", str(work / "live.yaml"), "indicators",
         "update", "--source", "ofac-sdn-mirror", "--file", str(work / "test-list.txt")],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )  # fmt: skip
    spawn("mock", [PY, "scripts/mock_mempool.py", "--port", "8971", "--tx-per-second", "4"])
    wait("http://127.0.0.1:8971/__mock/stats")
    spawn(
        "collector",
        [PY, "-m", "chainwatch.live.cli", "--config", str(work / "live.yaml"), "collect"],
    )
    spawn(
        "edge",
        [
            PY,
            "-m",
            "chainwatch.live.cli",
            "--config",
            str(work / "live.yaml"),
            "serve-edge",
            "--port",
            "8972",
        ],
    )
    wait("http://127.0.0.1:8972/health/live")
    denv = {
        **env,
        "CHAINWATCH_CONFIG": str(ROOT / "configs/public-demo-live.yaml"),
        "CHAINWATCH_LIVE_URL": "http://127.0.0.1:8972",
    }
    spawn("dash", [PY, "-m", "chainwatch.cli", "serve", "--port", "8973"], denv)
    wait("http://127.0.0.1:8973/health/live", 120)
    time.sleep(8)
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto("http://127.0.0.1:8973")
        pg.get_by_role("tab", name="Live monitor").click()
        pg.wait_for_selector("tbody tr button.rowbtn", timeout=60000)
        body = pg.locator("body").inner_text()
        check(
            "mock source labelled (not REAL)",
            "LOCAL MOCK" in body and "REAL BITCOIN NETWORK DATA" not in body,
        )
        check("connected badge", "Connected" in pg.locator(".live-head").inner_text())
        check("via remote edge API", "remote collector" in body)
        n0 = pg.locator("tbody tr button.rowbtn").count()
        # a NEW transaction arrives through SSE (rows grow without reload)
        until(lambda: pg.locator("tbody tr button.rowbtn").count() > n0, 60)
        check(
            "new transactions arrive live via SSE",
            True,
            f"{n0} -> {pg.locator('tbody tr button.rowbtn').count()} rows",
        )
        btn = pg.locator("tbody tr button.rowbtn").first
        btn.click()
        pg.wait_for_selector("text=mock data: no explorer link", timeout=15000)
        detail = pg.locator(".detail, aside").last.inner_text()
        api_rows = httpx.get(
            "http://127.0.0.1:8973/v1/live/transactions?limit=200&outcome=fetched"
        ).json()["rows"]
        txid = next((r["txid"] for r in api_rows if r["txid"] in pg.content()), None)
        tx = httpx.get(f"http://127.0.0.1:8973/v1/live/transactions/{txid}").json() if txid else {}
        check(
            "detail panel matches API (txid, vsize, fee)",
            txid
            and txid in detail
            and str(tx.get("vsize")) in detail
            and f"{tx.get('fee'):,}" in detail.replace(NNBSP, ","),
            f"txid {str(txid)[:12]}",
        )
        pg.screenshot(path=str(OUT / "e2e_connected.png"), full_page=True)
        # ---- risk intelligence, end to end
        base = "http://127.0.0.1:8973/v1/live"
        until(lambda: httpx.get(f"{base}/findings?max_priority=1").json()["findings"], 90)
        p1 = httpx.get(f"{base}/findings?max_priority=1").json()["findings"][0]
        match = p1["screening"]["matches"][0]
        check(
            "source match in the API (point in time, with provenance)",
            match["address"] in listed and match["source_id"] == "ofac-sdn-mirror"
            and match["available_at"] <= p1["evaluated_at"],
            f"{p1['txid'][:12]} {match['role']} #{match['index']}",
        )  # fmt: skip
        rows = httpx.get(f"{base}/transactions?limit=200&outcome=fetched").json()["rows"]
        check(
            "every fetched row is assessed (same commit)",
            rows and all(r.get("risk_assessed") for r in rows),
            f"{len(rows)} rows",
        )
        summ = httpx.get(f"{base}/risk/summary?hours=1").json()
        check(
            "summary: source fresh, settings reported, supervised abstains",
            summ["summary"]["sources"][0]["stale"] is False
            and summ["engine"]["anomaly_min_reference"] == 1000
            and summ["engine"]["supervised"]["state"] == "abstaining"
            and set(summ["summary"]["supervised"]) == {"NO_VALIDATED_MODEL"},
            json.dumps({k: summ["engine"].get(k) for k in ("supervised", "anomaly_min_reference")}),
        )
        pg.wait_for_selector(f"ul.findings button[aria-label$='{p1['txid']}']", timeout=40000)
        live_text = pg.locator("body").inner_text()
        check(
            "risk panels rendered with caveats",
            all(t in live_text for t in ("Risk intelligence", "Investigation queue",
                                          "Recent findings", "not proof of fraud"))
            and "illicit" not in live_text.lower(),
        )  # fmt: skip
        pg.locator(f"ul.findings button[aria-label$='{p1['txid']}']").first.click()
        pg.wait_for_selector("#ra-h", timeout=15000)
        detail = pg.locator(".detail, aside").last.inner_text()
        check(
            "assessment in the detail panel matches the API",
            p1["txid"] in detail and match["address"] in detail and "Source match" in detail,
        )
        pg.screenshot(path=str(OUT / "e2e_risk.png"), full_page=True)
        # upstream outage: kill the collector (unclean) and keep it down
        procs["collector"].send_signal(signal.SIGKILL)
        procs["collector"].wait()
        t_kill = time.time()
        until(lambda: "Connected" not in pg.locator(".live-head").inner_text(), 60)
        head = pg.locator(".live-head").inner_text().splitlines()
        badge = head[1] if len(head) > 1 else " ".join(head)[:80]
        check(
            "outage reflected in the UI",
            not any("Connected" in line for line in head),
            f"after {time.time() - t_kill:.0f}s: {badge}",
        )
        pg.screenshot(path=str(OUT / "e2e_outage.png"), full_page=True)
        time.sleep(5)
        spawn(
            "collector",
            [PY, "-m", "chainwatch.live.cli", "--config", str(work / "live.yaml"), "collect"],
        )
        t_up = time.time()
        until(lambda: "Connected" in pg.locator(".live-head").inner_text(), 90)
        check("recovery reflected in the UI", True, f"{time.time() - t_up:.0f}s after restart")
        pg.wait_for_selector("text=Collector not running", timeout=60000)
        check("outage listed in gaps table", True, "collector_down 'unclean exit'")
        pg.screenshot(path=str(OUT / "e2e_recovered.png"), full_page=True)
        pg.get_by_role("tab", name="Replay").click()
        pg.wait_for_selector("text=SYNTHETIC DEMO DATA", timeout=30000)
        check(
            "replay tab still synthetic, no live rows mixed in",
            "LOCAL MOCK" not in pg.locator("main").inner_text().split("Replay")[-1][:2000],
        )
        b.close()
    check("no browser console errors", not errors, str(errors[:3]))
finally:
    for pr in procs.values():
        if pr.poll() is None:
            pr.terminate()
    for pr in procs.values():
        try:
            pr.wait(15)
        except subprocess.TimeoutExpired:
            pr.kill()
    for p in sorted(work.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    work.rmdir()
print(json.dumps({"passed": all(results.values()), "results": results}))
sys.exit(0 if results and all(results.values()) else 1)
