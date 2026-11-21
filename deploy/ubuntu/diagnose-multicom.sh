#!/usr/bin/env bash
# Collect the selected call's non-secret configuration and read-only Asterisk output.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/ats-beta
[[ $# == 1 ]] || { echo 'Usage: bash deploy/ubuntu/diagnose-multicom.sh CALL_UUID' >&2; exit 2; }
report_file=$(mktemp /tmp/ats-multicom-report.XXXXXX)
trap 'rm -f "$report_file"' EXIT
bash deploy/ubuntu/manage-beta.sh -m ats.cli diagnose-multicom "$1" \
  --env-file /etc/ats-beta/ats.env >"$report_file"
cat "$report_file"
commands=$(PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -c \
  'import json, sys; print("\n".join(json.load(open(sys.argv[1]))["asterisk_commands"]))' "$report_file")
result=0
while IFS= read -r command; do
  if ! asterisk -rx "$command"; then result=1; fi
done <<<"$commands"
exit "$result"
