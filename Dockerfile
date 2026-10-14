# syntax=docker/dockerfile:1
# ChainWatch: API + dashboard in one image. No Elliptic++ files are ever included (licence).
#
# Targets:
#   demo    (default) synthetic demo, data and models built at image-build time, read-only
#   runtime real-data serving; mount data/processed and artifacts/registry read-only
#
# Base images are pinned by digest; Python packages are installed from a hash-pinned lock.

# ---- 1. dashboard build ----------------------------------------------------------
FROM node:22.12.0-bookworm-slim@sha256:35531c52ce27b6575d69755c73e65d4468dba93a25644eed56dc12879cae9213 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
# Optional corporate-proxy CA: `docker build --secret id=ca_bundle,src=ca.crt ...` (unused otherwise)
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. runtime ------------------------------------------------------------------------
FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHAINWATCH_CONFIG=/app/configs/default.yaml
# No apt packages needed: xgboost-cpu vendors its OpenMP runtime.
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY requirements/serve.lock.txt requirements/serve.lock.txt
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --require-hashes -r requirements/serve.lock.txt
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY configs/ configs/
COPY reports/ reports/
COPY docker/entrypoint.sh /usr/local/bin/chainwatch-entrypoint
# editable install keeps the repo layout (configs/, reports/) next to the package; no
# dependency resolution and no build-isolation downloads (setuptools comes from the lock)
RUN pip install --no-deps --no-build-isolation -e . \
    && chmod 0755 /usr/local/bin/chainwatch-entrypoint \
    && mkdir -p data artifacts && chown app:app data artifacts
COPY --from=frontend /build/dist frontend/dist
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,urllib.request; u='http://127.0.0.1:%s/health/ready' % os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen(u, timeout=4).status == 200 else 1)"
ENTRYPOINT ["chainwatch-entrypoint"]

# ---- 3. synthetic demo (default target) --------------------------------------------------
FROM runtime AS demo
USER root
# Build the synthetic data, models and registry once, as root, then make them read-only:
# the serving user can read but never rewrite the pickles it loads.
RUN chainwatch --config configs/demo.yaml demo-setup \
    && chown -R root:root data artifacts \
    && chmod -R a-w data/demo artifacts/demo
ENV CHAINWATCH_DEMO=1 \
    CHAINWATCH_PREBUILT_ONLY=1 \
    CHAINWATCH_CONFIG=/app/configs/demo.yaml
USER app
