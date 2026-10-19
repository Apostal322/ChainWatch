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
  /v1/live/* proxy: 2 s shared cache, single flight, ──► browser "Live monitor" tab (SSE)
  cached failures, ≤3 upstream req/s
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
| Memory | about 40–95 MB collector (more with risk screening; grows with stored indicator rows, about 2 KB each) + 60 MB edge API + Caddy (≈ 40 MB) + OS | 1 GB (E2.1.Micro) is enough; 6 GB on A1 is plenty |
| Disk | ≈ 285 MB database at the 50,000-row cap with risk records, 7 uncompressed daily backups ≈ 2 GB, 68 MB virtualenv | 47 GB default boot volume |
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
| `deploy/oracle/*.service`, `*.timer` | systemd units: collector, edge API, daily backup, daily indicator refresh (`chainwatch-indicators`: official OFAC XML, falling back to the mirror) |
| `src/chainwatch/risk/` | risk engine run inside the collector: address screening, observed-graph relations, anomaly detector, supervised gate, alerts ([RISK_ENGINE.md](RISK_ENGINE.md)) |
| `deploy/oracle/Caddyfile.template` | HTTPS reverse proxy, everything except the edge routes answers 404 |
| `deploy/oracle/install.sh` | idempotent installer (packages, user, directories, venv, token, units, Caddy, firewall) |
| `deploy/oracle/smoke_live.sh` | post-deployment smoke test |

## 5. Reliability features

| Requirement | Implementation |
|---|---|
| WebSocket reconnection, exponential backoff | full-jitter backoff (1 s → 60 s; exponent clamped, so it never overflows however long the outage), reset only after 30 s of healthy stream; 90 s staleness watchdog; protocol pings every 20 s |
| No silent stalls | `run()` supervises its tasks: if the WebSocket loop, a worker or the heartbeat dies (e.g. the database cannot be written), the collector stops, records state `failed` and exits 1, so systemd restarts it. A bad single item is logged, counted (`internal_errors`) and stored as a failed row instead |
| Bounded queues, deduplication | 100-entry fetch queue (drops are counted); 200k-txid LRU; `UNIQUE(txid)` with `INSERT OR IGNORE` |
| Bounded retries | a transient detail-fetch failure (timeout, network error, HTTP 429, 5xx, or a first 404, which can be the explorer's indexing lag) is retried at most twice (`fetch_max_attempts: 3`), after 2.5–5 s and then 5–10 s (exponential backoff with jitter), never past `max_queue_age_s` and with at most half the queue pending. Every attempt draws from the same token bucket, so retries never exceed `fetch_per_minute`. Malformed or oversized payloads and other 4xx answers are final. Counters: `retries_scheduled`, `retries_succeeded`, `retries_exhausted`, `retries_dropped`; pending retries are recorded with their last error on shutdown |
| Conservative request limits | token bucket, 12 requests/min with a burst of 3, one at a time; pause on HTTP 429 for `Retry-After` (seconds or HTTP-date, no burst after the pause); 30 s pause after HTTP 5xx; txids that were removed or mined before their turn are not fetched (`gone_before_fetch`); queue age re-checked after waiting |
| Persistent state and restart recovery | SQLite on the boot volume. At start-up the collector records the time since the previous heartbeat as a `collector_down` gap, labelled "clean stop" or "unclean exit", and closes outages left open by a crash. Stored txids are not fetched again |
| Graceful shutdown | SIGTERM → final heartbeat `stopped`, open outage closed, HTTP client closed (`TimeoutStopSec=30`) |
| Crash restart / boot start | `Restart=always`, `StartLimitIntervalSec=0`, `RestartSteps`/`RestartMaxDelaySec` (10 s → 5 min); `WantedBy=multi-user.target` |
| Health metrics | heartbeat every 5 s, about 20 counters, `/health/ready` (200 only when connected with a fresh heartbeat), `chainwatch-live report` |
| Outage and gap tracking | `outages` table: `collector_down`, `disconnected` (WebSocket; starts at the last received message, so watchdog latency counts as down time), `sequence_gap`. A sequence gap is recorded when mempool's `sequence` (+1 per update, every update sent to every subscriber) jumps by more than 1 within one connection. These feed availability in `/v1/live/status` and the dashboard |
| Retention and bounded growth | ≤ 50,000 transactions (pruned every 60 s, so up to one minute of fetches, ≤ 12 rows here, above the cap while running; pruned again on a clean stop) and ≤ 7 days, 24 h of per-minute counters, 30 days of outages; hourly `wal_checkpoint(TRUNCATE)`; journald capped at 200 MB; 7 rotated backups |
| Resource caps | systemd `MemoryMax=300M` / `250M`, `CPUQuota=50%`, `TasksMax=64`; indicator refresh `MemoryMax=400M` (the 122 MB official XML is streamed: 16 MB peak Python memory measured) |
| Indicator freshness | the refresh runs daily (04:41 + up to 15 min, and 5 min after boot). A failed refresh keeps the previous version and exits 1 (visible in `systemctl --failed`); a source older than 7 days is reported `stale` in the API and the dashboard. The collector reloads new versions on its next heartbeat and raises retrospective alerts for stored transactions |
| Risk assessment cost | about 2 ms per fetched transaction with a full anomaly window and 531 indicators (§8 measurement environment); the observation and its assessment are committed together |

## 6. Security model

* **Network exposure:** only Caddy (TCP 80/443). SSH is protected by Oracle's key-only
  default. The edge API listens on 127.0.0.1, and SQLite is a local file.
* **Authentication:**
  * every `/v1/live/*` call needs `Authorization: Bearer <token>`;
  * the token is 64 hex characters generated **on the VM** by `install.sh` (`openssl rand -hex
    32`) and stored in `/etc/chainwatch/live.env` (**0600 root:root**: systemd reads it
    before dropping privileges, so the service user, and with it the collector that parses
    internet data, cannot read the file);
  * on Render it is a secret environment variable; it is never in Git and never sent to
    browsers.
  * Residual risk (accepted): both services run as `chainwatch`, so a compromised collector
    could read the edge process's environment through `/proc`. A separate user for the edge
    API would remove this, at the cost of shared-group permissions on the SQLite WAL files.
* **Limits:**
  * edge API: 240 requests/min per client for **unauthenticated** traffic (keyed on the
    address Caddy appends; IPv6 by /64). The token holder is not rate limited, so floods
    from a shared egress IP (Render's) cannot lock the dashboard out. 64 KB bodies,
    `limit ≤ 200`, GET only, no docs or OpenAPI schema; uvicorn `limit_concurrency=32`.
  * Caddy: header and body read timeouts of 10 s and a 60 s idle timeout (slow-client
    connections cannot hold the 32 uvicorn slots), and only `GET`/`HEAD` reach the edge
    routes.
  * dashboard: its existing 120/min public limit; the remote backend fetches each query at
    most once per 2 s (single flight for concurrent misses), caches failures for the same
    period, and caps all upstream traffic at 3 requests/s (burst 20), so neither visitor
    count nor varied query parameters multiply edge load. The fixed queries the dashboard
    polls bypass that budget, so a visitor scanning many parameter combinations cannot
    starve the dashboard; arbitrary `hours`/`window` values are snapped to a few steps, a
    failed refresh serves the last good answer for up to 60 s (flagged stale), and every
    upstream call has an overall deadline.
  * event streams: at most 10 on Render (below its 32-connection limit) and 2 per client;
    slots are reserved before any await and always released; each stream ends after 300 s,
    and a hard deadline closes it even if the client never reads.
* **Sandbox:** units run as the `chainwatch` user with `ProtectSystem=strict`,
  `NoNewPrivileges`, an empty capability set, `SystemCallFilter=@system-service` (minus
  privileged, resource, mount and debug calls), `ProtectProc=invisible`, `RemoveIPC`, and
  only `/var/lib/chainwatch` writable. The edge API may only talk to loopback
  (`IPAddressDeny=any`, `IPAddressAllow=localhost`). `systemd-analyze security` exposure:
  collector **1.3**, API **1.1**, backup 2.3 (all "OK"; previously 3.0 / 3.0 / 2.3).
* **Supply chain:** hash-locked pip install (`--require-hashes`); code is cloned from the
  public repository at a chosen ref (validated); `EXPECTED_SHA` pins the exact commit.
  Recommended: install a release tag, e.g. `REF=v0.8.0 EXPECTED_SHA=<commit>`. CI installs
  from its own hash lock (`requirements/dev.lock.txt`) and audits all three locks
  (pip-audit) and the dashboard's npm tree on every run; Dependabot proposes updates as
  pull requests; the public demo image is published only after a successful CI run for
  that exact commit (docs/DEPLOYMENT.md).
* **Errors:** 500 answers carry the same security headers as every other answer and a
  JSON error body without internal details.
* **Installer input:** `CHAINWATCH_HOST` must be a bare hostname; the Caddyfile is rendered
  to a temporary file and checked with `caddy validate` before it replaces the current one.
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
REF=main                     # a branch or tag (git clone --branch); after the v0.8.0 merge
SHA=<full commit SHA of $REF> # from GitHub; install.sh aborts if the checkout differs
curl -fsSLO "https://raw.githubusercontent.com/Apostal322/ChainWatch/$SHA/deploy/oracle/install.sh"
less install.sh                                  # read it before running it as root
sudo CHAINWATCH_HOST="$HOST" REF="$REF" EXPECTED_SHA="$SHA" bash install.sh
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
| Database at the retention cap | **283.4 MB** for 50,000 transaction rows with 47,525 risk records, 699,894 address rows and the v0.8 covering indexes (277.8 MB before them; the risk table alone is 67 MB). The v0.5 figure of 62.7 MB had no risk tables |
| Virtualenv on the VM | 68 MB |

Edge API latency at the 50,000-row cap (synthetic rows, one persistent client like the
Render proxy; p50). The audit found three slow read paths; the fix is a covering
`(outcome, fee_rate)` index plus planner hints, measured before and after:

| Request | Before | After |
|---|---|---|
| store `list` (default page, `outcome=fetched`) | 15.7 ms | 2.8 ms |
| store `list` with `min_fee_rate`, sorted by fee | 69.2 ms | 3.0 ms |
| store `stats(60)` | 91.8 ms | 14.4 ms |
| store `list` with `flag=` | 26.4 ms | 27.3 ms (unchanged: every row's flags are read) |
| HTTP `/v1/live/status`, `/transactions?limit=50`, `/stats` | — | 3.0 / 5.6 / 18.8 ms |

v0.8 measurement over HTTP at the cap (50,000 synthetic rows with risk records, 480 alerts;
one persistent client; p50 / p95 in ms):

| Request | p50 | p95 |
|---|---|---|
| status, transaction detail, outages | 2.4–4.1 | 3.7–5.7 |
| `/transactions` default, `limit=200`, `first_seen` offset 45,000 | 6.6–8.0 | 7.5–9.4 |
| `/stats?window=60` | 10.8 | 12.8 |
| `/risk/summary` (cached for 15 s; cold 0.48 s → **0.10 s** after the covering-index rewrite) | 2.0 | 2.9 |
| `/risk/findings`, `/risk/alerts` with `limit=200` | 8.8–13.5 | 10.3–16.2 |
| slow: `/transactions` with flag + fee range + deep offset | 209.8 | 248.5 |
| slow: `/stats?window=1440` (24 h histogram) | 254.1 | 301.9 |
| slow: `sort=vsize` with offset 100,000 | 144.8 | 161.0 |

The slow paths are bounded (offset and window are capped, the Render proxy snaps windows to
60 / 240 / 1,440 and caches results) and acceptable at demo traffic; they are listed as
open performance work in docs/AUDIT.md.

(A first soak run reported ~55 ms per edge request: that was the probe opening a new HTTP
client, and loading a CA bundle, per request, not the server.)

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
| Collector state `failed` / dashboard "Collector failed" | the process hit a fatal internal error (e.g. database unwritable) and exited 1; systemd restarts it. `journalctl -u chainwatch-collector \| grep "pipeline task died"` names the cause; check `df -h /var/lib/chainwatch` |
| Dashboard "Connected, no new data" | the WebSocket is up but nothing was fetched for 2 min: check the report's `failures` (429 → lower `fetch_per_minute`; `http_5xx` → mempool.space trouble) |
| Dashboard "Status unknown" | Render cannot reach the edge API: `curl https://$HOST/health/live`, then `systemctl status caddy chainwatch-live-api`, then the Render logs (`live_upstream_unavailable`) |
| Restore a backup | `sudo systemctl stop chainwatch-collector chainwatch-live-api`; `sudo -u chainwatch /opt/chainwatch/venv/bin/chainwatch-live --config /opt/chainwatch/app/configs/live-cloud.yaml restore /var/backups/chainwatch/live-<stamp>.sqlite` (integrity-checked; refuses while the collector runs; keeps the replaced files as `*.pre-restore-<stamp>`); start both units |
| Rotate the token (no downtime) | 1. add `CHAINWATCH_LIVE_TOKEN_NEXT=<new>` (generate with `openssl rand -hex 32`) to `/etc/chainwatch/live.env` (mode 0600) and `sudo systemctl restart chainwatch-live-api`: the edge now accepts both tokens; 2. update the Render secret to the new token and redeploy; 3. make the new token `CHAINWATCH_LIVE_TOKEN`, delete the `_NEXT` line and restart the edge again. Tokens must match `[A-Za-z0-9._~+/=-]{32,}`; a malformed or whitespace-padded token makes the edge exit 78 (systemd does not restart-loop) |
| Upgrade | `sudo REF=<new tag> EXPECTED_SHA=<commit> CHAINWATCH_HOST=$HOST bash /opt/chainwatch/app/deploy/oracle/install.sh` (idempotent; keeps the token and data; schema migrations run at start-up) |
| Roll back | the same command with the previous tag. Schema changes so far only add columns and indexes, so an older collector still opens a newer database; take a backup first anyway |
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
   * database < 150 MB after 24 h (at most 17,280 fetches a day at 12 per minute, about
     5.7 KB per row with its risk record; about 285 MB at the 50,000-row cap), journal
     < 200 MB;
   * no unexpected Oracle charges.
6. Publish the report (numbers and gaps) in this file. Only then describe the system as
   having run continuously for 24 hours, and say "sampled", never "complete".

## 11. Tests actually performed (this environment)

| Test | Result |
|---|---|
| Real systemd (Ubuntu 24.04 container booted with systemd 255 as PID 1; real `install.sh`; units pointed at a local mock by drop-ins) | **all 10 sections PASS**: install 20 s; token file 0600 root:root and unreadable by the service user; units enabled; no seccomp kills under `SystemCallFilter`; `smoke_live.sh` PASS; SIGKILL → restarted by systemd after 10 s, 15.4 s `collector_down` "unclean exit"; another process holding an exclusive SQLite lock for 25 s → edge reads continue (WAL) → collector `task heartbeat died: OperationalError: database is locked` → exit 1 → restarted; backup through `chainwatch-backup.service`; `restore` refused while running, works after stop; re-running `install.sh` keeps token and data; bad host / REF / `EXPECTED_SHA` rejected; `caddy validate` (2.6.2) on the rendered template; exposure 1.3 / 1.1 / 2.3; container reboot → all units back, 0.9 s "clean stop" gap |
| Same real-systemd test, v0.8 (commit `53f6f65`, 14 sections) | **35/35 PASS** (sections 1–10 as above, plus): 11. `chainwatch-indicators.service` with no route to the official file exits 1 and keeps the loaded list (versions 1 → 1), and every fetched row (98) has a risk record; 12. an invalid config (`retention_rows: 0`) exits 78 with `NRestarts=0`, no restart loop; 13. a whitespace-padded token makes the edge exit 78 and stay down; 14. token rotation: the current and `CHAINWATCH_LIVE_TOKEN_NEXT` tokens are both accepted. `caddy validate` passes with the new timeouts. Container reboot: both units active, timers enabled, ready, 1.0 s "clean stop" gap, integrity ok |
| Same real-systemd test, **final v0.8.0 source** (commit `6bd4f52` installed by `install.sh` from the local repository; 14 sections) | **32 checks PASS, 0 FAIL**: install 24 s; token 0600 root:root, unreadable by the service user; no seccomp kills; `smoke_live.sh` PASS; SIGKILL → restarted by systemd, recorded as `collector_down` "unclean exit"; 25 s exclusive lock → edge reads continue, collector exits 1 and is restarted; backup via `chainwatch-backup.service`, restore refused while running and works after stop; idempotent re-install keeps token and data; bad host / REF / `EXPECTED_SHA` rejected; `caddy validate`; exposure 1.3 / 1.1 / 2.3; failed indicator refresh exits 1 and keeps the list, every fetched row assessed; invalid config exit 78 (no restart loop); malformed token exit 78; token rotation. **Reboot** (container restart): both units active, timers enabled, `/health/ready` 200, 401 without or with a wrong token and 200 with the real one, 0.9 s "clean stop" gap, integrity ok |
| Fault-injection soak, 6 min, one fault every 25 s (`scripts/soak_test.py`) | **11/11 checks PASS**: REST 429 / 500 / malformed JSON / schema drift / slow, WebSocket drop / 40 s silence / 5,000-txid burst / sequence gap, collector SIGKILL, edge restart, 5 SSE clients dropped mid-stream. Recovery: WS drop 4.6 s, kill 5.6 s, edge restart 0.6 s, silence 23 s (watchdog 20 s). Edge and dashboard availability 100 % (169 probes each), integrity ok, 0 tracebacks, clean SIGTERM for every process |
| Full system in a browser (`scripts/e2e_live_browser.py`: mock → collector → SQLite → edge API with token → dashboard in remote mode → SSE → React) | **10/10 PASS**: mock source labelled (not REAL); connected; new transactions arrive live; the detail panel matches the API (txid, vsize, fee); collector SIGKILL shown as "Disconnected" after 13 s; recovery shown 10 s after restart; the gap appears in the gaps table; the Replay tab stays synthetic; 0 console errors |
| Fault-injection soak, **110 min** (6,601 s), one fault every 2 min, commit `06d59da` (`scripts/soak_test.py --minutes 110 --fault-every 120`) | 52 faults (each of the 12 kinds 4–5 times, incl. 4 collector SIGKILLs and 4 edge restarts); every recovery within 90 s (WS drop median 5.2 s, kill 5.6 s, edge restart 0.7 s, 40 s silence 22.3 s); edge and dashboard availability **99.97 %** (1 failed probe of 3,072 each, during edge restarts); SQLite integrity ok; 0 tracebacks; clean SIGTERM shutdown of every process; collector 0.23 % CPU, RSS 38 → 44 MB (peak 43.8 MB first half, 44.2 MB second half: the bounded dedupe LRU filling); edge 0.57 % CPU, 57 MB flat; queue max 61/100; 4 `collector_down`, 13 `disconnected`, 4 `sequence_gap` outages recorded. **10/11 checks passed**: the final row count was 3,026 against a 3,000 cap, because retention runs every 60 s and the run ended between prunes. Fixed afterwards: a clean stop now prunes, and the check now uses the documented bound (cap + one minute of fetches) (`test_clean_stop_leaves_the_database_within_the_row_cap`). An earlier 3-hour attempt was cut off after 17 minutes when the build container was recycled; that partial run had 0 tracebacks and integrity ok, but produced no report |
| Fault-injection soak with **risk intelligence**, **60 min** (3,602 s), one fault every 90 s, commit `ac6d4c8` (`scripts/soak_test.py --minutes 60`) | **17/17 checks PASS**. **Faults:** 38 (13 kinds, including 3 collector SIGKILLs, 3 edge restarts and 3 indicator refreshes). The refreshes used a local **TEST** list of about 12,000 mock addresses, not sanctions data. **Recovery and availability:** every recovery within 90 s (WS drop at most 5.1 s, kill at most 6.2 s, edge restart at most 1.2 s, 40 s silence 23 s); edge availability 99.88 % and dashboard 99.94 % (1,684 probes each); SQLite integrity ok; 0 tracebacks; clean SIGTERM. **Risk:** every stored fetched row had a risk record (2,160 assessed: 197 P1 source matches, 2 P3, 2 P4); 205 retrospective alerts after refreshes; 0 layer errors; 0 assessment errors; latency p50 2.9 ms and p95 4.6 ms with a full 2,000-transaction anomaly window. **Resources:** collector 0.52 % CPU and edge 0.64 % CPU; collector RSS 50 MB, rising in steps **only at indicator refreshes** (+24 MB and +28 MB, flat in between; a restart reloads it smaller). Memory scales with stored indicator rows (about 2 KB per row including the reload peak), so the real OFAC list (531 addresses) costs under 2 MB |
| Fault-injection soak, v0.8 final code, **90 min** (5,401 s), one fault every 90 s, commit `4872b7b` | **16/17 checks**. 57 faults (13 kinds, including 4 collector SIGKILLs and 4 edge restarts); every recovery within 24 s (WS drop at most 5.7 s, kill at most 5.7 s, edge restart at most 0.65 s, 40 s silence 23.8 s); edge and dashboard availability **99.96 %** (2,547 probes each); SQLite integrity ok; 0 tracebacks; clean SIGTERM; collector 0.47 % CPU, RSS peak 56.5 MB first half / 57.0 MB second half; edge 0.60 % CPU, 58.6 MB flat; queue max 91/100; every fetched row had a risk record, 0 layer errors. **Failed check: "every indicator update succeeded"** (1 of 5): each soak refresh replaced the whole TEST list, which the v0.8 mass-removal guard refuses by design ("would delist 11999 of 11999 active listings"). The soak scenario was wrong, not the code; it was changed to realistic updates and re-run (next row) |
| Fault-injection soak, v0.8 final code with realistic list updates, **40 min** (2,402 s), one fault every 60 s, commit `4501f4e` (same `src/` as `4872b7b`) | **17/17 checks PASS**. 37 faults (13 kinds, including 3 SIGKILLs, 3 edge restarts, 2 indicator refreshes); every recovery within 23 s; availability 99.91 % (1,132 probes each); integrity ok; 0 tracebacks; clean SIGTERM; collector 0.40 % CPU, RSS 67.3 → 69.1 MB (the refreshes add indicator rows); 3/3 indicator updates succeeded, 95 retrospective alerts, 151 live source matches, every fetched row assessed, 0 layer errors |
| 120-minute soak attempt, commit `53f6f65` | **no result**: the run was stopped by this environment's 2-hour task limit just before it wrote its report (output was buffered). Not counted |
| Python unit and integration tests | see the PR. Regression tests from the audit: `tests/test_live_faults.py` (20) and `tests/test_live_security.py` (13). Cloud-specific tests (`tests/test_live_cloud.py`, 24): crash and clean restart recovery, dangling outages, disconnect outage and sequence gap against a local WebSocket server, availability merging, v0.5.0 schema migration, row and age retention, WAL checkpoint, backup integrity and rotation, reliability report, edge auth (missing, wrong and malformed token), health checks, docs off, GET-only, limits, rate limit, weak token refused, dashboard to edge in remote mode (auth header, shared cache, transactions, detail, outages, stats, SSE), clean 502 when the edge rejects or is down, https-only remote URL, configs, no ML imports on the VM, lock without ML packages, systemd unit invariants |
| `systemd-analyze verify` (systemd 255, as on Ubuntu 24.04) | all 4 units OK; security exposure 1.3 / 1.1 / 2.3 ("OK") |
| `install.sh` in a fresh `ubuntu:24.04` container (no systemd or Caddy) | exit 0 in 41–55 s; user, directories and token permissions correct; no ML packages; re-run idempotent with the token kept |
| Same container, real processes against a local mock source | `smoke_live.sh` **PASS** (health, 401 ×2, status, transactions, limits, no DB/docs exposure); `kill -9` → restart → one 21 s `collector_down` gap "unclean exit"; backup OK; SIGTERM → `stopped` |
| 10-minute resource run (real processes, mock at 6 tx/s, client polling every 2 s) | collector 0.08 % CPU / 39 MB, edge 0.43 % CPU / 57 MB; 3,600 txids, 0 dropped, 0 sequence gaps (§8) |
| Dashboard in remote mode, real processes (`public-demo-live.yaml` → edge API with a 64-hex token → collector) | status `via remote collector (edge API)`; a 36 s restart gap appears in the gaps table and in the 24-hour observed share (94.7 %); SSE, detail panel and the mock-source label work; Replay tab still shows the synthetic banner; 0 browser console errors (Playwright) |
| Wheels for ARM64 and x86-64 | all 26 locked wheels downloaded with hash verification for both |
| Not tested here | real mempool.space traffic, Oracle itself (shape, network, reclamation), Caddy serving and Let's Encrypt issuance, a real VM reboot, a 24-hour run on the public network |
