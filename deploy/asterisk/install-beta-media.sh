#!/usr/bin/env bash
# Prepare local files only. Does not load dialplans, reload Asterisk, or enable live calls.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/ats-beta
id asterisk >/dev/null
id ats-beta >/dev/null
export NEEDRESTART_MODE=l
apt-get install -y --no-install-recommends espeak-ng sox
install -d -o ats-beta -g asterisk -m 2750 /var/lib/ats-beta-media
install -d -o asterisk -g asterisk -m 0750 /var/lib/asterisk/sounds/ats-beta /var/spool/asterisk/monitor/ats-beta
usermod -a -G asterisk ats-beta
agi_dir="$(asterisk -rx 'core show settings' | awk -F: '/AGI Scripts directory/ {sub(/^[[:space:]]+/, "", $2); sub(/[[:space:]]+$/, "", $2); print $2; exit}')"
[[ "$agi_dir" == /* && -d "$agi_dir" ]] || { echo 'Cannot determine existing Asterisk AGI directory.' >&2; exit 1; }
install -o root -g asterisk -m 0750 deploy/asterisk/agi/ats_beta.py "$agi_dir/ats_beta.py"
install -d -o root -g root -m 0755 /etc/systemd/system/ats-beta-worker@.service.d
cat > /etc/systemd/system/ats-beta-worker@.service.d/media.conf <<'UNIT'
[Service]
ReadWritePaths=/var/lib/ats-beta-media
UNIT
systemctl daemon-reload
printf 'Media prerequisites installed. Review extensions-ats-beta.conf, enable ATS_MEDIA_ENABLED in beta env and restart only beta workers after reviewing the live test profile.\n'
