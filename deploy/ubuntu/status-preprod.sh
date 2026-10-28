#!/usr/bin/env bash
# Read-only operational status for the isolated preprod stack.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }

units=(ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony \
  ats-beta-worker@inbox ats-beta-worker@crm)
printf '%-34s %s\n' UNIT STATUS
for unit in "${units[@]}"; do
  printf '%-34s %s\n' "$unit" "$(systemctl is-active "$unit" || true)"
done

printf '\nREADINESS\n'
curl --fail --silent --show-error http://127.0.0.1:9130/health/ready || true
printf '\n\nDATABASE QUEUES\n'
runuser -u ats-beta -- psql --host=/var/run/postgresql --dbname=ats_beta --tuples-only --no-align \
  --command="SELECT 'attempts_active=' || count(*) FROM call_attempts WHERE status IN ('prepared','dispatching','accepted','answered','unknown'); SELECT 'outbox_pending=' || count(*) FROM outbox WHERE status='pending'; SELECT 'outbox_leased=' || count(*) FROM outbox WHERE status='leased'; SELECT 'outbox_unknown=' || count(*) FROM outbox WHERE status='unknown';" \
  2>/dev/null || echo 'database query unavailable'

printf '\nLISTENING\n'
ss -ltnp | grep -E ':(9130)\\b' || true
