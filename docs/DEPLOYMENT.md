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
| **Render, free web service** (Docker) | $0. Hours are capped (750 instance hours per workspace per month); whether signup needs a card was not confirmed | 512 MB RAM, 0.1 CPU; sleeps after 15 min idle; about 1 min platform wake-up | **Fits.** Memory measured at 255 MB of 512 MB. Slow: ready 73–109 s after a cold start (two measured runs), first chart load 8–11 s | Render account |
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

## 3. Option A (recommended, $0): Render free web service

The repository contains a Blueprint, [`render.yaml`](../render.yaml): one web service,
Docker runtime, **`plan: free`** (stated explicitly, never defaulted), branch `main`,
health check `/health/ready`, and `CHAINWATCH_CONFIG=/app/configs/public-demo.yaml`.
Render sets `PORT`, and the container entrypoint honours it. No database, disk, worker
or other resource is declared, so applying the Blueprint can create only this one free
service.

Validation status: the file was checked structurally against the field names in Render's
Blueprint reference, and every referenced path exists. It was **not** checked against
Render's live JSON schema (`render.com/schema/render.yaml.json`), which is unreachable
from the environment that prepared this release. Render validates the file when the
Blueprint is created (step 3), and the Render CLI can validate it beforehand. The
deprecated `autoDeploy` key is deliberately absent, so Render's default applies: each
push to `main` redeploys the free service. To deploy only after CI passes, choose
**Settings → Auto-Deploy → After CI Checks Pass** in the dashboard.

1. **Prerequisite:** the release is on `main` of the public repository you will connect
   (see [RELEASE_READINESS.md](RELEASE_READINESS.md)).
2. Optional: `render blueprints validate render.yaml` (Render CLI 2.7 or later).
3. In the Render dashboard: **New → Blueprint**, connect the public `ChainWatch`
   repository, and check that the plan column shows **Free** for `chainwatch-demo` before
   you apply. Whether Render requires a payment method for free services could not be
   confirmed; if it asks for one, stop and decide, because that is a cost decision.
4. The first build takes several minutes, because it trains the synthetic models in the
   Docker build. If the build fails at a `RUN --mount=type=secret` line, the builder lacks
   BuildKit secret support. The secret is optional (it only supplies a corporate CA
   bundle); report the log line rather than editing the Dockerfile blindly.
5. When the service shows "Live", run the end-to-end check from your own machine:
   `WAIT_SECONDS=300 scripts/smoke_demo.sh https://chainwatch-demo.onrender.com`
   (use the URL Render shows; it can carry a suffix).
6. Only after step 5 prints `PASS`: add the URL to the README, the repository's website
   field and `docs/PORTFOLIO.md`, and note that the first visit after idle can take about
   two minutes.

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
| Client identity | `trusted_proxy_hops: 1` | The rate limiter uses the `X-Forwarded-For` entry appended by the platform proxy (counted from the right), so a client cannot change its own key by sending the header. If the platform has more proxy hops, all clients share one bucket: that is safe, but tighter than intended. Set `0` when exposing uvicorn directly. |
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
