#!/usr/bin/env bash
# Management commands always execute as the beta OS user against the beta database.
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then
  printf 'Run this management wrapper as root; it drops privileges to ats-beta.\n' >&2
  exit 1
fi
cd /opt/ats-beta
exec runuser -u ats-beta -- env -i HOME=/var/lib/ats-beta LANG=C.UTF-8 PATH=/usr/bin:/bin \
  ATS_DATABASE_URL='postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql' \
  /opt/ats-beta/.venv/bin/python "$@"
