#!/usr/bin/env bash
# End-to-end smoke test of a running *public synthetic demo* (configs/public-demo.yaml).
#
#   scripts/smoke_demo.sh http://localhost:8000          # CI / local container
#   scripts/smoke_demo.sh https://<your-demo-host>       # after deploying (docs/DEPLOYMENT.md)
#
# Waits up to WAIT_SECONDS (default 300, enough for a free-tier cold start) for readiness,
# then checks the dashboard, the API, the synthetic-data guarantee and the exposure
# controls. Set SKIP_RATE_LIMIT=1 to skip the burst test. Exits non-zero on any failure.
set -euo pipefail

BASE="${1:?usage: smoke_demo.sh <base-url>}"
BASE="${BASE%/}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

deadline=$(( $(date +%s) + WAIT_SECONDS ))
until curl -sf --max-time 10 "$BASE/health/ready" > /dev/null; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "not ready after ${WAIT_SECONDS}s"
  sleep 3
done
ok "ready: $(curl -sf "$BASE/health/ready")"

curl -sf "$BASE/v1/info" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["data_mode"] == "synthetic", f"data_mode is {d['"'"'data_mode'"'"']!r}, expected synthetic"
print("ok   data_mode=synthetic, version", d["service_version"])'

page=$(curl -sf -D - "$BASE/")
grep -qi '^content-security-policy:' <<< "$page" || fail "dashboard has no CSP header"
grep -qi '^x-content-type-options: nosniff' <<< "$page" || fail "dashboard has no nosniff header"
grep -q '<div id="root">' <<< "$page" || fail "dashboard HTML not served"
ok "dashboard served with security headers"

curl -sf "$BASE/v1/replay/steps/45/ranking?limit=5" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["rows"], "empty ranking"
print("ok   ranking: step", d["step"], "top tx", d["rows"][0]["tx_id"])'

curl -sf -X POST "$BASE/v1/predict/batch" -H 'content-type: application/json' \
  -d '{"tx_ids":[1]}' > /dev/null || fail "batch endpoint"
ok "batch endpoint"

[ "$(code "$BASE/docs")" = "404" ] || fail "/docs should be disabled in the public demo"
ok "/docs disabled"

if [ "${SKIP_RATE_LIMIT:-0}" != "1" ]; then
  codes=$(for _ in $(seq 1 60); do code "$BASE/v1/models"; echo; done)
  grep -q 429 <<< "$codes" || fail "no 429 after 60 rapid requests (rate limiting inactive?)"
  ok "rate limiting active (429 within 60 requests)"
  [ "$(code "$BASE/health/ready")" = "200" ] || fail "health check rate-limited"
  ok "health checks exempt from rate limiting"
fi
echo "PASS $BASE"
