#!/bin/sh
# Start the ChainWatch service with $CHAINWATCH_CONFIG.
#   demo target:    the synthetic registry is already built into the image (read-only), and
#                   CHAINWATCH_PREBUILT_ONLY=1 forbids any training at start-up.
#   runtime target: mount data/processed and artifacts/registry (read-only) first.
# If CHAINWATCH_DEMO=1 and no demo registry exists (runtime target only), build it once.
set -eu
CFG="${CHAINWATCH_CONFIG:-/app/configs/default.yaml}"
if [ "${CHAINWATCH_DEMO:-0}" = "1" ] && [ ! -f /app/artifacts/demo/registry/registry.json ]; then
  if [ "${CHAINWATCH_PREBUILT_ONLY:-0}" = "1" ]; then
    # Training on a small host takes hours; fail fast so the platform reports the broken image.
    echo '{"level": "ERROR", "msg": "prebuilt synthetic registry missing; refusing to train at start-up"}' >&2
    exit 1
  fi
  echo '{"msg": "building synthetic demo (no prebuilt registry found)"}'
  chainwatch --config "$CFG" demo-setup
fi
exec chainwatch --config "$CFG" serve --host 0.0.0.0 --port "${PORT:-8000}"
