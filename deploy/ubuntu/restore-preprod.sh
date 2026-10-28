#!/usr/bin/env bash
# Restore a preprod PostgreSQL snapshot. Destructive mode requires --apply.
set -Eeuo pipefail
umask 0077

[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
backup_dir=${1:-}
apply=${2:-}
[[ -n "$backup_dir" && -f "$backup_dir/ats_beta.dump" ]] \
  || { echo 'Usage: restore-preprod.sh /var/lib/ats-beta/backups/TIMESTAMP --apply' >&2; exit 2; }
[[ "$apply" == --apply ]] || {
  echo "DRY RUN: would restore $backup_dir/ats_beta.dump into ats_beta"
  echo 'Pass --apply to stop beta services and replace the database.'
  exit 0
}

if [[ -f "$backup_dir/SHA256SUMS" ]]; then
  (cd "$backup_dir" && sha256sum --check SHA256SUMS)
fi

units=(ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony \
  ats-beta-worker@inbox ats-beta-worker@crm)
systemctl stop "${units[@]}"
trap 'systemctl start "${units[@]}" || true' EXIT
before_restore="/var/lib/ats-beta/backups/before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -o ats-beta -g ats-beta -m 0700 "$before_restore"
runuser -u ats-beta -- pg_dump \
  --host=/var/run/postgresql --dbname=ats_beta --format=custom \
  --file="$before_restore/ats_beta.dump"

runuser -u ats-beta -- pg_restore --clean --if-exists --exit-on-error \
  --no-owner --host=/var/run/postgresql --dbname=ats_beta "$backup_dir/ats_beta.dump"
systemctl start "${units[@]}"
trap - EXIT
curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 \
  http://127.0.0.1:9130/health/ready >/dev/null
printf 'Preprod database restored from %s\n' "$backup_dir"
printf 'Safety backup of previous database: %s/ats_beta.dump\n' "$before_restore"
