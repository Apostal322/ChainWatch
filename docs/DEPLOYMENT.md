# Deploying the public synthetic demo

**Status: no public deployment exists, and no live URL is claimed anywhere in this
project.** The hosting providers could not be reached from the environment used to
prepare this release (its network policy blocks them), so nothing below has been run on a
provider. Every measurement was made locally with Docker under resource limits that
match the free tiers. A demo URL should be published only after
`scripts/smoke_demo.sh <url>` passes against it (§5).

The public demo uses **synthetic data only**. No Elliptic++ file is downloaded, bundled or
served: the image's default build target (`demo`) generates synthetic data, trains the
models and builds a read-only model registry at image-build time.

## 1. Hosting options (checked October 2026)

| Option | Cost | Resources | Fit for this app | Approval needed |
|---|---|---|---|---|
| **Render, free web service** (prebuilt image from GHCR) | $0. Hours are capped (750 instance hours per workspace per month); whether signup needs a card was not confirmed | 512 MB RAM, 0.1 CPU; sleeps after 15 min idle; about 1 min platform wake-up | **Fits.** Memory measured at 255 MB of 512 MB. Slow: ready 73–109 s after a cold start (two measured runs), first chart load 8–11 s | Render account |
| **Hugging Face Spaces, Docker SDK, CPU Basic** | **Not free for new Spaces.** Since about July 2026, creating Docker or Gradio Spaces on CPU Basic needs a paid plan (PRO, $9/month); only static Spaces stay free | 2 vCPU, 16 GB RAM, 50 GB ephemeral disk; sleeps after 48 h idle | **Fits technically**, with a good experience: ready in 4–6 s (measured at 2 CPUs) | HF account **and $9/month** |
| Static hosting (GitHub Pages, HF static Space) | $0 | n/a | **Does not fit** without new work: the dashboard needs the live API | n/a |
| Cloud Run, Fly.io, Koyeb and similar | free tiers vary; most need a billing account or card | n/a | Not evaluated in detail; a billing account brings a risk of charges | yes |

Sources (checked 2026-10-13; re-check before deploying, because terms change):
- **Hugging Face:**
  - [Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview) (2 CPU, 16 GB, 50 GB);
  - [managing Spaces](https://huggingface.co/docs/huggingface_hub/en/guides/manage-spaces) (48 h sleep on CPU Basic);
  - [huggingface_hub PR #5037](https://github.com/huggingface/huggingface_hub/pull/5037) and [forum thread on Docker marked "Paid"](https://discuss.huggingface.co/t/docker-sdk-now-marked-as-paid-when-creating-a-new-space/177580) (paid plan required for new Docker Spaces).
- **Render:**
  - [free tier](https://render.com/docs/free) (15 min spin-down, 750 hours);
  - [compute plans](https://render.com/docs/compute-plans) (free: 0.1 CPU, 512 MB);
  - [Blueprint spec](https://render.com/docs/blueprint-spec).

**Recommendation.** Use **Render free** for a $0 demo, and say in the README that the
first visit after idle can take about two minutes. Use **Hugging Face Spaces (PRO)** only if
you approve the $9/month: it gives a much better first impression (4–6 s starts and
16 GB). Nothing in the repository depends on the choice.

## 2. Measured resource profile (local Docker)

| Check | Unconstrained (`--cpus 2`, 1 GB) | Render-free limits (`--cpus 0.1`, `--memory 512m`) |
|---|---|---|
| Image size | 1.2 GB (`demo` target) | same |
| Image build | about 5 min (includes training the synthetic models) | done by the platform builder |
| Time to `/health/ready` | 4–6 s | **73–109 s** over two runs (Python imports dominate at 0.1 CPU) |
| Memory after start-up and first requests | about 230 MB | **225–255 MB** of 512 MB |
| First ranking request | < 0.2 s | 1.8 s |
| First performance-chart request (per policy) | < 1 s | 8–11 s (cached afterwards) |
| Run as uid 1000 (Hugging Face convention) | works, ready in 3.6 s | n/a |
| Root filesystem | read-only works (`--read-only`, `/tmp` tmpfs) | same |

Reproduce:

```bash
docker build -t chainwatch:demo .            # default target = demo (what platforms build)
docker run --rm --read-only --tmpfs /tmp --memory 512m --cpus 0.1 -p 10000:10000 \
  -e PORT=10000 -e CHAINWATCH_CONFIG=/app/configs/public-demo.yaml chainwatch:demo
WAIT_SECONDS=300 scripts/smoke_demo.sh http://localhost:10000
```

## 3. Option A (recommended, $0): Render free web service, prebuilt image

**Why a prebuilt image.** The first Render deployment (commit `7caf60f`, Dockerfile build)
failed after 2 h 00 m with exit status 143. Render's build trains the 31 synthetic models
inside `docker build`, and on Render's build hardware each snapshot took 600–920 s (on
GitHub's runners the whole training takes about 4 minutes). Render now **pulls a prebuilt
image**, so nothing is trained on Render, neither at build time nor at start-up.

**Pipeline.**
1. [`publish-demo-image.yml`](../.github/workflows/publish-demo-image.yml) runs only after
   the **CI** workflow has **succeeded for a push to `main`** (`workflow_run`), and builds
   exactly the commit CI tested; a failed, cancelled or still-running CI publishes nothing.
   Its first step re-checks through the API that CI succeeded for that commit (also for a
   manual dispatch on `main`). CI covers the Python tests on Linux and Windows, the
   dashboard, the dependency audit (pip-audit and npm audit), shellcheck and the Docker
   smoke test. It builds the same `demo` target as CI for `linux/amd64`
   (`tests/test_release_gate.py` keeps these conditions from regressing).
2. [`scripts/check_demo_image.sh`](../scripts/check_demo_image.sh) gates the image:
   - the image holds synthetic data and synthetic-trained models only (marker file, raw-file
     hashes that do not match the published Elliptic++ files, `data_mode: synthetic`);
   - `scripts/smoke_demo.sh` passes at 512 MB / 0.1 CPU with a read-only root filesystem;
   - the logs show no `demo-setup` or training;
   - memory stays below 450 MiB.
3. Only then does it push to GHCR as `ghcr.io/apostal322/chainwatch-demo`, with an
   immutable `sha-<commit>` tag and the version tag from `pyproject.toml` (for example
   `0.4.1`). It never overwrites an existing version tag, and the step summary records the
   image digest.
4. Only that job holds `packages: write`. It authenticates with the workflow's own
   `GITHUB_TOKEN`; no credential is stored in the repository.

The image sets `CHAINWATCH_PREBUILT_ONLY=1`. If its registry were ever missing, the
container exits with an error instead of training, which on 0.1 CPU would take hours.

**Blueprint** ([`render.yaml`](../render.yaml)): one web service, `runtime: image`,
`image.url: ghcr.io/apostal322/chainwatch-demo:0.4.1`, **`plan: free`**, health check
`/health/ready`, `CHAINWATCH_CONFIG=/app/configs/public-demo.yaml`. There are no
registry credentials, because the package must be **public** (step 2). Image-backed
services do not auto-deploy: after changing the tag, use **Manual Deploy**.

**Steps (owner):**
1. Merge to `main` and wait for **Publish demo image** to pass. Note the digest in its
   step summary.
2. **Make the package public** (it starts private, and the change is irreversible): GitHub
   → your profile → Packages → `chainwatch-demo` → Package settings → Danger Zone →
   Change visibility → Public. Verify anonymous access from a logged-out machine:
   `docker pull ghcr.io/apostal322/chainwatch-demo:0.4.1`. It must succeed without
   `docker login`.
3. **Switch the existing failed service** `chainwatch-demo` to the image. Render documents
   changing a service's source in the dashboard (Settings → Build & Deploy → Source →
   Edit → Existing image, URL `ghcr.io/apostal322/chainwatch-demo:0.4.1`); only a
   service's *type* cannot change. How a Blueprint **sync** behaves when it finds
   `runtime: image` on a service created with `runtime: docker` is not documented in the
   sources checked. Therefore either:
   - (a) change the source in the dashboard first, then sync the Blueprint (the YAML then
     matches the service); or
   - (b) delete the failed service (it never went live; free plan, no data), then sync the
     Blueprint to create it again as an image service.
4. Confirm that the plan still shows **Free** before you save. If Render asks for a
   payment method, stop: that is a cost decision.
5. **Manual Deploy**, wait for "Live", then from your own machine:
   `WAIT_SECONDS=300 scripts/smoke_demo.sh https://<service>.onrender.com`
6. Only after it prints `PASS`, publish the URL (README, website field,
   `docs/PORTFOLIO.md`), noting that the first visit after idle can take about two minutes.

Pinning by digest (`ghcr.io/apostal322/chainwatch-demo@sha256:…`, taken from the
workflow summary) is a stricter alternative to the version tag. Use it if you want each
deploy to be byte-for-byte reproducible.

## 4. Option B (paid, $9/month): Hugging Face Space

1. **Approve the cost first.** A Docker Space on CPU Basic needs a PRO subscription.
2. Create a Space: **SDK: Docker**, hardware **CPU Basic**, visibility public.
3. Push the repository contents to the Space's git repository, replacing its `README.md`
   with [`deploy/huggingface/README.md`](../deploy/huggingface/README.md). That file holds
   the Space configuration (`sdk: docker`, `app_port: 8000`):

   ```bash
   git clone https://huggingface.co/spaces/<user>/chainwatch-demo hf-space
   git -C . archive HEAD | tar -x -C hf-space          # code only; data/ and artifacts/ are untracked
   cp deploy/huggingface/README.md hf-space/README.md
   rm -rf hf-space/docs/images                         # not needed in the image
   cd hf-space && git add -A && git commit -m "ChainWatch synthetic demo" && git push
   ```

4. In the Space **Settings → Variables**, add
   `CHAINWATCH_CONFIG=/app/configs/public-demo.yaml`.
5. Verify: `scripts/smoke_demo.sh https://<user>-chainwatch-demo.hf.space`

## 5. Verifying a deployment end to end

[`scripts/smoke_demo.sh`](../scripts/smoke_demo.sh) is the same check CI runs against
the container. It verifies:
- readiness (it waits through a cold start);
- `data_mode: synthetic`;
- the dashboard HTML with its CSP and `nosniff` headers;
- a ranking and a batch request;
- `/docs` disabled;
- rate limiting (429 within 60 requests);
- health checks exempt from the limit.

A deployment counts as "live" only when this script passes against its public URL.

## 6. Exposure controls (`configs/public-demo.yaml`)

| Control | Setting | Notes |
|---|---|---|
| Authentication | none | Read-only API over synthetic data; no user data, no write endpoint. |
| CORS | disabled | The dashboard is same-origin. |
| Rate limit | 120/min per client on `/v1/*`, burst 30 | In-process token bucket; health checks exempt. |
| Client identity | `trusted_proxy_hops: 1` (**unverified on Render**, see below) | The rate limiter and the per-client event-stream cap use the `X-Forwarded-For` entry appended by the outermost trusted proxy (counted from the right). The value must equal the real number of appending proxies. **Too high:** a client chooses its own key by sending forged entries and bypasses the 120/min limit and the 2-stream cap. **Too low:** every visitor shares one bucket, so one anonymous client sending about 2 requests/s gets every visitor a 429: a low-effort denial of service, not a "safe" setting (v0.8.1 audit). Set `0` when exposing uvicorn directly. |
| Request body | 2 MB maximum (ASGI middleware, before parsing); batch size 200 | Larger requests get 413. |
| Concurrency | `limit_concurrency: 32`, 1 worker | All state is in memory. |
| Headers | same-origin CSP, `nosniff`, `X-Frame-Options: DENY`, `no-referrer` | |
| API docs | `/docs` and `/redoc` off (they load third-party JavaScript); `/openapi.json` on | |
| Logs | JSON lines with sanitised request IDs; no bodies or client addresses logged | |

The in-process rate limiter protects a small demo. It is not a WAF.

## 7. Serving the real Elliptic++ registry (private use only)

```bash
docker compose --profile real up --build   # mounts data/processed and artifacts/registry read-only
```

Do **not** expose this publicly. The dataset has no explicit redistribution licence (see
[DATASET.md](DATASET.md)), and the API returns per-transaction features and labels.

### Verifying `trusted_proxy_hops` on Render (do this before relying on the rate limit)

Render's documentation does not state how many `X-Forwarded-For` entries its proxy appends,
and community reports suggest it keeps client-supplied values. No debug endpoint is added for
this; the rate limiter itself is the probe (burst 30, refill 2/s). Wait at least 20 s between
steps.

1. **Too high?** From network A, send 40 rapid `GET /v1/info` requests, each with a different
   forged header, e.g. `X-Forwarded-For: 10.9.$i.1, 10.8.$i.1`. A 429 after about 30 means
   the key is not attacker-controlled; 40 × 200 means `trusted_proxy_hops` is too high.
2. **Too low?** Drain the bucket from network A (35 requests, no forged header), then within
   2 s send 3 requests from network B (e.g. a phone hotspot; compare `curl ifconfig.me`).
   B gets 200: the key is per client (correct). B gets 429: the key is shared (too low).
   Repeat about 5 times: Render may run several proxy instances.
3. **Optional, to read the chain:** deploy a separate, throwaway Render web service (same
   plan and region, never the production one) running a few-line app that echoes
   `X-Forwarded-For`; call it with `X-Forwarded-For: 192.0.2.1`. The correct value is the
   position of your real IP counted from the right. Delete the service afterwards.
4. Set `trusted_proxy_hops` to that value, repeat steps 1-2 against production, and record
   the result in `docs/AUDIT.md`.
