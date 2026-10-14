# ChainWatch Live: always-on collection at $0 (deployment design)

**Status: prepared and tested, not deployed.** No cloud resource has been created, no money
has been spent, and the production Render demo is unchanged. Nothing here claims 24/7
reliability: that requires the 24-hour observation in §10 on the real VM. Real mempool.space
ingestion is still unverified, because the build environment's egress proxy blocks
mempool.space (docs/LIVE_MONITOR.md §7).

## 1. Architecture

```
mempool.space (public)                     Oracle Cloud Always Free VM (Ubuntu 24.04)
  wss /api/v1/ws  ───────────────────────►  chainwatch-collector.service   (systemd, Restart=always)
  GET /api/tx/:id ◄── ≤12 req/min ──────┘    │  reconnect + backoff, dedupe, bounded queue,
                                             │  sampling, outage/gap tracking, restart recovery
                                             ▼
                                           /var/lib/chainwatch/live.sqlite (WAL, ≤50k rows, ≤7 days)
                                             │  read-only (mode=ro)
                                             ▼
                                           chainwatch-live-api.service  127.0.0.1:8100
                                             │  bearer token, rate limit, GET-only, bounded
                                             ▼
                                           Caddy :443  (Let's Encrypt via <ip>.sslip.io)
                                             ▲
            HTTPS + Authorization: Bearer ───┘  (server-to-server; the token never reaches browsers)
Render Free web service (existing demo image, remote live mode)
  /v1/live/* proxy with a 2 s shared cache ──► browser "Live monitor" tab (SSE)
  /v1/replay/* synthetic demo + research results (unchanged)
```

* **Separation:**
  * The VM runs only `chainwatch-live` (collector, edge API, backups) from a 26-package
    hash-locked set (`requirements/live.lock.txt`). There is no pandas, scikit-learn or
    XGBoost, and no command that trains a model. A test checks that the VM entry points
    import none of them.
  * The historical research and the synthetic demo stay on Render, unchanged.
* **Always on:**
  * systemd restarts each process after a crash (`Restart=always`, never giving up) and
    starts it at boot.
  * The collector reconnects with exponential backoff and jitter.
  * Render sleeping, nobody visiting, or your computer being off does not affect
    collection.
* **Secure integration:**
  * The SQLite file is never reachable from the network.
  * Caddy terminates TLS.
  * The edge API listens only on 127.0.0.1 and requires a ≥ 32-character bearer token
    (constant-time comparison).
  * Render calls it server to server, and browsers only ever talk to Render.

## 2. Provider evaluation (checked 2026-10-14; re-check before you sign up)

### Oracle Cloud Infrastructure (OCI) Always Free: recommended, with caveats

| Topic | Finding | Source |
|---|---|---|
| Arm compute | **Changed in 2026.** Reports say the Ampere A1 allowance for free-tier accounts was halved, from 3,000 to **1,500 OCPU-hours** and from 18,000 to **9,000 GB-hours** per month (2 OCPU / 12 GB), effective mid-2026. Oracle's own page was still showing 4 OCPU / 24 GB in cached results, and Oracle support reportedly said Pay As You Go (PAYG) accounts keep 4 / 24. **Treat 1 OCPU / 6 GB as the safe target.** ChainWatch needs far less (§8) | [InfoQ](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/), [heise](https://www.heise.de/en/news/Oracle-halves-free-cloud-resources-11334516.html), [terminalbytes](https://terminalbytes.com/oracle-cloud-free-tier-changes-2026/), [Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm) |
| x86 compute | 2 × VM.Standard.E2.1.Micro (1/8 OCPU, 1 GB). Also sufficient for ChainWatch | [Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm) |
| ARM64 | Every locked wheel exists for aarch64 and x86-64 (CPython 3.12, manylinux): **verified** by hash-checked download of all 26 wheels for both architectures | this repo |
| Capacity | "Out of host capacity" for A1 is common. Oracle's advice is to try another availability domain or retry later. Your **home region is permanent** and Always Free compute only exists there | [Oracle FAQ](https://www.oracle.com/cloud/free/faq/) |
| Storage | 200 GB block or boot volume in total (third-party reports, not re-confirmed officially). ChainWatch needs < 1 GB of data on the 47 GB default boot volume | [fullmetalbrackets](https://fullmetalbrackets.com/blog/oci-free-tier-breakdown) |
| Bandwidth | **10 TB outbound per month** free (Oracle free-tier page). Inbound traffic is not charged. ChainWatch uses roughly 3–6 GB in and < 1 GB out per month (§8) | [Oracle free tier](https://oracle.com/at/cloud/free) |
| Sign-up | A credit or debit card for identity verification (prepaid and many virtual cards are rejected) and a phone number. The card is not charged unless you upgrade | [Oracle FAQ](https://www.oracle.com/cloud/free/faq/) and community reports |
| **Idle reclamation** | Oracle docs: an Always Free instance is idle, and **may be reclaimed**, if over 7 days its 95th-percentile CPU < 20 %, network < 20 % **and** memory < 20 % (memory applies to A1 only). **ChainWatch will be idle by this definition** (§8). Third-party sources say PAYG accounts are exempt | [Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm), [Customer Connect](https://community.oracle.com/customerconnect/discussion/671904/reclamation-of-idle-compute-instances) |
| Billing risk | A free-tier-only account cannot be billed. **Upgrading to PAYG** removes reclamation and keeps Always Free resources free, but anything beyond the limits is billed to your card: a larger shape, a second boot volume, a load balancer, extra bandwidth | Oracle free tier and FAQ |

**Do not** run artificial CPU load to dodge the idle policy. It wastes shared capacity,
works against the intent of the rule, and could breach the terms.

You can choose between:
* **(a) Stay free-tier only:** zero billing risk. Accept that Oracle may reclaim (stop) the
  VM. The recovery steps in §9 and the gap tracking make any such stop visible and short.
* **(b) Upgrade to PAYG:** reclamation stops. Protect yourself against charges with a budget
  of $1 and an email alert at $0.01, and create only the resources listed in §7.

**Recommendation: (a) first.** It cannot cost money. Move to (b) only if reclamation actually
happens and you accept the card risk.

### Alternatives

| Option | Zero cost? | Limitations |
|---|---|---|
| Google Cloud free tier e2-micro (us-west1/us-central1/us-east1, 30 GB disk, 1 GB egress per month) | **Probably not.** Third-party sources say an in-use external IPv4 address is billed at about $0.005 per hour (≈ $3.65 per month) and is not covered by the free tier | 0.25–2 vCPU (burst), 1 GB RAM: enough. Billing account required. [Google docs](https://cloud.google.com/free/docs/compute-getting-started), [agentdeals](https://agentdeals.dev/vendor/google-compute-engine) |
| Render background worker | No: workers have no free plan | — |
| Render free web service running the collector | No in practice: it **sleeps after 15 min idle**, which stops collection | — |
| GitHub Actions on a schedule | Free for public repositories, but **not continuous** (jobs ≤ 6 h, delayed cron) and outside the intended use of Actions | not recommended |
| A device at home (Raspberry Pi) | No cloud bill, but needs hardware and power | does not meet "computer off" |

## 3. Infrastructure requirements (measured, §8)

| Resource | Needed | Always Free |
|---|---|---|
| CPU | < 2 % of one core on average | E2.1.Micro (1/8 OCPU) or 1 A1 OCPU |
| Memory | about 40 MB collector + 60 MB edge API + Caddy (≈ 40 MB) + OS | 1 GB (E2.1.Micro) is enough; 6 GB on A1 is plenty |
| Disk | 63 MB database at the 50,000-row cap, 7 daily backups ≤ 450 MB, 68 MB virtualenv | 47 GB default boot volume |
| Network | 1 outbound WebSocket and ≤ 12 HTTPS requests per minute to mempool.space; inbound 443 only | 10 TB per month outbound |
| OS | Ubuntu 24.04 (Python 3.12, systemd 255, `caddy` in universe) | standard platform image |

## 4. Components in this repository

| Path | Purpose |
|---|---|
| `src/chainwatch/live/collector.py` | collector: reconnect, backoff, dedupe, sampling, bounded queue, 429 handling, restart recovery, outage and sequence-gap tracking, retention, hourly WAL checkpoint |
| `src/chainwatch/live/store.py` | SQLite schema (WAL, indexes, `outages`), retention by rows **and** age, backup API, availability calculation |
| `src/chainwatch/live/edge.py` | edge API: token auth, rate limit, health checks, no docs |
| `src/chainwatch/live/cli.py` | `chainwatch-live collect / serve-edge / backup / report` (no ML imports) |
| `src/chainwatch/serving/live_api.py` | `/v1/live/*` with a local backend (SQLite) or a remote backend (HTTPS edge proxy with cache) |
| `configs/live-cloud.yaml` | VM settings: 2 % sample, 12 requests/min, 50k rows, 7 days |
| `configs/public-demo-live.yaml` | Render dashboard in remote mode (URL and token from environment variables) |
| `requirements/live.lock.txt` | hash-locked VM dependencies (x86-64 and ARM64) |
| `deploy/oracle/*.service`, `*.timer` | systemd units: collector, edge API, daily backup |
| `deploy/oracle/Caddyfile.template` | HTTPS reverse proxy, everything except the edge routes answers 404 |
| `deploy/oracle/install.sh` | idempotent installer (packages, user, directories, venv, token, units, Caddy, firewall) |
| `deploy/oracle/smoke_live.sh` | post-deployment smoke test |

## 5. Reliability features

| Requirement | Implementation |
|---|---|
| WebSocket reconnection, exponential backoff | full-jitter backoff (1 s → 60 s), reset after a working stream; 90 s staleness watchdog; protocol pings every 20 s |
| Bounded queues, deduplication | 100-entry fetch queue (drops are counted); 200k-txid LRU; `UNIQUE(txid)` with `INSERT OR IGNORE` |
| Conservative request limits | token bucket, 12 requests/min with a burst of 3, one at a time; pause on HTTP 429 for `Retry-After` |
| Persistent state and restart recovery | SQLite on the boot volume. At start-up the collector records the time since the previous heartbeat as a `collector_down` gap, labelled "clean stop" or "unclean exit", and closes outages left open by a crash. Stored txids are not fetched again |
| Graceful shutdown | SIGTERM → final heartbeat `stopped`, open outage closed, HTTP client closed (`TimeoutStopSec=30`) |
| Crash restart / boot start | `Restart=always`, `StartLimitIntervalSec=0`, `RestartSteps`/`RestartMaxDelaySec` (10 s → 5 min); `WantedBy=multi-user.target` |
| Health metrics | heartbeat every 5 s, about 20 counters, `/health/ready` (200 only when connected with a fresh heartbeat), `chainwatch-live report` |
| Outage and gap tracking | `outages` table: `collector_down`, `disconnected` (WebSocket), `sequence_gap`. A sequence gap is recorded when mempool's `sequence` (+1 per update, every update sent to every subscriber) jumps by more than 1 within one connection. These feed availability in `/v1/live/status` and the dashboard |
| Retention and bounded growth | ≤ 50,000 transactions and ≤ 7 days, 24 h of per-minute counters, 30 days of outages; hourly `wal_checkpoint(TRUNCATE)`; journald capped at 200 MB; 7 rotated backups |
| Resource caps | systemd `MemoryMax=300M` / `250M`, `CPUQuota=50%`, `TasksMax=64` |

## 6. Security model

* **Network exposure:** only Caddy (TCP 80/443). SSH is protected by Oracle's key-only
  default. The edge API listens on 127.0.0.1, and SQLite is a local file.
* **Authentication:**
  * every `/v1/live/*` call needs `Authorization: Bearer <token>`;
  * the token is 64 hex characters generated **on the VM** by `install.sh` (`openssl rand -hex
    32`) and stored in `/etc/chainwatch/live.env` (0640 root:chainwatch);
  * on Render it is a secret environment variable; it is never in Git and never sent to
    browsers.
* **Limits:**
  * edge API: 240 requests/min per client (keyed on the address Caddy appends), 64 KB
    bodies, `limit ≤ 200`, GET only, no docs or OpenAPI schema;
  * dashboard: its existing 120/min public limit plus a 2 s shared cache, so the number of
    browsers does not multiply edge load.
* **Sandbox:** units run as the `chainwatch` user with `ProtectSystem=strict`,
  `NoNewPrivileges`, an empty capability set and only `/var/lib/chainwatch` writable.
  `systemd-analyze security` exposure scores: collector 3.0, API 3.0, backup 2.3 (all "OK").
* **Supply chain:** hash-locked pip install (`--require-hashes`); code is cloned from the
  public repository at a chosen ref.
* **Logs:** JSON lines with no request bodies, tokens or client addresses.

## 7. Activate it: manual steps (owner)

**A. Oracle account** (cannot be automated; your decision on billing):
1. Sign up at oracle.com/cloud/free with a credit or debit card and phone number. Pick a home
   region with available A1 capacity and, if possible, 3 availability domains. **The choice
   is permanent.**
2. Optionally set a budget (Billing → Budgets, $1, alert at $0.01) as an early warning.
3. Stay on the free-tier account (§2, option a) unless you decide otherwise.

**B. VM** (Console → Compute → Instances → Create):
1. Image **Canonical Ubuntu 24.04**.
2. Shape **VM.Standard.A1.Flex, 1 OCPU, 6 GB**. If capacity is missing, use
   **VM.Standard.E2.1.Micro**.
3. Default 47 GB boot volume, public IPv4, upload your SSH public key.
4. VCN security list: add ingress TCP **80** and **443** from 0.0.0.0/0 (22 stays as is).
5. Create nothing else: no block volumes, load balancers or databases.

**C. Install** (SSH into the VM):
```bash
IP=$(curl -s https://ifconfig.me); HOST="${IP//./-}.sslip.io"     # e.g. 203-0-113-7.sslip.io
curl -fsSLO https://raw.githubusercontent.com/Apostal322/ChainWatch/main/deploy/oracle/install.sh
less install.sh                                  # read it before running it as root
sudo CHAINWATCH_HOST="$HOST" bash install.sh
sudo cat /etc/chainwatch/live.env                # copy the token once; keep it secret
```

**D. Verify on the VM, then from your machine:**
```bash
systemctl status chainwatch-collector chainwatch-live-api caddy --no-pager
TOKEN=<token> ./deploy/oracle/smoke_live.sh "https://$HOST"   # from a clone of the repo
```

**E. Connect the dashboard** (only after D passes; this changes production):
1. Merge the pull request so that GitHub Actions publishes the new demo image.
2. Change `render.yaml` to the new image tag.
3. In the Render dashboard, set:
   * `CHAINWATCH_CONFIG=/app/configs/public-demo-live.yaml`
   * `CHAINWATCH_LIVE_URL=https://$HOST`
   * `CHAINWATCH_LIVE_TOKEN` as a **secret**.
4. Manual Deploy.
5. Run `scripts/smoke_demo.sh` against the Render URL and open the Live monitor tab.

## 8. Resource and usage estimates

Measured with the real processes (`chainwatch-live collect` and `serve-edge`) against a local
mock source that announces 6 tx/s, with `configs/live-cloud.yaml` limits and a client polling
the edge API every 2 s:

| Measure | Value |
|---|---|
| Collector CPU | 0.46 CPU-s over 600 s ⇒ **0.08 %** of one core (3,600 txids announced, 79 sampled, 78 fetched, 1 failed, 0 dropped) |
| Edge API CPU (with polling) | 2.6 CPU-s over 600 s ⇒ **0.43 %** of one core (about 560 authenticated requests) |
| Collector memory (peak RSS) | **39.4 MB** (VmHWM) |
| Edge API memory (peak RSS) | **56.6 MB** (VmHWM) |
| Container test (fresh Ubuntu 24.04, install.sh) | collector 37 MB, edge API 57 MB RSS; < 1 % CPU |
| Database at the retention cap | **62.7 MB** for 50,000 rows (1,253 bytes per row) |
| Virtualenv on the VM | 68 MB |

Monthly estimates (derived, not measured on the network):
* **Requests to mempool.space:** ≤ 12 per minute ⇒ ≤ 17,300 per day ⇒ ≤ 520k per month,
  plus one long-lived WebSocket.
* **Inbound traffic:** about 64-byte txids at a few per second (≈ 1–2 GB per month), plus
  detail payloads of 2–5 KB × ≤ 17k per day (≈ 1–3 GB per month).
* **Outbound traffic:** dashboard polling; below 1 GB per month at demo traffic.
* **Cost:** $0 within Always Free (§2), provided you create only the resources in §7.

## 9. Monitoring and recovery

| Situation | What to do |
|---|---|
| Health at a glance | `curl -s https://$HOST/health/ready` (200 = connected); the dashboard shows last observation, observed share and gaps |
| Detailed state | `sudo -u chainwatch /opt/chainwatch/venv/bin/chainwatch-live --config /opt/chainwatch/app/configs/live-cloud.yaml report --hours 24` |
| Logs | `journalctl -u chainwatch-collector -u chainwatch-live-api --since -1h` |
| Process crashed | nothing: systemd restarts it within 10 s to 5 min, and the gap appears as `collector_down` |
| VM rebooted | nothing: the units and Caddy start at boot |
| VM stopped or reclaimed by Oracle | Console → Instances → Start. Data is on the boot volume, and the gap is recorded on start-up |
| mempool.space down or rate-limited | automatic backoff; `disconnected` gaps and `rate_limited` counters show it. Lower `fetch_per_minute` if 429s persist |
| Restore a backup | `sudo systemctl stop chainwatch-collector chainwatch-live-api`; `sudo -u chainwatch cp /var/backups/chainwatch/live-<stamp>.sqlite /var/lib/chainwatch/live.sqlite`; `sudo rm -f /var/lib/chainwatch/live.sqlite-wal /var/lib/chainwatch/live.sqlite-shm`; start both units |
| Rotate the token | edit `/etc/chainwatch/live.env` (`openssl rand -hex 32`), `systemctl restart chainwatch-live-api`, update the Render secret |
| Upgrade | `sudo REF=<tag-or-branch> CHAINWATCH_HOST=$HOST bash /opt/chainwatch/app/deploy/oracle/install.sh` |
| Uninstall | `systemctl disable --now chainwatch-*`; remove `/opt/chainwatch`, `/var/lib/chainwatch`, `/etc/chainwatch`; then terminate the VM in the Console |

## 10. 24-hour reliability test plan (before any 24/7 claim)

1. Deploy (§7 A to D) and start the clock when `smoke_live.sh` passes.
2. During the 24 hours, without manual fixes:
   * hour 1: `sudo systemctl kill -s SIGKILL chainwatch-collector`; expect a restart within 15 s
     and one `collector_down` gap;
   * hour 2: `sudo reboot`; expect both services back and a gap equal to the reboot time;
   * all day: leave it alone.
3. Every 6 hours, from your machine, run `smoke_live.sh` and save
   `chainwatch-live report --hours 6`.
4. At 24 hours, run `chainwatch-live report --hours 24` and record the results.
5. **Pass criteria:**
   * observed share ≥ 99 % excluding the two deliberate outages;
   * every gap explained;
   * last observation < 10 min old at every check;
   * no OOM kills or restarts apart from the deliberate ones (`journalctl -u chainwatch-collector | grep -c Started`);
   * database < 100 MB, journal < 200 MB;
   * no unexpected Oracle charges.
6. Publish the report (numbers and gaps) in this file. Only then describe the system as
   having run continuously for 24 hours, and say "sampled", never "complete".

## 11. Tests actually performed (this environment)

| Test | Result |
|---|---|
| Python unit and integration tests | see the PR. Cloud-specific tests (`tests/test_live_cloud.py`, 24): crash and clean restart recovery, dangling outages, disconnect outage and sequence gap against a local WebSocket server, availability merging, v0.5.0 schema migration, row and age retention, WAL checkpoint, backup integrity and rotation, reliability report, edge auth (missing, wrong and malformed token), health checks, docs off, GET-only, limits, rate limit, weak token refused, dashboard to edge in remote mode (auth header, shared cache, transactions, detail, outages, stats, SSE), clean 502 when the edge rejects or is down, https-only remote URL, configs, no ML imports on the VM, lock without ML packages, systemd unit invariants |
| `systemd-analyze verify` (systemd 255, as on Ubuntu 24.04) | all 4 units OK; security exposure 3.0 / 3.0 / 2.3 ("OK") |
| `install.sh` in a fresh `ubuntu:24.04` container (no systemd or Caddy) | exit 0 in 41–55 s; user, directories and token permissions correct; no ML packages; re-run idempotent with the token kept |
| Same container, real processes against a local mock source | `smoke_live.sh` **PASS** (health, 401 ×2, status, transactions, limits, no DB/docs exposure); `kill -9` → restart → one 21 s `collector_down` gap "unclean exit"; backup OK; SIGTERM → `stopped` |
| 10-minute resource run (real processes, mock at 6 tx/s, client polling every 2 s) | collector 0.08 % CPU / 39 MB, edge 0.43 % CPU / 57 MB; 3,600 txids, 0 dropped, 0 sequence gaps (§8) |
| Dashboard in remote mode, real processes (`public-demo-live.yaml` → edge API with a 64-hex token → collector) | status `via remote collector (edge API)`; a 36 s restart gap appears in the gaps table and in the 24-hour observed share (94.7 %); SSE, detail panel and the mock-source label work; Replay tab still shows the synthetic banner; 0 browser console errors (Playwright) |
| Wheels for ARM64 and x86-64 | all 26 locked wheels downloaded with hash verification for both |
| Not tested here | real mempool.space traffic, Oracle itself, Caddy and Let's Encrypt issuance, systemd restart and boot behaviour on a real VM, a 24-hour run |
