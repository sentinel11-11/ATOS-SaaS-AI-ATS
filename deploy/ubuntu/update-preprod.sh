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
for key in ATS_DEPLOYMENT ATS_INSTANCE_NAME ATS_LIVE_CALLS_ENABLED ATS_CRM_ENABLED ATS_INBOUND_ENABLED ATS_MEDIA_ENABLED ATS_RECORDINGS_DIR ATS_CONNECTION_ENCRYPTION_KEY; do
  grep -q "^${key}=" "$env_file" || missing_identity=1
done
encryption_key_value=$(grep -E '^ATS_CONNECTION_ENCRYPTION_KEY=' "$env_file" | head -n 1 | cut -d= -f2- || true)
[[ "$encryption_key_value" =~ ^[A-Za-z0-9_-]{40,}$ ]] || missing_identity=1
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
  ensure_key ATS_RECORDINGS_DIR /var/lib/ats-beta/recordings
  if [[ -z "$encryption_key_value" ]]; then
    encryption_key="$(/opt/ats-beta/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode("ascii"))')"
    if grep -q '^ATS_CONNECTION_ENCRYPTION_KEY=' "$env_file"; then
      sed -i "s|^ATS_CONNECTION_ENCRYPTION_KEY=.*$|ATS_CONNECTION_ENCRYPTION_KEY=$encryption_key|" "$env_file"
    else
      ensure_key ATS_CONNECTION_ENCRYPTION_KEY "$encryption_key"
    fi
  elif [[ ! "$encryption_key_value" =~ ^[A-Za-z0-9_-]{40,}$ ]]; then
    echo 'Refusing to replace malformed non-empty ATS_CONNECTION_ENCRYPTION_KEY; restore a valid key manually.' >&2
    exit 1
  fi
  chown root:ats-beta "$env_file"
  chmod 0640 "$env_file"
  echo "Added preprod identity flags; previous env saved at $migration_backup"
fi

bash deploy/ubuntu/preprod-audit.sh

if [[ -n "$(runuser -u ats-beta -- git -C /opt/ats-beta status --porcelain)" ]]; then
  echo 'Preprod checkout has local changes; preserve them before pulling.' >&2
  exit 1
fi
previous_revision=$(runuser -u ats-beta -- git -C /opt/ats-beta rev-parse HEAD)
revision_backup="/var/lib/ats-beta/backups/pre-update-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -o ats-beta -g ats-beta -m 0700 "$revision_backup"
runuser -u ats-beta -- git -C /opt/ats-beta archive --format=tar "$previous_revision" \
  | gzip >"$revision_backup/code-$previous_revision.tar.gz"
printf '%s\n' "$previous_revision" >"$revision_backup/code-revision.txt"
chown ats-beta:ats-beta "$revision_backup/code-revision.txt" "$revision_backup/code-$previous_revision.tar.gz"
runuser -u ats-beta -- git -C /opt/ats-beta pull --ff-only origin main
bash deploy/ubuntu/update-beta.sh
bash deploy/ubuntu/preprod-audit.sh
printf 'Previous code snapshot: %s\n' "$revision_backup"
