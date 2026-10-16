#!/usr/bin/env bash
# Deployment smoke test of the collector's edge API (run from anywhere, after install.sh).
#
#   TOKEN=<edge token> deploy/oracle/smoke_live.sh https://203-0-113-7.sslip.io
#
# Checks: health, auth enforcement (401 without / with a wrong token), status with the
# token, that the source is the public mempool.space service (unless ALLOW_MOCK=1), that the
# collector is connected and has observed a transaction recently (MAX_AGE_S, default 900),
# bounded list responses, and that the database is not downloadable. Exit 0 = PASS.
set -euo pipefail
BASE="${1:?usage: TOKEN=... smoke_live.sh <base-url>}"
BASE="${BASE%/}"
TOKEN="${TOKEN:?set TOKEN to the edge API token}"
MAX_AGE_S="${MAX_AGE_S:-900}"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }

[ "$(code "$BASE/health/live")" = "200" ] || fail "/health/live"
ok "health/live"
[ "$(code "$BASE/v1/live/status")" = "401" ] || fail "status without token must be 401"
[ "$(code -H "authorization: Bearer wrong-token-0000000000000000000000000000" "$BASE/v1/live/status")" = "401" ] \
  || fail "status with a wrong token must be 401"
ok "auth enforced (401 without / with wrong token)"

status=$(curl -sf --max-time 15 -H "authorization: Bearer $TOKEN" "$BASE/v1/live/status") || fail "status with token"
ALLOW_MOCK="${ALLOW_MOCK:-0}" MAX_AGE_S="$MAX_AGE_S" python3 -c '
import json, os, sys, time
s = json.loads(sys.argv[1])
assert s["mode"] == "live", s.get("mode")
if os.environ["ALLOW_MOCK"] != "1":
    assert s["source_is_public_mempool"], f"source is {s['"'"'source_url'"'"']}, not mempool.space"
assert s["connection"] == "connected", f"collector connection: {s['"'"'connection'"'"']}"
last = s.get("last_observation_at")
age = time.time() - last if last else None
assert age is not None and age < float(os.environ["MAX_AGE_S"]), f"last observation age {age}"
a = s.get("availability_24h") or {}
print(f"ok   status: connected, source {s['"'"'source_url'"'"']}, last observation {age:.0f}s ago, "
      f"observed share (24 h) {a.get('"'"'observed_share'"'"')}")
' "$status"

curl -sf --max-time 15 -H "authorization: Bearer $TOKEN" "$BASE/v1/live/transactions?limit=5" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["rows"] and len(d["rows"]) <= 5, "expected 1..5 rows"
print("ok   transactions:", d["total"], "stored; newest", d["rows"][0]["txid"][:16], "...")'
[ "$(code -H "authorization: Bearer $TOKEN" "$BASE/v1/live/transactions?limit=1000")" = "422" ] || fail "limit cap"
ok "request bounds enforced (limit > 200 rejected)"
for p in /live.sqlite /var/lib/chainwatch/live.sqlite /docs /openapi.json; do
  [ "$(code "$BASE$p")" = "404" ] || fail "$p must not be served"
done
ok "database, docs and schema are not exposed"
echo "PASS $BASE"
