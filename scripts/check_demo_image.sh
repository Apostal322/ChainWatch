#!/usr/bin/env bash
# Release gate for the public synthetic-demo image (used by .github/workflows/publish-demo-image.yml).
#
#   scripts/check_demo_image.sh <image> [port]
#
# 1. Contents: only synthetic data and models are inside the image.
# 2. Runtime, under Render Free limits (512 MB, CPUS=0.1 by default, read-only root fs):
#    - starts without running demo-setup or any model training,
#    - passes scripts/smoke_demo.sh (health, API, dashboard, replay, exposure controls),
#    - stays below MAX_MEM_MIB (default 450) after the smoke test.
# Exits non-zero on any failure. Prints the measured start-up time and memory.
set -euo pipefail

IMAGE="${1:?usage: check_demo_image.sh <image> [port]}"
PORT="${2:-8000}"
CPUS="${CPUS:-0.1}"
MAX_MEM_MIB="${MAX_MEM_MIB:-450}"
NAME="chainwatch-gate-$$"
HERE="$(cd "$(dirname "$0")" && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }

# ---- 1. contents --------------------------------------------------------------------------
docker run --rm --entrypoint python "$IMAGE" -c '
import json, os, pathlib, sys
app = pathlib.Path("/app")
def only(d, allowed):
    extra = sorted(p.name for p in (app / d).iterdir() if p.name not in allowed)
    assert not extra, f"unexpected entries in /app/{d}: {extra}"
only("data", {"demo"})
only("artifacts", {"demo"})
assert (app / "data/demo/raw/SYNTHETIC_DEMO_DATA").is_file(), "synthetic marker missing"
manifest = json.loads((app / "data/demo/processed/manifest.json").read_text())
real = [f for f, v in manifest["raw_files"].items() if v.get("matches_published") is not False]
assert not real, f"raw files match the published Elliptic++ hashes: {real}"
reg = json.loads((app / "artifacts/demo/registry/registry.json").read_text())
assert reg["data_mode"] == "synthetic", reg["data_mode"]
assert os.environ.get("CHAINWATCH_PREBUILT_ONLY") == "1", "PREBUILT_ONLY not set"
print("ok   contents: synthetic data and models only; registry data_mode=synthetic")
' || fail "image contents"

# ---- 2. runtime ---------------------------------------------------------------------------
docker rm -f "$NAME" > /dev/null 2>&1 || true
trap 'docker logs "$NAME" > "/tmp/$NAME.log" 2>&1 || true; docker rm -f "$NAME" > /dev/null 2>&1 || true' EXIT
start=$(date +%s)
docker run -d --name "$NAME" --read-only --tmpfs /tmp --memory 512m --cpus "$CPUS" \
  -p "$PORT:$PORT" -e PORT="$PORT" -e CHAINWATCH_CONFIG=/app/configs/public-demo.yaml \
  "$IMAGE" > /dev/null
WAIT_SECONDS="${WAIT_SECONDS:-300}" "$HERE/smoke_demo.sh" "http://localhost:$PORT"
ok "start-up to ready + smoke test: $(( $(date +%s) - start ))s at --cpus $CPUS"

logs=$(docker logs "$NAME" 2>&1)
if grep -qiE 'demo-setup|building synthetic demo|exported snap|training|refusing to train' <<< "$logs"; then
  fail "container log shows set-up or training activity"
fi
ok "no demo-setup or model training at start-up"

mem=$(docker stats --no-stream --format '{{.MemUsage}}' "$NAME" | awk '{print $1}')
mib=$(python3 -c "
import re, sys
v, u = re.match(r'([0-9.]+)([KMG]i?B)', '$mem').groups()
print(round(float(v) * {'KiB': 1/1024, 'MiB': 1, 'GiB': 1024, 'KB': 1/1000, 'MB': 1, 'GB': 1000}[u]))")
[ "$mib" -lt "$MAX_MEM_MIB" ] || fail "memory ${mib} MiB >= ${MAX_MEM_MIB} MiB"
ok "memory after smoke test: ${mib} MiB (limit 512 MiB, gate ${MAX_MEM_MIB} MiB)"
echo "PASS $IMAGE"
