#!/usr/bin/env bash
# Pull and update only the isolated pre-production stack.
# The legacy ATS service, nginx and Asterisk are never restarted here.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }

cd /opt/ats-beta

# Older beta installations predate the explicit preprod identity flags. Add
# only missing safe defaults, after preserving the original file; reject any
# conflicting value instead of silently taking ownership of a live profile.
env_file=/etc/ats-beta/ats.env
[[ -f "$env_file" ]] || { echo "Missing $env_file; run install-preprod.sh first." >&2; exit 1; }
ensure_key() {
  local key=$1 expected=$2 current
  current=$(grep -E "^${key}=" "$env_file" || true)
  if [[ -n "$current" ]]; then
    [[ "$current" == "${key}=${expected}" ]] || {
      echo "Refusing to change conflicting $key in $env_file" >&2
      exit 1
    }
  else
    printf '\n%s=%s\n' "$key" "$expected" >>"$env_file"
  fi
}
missing_identity=0
for key in ATS_DEPLOYMENT ATS_INSTANCE_NAME ATS_LIVE_CALLS_ENABLED ATS_CRM_ENABLED ATS_INBOUND_ENABLED ATS_MEDIA_ENABLED; do
  grep -q "^${key}=" "$env_file" || missing_identity=1
done
if [[ "$missing_identity" == 1 ]]; then
  migration_backup="/var/lib/ats-beta/preprod-env-before-$(date -u +%Y%m%dT%H%M%SZ).env"
  install -d -o ats-beta -g ats-beta -m 0700 /var/lib/ats-beta
  install -o ats-beta -g ats-beta -m 0600 "$env_file" "$migration_backup"
  ensure_key ATS_DEPLOYMENT preprod
  ensure_key ATS_INSTANCE_NAME ats-preprod
  ensure_key ATS_LIVE_CALLS_ENABLED false
  ensure_key ATS_CRM_ENABLED false
  ensure_key ATS_INBOUND_ENABLED false
  ensure_key ATS_MEDIA_ENABLED false
  chown root:ats-beta "$env_file"
  chmod 0640 "$env_file"
  echo "Added preprod identity flags; previous env saved at $migration_backup"
fi

bash deploy/ubuntu/preprod-audit.sh

if [[ -n "$(runuser -u ats-beta -- git -C /opt/ats-beta status --porcelain)" ]]; then
  echo 'Preprod checkout has local changes; preserve them before pulling.' >&2
  exit 1
fi
runuser -u ats-beta -- git -C /opt/ats-beta pull --ff-only origin main
bash deploy/ubuntu/update-beta.sh
bash deploy/ubuntu/preprod-audit.sh
