#!/usr/bin/env bash
# Read-only guard for the isolated pre-production installation.
# It intentionally does not stop legacy ATS, Asterisk, nginx or PostgreSQL.
set -Eeuo pipefail

[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }

project_dir=/opt/ats-beta
legacy_dir=/opt/ats/app
legacy_service=ats
env_file=/etc/ats-beta/ats.env
api_unit=ats-beta-api.service

fail() { printf 'PREPROD AUDIT FAILED: %s\n' "$*" >&2; exit 1; }
pass() { printf 'OK: %s\n' "$*"; }

[[ -d "$project_dir/.git" && -f "$project_dir/pyproject.toml" ]] \
  || fail "expected checkout at $project_dir"
[[ "$project_dir" != "$legacy_dir" ]] || fail 'preprod and legacy paths are identical'
[[ -d "$legacy_dir" ]] && pass "legacy checkout remains at $legacy_dir" || printf 'WARN: legacy checkout is absent: %s\n' "$legacy_dir"

getent passwd ats-beta >/dev/null || fail 'ats-beta OS user is missing'
getent group ats-beta >/dev/null || fail 'ats-beta OS group is missing'
[[ -f "$env_file" ]] || fail "missing $env_file"
grep -Fxq 'ATS_DEPLOYMENT=preprod' "$env_file" || fail 'ATS_DEPLOYMENT must be preprod'
grep -Fxq 'ATS_INSTANCE_NAME=ats-preprod' "$env_file" || fail 'ATS_INSTANCE_NAME must be ats-preprod'
grep -Fxq 'ATS_DATABASE_URL=postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql' "$env_file" \
  || fail 'preprod must use the dedicated ats_beta database'
grep -Fxq 'ATS_PUBLIC_ORIGIN=http://127.0.0.1:9130' "$env_file" \
  || fail 'preprod API must stay on loopback port 9130'
grep -Fxq 'ATS_RECORDINGS_DIR=/var/lib/ats-beta/recordings' "$env_file" \
  || fail 'recordings must stay under the writable preprod state directory'
encryption_key=$(grep -E '^ATS_CONNECTION_ENCRYPTION_KEY=' "$env_file" | cut -d= -f2- || true)
[[ "$encryption_key" =~ ^[A-Za-z0-9_-]{40,}$ ]] \
  || fail 'ATS_CONNECTION_ENCRYPTION_KEY is missing or malformed'
for key in ATS_LIVE_CALLS_ENABLED ATS_INBOUND_ENABLED ATS_MEDIA_ENABLED ATS_CRM_ENABLED; do
  grep -Fxq "$key=false" "$env_file" || fail "$key must remain false in preprod"
done
pass 'preprod environment is simulator-only and has no shared telephony ownership'

systemctl cat "$api_unit" >/tmp/ats-preprod-api-unit.$$ 2>/dev/null \
  || fail "$api_unit is not installed"
grep -Fq 'WorkingDirectory=/opt/ats-beta' /tmp/ats-preprod-api-unit.$$ \
  || fail "$api_unit does not point to $project_dir"
grep -Fq -- '--port 9130' /tmp/ats-preprod-api-unit.$$ \
  || fail "$api_unit is not bound to loopback port 9130"
rm -f /tmp/ats-preprod-api-unit.$$
pass 'systemd API unit is separate from legacy ATS'

worker_unit=ats-beta-worker@scheduler.service
systemctl cat "$worker_unit" >/tmp/ats-preprod-worker-unit.$$ 2>/dev/null \
  || fail "$worker_unit is not installed"
grep -Fq 'WorkingDirectory=/opt/ats-beta' /tmp/ats-preprod-worker-unit.$$ \
  || fail "$worker_unit does not point to $project_dir"
rm -f /tmp/ats-preprod-worker-unit.$$
pass 'systemd worker units are separate from legacy ATS'

if systemctl cat "$legacy_service" >/tmp/ats-preprod-legacy-unit.$$ 2>/dev/null; then
  if grep -Fq '/opt/ats-beta' /tmp/ats-preprod-legacy-unit.$$; then
    rm -f /tmp/ats-preprod-legacy-unit.$$
    fail "legacy unit $legacy_service points to $project_dir"
  fi
  rm -f /tmp/ats-preprod-legacy-unit.$$
  pass "legacy systemd unit $legacy_service does not point to preprod"
else
  printf 'WARN: legacy systemd unit %s was not found; verify the old service manually.\n' "$legacy_service"
fi

if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E '(^|:)9130$' >/dev/null; then
  pass 'loopback API port 9130 is occupied by the preprod API or should be checked with ss -ltnp'
else
  printf 'WARN: port 9130 is not listening; start ats-beta-api before browser testing.\n'
fi

if command -v asterisk >/dev/null 2>&1; then
  printf '\nAsterisk is intentionally shared only for inspection; preprod live calls are disabled.\n'
  asterisk -rx 'pjsip show registrations' 2>/dev/null | sed -n '1,18p' || true
  asterisk -rx 'pjsip show transports' 2>/dev/null | sed -n '1,18p' || true
fi

printf '\nPreprod isolation audit passed. Legacy ATS and Asterisk were not changed.\n'
