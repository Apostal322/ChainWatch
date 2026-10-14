#!/usr/bin/env bash
# Install / upgrade the ALWAYS-ON ChainWatch live collector on a fresh Ubuntu 24.04 VM
# (Oracle Cloud Always Free: VM.Standard.A1.Flex ARM64 or VM.Standard.E2.1.Micro x86-64).
# See docs/LIVE_CLOUD.md. Idempotent: re-run it to upgrade to a newer ref.
#
#   sudo CHAINWATCH_HOST=203-0-113-7.sslip.io bash install.sh
#
# Environment:
#   CHAINWATCH_HOST   public hostname for HTTPS (required unless SKIP_CADDY=1)
#   REPO_URL, REF     source to install (default: the public repository, branch main)
#   DRY_RUN=1         print the commands instead of running them
#   SKIP_SYSTEMD=1    do not install/enable systemd units (container tests)
#   SKIP_CADDY=1      do not install/configure Caddy (container tests)
#
# It never creates cloud resources, never prints the token after first creation, and never
# opens any port except 80/443 (Caddy) in the host firewall.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Apostal322/ChainWatch.git}"
REF="${REF:-main}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"
SKIP_CADDY="${SKIP_CADDY:-0}"
PREFIX=/opt/chainwatch
APP="$PREFIX/app"
VENV="$PREFIX/venv"
DATA=/var/lib/chainwatch
BACKUPS=/var/backups/chainwatch
ETC=/etc/chainwatch

run() { echo "+ $*"; if [ "$DRY_RUN" != "1" ]; then "$@"; fi; }
note() { echo "== $*"; }

[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
if [ "$SKIP_CADDY" != "1" ] && [ -z "${CHAINWATCH_HOST:-}" ]; then
  echo "set CHAINWATCH_HOST (e.g. <public-ip-with-dashes>.sslip.io)" >&2; exit 1
fi

note "packages"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -q
run apt-get install -y -q --no-install-recommends python3 python3-venv git sqlite3 openssl ca-certificates
[ "$SKIP_CADDY" = "1" ] || run apt-get install -y -q caddy

note "service user and directories"
if ! id chainwatch >/dev/null 2>&1; then
  run useradd --system --home-dir "$PREFIX" --no-create-home --shell /usr/sbin/nologin chainwatch
fi
run install -d -m 0755 -o root -g root "$PREFIX"
run install -d -m 0750 -o chainwatch -g chainwatch "$DATA" "$BACKUPS"
run install -d -m 0750 -o root -g chainwatch "$ETC"

note "source ($REPO_URL @ $REF), owned by root and read-only to the service"
if [ -d "$APP/.git" ]; then
  run git -C "$APP" fetch --depth 1 origin "$REF"
  run git -C "$APP" checkout -q --force FETCH_HEAD
else
  run git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$APP"
fi

note "virtualenv from the hash-locked live dependency set (no ML stack)"
[ -x "$VENV/bin/python" ] || run python3 -m venv "$VENV"
run "$VENV/bin/pip" install -q --upgrade --require-hashes -r "$APP/requirements/live.lock.txt"
run "$VENV/bin/pip" install -q --no-deps --no-build-isolation -e "$APP"

note "secret: edge API token (generated here, never committed)"
if [ ! -s "$ETC/live.env" ]; then
  if [ "$DRY_RUN" != "1" ]; then
    umask 077
    printf 'CHAINWATCH_LIVE_TOKEN=%s\n' "$(openssl rand -hex 32)" > "$ETC/live.env"
    chown root:chainwatch "$ETC/live.env"
    chmod 0640 "$ETC/live.env"
    echo "   created $ETC/live.env. Read it ONCE with: sudo cat $ETC/live.env"
    echo "   and store the value as the Render secret CHAINWATCH_LIVE_TOKEN."
  else
    echo "+ (write $ETC/live.env with a random 64-hex token, mode 0640 root:chainwatch)"
  fi
else
  echo "   $ETC/live.env exists; keeping the current token"
fi

note "journald size cap"
run install -d -m 0755 /etc/systemd/journald.conf.d
if [ "$DRY_RUN" != "1" ]; then
  printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/chainwatch.conf
fi

if [ "$SKIP_SYSTEMD" != "1" ]; then
  note "systemd units (restart on crash, start on boot)"
  for unit in chainwatch-collector.service chainwatch-live-api.service \
              chainwatch-backup.service chainwatch-backup.timer; do
    run install -m 0644 "$APP/deploy/oracle/$unit" "/etc/systemd/system/$unit"
  done
  run systemctl daemon-reload
  run systemctl restart systemd-journald
  run systemctl enable --now chainwatch-collector.service chainwatch-live-api.service chainwatch-backup.timer
  run systemctl restart chainwatch-collector.service chainwatch-live-api.service
fi

if [ "$SKIP_CADDY" != "1" ]; then
  note "Caddy: HTTPS for $CHAINWATCH_HOST -> 127.0.0.1:8100"
  if [ "$DRY_RUN" != "1" ]; then
    sed "s/__CHAINWATCH_HOST__/$CHAINWATCH_HOST/" "$APP/deploy/oracle/Caddyfile.template" > /etc/caddy/Caddyfile
  fi
  run systemctl enable --now caddy
  run systemctl reload caddy

  note "host firewall: Oracle's Ubuntu images REJECT everything but SSH in iptables"
  if [ -f /etc/iptables/rules.v4 ] && command -v iptables >/dev/null; then
    for port in 80 443; do
      if ! iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
        run iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
      fi
    done
    command -v netfilter-persistent >/dev/null && run netfilter-persistent save
  fi
fi

note "done. Verify:"
echo "   systemctl status chainwatch-collector chainwatch-live-api --no-pager"
echo "   sudo -u chainwatch $VENV/bin/chainwatch-live --config $APP/configs/live-cloud.yaml report --hours 1"
[ "$SKIP_CADDY" = "1" ] || echo "   TOKEN=... $APP/deploy/oracle/smoke_live.sh https://$CHAINWATCH_HOST"
