#!/usr/bin/env bash
# Exercise the updater in a disposable container with real PostgreSQL and Unix permissions.
# Service management, Git, dependency installation and the HTTP probe use controlled stubs.
set -euo pipefail
adduser -D -h /var/lib/ats-beta ats-beta
export PGDATA=/tmp/recovery-postgres
mkdir -p "$PGDATA" /var/run/postgresql
chown postgres:postgres "$PGDATA" /var/run/postgresql
gosu postgres initdb -D "$PGDATA" --auth=trust >/tmp/initdb.log
gosu postgres pg_ctl -D "$PGDATA" -l /tmp/postgres.log -o '-c listen_addresses=' -w start
psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'CREATE ROLE "ats-beta" LOGIN'
createdb -U postgres -O ats-beta ats_beta
psql -U ats-beta -d ats_beta -v ON_ERROR_STOP=1 -c 'CREATE TABLE call_attempts (status text)'
ln -s /usr/local/bin/psql /usr/bin/psql

mkdir -p /opt/ats-beta/.venv/bin /etc/ats-beta /var/lib/ats-beta
mkdir -p /opt/ats-beta-tools/installer/bin /opt/ats-beta-tools/node-v24.19.0-linux-x64/bin
printf '%s\n' 'ATS_DATABASE_URL=postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql' >/etc/ats-beta/ats.env
cat >/usr/bin/runuser <<'SCRIPT'
#!/bin/sh
test "$1" = -u
user=$2
shift 3
exec /usr/local/bin/gosu "$user" "$@"
SCRIPT
cat >/usr/bin/git <<'SCRIPT'
#!/bin/sh
if [ "$1" = rev-parse ]; then printf '%040d\n' 1; fi
SCRIPT
cat >/usr/bin/systemctl <<'SCRIPT'
#!/bin/sh
printf 'systemctl %s\n' "$*" >>/tmp/update-events
SCRIPT
cat >/usr/bin/curl <<'SCRIPT'
#!/bin/sh
printf 'health probe\n' >>/tmp/update-events
SCRIPT
cat >/opt/ats-beta/.venv/bin/python <<'SCRIPT'
#!/bin/sh
echo 'ModuleNotFoundError: No module named ats' >&2
exit 1
SCRIPT
cat >/opt/ats-beta-tools/installer/bin/uv <<'SCRIPT'
#!/bin/sh
set -eu
case " $* " in *' --reinstall-package atos-ats '*) ;; *) exit 10 ;; esac
rm -r /opt/ats-beta/.venv/lib/python3.12/site-packages/cryptography/__pycache__
printf 'uv repaired\n' >>/tmp/update-events
cat >/opt/ats-beta/.venv/bin/python <<'PYTHON'
#!/bin/sh
test "$1" = -m && test "$2" = alembic
printf 'alembic upgrade\n' >>/tmp/update-events
PYTHON
SCRIPT
cat >/opt/ats-beta-tools/node-v24.19.0-linux-x64/bin/npm <<'SCRIPT'
#!/bin/sh
printf 'npm %s\n' "$*" >>/tmp/update-events
SCRIPT
chmod +x /usr/bin/runuser /usr/bin/git /usr/bin/systemctl /usr/bin/curl
chmod +x /opt/ats-beta/.venv/bin/python /opt/ats-beta-tools/installer/bin/uv /opt/ats-beta-tools/node-v24.19.0-linux-x64/bin/npm
chown -R ats-beta:ats-beta /opt/ats-beta
mkdir -p /opt/ats-beta/.venv/lib/python3.12/site-packages/cryptography/__pycache__
chmod 700 /opt/ats-beta/.venv/lib/python3.12/site-packages/cryptography/__pycache__
printf 'old bytecode\n' >/opt/ats-beta/.venv/lib/python3.12/site-packages/cryptography/__pycache__/module.pyc
touch /tmp/external-interpreter /tmp/update-events
chmod 666 /tmp/update-events
ln -s /tmp/external-interpreter /opt/ats-beta/.venv/bin/interpreter-link

# The failure is observable with the service user's actual filesystem permissions.
if gosu ats-beta rm -r /opt/ats-beta/.venv/lib/python3.12/site-packages/cryptography/__pycache__ 2>/dev/null; then
  echo 'Root-owned cache fixture did not reproduce the permission failure' >&2
  exit 1
fi
bash /input/update-beta.sh
test "$(stat -c %U /tmp/external-interpreter)" = root
grep -Fxq 'uv repaired' /tmp/update-events
grep -Fxq 'alembic upgrade' /tmp/update-events
grep -Fxq 'health probe' /tmp/update-events
test -s /var/lib/ats-beta/backups/*/ats_beta.dump

# An active call must stop the retry before installation and restore the scheduler.
psql -U ats-beta -d ats_beta -v ON_ERROR_STOP=1 -c "INSERT INTO call_attempts VALUES ('answered')"
: >/tmp/update-events
if bash /input/update-beta.sh; then exit 1; fi
test "$(wc -l </tmp/update-events)" = 3
grep -Fxq 'systemctl start ats-beta-worker@scheduler' /tmp/update-events

# A failed database query must also prevent installation.
psql -U ats-beta -d ats_beta -v ON_ERROR_STOP=1 -c 'DROP TABLE call_attempts'
: >/tmp/update-events
if bash /input/update-beta.sh; then exit 1; fi
test "$(wc -l </tmp/update-events)" = 3
grep -Fxq 'systemctl start ats-beta-worker@scheduler' /tmp/update-events
printf 'Update recovery, ownership, symlink preservation and database guards passed\n'
