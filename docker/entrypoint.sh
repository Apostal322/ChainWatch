#!/bin/sh
# Start the ChainWatch service with $CHAINWATCH_CONFIG.
#   demo target:    the synthetic registry is already built into the image (read-only).
#   runtime target: mount data/processed and artifacts/registry (read-only) first.
# If CHAINWATCH_DEMO=1 and no demo registry exists (e.g. runtime target), build it once.
set -eu
CFG="${CHAINWATCH_CONFIG:-/app/configs/default.yaml}"
if [ "${CHAINWATCH_DEMO:-0}" = "1" ] && [ ! -f /app/artifacts/demo/registry/registry.json ]; then
  echo '{"msg": "building synthetic demo (no prebuilt registry found)"}'
  chainwatch --config "$CFG" demo-setup
fi
exec chainwatch --config "$CFG" serve --host 0.0.0.0 --port "${PORT:-8000}"
