#!/usr/bin/env bash
# Create a restorable snapshot of the isolated pre-production installation.
set -Eeuo pipefail
umask 0077

[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
project_dir=/opt/ats-beta
state_dir=/var/lib/ats-beta
env_file=/etc/ats-beta/ats.env
[[ -d "$project_dir/.git" && -x "$project_dir/.venv/bin/python" ]] \
  || { echo "Missing preprod checkout at $project_dir" >&2; exit 1; }
[[ -f "$env_file" ]] || { echo "Missing $env_file" >&2; exit 1; }

stamp=${1:-$(date -u +%Y%m%dT%H%M%SZ)}
backup_dir="$state_dir/backups/$stamp"
[[ ! -e "$backup_dir" ]] || { echo "Backup already exists: $backup_dir" >&2; exit 1; }
install -d -o ats-beta -g ats-beta -m 0700 "$backup_dir"

runuser -u ats-beta -- pg_dump \
  --host=/var/run/postgresql --dbname=ats_beta --format=custom \
  --file="$backup_dir/ats_beta.dump"
install -o ats-beta -g ats-beta -m 0600 "$env_file" "$backup_dir/ats.env"
runuser -u ats-beta -- git -C "$project_dir" rev-parse HEAD >"$backup_dir/code-revision.txt"
runuser -u ats-beta -- git -C "$project_dir" archive --format=tar \
  "$(cat "$backup_dir/code-revision.txt")" | gzip >"$backup_dir/code.tar.gz"

for unit in ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony \
  ats-beta-worker@inbox ats-beta-worker@crm; do
  systemctl cat "$unit" >"$backup_dir/$unit.unit.txt" 2>&1 || true
done

if [[ "${ATS_BACKUP_RECORDINGS:-false}" == true && -d "$state_dir/recordings" ]]; then
  tar -C "$state_dir" -czf "$backup_dir/recordings.tar.gz" recordings
fi

sha256sum "$backup_dir"/* >"$backup_dir/SHA256SUMS"
chown -R ats-beta:ats-beta "$backup_dir"
printf 'Preprod backup created: %s\n' "$backup_dir"
printf 'Code revision: %s\n' "$(cat "$backup_dir/code-revision.txt")"
