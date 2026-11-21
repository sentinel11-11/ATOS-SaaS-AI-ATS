#!/usr/bin/env bash
# Update only an existing native Ubuntu beta installation. Legacy ATS/Asterisk are untouched.
set -Eeuo pipefail
umask 0077
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/ats-beta
[[ -x .venv/bin/python && -f /etc/ats-beta/ats.env ]] || { echo 'Complete the initial beta installation first.' >&2; exit 1; }
grep -Fxq 'ATS_DATABASE_URL=postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql' /etc/ats-beta/ats.env || { echo 'Custom database configuration: use a reviewed migration procedure.' >&2; exit 1; }
[[ -z "$(runuser -u ats-beta -- git status --porcelain)" ]] || { echo 'Preserve local checkout changes before updating.' >&2; exit 1; }
[[ ! -L .venv ]] || { echo 'Review the virtual environment symlink before changing its ownership.' >&2; exit 1; }
# Root management commands on older installs could create root-owned bytecode.
# Limit ownership repair to the generated environment and preserve symlink targets.
chown -hR ats-beta:ats-beta .venv
units=(ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm)
scheduler_was_active=0
systemctl is-active --quiet ats-beta-worker@scheduler && scheduler_was_active=1
systemctl stop ats-beta-worker@scheduler
active_attempts_sql="SELECT count(*) FROM call_attempts WHERE status IN ('prepared','dispatching','accepted','answered','unknown')"
if ! active_attempts="$(runuser -u ats-beta -- env -i HOME=/var/lib/ats-beta LANG=C.UTF-8 \
  PATH=/usr/bin:/bin psql --no-psqlrc --no-password --host=/var/run/postgresql \
  --dbname=ats_beta --no-align --tuples-only --set=ON_ERROR_STOP=1 --command="$active_attempts_sql")"; then
  active_attempts=unavailable
fi
printf 'Active or unreconciled beta attempts: %s\n' "$active_attempts"
if [[ "$active_attempts" != 0 ]]; then
  if [[ "$scheduler_was_active" == 1 ]]; then systemctl start ats-beta-worker@scheduler; fi
  echo 'Update requires a confirmed zero count. Check PostgreSQL access and reconcile active beta attempts.' >&2
  exit 1
fi
systemctl stop "${units[@]}"
backup_dir="/var/lib/ats-beta/backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -o ats-beta -g ats-beta -m 0700 /var/lib/ats-beta/backups "$backup_dir"
trap 'systemctl stop "${units[@]}" || true; printf "Beta update failed; beta units are stopped. Backup directory: %s\nLegacy ATS and Asterisk were not restarted.\n" "$backup_dir" >&2' ERR
runuser -u ats-beta -- pg_dump --host=/var/run/postgresql --dbname=ats_beta --format=custom --file="$backup_dir/ats_beta.dump"
install -m 0600 /etc/ats-beta/ats.env "$backup_dir/ats.env"
runuser -u ats-beta -- git rev-parse HEAD > "$backup_dir/target-revision.txt"
node_dir=/opt/ats-beta-tools/node-v24.19.0-linux-x64
run_beta() {
  runuser -u ats-beta -- env -i HOME=/var/lib/ats-beta LANG=C.UTF-8 \
    PATH="$node_dir/bin:/usr/bin:/bin" UV_CACHE_DIR=/var/lib/ats-beta/.cache/uv \
    npm_config_cache=/var/lib/ats-beta/.cache/npm \
    ATS_DATABASE_URL='postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql' "$@"
}
run_beta /opt/ats-beta-tools/installer/bin/uv sync --frozen --no-dev --reinstall-package atos-ats --python /usr/bin/python3
run_beta "$node_dir/bin/npm" --prefix frontend ci
run_beta "$node_dir/bin/npm" --prefix frontend run build
run_beta .venv/bin/python -m alembic upgrade head
install -d -o ats-beta -g ats-beta -m 0700 /var/lib/ats-beta/recordings
if ! grep -q '^ATS_RECORDINGS_DIR=' /etc/ats-beta/ats.env; then
  printf '\nATS_RECORDINGS_DIR=/var/lib/ats-beta/recordings\n' >> /etc/ats-beta/ats.env
fi
systemctl start "${units[@]}"
curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 http://127.0.0.1:9130/health/ready
printf '\nBeta updated. Database backup: %s/ats_beta.dump\n' "$backup_dir"
