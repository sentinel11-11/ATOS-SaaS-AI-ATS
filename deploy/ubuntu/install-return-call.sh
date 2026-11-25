#!/usr/bin/env bash
# Install the local return-call bridge after upgrading the database.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/ats-beta
getent group asterisk >/dev/null
id ats-beta >/dev/null
agi_dir="$(asterisk -rx 'core show settings' | awk -F: '/AGI Scripts directory/ {sub(/^[[:space:]]+/, "", $2); sub(/[[:space:]]+$/, "", $2); print $2; exit}')"
[[ "$agi_dir" == /* && -d "$agi_dir" ]] || { echo 'Cannot determine the Asterisk AGI directory.' >&2; exit 1; }

sources=(
  deploy/asterisk/agi/ats_return_call.py
  deploy/asterisk/extensions-ats-beta-return-call.conf
  deploy/ubuntu/ats-beta-return-call.socket
  deploy/ubuntu/ats-beta-return-call.service
)
targets=(
  "$agi_dir/ats_return_call.py"
  /etc/asterisk/extensions-ats-beta-return-call.conf
  /etc/systemd/system/ats-beta-return-call.socket
  /etc/systemd/system/ats-beta-return-call.service
)
for i in "${!targets[@]}"; do
  if [[ -L "${targets[i]}" ]] || { [[ -e "${targets[i]}" ]] && ! cmp -s "${sources[i]}" "${targets[i]}"; }; then
    printf 'Review the existing file %s before installing.\n' "${targets[i]}" >&2
    exit 1
  fi
done
install -o root -g asterisk -m 0750 "${sources[0]}" "${targets[0]}"
install -o root -g asterisk -m 0640 "${sources[1]}" "${targets[1]}"
for i in 2 3; do
  install -o root -g root -m 0644 "${sources[i]}" "${targets[i]}"
done
systemctl daemon-reload
systemctl enable --now ats-beta-return-call.socket
systemctl start ats-beta-return-call.service
printf 'Return-call lookup is installed. Add the include and Gosub on the intended Multicom inbound DID.\n'
