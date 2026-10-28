#!/usr/bin/env bash
# Roll back preprod code to a revision saved by update-preprod.sh.
# The database is intentionally not changed by this script.
set -Eeuo pipefail
umask 0077

[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
backup_dir=${1:-}
apply=${2:-}
revision_file="$backup_dir/code-revision.txt"
[[ -f "$revision_file" ]] || {
  echo 'Usage: rollback-preprod.sh /var/lib/ats-beta/backups/pre-update-TIMESTAMP --apply' >&2
  exit 2
}
[[ "$apply" == --apply ]] || {
  echo "DRY RUN: would restore code revision $(cat "$revision_file")"
  echo 'The database is not changed. Pass --apply to continue.'
  exit 0
}

project_dir=/opt/ats-beta
revision=$(tr -d '[:space:]' <"$revision_file")
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid revision file.' >&2; exit 1; }
runuser -u ats-beta -- git -C "$project_dir" cat-file -e "$revision^{commit}" \
  || { echo "Revision is not present in $project_dir/.git: $revision" >&2; exit 1; }

units=(ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony \
  ats-beta-worker@inbox ats-beta-worker@crm)
systemctl stop "${units[@]}"
trap 'systemctl start "${units[@]}" || true' EXIT
runuser -u ats-beta -- git -C "$project_dir" checkout --force "$revision"
bash "$project_dir/deploy/ubuntu/update-beta.sh"
trap - EXIT
printf 'Preprod code rolled back to %s; database was not restored.\n' "$revision"
